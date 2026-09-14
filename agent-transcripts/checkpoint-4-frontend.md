# Checkpoint 4 — Frontend

**Scope:** a React/TypeScript frontend against the checkpoints 1-3 API —
streaming chat with phase feedback, source cards, an explicit refusal state,
a sandboxed artifact viewer, provider/session UI, accessibility, and the
`docs/design.md` / `docs/manual-test-plan.md` documents this checkpoint
introduces. Plus one pre-existing infrastructure bug fixed before starting:
the containerised `api` service was crash-looping.

**Why this was next:** checkpoints 1-3 built and live-verified a stable
streaming API; this checkpoint is the first one a real user actually looks
at, and the first one built against a *second* origin (the frontend is its
own Compose service, not served by the API) — which turned out to matter a
lot more than expected (see Failures 2 and 3).

---

## Pre-existing bug fixed first: `api` container crash-looping

**Symptom.** `docker compose ps` showed `lenny-growth-assistant-api-1`
restarting continuously; logs showed `ModuleNotFoundError: No module named
'nh3'`.

**Cause.** `backend/Dockerfile` installed a hand-copied, hardcoded `pip
install` list instead of installing from `pyproject.toml`'s `dependencies`
array. It was copied once, at some earlier point, and never revisited — so
when `nh3` was added to `pyproject.toml` in checkpoint 3, the Dockerfile
silently kept shipping the old list. This is a pattern bug (a duplicated
source of truth that can only drift, not a one-off typo), not specific to
`nh3`.

**Correction.** Replaced the hardcoded list with `pip install .` against the
copied `pyproject.toml` and `app/`, so the image always installs exactly
what `pyproject.toml` declares — the same file `uv pip install -e ".[dev]"`
and pytest already use locally. Rebuilt (`docker compose build api`) and
brought the full stack up
(`docker compose up -d db && docker compose run --rm ingest && docker
compose up -d api`): `/health/detail` reported every dependency `ok`
(40 episodes, 4448 chunks embedded, embeddings and LLM both reachable).
`.env`'s `DATABASE_URL`/`OLLAMA_BASE_URL` were already correctly pointed at
`db`/`host.docker.internal` from a prior session's fix — re-verified, not
re-fixed.

---

## Decision: Vite + React + TypeScript

Considered against Next.js: this is a single-page app with no server-side
rendering requirement (there is nothing to render for a crawler or a cold
first paint that matters here — every view depends on live session/chat
state), and no routing complexity beyond "which session is active," which is
plain component state, not a router. Vite gives a fast dev loop and a small,
auditable static build for the `web` Compose service, without the
server-runtime surface Next.js would add for zero benefit in this shape of
app. `react-markdown` + `remark-gfm` render Markdown; no state library
beyond React's own hooks, because the state is a handful of values (active
session, message list, one in-flight stream) with no cross-cutting sharing
need that would justify Redux/Zustand/etc.

## Decision: fetch + a hand-rolled SSE reader, not `EventSource`

`EventSource` only supports `GET`; the chat endpoint is a `POST` that streams
its response. `frontend/src/api/sse.ts` reads the response body with the
Streams API and splits it into frames itself. This is the one piece of
wire-format parsing in the frontend, so it is kept as pure, unit-tested
functions (`parseSseFrame`, `readSseStream`) separate from the
fetch/AbortController plumbing in `client.ts` — the same principle the
backend transcripts describe applying to the citation validator: isolate the
part most likely to have an off-by-one so it can be tested directly against
scripted byte sequences.

## Decision: no `rehype-raw` when rendering Markdown

The server already HTML-escapes literal `<tag>` text a model wrote into a
Markdown artifact (`sanitise_markdown`) before it is ever persisted. The
frontend does not treat that as sufficient defense on its own: `react-markdown`
is configured without `rehype-raw`, so this renderer has no code path that
turns markdown source into live HTML DOM nodes at all, regardless of what
the string contains — a second, independent reason content can't execute,
not a restatement of the server's own guarantee. Documented in
`docs/design.md` §5 and in `ArtifactViewer.tsx`'s comments.

---

## Failures and corrections

### 1. The SSE reader never advanced past the first phase — CRLF vs. bare `\n\n`

**Symptom.** Live in the browser (via Playwright): submitting "How do I
improve activation for a B2B SaaS product?" showed "Working out what you're
asking…" and never changed, for well over a minute — while `docker compose
logs api` showed routing, retrieval, and generation had all completed
within a couple of seconds and the turn had actually finished successfully
(146 s later, entirely server-side).

**Investigation.** A raw `fetch` + manual reader loop run directly in the
page via Playwright's `browser_evaluate` (bypassing the app's own code)
showed the real byte stream: every frame was terminated
`\r\n\r\n`, not `\n\n`. `frontend/src/api/sse.ts`'s boundary search looked
for a literal `"\n\n"` substring, which never occurs in `\r\n\r\n` (there is
always a `\r` between the two `\n` characters), so the buffer just grew
without ever finding a boundary — until the stream closed, at which point
the "no boundary found, treat the whole buffer as one trailing frame"
fallback parsed only the *first* `data:` line in the entire accumulated
buffer (the very first `routing` phase event) and returned, since the
generator then ended.

**Correction.** Normalise `\r\n` to `\n` on each decoded chunk before
boundary search, in `readSseStream`. Added a regression test
(`sse.test.ts`) using the exact CRLF byte sequence `sse_starlette` actually
sends, so a change that reintroduces a bare-`\n\n` assumption fails
immediately rather than only failing live, in a browser, against a real
model call slow enough to make the bug easy to misread as "the model is
just slow" instead of "the client stopped listening."

This is exactly the class of bug the task's Playwright requirement exists
to catch: nothing in a component test or a schema check would have found
it, because the mocked/scripted event sequences in every component test are
constructed as already-parsed objects, never as raw SSE bytes. It only
surfaced by driving the real running app against the real streaming
response.

### 2. The artifact iframe could never load — `frame-ancestors 'self'` blocks a second origin

**Symptom.** Live in the browser: opening a session with an existing HTML
artifact showed an empty artifact panel and a console error: `Framing
'http://localhost:8000/' violates the following Content Security Policy
directive: "frame-ancestors 'self'". The request has been blocked.`

**Cause.** `GET /api/artifacts/{id}/document` (checkpoint 3) sent
`Content-Security-Policy: frame-ancestors 'self'` and `X-Frame-Options:
SAMEORIGIN`. Both were written and tested against an implicit assumption
that the frontend would share the API's origin. Checkpoint 4's actual
architecture (confirmed by `docker-compose.yml`'s own service comments,
written in checkpoint 3) is a *separate* `web` service — Vite dev server on
`:5173`, or the built `web` container on `:4173` — a different origin from
the API's `:8000` either way. `'self'` on the API's own document therefore
means "only the API's own origin may frame this," which never includes the
frontend. This was invisible to checkpoint 3's test suite because it never
had a second origin to test against — `httpx.AsyncClient` against the ASGI
app has no browser-enforced framing policy at all.

**Correction.** `app/agent/sanitize.py` gained `response_csp(frontend_
origins: list[str])`, which builds the header actually sent by
`GET /.../document` with `frame-ancestors 'self' <origins>` instead of a
hardcoded `'self'`. The allowed origins are the existing `CORS_ORIGINS`
setting (`app/api/artifacts.py` now takes `SettingsDep` and passes
`settings.cors_origins`) — not a second place to configure the frontend's
address, since the API already needed to know it for CORS. `X-Frame-Options:
SAMEORIGIN` was removed from the response entirely rather than left
alongside the fixed CSP: XFO has no syntax for "allow this other origin" (its
deprecated `ALLOW-FROM` value is unsupported by every evergreen browser), and
every browser that understands CSP's `frame-ancestors` prioritises it over
XFO when both are present — so keeping XFO would have added a header that
could only ever conflict with the fix, never help it. `ARTIFACT_CSP` (the
constant baked into the artifact document's own `<meta http-equiv>`) was left
unchanged: the CSP spec disallows `frame-ancestors` and `sandbox` in a
meta-delivered policy, and Chromium confirms this by logging (not
enforcing) both directives as ignored there — confirmed directly in the
live browser console after the fix, which is how the meta-tag copy was
established to be inert for this purpose rather than assumed.

Updated `test_db_artifacts.py::test_document_is_served_with_isolating_headers`
(the `x-frame-options` assertion was replaced with a `frame-ancestors`
assertion that lists the real default `CORS_ORIGINS`, and an assertion that
`x-frame-options` is now absent) and added two new unit tests
in `test_sanitize.py` for `response_csp` directly. Re-verified live: rebuilt
and restarted the `api` container, reloaded the same session in the browser,
and the artifact rendered inside the iframe with no console error beyond the
two informational "ignored in a meta element" notices (expected, and now
understood rather than alarming). Full backend suite re-run:
**375 passed** (`-m "not llm"`), ruff clean.

### 3. The containerised `web` service couldn't reach the API at all — `CORS_ORIGINS` didn't include its own port

**Symptom.** After building and running the actual `web` Compose service
(not the Vite dev server) and loading `http://localhost:4173`, the header
showed "API unreachable" and the console showed `Access to fetch at
'http://localhost:8000/api/provider' from origin 'http://localhost:4173'
has been blocked by CORS policy`.

**Cause.** `CORS_ORIGINS`'s default (`Settings.cors_origins`) listed
`:5173` (Vite dev) and `:3000` (a common alternate dev port) — both
plausible guesses from before this checkpoint's frontend existed — but not
`:4173`, the port `frontend/Dockerfile`'s `serve` stage and
`docker-compose.yml`'s `web` service actually publish. The dev server had
been the only thing tested up to this point in the session, so the gap
was invisible until the containerised build was actually run.

**Correction.** Added `http://localhost:4173` to `Settings.cors_origins`'s
default and to `.env` / `.env.example`. Because `GET
/api/artifacts/{id}/document`'s `frame-ancestors` (Failure 2) is derived
from the same setting, this one list now has to be right for both CORS and
artifact framing — which is the intended coupling (one place to add a
frontend origin), not an accident, but it does mean this default is worth
keeping in sync with `docker-compose.yml`'s actual published ports if either
ever changes. Rebuilt and restarted the `api` container, re-ran the
backend suite (still 375 passed — no test asserted the exact default list,
so nothing needed updating there), and reloaded `http://localhost:4173`:
zero console errors, provider badge populated, and an existing session's
fully-cited answer rendered correctly from a fresh container with no dev
server involved at all.

This is the third distinct cross-origin issue this checkpoint surfaced (after
the CSP frame-ancestors gap), all invisible to any test that only exercises
one origin at a time — which is every test in the suite before this
checkpoint, since there was only ever one origin before the frontend
existed as a separate service.

### 4. `oxlint`'s `set-state-in-effect` flagged a real (if minor) pattern worth removing anyway

**Symptom.** `useArtifact`'s effect called `setLoading(true)` synchronously
at its top before the async fetch, which the linter flagged as an
unnecessary extra render.

**Correction.** Removed the separate `loading` state entirely; it is now
derived during render as `artifactId != null && artifact?.id !==
artifactId` — "loading" is exactly "we have an id but don't yet have its
matching artifact," so it cannot drift out of sync with `artifact` the way a
second piece of state could. Left one remaining warning
(`setArtifact(null)` inside the same effect, for the "id went back to
null" branch) as an accepted, idiomatic reset-on-prop-change effect rather
than restructuring around React's `key`-remount pattern for a single
two-line effect — `oxlint` exits 0 on a warning, so this does not block a
clean lint run, and is noted here rather than silently left unexplained.

---

## Verification performed

| Check | Result |
|---|---|
| Frontend unit/component tests (`npm test`, vitest + Testing Library) | **41 passed** — SSE frame parsing (including the CRLF regression case), the chat stream reducer, citation linkification, refusal detection, source-card cited/uncited rendering, refusal vs. error banner semantics (`role=status` vs `role=alert`), the artifact viewer's sandbox pass-through and tab/copy/download behaviour, and citation-link click-to-focus |
| Frontend typecheck (`tsc -b`) | clean |
| Frontend lint (`oxlint`) | 0 errors, 1 accepted warning (see Failure 3) |
| Backend suite after the CSP fix (`pytest -m "not llm"`) | **375 passed**, 0 failed |
| Backend lint (`ruff check app tests`) | clean |
| Live: grounded Q&A, cold start | Real turn against real Postgres + real Ollama: confidence 0.4976, cited answer, **146.2 s** end to end (see manual test plan §7 for the full M4 result, including that this misses the < 2 min target) |
| Live: grounded Q&A, warm model | Second live turn: confidence 0.5238, 2 real citations (`[S2]`, `[S5]`) correctly linked to their source cards, **127.4 s** |
| Live: refusal | "How should I think about pricing for a B2B SaaS product?" → confidence 0.45 (below the 0.48 threshold) → refusal banner rendered, distinct from a normal answer, 3 near-miss sources shown, all "not cited" |
| Live: HTML artifact viewer | Existing session's HTML artifact ("Improving Activation for B2B SaaS Products") rendered inside the sandboxed iframe after the CSP fix; `iframe.getAttribute('sandbox')` confirmed via `browser_evaluate` to be exactly `allow-popups allow-popups-to-escape-sandbox`, matching the API's `ARTIFACT_SANDBOX` with no `allow-same-origin`/`allow-scripts` |
| Live: the actual `web` Compose service (not the dev server) | `docker compose build web && docker compose up -d web`, loaded `http://localhost:4173` in a real browser after fixing Failure 3: zero console errors, provider badge populated, an existing session's cited answer rendered correctly from the container build with no dev server involved |
| Live: keyboard-only interaction | Typed a full question and submitted with Enter without ever clicking; phase/source/streaming updates all rendered correctly |
| Live: provider header | `ollama · qwen2.5:7b-instruct`, read from `/api/provider`, confirmed against a live response, not a hardcoded string |
| Responsive spot check | 400px viewport: no horizontal scroll, panels stack vertically, all controls remain reachable (screenshot in the manual test plan) |

Full detail and pass/fail per PRD user flow is in
[`docs/manual-test-plan.md`](../docs/manual-test-plan.md).

---

## What was deliberately not built or verified here

- **A full live Ship 30 essay generation through the UI.** Checkpoint 3
  already verified the essay skill itself end-to-end against the live model
  (a full 7-call run, ~18 minutes on this CPU-only setup); re-running that
  through the browser would mostly re-prove the same skill, not the
  frontend, for a very large time cost. The frontend's Markdown artifact
  rendering path (react-markdown, source/preview toggle, copy/download) is
  verified directly against a real HTML artifact and against unit tests;
  the Markdown-specific rendering is unit-tested but not yet driven live
  end-to-end through a real essay. Flagged in the manual test plan for
  checkpoint 5.
- **A full screen-reader pass** (NVDA/VoiceOver). Verified through the
  accessibility tree (Playwright snapshots) and keyboard-only interaction,
  which catch the most common failures (missing roles, unreachable
  controls, missing live regions) but are not a substitute for an actual
  screen reader. Flagged for checkpoint 5.
- **A device/browser matrix.** One 400px viewport spot check, not a full
  responsive/cross-browser suite — out of scope for this engagement's time
  budget, and not called for by the PRD's acceptance criteria.
- **Rewriting M4's target.** The measured ~127-146s is reported as a
  finding, not adjusted away; see the manual test plan §7 for the full
  reasoning and the options this leaves for later.
