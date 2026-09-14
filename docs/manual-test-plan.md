# Manual test plan — frontend

**Status:** written and executed in checkpoint 4, against the real stack
(PostgreSQL + Ollama `qwen2.5:7b-instruct` + FastAPI + the React frontend),
using Playwright for browser automation rather than eyeballing. Results below
are from an actual run, not placeholders — see
[`agent-transcripts/checkpoint-4-frontend.md`](../agent-transcripts/checkpoint-4-frontend.md)
for the full account, including two real bugs this pass found.

This plan checks the frontend against `docs/design.md` and the PRD's
acceptance criteria (section 3) and user flows (section 2). It assumes
`docker compose up -d db && docker compose run --rm ingest && docker compose
up -d api` (or the equivalent local dev servers) are already running, per the
README.

---

## 1. Grounded question, cited answer (PRD 2.1)

**Steps:** Open the app → click an example question (or new session, ask
"How do I improve activation for a B2B SaaS product?") → observe.

**Checks:**
- [x] Phase status updates through routing → retrieving → generating →
  validating → done, visibly, before any answer text appears.
- [x] Source cards appear as soon as retrieval finishes, before the answer
  text starts streaming.
- [x] The answer streams token-by-token with a visible typing indicator.
- [x] Every `[S#]` marker in the rendered answer is a real link; clicking one
  scrolls to and focuses the matching source card.
- [x] Cited sources are visually distinct from uncited ones (solid vs.
  dashed border, explicit "cited"/"not cited" badge).
- [x] Each source card shows episode, guest, timestamp and a working
  `youtube.com/watch?v=<id>&t=<seconds>` deep link.
- [x] Provider/model and latency are shown under the finished answer.

**Result: PASS**, after fixing a real bug — see §7.

## 2. Insufficient evidence → refusal (PRD 2.2)

**Steps:** Ask an out-of-corpus question ("How should I think about pricing
for a B2B SaaS product?" reliably refuses at the current 0.48 threshold).

**Checks:**
- [x] The response renders in the distinct refusal banner (warm-neutral, not
  error-red), never in a normal assistant bubble.
- [x] The banner explicitly says no supporting material was found.
- [x] "Closest matches (not used)" source cards are shown, all marked
  "not cited" — PRD A2, the near-miss passages are shown so refusal is
  verifiable, not a dead end.
- [x] Retrieval confidence and the threshold are visible.

**Result: PASS.** Live run: confidence 0.45 against threshold 0.48, rendered
as `🚫 No supporting material found` with 3 near-miss cards, all "not cited".

## 3. Ship 30 essay → artifact (PRD 2.3)

**Steps:** After a grounded answer, ask "Turn that into a Ship 30 essay."

**Checks:**
- [ ] Essay opens in the artifact panel as a Markdown artifact.
- [ ] Structural elements (hook, headings, bullets, takeaway) render.
- [ ] Claims trace back to the same sources as the original answer.

**Result: NOT RUN this pass** — a full essay generation is 7 sequential
model calls (~15-20 minutes on this CPU-only Ollama setup per checkpoint
3's measurement) and checkpoint 3 already verified the skill itself
end-to-end against the live model. This pass verified the *frontend's*
Markdown artifact rendering path directly against a real HTML artifact
(§4) and against unit tests of the Markdown renderer; re-run this row
before checkpoint 5's final gate to close the gap.

## 4. Artifact generation and viewer (PRD 2.4)

**Steps:** In a session with a grounded answer, ask "Turn that into an HTML
landing page with CSS." Once generated, use the artifact panel.

**Checks:**
- [x] Artifact panel opens automatically with the new artifact, defaulting to
  Preview.
- [x] Preview renders the HTML inside a sandboxed `<iframe>`, `src` pointed at
  `GET /api/artifacts/{id}/document`.
- [x] The iframe's `sandbox` attribute is read verbatim from the API
  response (`allow-popups allow-popups-to-escape-sandbox`) — confirmed via
  `document.querySelector('iframe').getAttribute('sandbox')` in the live
  page, not just by reading the component source.
- [x] Neither `allow-same-origin` nor `allow-scripts` appears anywhere in
  that attribute.
- [x] Source tab shows the raw sanitised HTML in a `<pre>`.
- [x] Copy button copies the current content and shows a confirmation.
- [x] Download saves a file with the artifact's title and the right
  extension.
- [x] Switching sessions loads that session's own latest artifact (or shows
  no panel if it has none).

**Result: PASS**, after fixing a real cross-origin framing bug — see §7.

## 5. Provider indicator and model switch (PRD 2.5)

**Steps:** Load the app; read the header badge. (Switching `LLM_PROVIDER`
and restarting was not re-exercised this pass — checkpoint 2 already proved
the backend side; this pass only proves the header reads it live.)

**Checks:**
- [x] Header shows `ollama · qwen2.5:7b-instruct`, read from
  `GET /api/provider`, not hardcoded.
- [x] A failed `/api/provider` fetch renders "API unreachable," not a blank
  header or a silent crash.

**Result: PASS.**

## 6. Accessibility (docs/design.md §6)

**Checks:**
- [x] The chat input, send button, session rows, example-question buttons,
  citation links, artifact tabs, and copy/download controls are all real
  interactive elements, reachable and operable by keyboard alone (verified:
  typed a question with the keyboard, submitted with Enter, without ever
  clicking).
- [x] Focus stays in the composer after sending, so a follow-up is one
  keystroke away.
- [x] The phase status line is `role="status"`/`aria-live="polite"` and
  updates on every phase transition, verified via the accessibility tree
  (Playwright snapshot), not just the DOM.
- [x] Cited vs. uncited sources, and refusal vs. error, are distinguished by
  more than color (badge text, border style, heading text, icon).
- [x] The iframe has a descriptive `title` ("Generated document: {title}").
- [ ] Full screen-reader pass (NVDA/VoiceOver) — not run this pass; the
  above was verified through the accessibility tree and keyboard-only
  interaction, which catches the most common failures but is not a
  substitute for a real screen reader. Flagged for checkpoint 5.

**Result: PASS** on the items run; one item deferred (see above).

## 7. M4 — time to first useful answer (PRD 1.3, target < 2 min)

**Method:** Playwright against the live stack (real Postgres, real Ollama,
`qwen2.5:7b-instruct`, CPU inference — no GPU in this environment). Measured
wall-clock from the user's message being sent to the `result` event
rendering a cited answer, using the backend's own `latency_ms` (which starts
at routing and ends when the skill returns — the most precise number
available, more precise than a Playwright-side stopwatch subject to tool
round-trip overhead).

| Run | Condition | Measured | Target |
|---|---|---|---|
| 1 | Cold: first request after `docker compose up`, Ollama loading the model into memory for the first time | **146.2 s** | < 120 s |
| 2 | Warm: model already resident, second distinct question in a fresh session | **127.4 s** | < 120 s |

**Result: MISSES the < 2 minute target**, by a real, measured, and
consistent margin (not a one-off) — both runs are in the same 2:06–2:26
range, so this is not noise. This is honestly reported rather than adjusted
after the fact: the PRD's target was set before this environment's actual
CPU-only Ollama throughput was measured end-to-end through the full pipeline
(routing + retrieval + a full generation call), and the real number is a
finding, not a UI defect — the frontend surfaces the wait accurately (phase
progress the whole time, sources visible within ~2 seconds of retrieval
finishing) rather than hiding it. Recorded here rather than quietly loosening
the target, per the project's own convention (PRD 1.1: measure, don't guess)
of taking a real measurement over an assumption. Options going forward,
outside this checkpoint's scope: a smaller/quantized model, GPU inference,
or explicitly re-scoping M4's target to this environment's real hardware
constraint.

## 8. Responsive layout

**Steps:** Resize the viewport to 400px wide (a spot check, not a full device
matrix).

**Checks:**
- [x] No horizontal scroll on the page body.
- [x] Sidebar, chat, and artifact panel stack vertically instead of
  compressing into unreadable columns.
- [x] All controls remain reachable and legible.

**Result: PASS.**

---

## 9. Bugs found by this pass (both fixed and re-verified)

1. **SSE parser never advanced past the first phase.** `sse_starlette`
   writes CRLF (`\r\n\r\n`) between frames; the frontend's stream reader
   searched for a bare `\n\n` boundary, which never matched, so no event was
   parsed until the connection closed — the UI looked permanently stuck on
   "routing" while the backend had long since finished. Found by watching a
   live turn sit on "Working out what you're asking…" well past the
   backend's own retrieval/generation log lines. Fixed in
   `frontend/src/api/sse.ts` by normalising `\r\n` to `\n` before boundary
   detection; regression-tested with the exact CRLF byte sequence the real
   backend sends (`frontend/src/api/sse.test.ts`).
2. **The artifact iframe could never load across the frontend/API origin
   split.** `GET /api/artifacts/{id}/document` sent `Content-Security-Policy:
   frame-ancestors 'self'`, which permits framing only from the API's own
   origin — but the frontend is a separate Compose service on its own origin
   by design (checkpoint 4's whole point). Every embed was silently blocked
   by the browser. Fixed by computing `frame-ancestors` from the same
   `CORS_ORIGINS` setting already used for the API's CORS policy
   (`app/agent/sanitize.py::response_csp`), and dropping the
   `X-Frame-Options: SAMEORIGIN` header entirely (it cannot express "allow
   this other origin," and CSP's `frame-ancestors` is what every evergreen
   browser actually enforces when both are present). Full account, including
   why this was invisible to the checkpoint-3 test suite (which never had a
   second origin to test against), in
   `agent-transcripts/checkpoint-4-frontend.md`.

3. **The actual containerised `web` service couldn't reach the API at all.**
   `CORS_ORIGINS`'s default listed `:5173` and `:3000` but not `:4173` — the
   port the `web` Compose service (frontend/Dockerfile's `serve` stage)
   actually publishes. Invisible until the container was actually built and
   run (the dev server on `:5173` was already an allowed origin, so this
   passed every check up to that point). Fixed by adding `:4173` to the
   default `CORS_ORIGINS` in `app/config.py`, `.env`, and `.env.example`.
   Re-verified: rebuilt `api`, rebuilt and started `web`, loaded
   `http://localhost:4173` — zero console errors.

All three bugs were found by driving the real, running app (dev server *and*
the actual Docker Compose `web` service) with Playwright against the real
backend — none would have been caught by a component test, a schema check,
or reading the code, which is exactly the gap this manual pass exists to
close.
