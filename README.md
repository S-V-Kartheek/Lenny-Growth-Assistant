# The Lenny Growth Assistant

A grounded product-and-growth assistant built on transcripts from
[Lenny's Podcast](https://www.youtube.com/@LennysPodcast). Ask a real product
question, get an answer supported by what actual operators said — with citations
that deep-link to the exact second of the episode the claim came from.

It runs entirely on your machine: **Ollama** for the model, **PostgreSQL +
pgvector** for the index, **FastAPI** for the API. Cloud providers (Anthropic,
OpenAI) are a configuration change, not a code change.

> **Status:** Checkpoint 1 of 5 complete — knowledge spine (ingestion, hybrid
> retrieval, schema, health, evaluation harness). The conversational API, skills,
> artifact viewer and frontend land in later checkpoints. This README documents
> only what is actually implemented and verified today.

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
                 │  Frontend (checkpoint 4)     │
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
      │  lexical +   │   │ (checkpoint  │   │  + pgvector  │
      │  semantic    │   │   2 & 3)     │   │              │
      │  → RRF fuse  │   └──────┬───────┘   └──────────────┘
      └──────┬───────┘          │
             │            ┌─────▼──────┐
             │            │ LLM layer  │  Ollama / Anthropic / OpenAI
             │            └────────────┘
             ▼
      ┌──────────────────────────────────────┐
      │  Ingestion                           │
      │  pinned tarball → parse → chunk →    │
      │  index (tsvector + embeddings)       │
      └──────────────────────────────────────┘
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
    db/                 Engine, migration runner, SQL schema
    ingestion/          fetch → parse → chunk → select → pipeline
    retrieval/          Hybrid retriever, embeddings, inspector CLI
    evals/              Golden set + retrieval evaluation harness
  tests/                Pure tests (always run) + db-marked tests
docker-compose.yml      db · ingest · api
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

That starts PostgreSQL, runs ingestion, and starts the API on
<http://localhost:8000> (interactive docs at `/docs`).

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

## Tests

```bash
cd backend
uv venv --python 3.11 .venv && uv pip install -e ".[dev]" --python .venv

.venv/Scripts/python -m pytest                      # everything
.venv/Scripts/python -m pytest -m "not db"          # no PostgreSQL needed
```

Tests come in two tiers. The pure tier (parsing, chunking, selection, fusion,
confidence, configuration) needs nothing and must always pass. The `db` tier
needs PostgreSQL and **skips with an explanatory message** when it is absent,
rather than failing and drowning out real regressions.

To run the `db` tier locally:

```bash
docker compose up -d db
docker exec lenny-growth-assistant-db-1 psql -U lenny -d postgres -c "CREATE DATABASE lenny_test;"
cd backend && .venv/Scripts/python -m pytest
```

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
