# Architecture

**Status:** written in checkpoint 5, against the actual code as it exists
today (not the plan for it) — every claim below was checked against the
source file it describes, and the security/deployment sections were
additionally checked against the live, running Docker Compose stack, not
just read off the Dockerfiles. This is the doc the README points to for
"full detail, including the database schema and the reasoning behind each
boundary."

---

## 1. System overview

```
                 ┌──────────────────────────────┐
                 │  Frontend (React + Vite)      │
                 │  its own Compose service,     │
                 │  its own origin               │
                 └───────────────┬───────────────┘
                                 │ HTTP + SSE (fetch, not EventSource)
                 ┌───────────────▼───────────────┐
                 │  FastAPI (`api`)               │
                 │  CORS → rate limit →           │
                 │  request-id/security headers → │
                 │  routing                       │
                 └───────────────┬───────────────┘
                                 │
              ┌──────────────────┼──────────────────┐
              ▼                  ▼                  ▼
      ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
      │  Retrieval    │   │  Agent layer │   │  PostgreSQL   │
      │  lexical +    │   │  router →    │   │  + pgvector   │
      │  semantic     │   │  skill →     │   │               │
      │  → RRF fuse   │   │  runtime     │   │               │
      └──────┬───────┘   └──────┬───────┘   └──────────────┘
             │                   │                   ▲
             │            ┌──────▼─────┐             │
             │            │ LLM gateway│  Ollama /    │ sanitised
             │            │            │  Anthropic / │ artifact
             │            │            │  OpenAI /    │ content
             │            └────────────┘  Gemini /Grok│
             ▼                                        │
      ┌──────────────────────────────────────┐        │
      │  Ingestion (`ingest`, one-shot)       │        │
      │  pinned tarball → parse → chunk →     │        │
      │  index (tsvector + embeddings)        │        │
      └──────────────────────────────────────┘        │
                                                        │
      HTML artifact → nh3 allow-list → independent ────┘
      re-verify (stdlib) → reject or persist
```

Four Compose services (`db`, `ingest`, `api`, `web`); Ollama runs on the host,
not in Compose — see §6.

---

## 2. Database schema

PostgreSQL 16 + pgvector, applied by an idempotent, hand-rolled migration
runner (`app/db/migrate.py`) rather than a framework — two small SQL files
(`app/db/migrations/001_init.sql`, `002_vector.sql`) are proportionate to a
schema this size, and each is safe to re-run. Migrations execute over the
asyncpg **driver connection directly**, not through SQLAlchemy's prepared-
statement path — a multi-statement `.sql` file cannot be PREPAREd as a single
statement, which is exactly the failure this caused the first time (see
`agent-transcripts/checkpoint-1-knowledge-spine.md`).

Two independent domains meeting only at `message_sources`:

### Knowledge (write path: `ingest`; read path: `api`, read-only)

| Table | Purpose | Notable columns |
|---|---|---|
| `episodes` | One row per ingested episode | `source_repo`/`source_commit`/`content_hash` (provenance — which upstream revision this text came from), `view_count` (drives selection ranking) |
| `chunks` | One row per retrievable passage | `speakers TEXT[]`, `start_seconds`/`end_seconds` (nullable — format-C transcripts have no timestamps, see below), `tsv` a **generated, stored** `tsvector` column (`GENERATED ALWAYS AS (to_tsvector('english', content)) STORED`, so it can never drift from `content`), `embedding vector(768)` + `embedding_model` (added by migration 002, nullable — a chunk with no embedding still participates in lexical search) |
| `ingestion_runs` | One row per pipeline run | Counts (`episodes_ingested`/`skipped`, `chunks_created`/`embedded`) and `status` — an operational audit trail, not just a log line |

Indexes: a GIN index on `tsv` for lexical search, an **HNSW** index on
`embedding` (`vector_cosine_ops`) — chosen over IVFFlat specifically because
HNSW needs no separate "train after bulk load" step, which matters because
ingestion and querying can interleave (an evaluator can query while a second
`ingest --force` run is still embedding).

### Conversation (read/write path: `api` only)

| Table | Purpose | Notable columns |
|---|---|---|
| `users` | One row per caller | `external_id` — see §4, this is a label, not an authenticated identity |
| `sessions` | One chat thread | `updated_at` kept current by a trigger (`touch_session_updated_at`) fired on every `messages` insert, so "recent sessions first" needs no application-side bookkeeping |
| `messages` | One row per turn (user or assistant) | `intent`, `provider`, `model`, `latency_ms`, `grounding JSONB`, `error JSONB` — routing and grounding are recorded per message, not just logged, so they're inspectable after the fact; `error` is also returned on `MessageRecord` via the API (checkpoint 6 — a failed turn was writing this column correctly but the read path and API schema both dropped it, so a timeout was served to the client as an empty, unexplained message) |
| `message_sources` | Which chunk backed which answer | `chunk_id` (`ON DELETE SET NULL` — a citation survives even if the source chunk is later removed by a corpus refresh), `cited BOOLEAN`, `rank`, `score`, `snapshot JSONB` (a frozen copy of the source's display fields, so a citation still renders correctly even if `episodes`/`chunks` change under it) |
| `artifacts` | Generated Markdown/HTML | `content` (sanitised — the only field ever returned by the API) vs. `raw_content` (unsanitised model output, debugging-only); `version` atomic per session |

**Why `message_sources` is the load-bearing join.** It is what makes the
PRD's central promise auditable after the fact, not just at response time: a
citation isn't "the model said so," it's a database row linking a specific
assistant message to a specific transcript chunk, with whether it was
actually cited recorded independently of whether it was merely retrieved.

**Why `chunks.start_seconds` is nullable.** One of the three upstream
transcript formats (`agent-transcripts/checkpoint-1-knowledge-spine.md`,
finding 3) carries no timestamps at all. Making the column nullable rather
than excluding that episode kept its content in the corpus, citable at
episode granularity instead of dropped entirely.

---

## 3. API endpoints

All routes are under `/api` except health (unversioned, liveness tooling
expects a fixed path) and docs (`/docs`, `/openapi.json`).

| Method & path | Purpose | Auth/scoping |
|---|---|---|
| `GET /health` | Liveness — process is up. Touches nothing external | none |
| `GET /health/ready` | Readiness — database + knowledge base reachable. 503 if not | none |
| `GET /health/detail` | Per-dependency status (db, knowledge base, embeddings, LLM, corpus commit) | none |
| `POST /api/sessions` | Create a session | scoped to the resolved user |
| `GET /api/sessions` | List a user's sessions | scoped to the resolved user |
| `GET /api/sessions/{id}` | Full history with sources | ownership-checked (404 if not the caller's) |
| `POST /api/sessions/{id}/messages` | Post a message, **stream the answer as SSE** | ownership-checked before the stream opens |
| `GET /api/provider` | Active provider/model/context window | none |
| `GET /api/sessions/{id}/artifacts` | List a session's artifacts | ownership checked on the session |
| `GET /api/artifacts/{id}` | One artifact, sanitised JSON | ownership checked via the artifact's session |
| `GET /api/artifacts/{id}/document` | Isolated HTML document for the iframe | ownership checked via the artifact's session |

**SSE wire format** (`POST /api/sessions/{id}/messages`): named events
`phase → routing → sources → delta* → result` (or `error`), each a JSON
object with `{kind, phase?, text?, data?, result?}`. `sse_starlette` frames
these with **CRLF** (`\r\n\r\n`), not a bare `\n\n` — the frontend's reader
normalises this explicitly (`frontend/src/api/sse.ts`) after a real bug where
it didn't (checkpoint 4, §7 below).

**Error envelope**, identical on every failure path (`app/errors.py`,
`app/main.py::_register_error_handlers`):

```json
{"error": {"code": "provider_unavailable", "message": "...", "remediation": "...", "request_id": "..."}}
```

`AppError` subclasses map to it directly; Starlette's own `HTTPException`
(404/405/…) and Pydantic's `RequestValidationError` are both reshaped into
the same envelope rather than leaking their own `{"detail": ...}` shape, so
the frontend has exactly one error contract to handle regardless of where a
request failed.

### Request routing inside a turn

`POST /api/sessions/{id}/messages` → `app/services/chat.py::stream_turn` →
`app/agent/orchestrator.py::Agent.run`:

1. **Route** (`IntentRouter`) — fast deterministic regex rules first
   (`knowledge_qa` / `ship30_essay` / `artifact`, plus a format sub-decision
   for `artifact`); a small LLM call only when the rules disagree or find
   nothing. Golden-fixture-tested (`test_router.py`), not just unit-tested in
   isolation.
2. **Run the matched skill** through `DirectRuntime` (`app/agent/runtime.py`)
   — a ~60-line in-process loop, not the Claude Agent SDK (evaluated and
   rejected; see `agent-transcripts/checkpoint-2-agent-runtime.md` for why:
   its only transport spawns the Claude Code CLI as a subprocess, which is
   Claude-only and irrelevant to a system whose default and primary path is
   Ollama). Every skill (`KnowledgeQASkill`, `Ship30EssaySkill`,
   `ArtifactSkill`) implements the same `Skill` contract
   (`app/agent/contracts.py`): retrieve → generate → validate → emit
   `SkillEvent`s.
3. **Retrieve** (`app/retrieval/retriever.py`) — lexical (`ts_rank_cd` over
   the generated `tsvector`) and semantic (pgvector cosine similarity, when
   `EMBEDDING_PROVIDER != none` and Ollama's embedding model is reachable)
   both run; rankings are fused with **Reciprocal Rank Fusion** (not a
   weighted score blend — `ts_rank_cd` and cosine similarity are on
   incomparable scales, and RRF only needs each leg's ordering). A per-episode
   cap (`RETRIEVAL_MAX_PER_EPISODE`, default 2) is applied post-fusion so one
   episode cannot dominate a citation list.
4. **Refuse or generate** — fused confidence is compared against
   `RETRIEVAL_MIN_CONFIDENCE` (0.48, calibrated against the golden set — see
   §7); below it, the skill refuses outright rather than prompting the model
   at all, and near-miss sources are still returned (labelled `not cited`) so
   a refusal is verifiable rather than a dead end.
5. **Validate and repair** — citation markers (`[S#]`) are checked against
   the actually-retrieved passage numbers; a hallucinated marker is stripped,
   and an answer with an uncited substantive paragraph is retried once before
   being shown (not before being refused — the refusal bar stays stricter, so
   a partially-uncited-but-real answer is still shown rather than discarded).

---

## 4. Security model

Stated plainly, because "single-tenant, no authentication" (PRD A4) is a
scoping decision, not an oversight, and what it does *not* cover matters as
much as what it does.

**Identity.** `X-User-Id` (`app/api/deps.py::get_current_user_id`) is an
optional, caller-chosen label used only to fold requests into the same
anonymous `users` row for multi-browser-tab testing. **It is not a
credential** — nothing validates it, and any caller can address any
`user_id` it likes. The only real boundary the system enforces is
*session ownership*: every session/message/artifact query is additionally
scoped by the resolved `user_id`, so one caller's guessed or reused label
cannot address a session created under a different one **unless it presents
that same label** — this is isolation between concurrent anonymous callers
who don't collide on a label, not authentication. Deploying this beyond a
single trusted operator's machine requires adding real auth first; this is
recorded here, in the README, and in the PRD (A4) as the first thing to add,
not discovered by an evaluator the hard way.

**Transport.** Plain HTTP, on `localhost`, by design for a local-first demo.
Putting this behind a real domain needs TLS (a reverse proxy in front of
`api`/`web`) added before any of the above identity model would be sound to
rely on for anything but casual isolation.

**CORS.** `CORS_ORIGINS` is an explicit allow-list (`app/config.py`), never
`*`, because the API is session-bearing (`allow_credentials=True` would be
unsafe with a wildcard origin regardless). The default list has to name every
origin a real client actually runs on — a real gap found live in checkpoint 4
(the containerised `web` service's port, `:4173`, was missing from the
default) and is the kind of thing worth checking again after any Compose
port change, not just trusting the code once matched reality.

**Rate limiting.** `RATE_LIMIT_PER_MINUTE` (`Settings.rate_limit_per_minute`,
default 60) and a dedicated error code (`ErrorCode.RATE_LIMITED`,
`RateLimited`, HTTP 429) existed since checkpoint 1 — but nothing enforced
either until checkpoint 5. Found live during this checkpoint's hardening pass
by hammering `/api/provider` and watching every request succeed regardless
of volume. Fixed with a per-process, in-memory sliding-window limiter
(`app/main.py::create_app`, the `rate_limit` middleware) keyed by `X-User-Id`
when present, else the connecting address, exempting `/health*` so
orchestrator/monitoring probes are never throttled. **Stated limitation:**
the counter lives in one process's memory, so it is correct for the single
`uvicorn` process this system runs as (Compose's `api` service, one
replica) and would under-count across multiple replicas or a restart — a
real deployment beyond this scope would move the counter to a shared store
(e.g. Redis) keyed the same way. Middleware **ordering matters** and is
documented at the registration site: CORS has to be the outermost layer (registered
*last*, since Starlette's `add_middleware` inserts each new layer at the
front of the stack) so a rate-limited or otherwise short-circuited response
still carries `Access-Control-Allow-Origin` — a browser cannot read the body
of a 429 that lacks it, which would have turned "the API is protecting
itself" into "the frontend can't tell why requests are failing."

**Malformed input.** Every request body is a Pydantic model; a malformed
body produces the same error envelope as every other failure (422,
`validation_error`, with a field-level `details.fields` list), not a raw
FastAPI/Pydantic traceback. Verified live in this checkpoint by sending
invalid JSON, a wrong-typed field, and an empty body to
`POST /api/sessions/{id}/messages` — all three returned clean 422s under the
shared envelope, never a 500.

**Artifact isolation (the highest-risk single boundary in the system).**
Generated HTML is treated as hostile by default (PRD A6: it is produced by a
model prompted with user- and transcript-supplied text, i.e. attacker-
influenceable input). Three independent layers, each of which alone would be
a defence, documented in full in `app/agent/sanitize.py`'s module docstring
and exercised by 129 adversarial tests (`tests/test_sanitize.py`):

1. **Allow-list sanitisation** — `nh3` (Rust bindings to `ammonia`, built on
   `html5ever`, the same tokenizer Firefox/Servo use), not a hand-rolled
   parser, specifically because sanitiser bypasses are usually *parser
   differentials* between the sanitiser's and the browser's idea of the
   token stream.
2. **Independent re-verification** — the sanitised *output* is re-parsed by
   a second, unrelated stdlib-only implementation (`verify_sanitised`) and
   checked against the same policy; disagreement rejects the artifact
   outright (`ArtifactRejected`, never served) rather than trusting one
   implementation. This is what a monkeypatched "make `nh3.clean` itself
   return something forbidden" test actually exercises.
3. **Isolation at render time** — `GET /api/artifacts/{id}/document` serves
   the document with `Content-Security-Policy: default-src 'none'` (no
   `script-src` at all, so nothing runs even past a total sanitiser bypass),
   `frame-ancestors` derived from `CORS_ORIGINS` (not hardcoded `'self'` —
   see the checkpoint-4 bug below), and the exact `sandbox` string
   (`ARTIFACT_SANDBOX = "allow-popups allow-popups-to-escape-sandbox"`) an
   embedding iframe must use — deliberately excluding `allow-same-origin`
   and `allow-scripts`, tested directly
   (`test_sandbox_contract_never_grants_same_origin_or_scripts`) because it
   is the one line that would undo every other layer if it regressed.

`raw_content` (the unsanitised model output, kept for debugging) has no field
on `ArtifactRecord`/`ArtifactRow` at all — reaching it requires calling
`get_raw_for_debugging()` by name, so a careless "serialise the whole row"
change cannot leak it through any API response.

**Response headers, always present** (`app/main.py`'s `request_context`
middleware): `X-Content-Type-Options: nosniff`, `Referrer-Policy:
no-referrer`, `X-Request-ID` (echoing the caller's own if supplied). The API
never serves HTML except the one deliberately-isolated artifact document
endpoint, so a response that somehow carried markup must never be sniffed
into execution by a browser that ignores content negotiation.

**Secrets.** `.env` is gitignored; `.env.example` ships only safe local
defaults and empty cloud-provider key placeholders. `/health/detail` and
every error response are tested (`test_health_never_leaks_the_database_url`)
to never echo `DATABASE_URL`, an API key, or any value matching
`observability.py`'s redaction key list — logging redacts the same set at
the formatter level, not by trusting call sites to remember.

---

## 5. Provider abstraction and degraded-mode behaviour

One interface (`app/llm/base.py`: `health`, `stream`, `complete` built on
`stream`), five implementations (`ollama.py`, `anthropic.py`, `openai.py`,
`gemini.py`, `grok.py` — the last reusing `openai.py`'s Chat Completions
shape against xAI's OpenAI-compatible endpoint, since that is what the
provider actually is), built on raw `httpx` rather than vendor SDKs — a
stated trade-off
(`app/llm/transport.py`'s docstring): the vendor SDKs would give free
retries and forward-compatibility, at the cost of normalising three
different retry/error/streaming shapes into this system's own instead of
one. `LLMGateway` (`app/llm/registry.py`) implements a **documented**
fallback policy, not an implicit one: it falls back to
`LLM_FALLBACK_PROVIDER` only on `ProviderUnavailable`/`ProviderTimeout`
(never a 4xx, which is a configuration bug to surface, not paper over), and
only **before the first token is emitted** — switching mid-stream would
splice two models' prose together into one answer.

**Verified live, this checkpoint, against the actual running stack** (not
inferred from reading the retry logic):

- **Ollama unreachable.** Stopping Ollama and hitting `/health/detail` shows
  `"llm": {"status": "unavailable", "reason": "..."}` without the endpoint
  itself erroring; posting a chat message under the same condition returns
  the standard error envelope (`provider_unavailable`, 503) with the
  documented remediation ("run `ollama serve`..."), not a raw connection
  traceback. Semantic retrieval degrades to lexical-only in the same
  condition and says so (`method: "lexical"` in the retrieval response and
  `embeddings.status: "unavailable"` in `/health/detail`, with an `impact`
  string explaining the consequence) — this was true since checkpoint 1 and
  re-confirmed here against the current code.
- **Postgres briefly unavailable.** `test_api_health.py`'s
  `client_without_database` fixture (an app instance pointed at a reachable
  IP with nothing listening) proves `/health` still returns 200 (liveness
  must never depend on the database, or a brief blip makes the orchestrator
  kill a perfectly healthy process), `/health/ready` returns 503 with
  `database.status: "unavailable"`, and `/health/detail` still answers 200
  with the same per-dependency detail rather than crashing.
  **Live-verified this checkpoint against a real Compose stack, and it
  found a real bug**: stopping the `db` container and posting a chat
  message returned a raw `500 internal_error`, not the documented `503
  database_unavailable`. Cause: `app.db.engine.connection()` only translated
  SQLAlchemy's `OperationalError`/`InterfaceError`/`DisconnectionError` into
  `DatabaseUnavailable` — but a **stopped Compose service's hostname stops
  resolving immediately** (Docker's embedded DNS), which raises a raw
  `socket.gaierror` (an `OSError` subclass) from the connection pool's
  `connect()` step, before SQLAlchemy's own error translation ever sees it.
  The existing test fixture used a refused TCP connection
  (`ConnectionRefusedError`, which *is* wrapped as `OperationalError`), so
  it never exercised the DNS-failure path a real stopped container actually
  takes. Fixed by also catching `OSError` in `connection()`; a regression
  test (`test_a_chat_request_is_database_unavailable_not_a_raw_500_on_dns_failure`)
  uses a `.invalid` hostname to exercise the DNS path specifically.
  Re-verified live after the fix: stopping/restarting `db` now produces the
  documented 503 and clean recovery, with no `api` restart needed, because
  nothing in the request path holds a connection open across requests in a
  way that a transient outage poisons.
- **Malformed requests.** Covered in §4.

---

## 6. Deployment topology

`docker-compose.yml`, four services, one Compose project
(`name: lenny-growth-assistant`):

| Service | Image / build | Role | Depends on |
|---|---|---|---|
| `db` | `pgvector/pgvector:pg16` | PostgreSQL + pgvector, named volume `pgdata` | — |
| `ingest` | `./backend` (same image as `api`) | One-shot: fetch pinned corpus → parse → chunk → index. Runs to completion before `api` is considered usable | `db` (healthy) |
| `api` | `./backend` | FastAPI, port 8000 | `db` (healthy), `ingest` (**completed successfully** — not just started) |
| `web` | `./frontend` | Static build served by `serve`, port 4173 | `api` (healthy) |

**Ollama runs on the host, not in Compose** — a deliberate boundary, not an
oversight: it needs GPU access and a model cache most evaluators already
have, and containerising it would force a multi-gigabyte image pull on every
fresh clone regardless of whether the host already has the model. Containers
reach it via `host.docker.internal` (mapped to the host gateway via
`extra_hosts` in `docker-compose.yml`, since `host.docker.internal` is not
resolvable by default on Linux Docker Engine — only Docker Desktop).

**Named volumes**, both required for the idempotency and reproducibility
claims elsewhere in this document to hold across restarts: `pgdata` (the
database — a restart does not lose sessions/messages/the index) and `corpus`
(the cached, pinned transcript tarball — `ingest` re-running does not
re-download it, and only re-embeds episodes whose content hash changed).

**Health-gating, not sleep-and-hope.** `db`'s healthcheck is `pg_isready`;
`api`'s `depends_on: ingest: condition: service_completed_successfully` means
`api` genuinely will not start against an unindexed database, and `api`'s
own healthcheck (`GET /health`, via a Python one-liner rather than `curl`
since the slim image doesn't ship curl) gates `web`'s startup the same way —
so `docker compose up --build`'s ordering is enforced by Compose itself, not
by a hopeful sleep in an entrypoint script.

**Images.** `backend/Dockerfile` is single-stage (`python:3.11-slim`); it
installs from `pyproject.toml` via `pip install .`, not a hand-copied
dependency list — the latter drifted out of sync when `nh3` was added in
checkpoint 3 and crash-looped the container (`ModuleNotFoundError`), a
duplicated-source-of-truth bug rather than a one-off typo, and `pip install
.` reads the exact same dependency array pytest/`uv` install from locally, so
it cannot drift again the same way. `frontend/Dockerfile` is two-stage
(`node:22-slim` build → `node:22-slim` + `serve`) — `VITE_API_BASE_URL` is
baked into the static bundle at *build* time (Vite inlines
`import.meta.env.*`), not read at container start, because the browser, not
the container, makes the API call. Both images run as an unprivileged
`appuser` (uid 10001) — nothing in either container needs root at runtime.

**Config.** `.env` (gitignored, copied from `.env.example`) is the single
source of truth read by all three application services via `env_file`;
`docker-compose.yml` additionally pins `DATABASE_URL`/`OLLAMA_BASE_URL`/
`CORPUS_CACHE_DIR` to their in-Compose values so the same `.env` works
whether an evaluator runs the API in a container or directly against a
local venv (only the host-vs-`db`/`host.docker.internal` distinction
differs, called out inline in `.env.example`).

---

## 7. Retrieval confidence and the refusal threshold

Not re-derived here — this is the canonical pointer, since the number
appears in the README, the PRD and this document. `RETRIEVAL_MIN_CONFIDENCE
= 0.48` is a **measured** value (`app.evals.retrieval_eval`, run against the
full 40-episode/4,448-chunk index), not a guess: at this threshold the golden
set scores 100% in-corpus answer rate and 100% out-of-corpus refusal rate
(balanced accuracy 1.00), with a clean confidence-separation gap (in-corpus
mean 0.741 vs. out-of-corpus mean 0.311). The original guessed default
(`0.25`) confidently answered a nonsense question about Kubernetes operators
for quantum annealing hardware — the full account, including why the eval
harness shipped in checkpoint 1 rather than at the end, is in
`agent-transcripts/checkpoint-1-knowledge-spine.md`. Re-run
`python -m app.evals.retrieval_eval` any time the corpus, embedding model, or
chunking changes — the number is only trustworthy as long as it keeps being
measured against the current index, not carried forward from memory.
