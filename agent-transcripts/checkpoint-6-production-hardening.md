# Checkpoint 6 — Production hardening and a live UI bug hunt

**Scope:** diagnosing and fixing two real production failures on the Render
deployment (embeddings, LLM fallback), adding Grok as a second cloud
provider, and a live end-to-end test pass against the actual running app
(Docker Compose stack, real Ollama, real Gemini) that surfaced a genuine
frontend bug — plus one false alarm along the way, kept here because the
brief for this file asks for failed attempts and how they were corrected,
not just the successful ones.

**Why this was needed:** the app had already been deployed to Render/Vercel
by the time this checkpoint started, and it was failing in ways the local
Compose demo never exercises — no Ollama host, and two independent findings
only visible in Render's own request logs.

---

## Finding 1: production ingestion was crash-looping on a connection error, and chat was silently degrading to a static refusal

**Symptom**, read directly from Render's log stream for the `lenny-api`
service: `raise OSError(err, f'Connect call failed {address}')` during the
pre-deploy ingestion step, and separately, a live chat request logging
`refusal_generation_failed_using_static` / `error_type: ProviderTimeout`.

**Cause.** `render.yaml` set `EMBEDDING_PROVIDER=none` (Ollama cannot run on
Render — no GPU, no persistent model cache, documented in the README's own
deployment section) but left `LLM_FALLBACK_PROVIDER` unset. So: the ingestion
pipeline's embedding step had nothing to embed *with* and errored instead of
degrading, and a Gemini timeout on a live chat request had no second provider
to retry on before the gateway's `_FALLBACK_TRIGGERS` handling gave up —
exactly the failure mode `app/llm/registry.py`'s own docstring describes
("with no fallback configured the system fails loudly").

**Fix.**

- `EMBEDDING_PROVIDER=gemini` in production, with a new `_embed_gemini`
  path in `app/retrieval/embeddings.py` using `text-embedding-004`
  (`outputDimensionality=768`, matching the existing `vector(768)` column —
  no migration needed) and Gemini's asymmetric `taskType`
  (`RETRIEVAL_DOCUMENT` for indexing, `RETRIEVAL_QUERY` for the live search
  query), which measurably improves retrieval quality over symmetric
  embedding and was worth the small extra branch.
- `app/retrieval/retriever.py`'s lexical leg now retries with an OR'd query
  when the stricter AND query (`websearch_to_tsquery`) returns nothing — a
  five-concept question needing every term to match one chunk was refusing
  outright even when several chunks answered a subset of it well; RRF fusion
  downstream still weeds out the resulting weaker matches, so this trades a
  little precision for recall that keeps a good question from being refused.
- Added Grok (`app/llm/grok.py`, OpenAI-compatible Chat Completions API,
  reusing the same shape as `openai.py` with its own base URL/key/model) and
  set `LLM_FALLBACK_PROVIDER=grok` in `render.yaml`, so a Gemini timeout now
  retries once on an independent provider before the request fails, rather
  than degrading straight to a canned refusal string.

**Verification.** Render's own deploy/log tooling confirmed the new deploy
went live; the fix could not be verified against a real Gemini timeout
in-band (that requires the failure condition to actually occur), so the
verification that matters here is architectural: `LLMGateway.stream()`'s
fallback path is exercised by existing tests (`test_llm.py`), and the new
`GrokProvider` follows the exact same contract (`health`/`stream`/`complete`)
as every other provider, so it participates in that already-tested path
without new provider-specific branching anywhere else in the codebase.

---

## Finding 2 (a false alarm, corrected): a chat request that looked hung was actually 192 seconds of real, expected Ollama latency

This is the failed-then-corrected investigation worth recording in full,
because the instinct at each step was reasonable and each one turned out to
be wrong.

**Symptom.** Testing the real running app (local Docker Compose stack, not a
mock) by sending "How do I improve activation for a B2B SaaS product?"
through the actual UI: the user's message appeared, retrieval completed
(logged, 6 sources, confidence 0.4976), and then — nothing. No phase
indicator, no answer, no error, for over a minute.

**First hypothesis: a session-routing race.** The browser had briefly shown
two different session IDs in flight (`GET` requests for one session
interleaved with the `POST .../messages` to another), so the working theory
was that the message landed in a session the UI wasn't displaying. Re-ran the
test from a clean reload with fresh element references — the message really
had gone to the session shown as active. Ruled out.

**Second hypothesis: the request genuinely hung — no timeout, no error, ever.**
`LLM_TIMEOUT_SECONDS=180` is configured, so a stuck request should eventually
surface a `ProviderTimeout` and either fall back or fail loudly. After
~150 seconds past retrieval with zero further log lines, this looked like a
real bug: `httpx`'s per-read timeout resets on every byte received, so a
provider that stops sending entirely (not just slowly) should still trip it —
unless something in the streaming path wasn't actually awaiting the way it
appeared to. Tested Ollama directly (`curl .../api/chat`, bypassing the app
entirely) to isolate whether Ollama itself was the problem: it answered in
14.6 seconds, most of that model *load* time since it had been idle. Ollama
was fine.

**What was actually happening.** Waited longer and polled the session via the
API instead of the UI. At 192,046 ms (measured server-side, in the persisted
message's own `latency_ms` field) the assistant's answer appeared — fully
grounded, four paragraphs, three real citations, `qwen2.5:7b-instruct`. This
matches exactly what the README's own troubleshooting table already says:
"Measured 127-146 s end-to-end on CPU-only Ollama" for a *previous*
checkpoint's measurement, and the prompt here was larger (6 retrieved
passages, 2,087 input tokens) than that measurement's. **There was no bug in
the backend at all** — the investigation's own impatience (checking after
15-90 seconds, when the documented, expected latency for this exact
configuration is 2-3+ minutes) manufactured a false positive. Recorded here
so a future session doesn't re-walk the same path: for `qwen2.5:7b-instruct`
on CPU-only Ollama with a full RAG prompt, wait at least 3 minutes before
concluding a request is actually stuck.

---

## Finding 3: the chat UI never auto-scrolled, which made the false alarm above look far worse than it was — and was a real bug on its own

While chasing Finding 2, the same session was reloaded from history (the
assistant's answer was confirmed present in the database via a direct API
call) and the browser **still** appeared to show nothing between the source
cards and the message composer. `document.querySelectorAll` confirmed the
answer's full text was genuinely in the DOM, correctly rendered, with normal
`visibility`/`opacity`/`display` — it was simply scrolled out of the
viewport, below six source cards, and nothing in the app ever scrolled the
chat panel to show new content.

**Grep confirmed there was no auto-scroll logic anywhere in the frontend** —
not on session load, not on a new message being sent, not as tokens stream
in. Every other piece of this app's UX (phase indicators, source-cards-before-
answer, the typing caret) is built around making a slow local model feel
responsive; landing the viewport on the wrong part of the page undid all of
that by making a fully-answered turn look empty.

**Fix** (`frontend/src/components/ChatWorkspace.tsx`): the scroll container
now tracks whether the user was already at (or near) the bottom before new
content arrives — via `onScroll`, a 80px threshold — and pins to the bottom
in a `useLayoutEffect` keyed on `messages`/`stream.text`/`stream.sources`/
`stream.phase` only when that's true, so a live answer streaming in (or a
freshly opened session) lands on the newest turn, but a user who has
deliberately scrolled up mid-stream to re-read an earlier source card isn't
yanked back down. Opening a session also force-resets the "stick to bottom"
flag, since landing on the most recent turn is always correct there.

**Verification.** Live-tested against the actual running app after the fix:
reloading the same session that previously required manual scrolling now
lands directly on the assistant's answer; sending a new message through
Gemini (fast enough to watch end-to-end) confirmed the view tracks the
streaming phase indicator, the source cards, and the growing answer text
without any manual scrolling, and stays put once the terminal state renders.

---

## Finding 4: a failed turn was persisted as a content-less, unexplained assistant message

Chasing Finding 2 turned up a second, genuine bug. A different live test (this
one against Gemini, restarted moments earlier to pick up a fresh
`GEMINI_API_KEY`) also produced a blank assistant bubble. Reading the row
directly from the database (bypassing the UI) showed the actual cause: a
`ProviderTimeout` — not from Gemini as first assumed, but from Ollama, still
the process's primary at that moment (a container restart resets the
in-memory `LLMGateway.primary` to the env default, and this request landed
50 seconds after restart, before the model had reloaded — `httpx`'s read
timeout resets per streamed token, so it fires on a slow *first* token the
same way a fully-silent connection would, even though a later, warmer
request for the same model completed fine at 192 s in Finding 2).

The timeout itself is correct, documented behavior. The bug is what happened
next: `app/services/chat.py::stream_turn` does persist error information on
an `error` SSE event (`sessions.append_message(session_id, "assistant", "",
error=event.data)` — content deliberately empty, the error stashed in a
separate JSONB column), matching this module's own stated contract ("persist
the assistant's result plus its sources after the agent finishes -- whether
that finish was an answer, a refusal, or an error"). But nothing downstream
of that write ever read the column back: `MessageRow` (`app/services/
sessions.py`), the `get_history` SQL `SELECT`, and the public `MessageRecord`
schema (`app/schemas/chat.py`) all omitted `error` entirely. A failed turn
was durably recorded with full diagnostic detail in Postgres and then served
to every client as an empty string with no other signal — exactly the blank
"ASSISTANT" bubble noticed once already, right at the start of this
checkpoint, in an unrelated older session, and dismissed at the time as
probably-stale test data. It wasn't; it was this same bug, hit twice.

**Fix**, threaded through every layer the value actually has to cross:

- `MessageRow.error` and the `get_history` `SELECT` (`app/services/
  sessions.py`) now read the existing `messages.error` column instead of
  silently dropping it.
- `MessageRecord.error` (`app/schemas/chat.py`) and `_record()`
  (`app/api/sessions.py`) now carry it into the public API response.
- The frontend's `MessageRecord` type gained the matching field, and
  `MessageList.tsx` now checks `m.error` **before** the refusal/normal-answer
  branches and renders the existing `ErrorBanner` component with the stored
  code/message/remediation, instead of falling through to a plain
  `MessageBubble` with nothing to show.

**Verification.** Re-fetched the exact session that produced the original
blank bubble after deploying the fix: `GET /api/sessions/{id}` now returns
`"error": {"code": "provider_timeout", "message": "The ollama model did not
respond in time.", "remediation": "..."}` on that message, where it
previously returned no `error` key and an empty `content` string at all.
Frontend type-check and the full test suite (41 tests) still pass with the
new required field threaded through every test fixture that constructs a
`MessageRecord`.

---

## What this checkpoint did not touch, and why

**Figma was considered and explicitly rejected for this pass.** The existing
frontend (landing page, chat UI, source cards, artifact viewer, dark mode,
responsive breakpoints) was already visually polished on inspection — the
gap found by live-testing it was a functional bug (no auto-scroll), not a
visual-design gap a redesign would have caught. Given the assignment's
submission deadline was the same day this checkpoint ran, a Figma
generate-then-reimplement round trip was judged higher-risk and lower-value
than directly fixing the real, reproducible bug found by running the actual
app — "UI/UX quality" is one of seven graded criteria, not the only one, and
a broken auto-scroll is a worse UX defect than any missing visual polish.
