# The Lenny Growth Assistant

A grounded product-and-growth assistant built on transcripts from
[Lenny's Podcast](https://www.youtube.com/@LennysPodcast). Ask a real product
question, get an answer supported by what actual operators said — with citations
that deep-link to the exact second of the episode the claim came from.

It runs entirely on your machine: **Ollama** for the model, **PostgreSQL +
pgvector** for the index, **FastAPI** for the API. Cloud providers (Anthropic,
OpenAI) are a configuration change, not a code change.

> **Status:** Checkpoint 4 of 5 complete — knowledge spine, provider-agnostic
> LLM layer, grounded Q&A, the Ship 30 essay skill, Markdown/sanitised-HTML
> artifact generation, and now a React frontend: streaming chat with phase
> feedback, cited/refused answer states, a sandboxed artifact viewer, and
> accessibility support. Final hardening and documentation land in
> checkpoint 5. This README documents only what is actually implemented and
> verified today.

---

## Why it is built this way

Three decisions shape everything else.

**Citations point at a moment, not a document.** Every transcript turn carries a
speaker and a timestamp, so a chunk can be traced to
`youtube.com/watch?v=<id>&t=<seconds>`. A citation you can click and verify in
five seconds is worth more than a footnote you have to trust.

**The corpus is pinned to one upstream commit.** Ingestion downloads
`ChatPRD/lennys-podcast-transcripts` at an exact SHA and stores that SHA on every
episode. If the archive changes upstream, existing citations still mean what they
meant when they were generated. Refreshing is a deliberate act: bump
`CORPUS_COMMIT`, re-run ingestion, and only changed episodes are re-indexed.

**Refusing is a feature.** Retrieval produces a confidence score, and below a
calibrated threshold the assistant says it has no supporting material instead of
producing fluent, well-cited nonsense. That threshold is *measured* against a
golden set, not guessed — see [Evaluating retrieval](#evaluating-retrieval).

---

## Architecture

```
                 ┌──────────────────────────────┐
                 │  Frontend (React + Vite)     │
                 └───────────────┬──────────────┘
                                 │ HTTP / SSE
                 ┌───────────────▼──────────────┐
                 │  FastAPI                     │
                 │  health · errors · logging   │
                 └───────────────┬──────────────┘
                                 │
              ┌──────────────────┼──────────────────┐
              ▼                  ▼                  ▼
      ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
      │  Retrieval   │   │  Agent layer │   │  PostgreSQL  │
      │  lexical +   │   │  router ·    │   │  sessions ·  │
      │  semantic    │   │  skills:     │   │  messages ·  │
      │  → RRF fuse  │   │  Q&A, essay, │   │  artifacts   │
      └──────┬───────┘   │  artifact    │   │  + pgvector  │
             │            └──────┬───────┘   └──────────────┘
             │                   │                   ▲
             │            ┌──────▼─────┐             │
             │            │ LLM layer  │  Ollama /    │ sanitised
             │            │            │  Anthropic / │ artifact
             │            └────────────┘  OpenAI      │ content
             ▼                                        │
      ┌──────────────────────────────────────┐        │
      │  Ingestion                           │        │
      │  pinned tarball → parse → chunk →    │        │
      │  index (tsvector + embeddings)       │        │
      └──────────────────────────────────────┘        │
                                                        │
      HTML artifact → allow-list sanitiser (nh3) → independent
      re-verify → reject or persist ────────────────────┘
```

Full detail, including the database schema and the reasoning behind each
boundary, lives in [`docs/architecture.md`](docs/architecture.md) *(written in
checkpoint 5)*.

### Repository layout

```
backend/
  app/
    config.py           Typed settings — every knob, one place
    main.py             App factory, middleware, error envelope
    observability.py    Structured JSON logging + request correlation
    errors.py           Error taxonomy with stable machine-readable codes
    api/health.py       Liveness / readiness / per-dependency diagnostics
    api/sessions.py     Sessions, message history, SSE streaming chat
    db/                 Engine, migration runner, SQL schema
    ingestion/          fetch → parse → chunk → select → pipeline
    retrieval/          Hybrid retriever, embeddings, inspector CLI
    llm/                Provider abstraction: Ollama · Anthropic · OpenAI
    agent/              Skill contract, router, runtime, skills (Q&A, essay,
                        artifact), essay contract, HTML/Markdown sanitiser
    api/artifacts.py    List/get artifacts, isolated HTML document endpoint
    services/           Session/message + artifact persistence, orchestration
    evals/              Golden set + retrieval + grounding + essay evaluation
  tests/                Pure tests (always run) + db-marked + llm-marked tests
frontend/
  src/
    api/               REST client, typed wire contracts, the SSE frame reader
    components/        Chat, source cards, refusal/error banners, artifact viewer
    hooks/             Session/provider/artifact data, the chat-stream state machine
    lib/               Pure logic: citation linking, refusal detection, the stream reducer
  Dockerfile           Build the static bundle, serve it with `serve`
docker-compose.yml      db · ingest · api · web
.env.example            Every setting, documented, safe defaults
```

---

## Prerequisites

| Requirement | Why | Notes |
|---|---|---|
| Docker Desktop | Runs PostgreSQL + pgvector and the API | The only hard dependency |
| [Ollama](https://ollama.com) | The local model, running on the **host** | Not containerised on purpose — see below |
| ~6 GB disk | Models and the transcript corpus | |

Ollama runs on the host rather than in Compose so it can use your GPU and your
existing model cache; containerising it would force a multi-gigabyte image pull
on every evaluator. Containers reach it via `host.docker.internal`.

Pull the two models the defaults expect:

```bash
ollama pull qwen2.5:7b-instruct   # generation
ollama pull nomic-embed-text      # embeddings (~274 MB)
```

Neither is mandatory. With a different chat model, set `OLLAMA_MODEL` **and**
`OLLAMA_CONTEXT_TOKENS` to match its real context window. Without the embedding
model the system still works — retrieval degrades to lexical-only and says so.

---

## Quick start

```bash
git clone <this-repo> && cd lenny-growth-assistant
cp .env.example .env
docker compose up --build
```

That starts PostgreSQL, runs ingestion, starts the API on
<http://localhost:8000> (interactive docs at `/docs`), and starts the
frontend on <http://localhost:4173>.

**First run takes roughly 10–15 minutes**, almost all of it embedding ~4,400
transcript chunks locally. It is a one-off: the index persists in a Docker volume
and re-running skips unchanged episodes. To trade coverage for speed, lower
`INGEST_MAX_EPISODES` in `.env` before the first `up`.

Verify it came up cleanly:

```bash
curl http://localhost:8000/health/detail
```

Every dependency reports its own status, so a partial failure is visible rather
than fatal:

```json
{
  "database":       { "status": "ok", "pgvector": true },
  "knowledge_base": { "status": "ok", "episodes": 40, "chunks": 4448 },
  "embeddings":     { "status": "ok", "model": "nomic-embed-text" },
  "corpus":         { "commit": "be8ab89a..." }
}
```

---

## Configuration

Every setting is documented inline in [`.env.example`](.env.example). The ones
worth knowing:

| Variable | Default | Effect |
|---|---|---|
| `LLM_PROVIDER` | `ollama` | `ollama` · `anthropic` · `openai` |
| `OLLAMA_MODEL` | `qwen2.5:7b-instruct` | Any pulled model |
| `OLLAMA_CONTEXT_TOKENS` | `32768` | **Must match the model.** Context budgeting depends on it |
| `EMBEDDING_PROVIDER` | `ollama` | `none` forces lexical-only retrieval |
| `RETRIEVAL_TOP_K` | `6` | Passages given to the model |
| `RETRIEVAL_MIN_CONFIDENCE` | `0.48` | Below this, the assistant refuses. Calibrated, see below |
| `RETRIEVAL_MAX_PER_EPISODE` | `2` | Stops one episode monopolising the citations |
| `INGEST_MAX_EPISODES` | `40` | Most-watched full episodes. Raise for coverage, lower for speed |
| `CORPUS_COMMIT` | pinned SHA | Bump to refresh the corpus |

Switching to a cloud model is two lines and no code change:

```bash
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
```

**No secrets are committed.** `.env` is gitignored; `.env.example` contains only
safe defaults and empty key placeholders.

---

## How ingestion works

```
pinned tarball  →  parse  →  select  →  chunk  →  index
```

1. **Fetch** — one immutable tarball for `CORPUS_COMMIT`, cached in a volume.
2. **Parse** — YAML frontmatter for metadata, then speaker turns. The upstream
   archive uses **three different transcript layouts**; all three are supported,
   because handling only the most common one silently dropped 10% of episodes
   (including Seth Godin and Teresa Torres) behind a log warning. If more than 5%
   of files fail to parse, ingestion logs an **error**, not a warning.
3. **Select** — full episodes only (`>= 20 min`, excluding promo clips), ranked
   by view count then slug so the selection is byte-identical on every machine.
4. **Chunk** — whole speaker turns accumulated to a token budget, never split
   mid-turn unless one turn alone overflows. Each chunk keeps its speakers and
   its start/end second offsets, which is what makes a citation deep-linkable.
5. **Index** — a generated `tsvector` column for lexical search, plus optional
   pgvector embeddings. Embedding is resumable: an interrupted run picks up
   exactly where it stopped.

Re-running is idempotent — an episode whose content hash is unchanged is skipped.

```bash
docker compose run --rm ingest          # refresh
docker compose run --rm ingest --force  # re-index everything
```

---

## How retrieval works

Lexical and semantic search each fail differently. Lexical nails product names
and jargon but misses paraphrase; semantic handles "how do I get people to stick
around" → retention, but happily returns topically-adjacent passages that do not
answer the question.

Both run, and their rankings are fused with **Reciprocal Rank Fusion**. RRF is
used instead of a weighted score blend because `ts_rank_cd` and cosine similarity
are on incomparable scales — RRF needs only the *ordering* from each leg, so
there is no fragile normalisation constant to tune.

A per-episode cap then ensures the citations reflect the corpus rather than one
dominant episode.

If pgvector or the embedding model is unavailable, the semantic leg is skipped,
`method` reports `lexical`, and the degradation is surfaced through the API —
the system does not pretend nothing changed.

### Inspecting retrieval

When an answer looks wrong, this separates "the model reasoned badly" from "the
model was handed the wrong passages" — very different bugs:

```bash
cd backend && .venv/Scripts/python -m app.retrieval.inspect "how do I improve activation?"
```

It prints the fused ranking, which leg contributed each passage, the confidence
score, and whether the system would answer or refuse.

### Evaluating retrieval

```bash
cd backend && .venv/Scripts/python -m app.evals.retrieval_eval
```

Runs a golden set of answerable and deliberately-unanswerable questions and
reports recall@k, the confidence separation between the two populations, and a
threshold sweep. Measured against the full 40-episode index:

```
recall@6       : 100% (11/11 questions)
confidence separation
  in-corpus      mean 0.741   min 0.532
  out-of-corpus  mean 0.311   max 0.471
  gap            +0.061  (clean separation)
current threshold 0.48:  answers 12/12 in-corpus, refuses 8/8 out-of-corpus
                          (balanced accuracy 1.00)
```

`RETRIEVAL_MIN_CONFIDENCE=0.48` is that measured value, not a guess. An earlier
guessed default of `0.25` confidently answered *"which Kubernetes operator
should I use for quantum annealing hardware?"* with full citations -- exactly the
failure this threshold exists to prevent. The full report, including every
question and its score, is at
[`docs/retrieval-eval-report.json`](docs/retrieval-eval-report.json).

---

## The conversational API

```
POST   /api/sessions                       create a session
GET    /api/sessions                       list sessions
GET    /api/sessions/{id}                  full message history, with sources
POST   /api/sessions/{id}/messages         post a message, stream the answer (SSE)
GET    /api/provider                       which provider/model is answering right now
GET    /api/sessions/{id}/artifacts        list a session's artifacts
GET    /api/artifacts/{id}                 one artifact, sanitised content only
GET    /api/artifacts/{id}/document        the isolated HTML document, for a sandboxed iframe
```

A message is routed to one of three intents -- `knowledge_qa`, `ship30_essay`,
`artifact` -- by a hybrid router: fast deterministic rules for unambiguous
phrasing, falling back to one small LLM call only when the rules disagree.
For an artifact request, a second routing decision picks the output format
(Markdown or HTML) from explicit cues in the message, with ties going to
Markdown -- the lower-risk path.

`knowledge_qa` is the grounded answer skill: it retrieves, refuses outright if
confidence is below `RETRIEVAL_MIN_CONFIDENCE`, otherwise prompts the model
with numbered passages and a hard citation contract, then validates the
result -- markers that do not resolve to a retrieved passage are stripped, and
an answer that ends up with no valid citation at all is retried once and then
refused rather than shown. See
[`agent-transcripts/checkpoint-2-agent-runtime.md`](agent-transcripts/checkpoint-2-agent-runtime.md)
for why an in-process runtime was used instead of the Claude Agent SDK.

`ship30_essay` writes a ~1,250-word Ship 30 for 30 essay -- hook, skimmable
headings and bullets, selective bold, a specific takeaway -- grounded in the
same retrieved passages as a normal answer. A single free-form "write me
1,250 words" prompt does not work reliably on a 7B model (checkpoint 1), so
generation is structured: a schema-validated outline (one repair attempt,
then a deterministic fallback outline) is written first, then each
part -- hook, each section, the takeaway -- is generated and citation-checked
separately, then assembled by code (headings are written by the skill, not
the model, so a required element cannot be missing), then repaired
(over-bolding and over-length are fixed deterministically; under-length gets
one targeted expansion call) and verified.

`artifact` produces a Markdown document or a sanitised HTML/CSS page -- a
checklist, one-pager or landing page -- from the conversation. Generated HTML
is treated as hostile by default (PRD assumption A6): it passes through an
allow-list sanitiser (`nh3`, the same `html5ever` tokenizer browsers use) and
then an independent, unrelated re-parse that checks the *output* against the
same policy and rejects the artifact outright if the two disagree, rather
than trusting one implementation. The served document
(`/api/artifacts/{id}/document`) carries its own `Content-Security-Policy`
(`default-src 'none'`, no `script-src` at all) and is designed to be embedded
in an iframe with `sandbox` and no `allow-same-origin` -- so even a total
sanitiser bypass would land in an opaque origin with no script execution, no
parent-page access, and no cookies or storage. What is blocked and what is
allowed is documented in full in `app/agent/sanitize.py`'s module docstring,
and exercised by 129 adversarial tests in `tests/test_sanitize.py` (script
tags in a dozen forms, event-handler attributes, obfuscated `javascript:`/
`data:`/`blob:` URLs, CSS-based exfiltration via `url()`/`expression()`/
`-moz-binding`, and mutation-XSS/foreign-content payloads).
`raw_content` is retained on the `artifacts` table for debugging only --
`ArtifactRow`, the shape every API response is built from, has no field for
it at all, so a careless "serialise everything" change cannot leak it.

The response streams as Server-Sent Events with phase events
(`routing` → `retrieving` → `generating` → `validating` → `done`) so a slow
local model still feels responsive, plus `sources` (as soon as retrieval
finishes) and `delta` (token-by-token text). Sessions are isolated at the
database layer -- every query is scoped by `session_id`, and history for one
session never leaks into another (`test_db_sessions.py`,
`test_api_chat.py`).

### Evaluating grounding

```bash
cd backend && .venv/Scripts/python -m app.evals.grounding_eval
```

Runs the real grounded Q&A skill against the live index and the configured
model, for every answerable golden question, and checks whether each answer is
actually grounded -- a citation that resolves to a retrieved passage, and no
uncited paragraph. Measured against `qwen2.5:7b-instruct` and the full
40-episode index:

```
M1 grounded answer rate : 100% (12/12 answered questions)
  12 in-corpus questions total, 0 refused (excluded from M1, see M2)
```

The first run measured 75% -- a citation-retry gate that only fired on a
*total* absence of citations let through answers with one uncited "in
summary" paragraph tacked onto an otherwise well-cited response. Widening the
retry trigger to cover that case brought it to 100% on the same golden set and
model, without moving M2/M3. Full account in
[`agent-transcripts/checkpoint-2-agent-runtime.md`](agent-transcripts/checkpoint-2-agent-runtime.md);
per-question numbers in
[`docs/grounding-eval-report.json`](docs/grounding-eval-report.json).

### Evaluating the Ship 30 essay skill

```bash
cd backend && .venv/Scripts/python -m app.evals.essay_eval --limit 3
```

Runs the real `Ship30EssaySkill` against the live index and model for
in-corpus golden questions, then measures each essay two ways: a **structure
pass rate** (`app.agent.essay.evaluate_structure` -- word-count band, title,
hook, section headings, bullets, selective bold, takeaway, all defined once
and applied identically by the skill's own repair logic, this harness, and
the tests) and a **grounded rate** -- the essay analogue of M1, and the one
that must not regress. Essays are slow on a 7B model (seven sequential model
calls, minutes each), so `--limit` runs a subset while iterating; omit it for
a full run. See
[`agent-transcripts/checkpoint-3-ship30-and-artifacts.md`](agent-transcripts/checkpoint-3-ship30-and-artifacts.md)
for a real run's numbers, including a genuine 91-word hook that the harness's
first version wrongly rejected -- and the fix.

---

## The frontend

React + TypeScript + Vite, served as the `web` Compose service (a static
build behind `serve`) or via `npm run dev` for local iteration. Chosen over
Next.js because there is no server-rendering or routing need this app
actually has -- every view depends on live session/chat state, and "which
session is active" is component state, not a router.

- **Streaming chat** reads `POST /api/sessions/{id}/messages`'s SSE response
  directly with the fetch Streams API (`EventSource` cannot do `POST`),
  surfacing every phase event (`routing → retrieving → generating →
  validating → done`) so a 30+ second local-model wait reads as progress.
- **Source cards** render as soon as retrieval finishes -- before the answer
  text -- and visually distinguish cited from uncited/near-miss sources.
- **Refusals** render in a distinct banner, never as a normal answer bubble.
- **The artifact viewer** embeds HTML artifacts in a sandboxed `<iframe>`
  using the exact `sandbox` string the API returns, and renders Markdown
  artifacts client-side without `rehype-raw` -- so the renderer has no code
  path that turns markdown text into live HTML at all, independent of the
  server's own escaping.
- **Accessibility**: keyboard-operable throughout, an `aria-live` phase
  status region, and states for empty/loading/streaming/error.

Full principles in [`docs/design.md`](docs/design.md), the executed test
plan (including two real cross-origin bugs this checkpoint found and fixed
-- an SSE parser byte-format bug and a CSP `frame-ancestors` gap) in
[`docs/manual-test-plan.md`](docs/manual-test-plan.md), and the full account
in
[`agent-transcripts/checkpoint-4-frontend.md`](agent-transcripts/checkpoint-4-frontend.md).

```bash
cd frontend
npm install
npm run dev       # local dev server, http://localhost:5173
npm test          # vitest + Testing Library, 41 tests
npm run lint      # oxlint
```

---

## Tests

```bash
cd backend
uv venv --python 3.11 .venv && uv pip install -e ".[dev]" --python .venv

.venv/Scripts/python -m pytest                      # everything
.venv/Scripts/python -m pytest -m "not db and not llm"  # no PostgreSQL or model needed
```

Tests come in three tiers. The **pure** tier (parsing, chunking, selection,
fusion, confidence, configuration, provider fallback policy, routing,
citation validation, the essay structure/outline contract, the artifact
skill, and the adversarial sanitiser suite) needs nothing and must always
pass -- 340 tests. The **`db`** tier needs PostgreSQL and the **`llm`** tier
needs a live model provider (Ollama by default); both **skip with an
explanatory message** when their dependency is absent, rather than failing
and drowning out real regressions. Current counts: 373 pure + db tests, plus
4 tests that exercise the full chat API against a real PostgreSQL and a real
Ollama together (`db and llm`) -- 385 total.

To run the `db` tier locally:

```bash
docker compose up -d db
docker exec lenny-growth-assistant-db-1 psql -U lenny -d postgres -c "CREATE DATABASE lenny_test;"
cd backend && .venv/Scripts/python -m pytest
```

The `llm` tier additionally needs Ollama running with `qwen2.5:7b-instruct`
pulled (see [Prerequisites](#prerequisites)); it is what proves the SSE chat
endpoint and session isolation against the real system, not a mock of it.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `/health/ready` → `knowledge_base: empty` | Ingestion has not run or failed | `docker compose run --rm ingest` and read its logs |
| `embeddings: unavailable` in `/health/detail` | Ollama down, or model not pulled | `ollama pull nomic-embed-text`. Retrieval still works, lexically |
| Ingestion: `embeddings_unavailable_lexical_only` | Same | Re-run ingestion later; embedding resumes where it stopped |
| `migration_skipped_optional: 002_vector.sql` | pgvector extension missing | Expected on stock Postgres. Use `pgvector/pgvector:pg16`, as Compose does |
| Containers cannot reach Ollama | `host.docker.internal` unavailable (some Linux setups) | Set `OLLAMA_BASE_URL=http://172.17.0.1:11434` |
| `database_unavailable` errors | Postgres not up | `docker compose ps db`; check `DATABASE_URL` |
| Ingestion is very slow | Embedding on CPU | Lower `INGEST_MAX_EPISODES`, or raise `EMBEDDING_BATCH_SIZE` |
| `llm: unavailable` in `/health/detail` | Model not pulled, or Ollama down | `ollama pull qwen2.5:7b-instruct`; `ollama serve` |
| Chat answers are slow to start | Local 7B model, cold load | Expected -- watch the SSE `phase` events; the UI is designed around this. Measured ~2-2.5 min end-to-end on CPU-only Ollama -- see `docs/manual-test-plan.md`'s M4 result |
| Frontend header says "API unreachable," or the artifact iframe stays blank | The frontend's origin isn't in `CORS_ORIGINS` | Add it (e.g. a custom port) to `CORS_ORIGINS` in `.env` -- it governs both CORS and the artifact document's `frame-ancestors` |
| A cloud provider returns `provider_unavailable` | Missing or wrong API key | Check `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` in `.env`; `/health/detail`'s `llm.reason` says which |

Every log line is one JSON object with a `request_id` that is also returned in
the `X-Request-ID` response header, so a user-visible failure can be traced to
its exact log lines. Set `LOG_FORMAT=console` for readable local output.

---

## Documentation

| Document | Contents |
|---|---|
| [`docs/PRD.md`](docs/PRD.md) | User, problem, success metrics, scope, risks |
| [`docs/architecture.md`](docs/architecture.md) | Schema, endpoints, routing, security, deployment |
| [`docs/design.md`](docs/design.md) | UI principles, interaction states, accessibility |
| [`docs/manual-test-plan.md`](docs/manual-test-plan.md) | UI test plan |
| [`agent-transcripts/`](agent-transcripts/) | Development log, including failed approaches |

*(Written across checkpoints 2–5.)*
