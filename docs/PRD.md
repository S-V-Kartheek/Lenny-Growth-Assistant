# PRD — The Lenny Growth Assistant

**Status:** living document, updated as checkpoints land
**Owner:** Forward-Deployed Engineer (this engagement)
**Last updated:** checkpoint 1

---

## 1. Forward deployment brief

### 1.1 The user and the job

**Primary user:** a **product manager or growth lead at a 20–500 person software
company** — someone who owns a metric, has to defend a decision in a room next
week, and has no research team to delegate to.

Not "everyone who likes podcasts". The distinction matters because it settles
several design arguments: this user wants a defensible answer they can cite in a
document, not a summary they have to re-verify. That is why grounding and
citation quality outrank breadth of knowledge in every trade-off below.

**The job to be done:**

> *"I have a decision to make this week — about activation, pricing, positioning,
> prioritisation, hiring, or launch — and I want to know what operators who have
> actually done it think, without spending three hours across YouTube."*

**Secondary user:** the **client engineer** who inherits the system. They are a
real user with a real job: run it, diagnose it when the model misbehaves, extend
it with a new skill or a new corpus. Their needs shape the observability, the
health endpoints, the evaluation harness and the documentation.

### 1.2 The pain being removed

Lenny's Podcast is one of the densest sources of operator knowledge in product
and growth: ~390 episodes, several hundred hours. Today, using it to answer a
specific question means remembering which guest discussed it, finding the
episode, scrubbing for the segment, and reconstructing the argument. The
knowledge is *public but not accessible* — the cost is retrieval time, not
availability.

The assistant collapses that to: ask → grounded answer → click the citation and
hear the person say it.

| Without | With |
|---|---|
| ~30–60 min to find and synthesise relevant material | ~1 min |
| Half-remembered attribution ("I think Shreyas said…") | Speaker, episode, timestamp, deep link |
| No way to know what the corpus does *not* cover | Explicit refusal when evidence is insufficient |
| Notes have to be rewritten into shareable form | Ship 30 essay / artifact generated from the same grounded answer |

### 1.3 Success metrics

Targets for this engagement, measured by the harness in `app/evals/` — not
projected production numbers, which would be invented.

| # | Metric | Target | How it is measured | Status |
|---|---|---|---|---|
| **M1** | **Grounded answer rate** — answers that cite at least one real transcript passage and make no uncited factual claim | ≥ 90% of in-corpus golden questions | `app.evals.grounding_eval`, live model | **100% (12/12)** on `qwen2.5:7b-instruct` |
| **M2** | **Honest refusal rate** — out-of-corpus questions declined rather than answered | ≥ 90% | Golden set, out-of-corpus population | **100% (8/8)** at threshold 0.48 |
| **M3** | **Retrieval recall@6** — an expected episode appears in the top 6 | ≥ 85% | `app.evals.retrieval_eval` | **100% (11/11)** |
| **M4** | **Time to first useful answer**, cold start to cited answer | < 2 min | Manual test plan | Checkpoint 4 |
| **M5** | **Setup reproducibility** — fresh clone to working system by documented steps only | 1 command + documented wait | Fresh-evaluator test | Checkpoint 5 |

**M1 and M2 are the primary metrics.** They encode the product's actual promise:
*if it answers, the answer is supported; if it cannot support an answer, it says
so.* A system that scores well on M3 while failing M2 is worse than useless for
this user, because it produces confident, well-formatted, unverifiable advice —
the exact thing they came here to avoid.

### 1.4 Assumptions

Recorded because the brief was intentionally incomplete. Each is a decision that
could reasonably have gone the other way.

| # | Assumption | Why | If wrong |
|---|---|---|---|
| A1 | The transcript archive is the **only** permitted knowledge source | The product's value is verifiable grounding; blending in model world-knowledge makes citations meaningless | Answers would be broader but unverifiable |
| A2 | Users prefer an honest "I don't have material on that" to a fluent unsupported answer | This user has to defend the decision to other people | If wrong, refusals feel unhelpful — mitigated by always showing what *was* found |
| A3 | A curated subset of episodes is better than the full archive for the demo | Full embedding is ~1 hr on a laptop; coverage past the top episodes has diminishing returns for common questions | `INGEST_ALL=true` exists for anyone who disagrees |
| A4 | Single-tenant, no authentication | Stated as an internal team tool; auth would consume the time budget without demonstrating anything the brief asks for | Documented as the first thing to add before real deployment |
| A5 | A pinned corpus commit is better than always-latest | Citations must keep meaning what they meant | Refresh is one config change |
| A6 | Generated HTML is hostile | Model output is untrusted input by definition | — |
| A7 | The local model is the constraint to design for, not the cloud one | The demo must run on Ollama; designing for a 200k-context cloud model and hoping it degrades gracefully is backwards | — |

### 1.5 Scope

**In scope**

- Grounded conversational Q&A over the transcripts, with follow-up context
- Per-session isolation and PostgreSQL persistence
- Source citations with speaker, timestamp and deep link
- Explicit refusal when evidence is insufficient
- Ship 30 for 30 essay skill (structured, not a one-off prompt)
- Markdown and HTML/CSS artifact generation with an in-app sandboxed viewer
- Provider abstraction: Ollama (demo default), Anthropic, OpenAI
- Health, structured logging, graceful degradation for every dependency
- Evaluation harness for retrieval, routing and grounding
- One-command startup, documented handoff

**Explicitly out of scope, and why**

| Excluded | Reason |
|---|---|
| Authentication / multi-tenancy | A4. Real, but demonstrates nothing the brief asks for |
| Streaming ingestion of new episodes | Corpus is pinned by design (A5); refresh is a documented operation |
| Cross-encoder reranking | Measured: RRF + diversity cap is sufficient. Latency budget is better spent on generation |
| Fine-tuning | Retrieval quality, not model weights, is the bottleneck |
| Voice, mobile app, analytics dashboards | No contribution to the core job |
| Kubernetes / cloud deployment | "Deploy it locally" is the stated requirement; Compose is the reproducible unit |
| More than three LLM providers | The abstraction is the deliverable, not the provider count |

### 1.6 Risks and trade-offs

| Risk | Severity | Mitigation | Residual |
|---|---|---|---|
| **Hallucination** — fluent, cited-looking, unsupported | High | Corpus-only prompting; confidence threshold calibrated on a golden set; citation IDs validated against retrieved chunks; refusal path | A model can still misread a passage it was correctly given. Timestamped citations let the user check in seconds |
| **Weak retrieval** — right question, wrong passages | High | Hybrid lexical + semantic, RRF fusion, per-episode diversity cap, recall measured against a golden set, `inspect` CLI to separate retrieval faults from reasoning faults | Recall is bounded by the ingested subset |
| **Local model quality** — 7B models produce weaker prose than frontier models | Medium | Structured skills with validated output contracts rather than free-form prompting; section-wise essay generation; schema validation with repair-retry and deterministic fallback | Essays are good, not great, on a 7B model. Switching to a cloud provider is a config change |
| **Latency** — local generation is slow | Medium | SSE streaming with phase feedback so the user sees progress; retrieval is fast and shown first | A long answer still takes tens of seconds locally |
| **Unsafe artifact rendering** — model-generated HTML executing in the app origin | High | Server-side sanitisation + sandboxed iframe with no `allow-same-origin`; CSP inside the artifact document; documented allow/block list | Documented explicitly in `architecture.md` |
| **Data leakage** — transcripts or prompts sent to a third party | Medium | Default configuration is fully local; cloud providers are opt-in and clearly indicated in the UI | Using a cloud provider does send context off-machine, by definition |
| **Operational opacity** — evaluator cannot tell why something failed | Medium | Per-dependency health, structured JSON logs with request correlation, stable error codes with remediation text | — |

---

## 2. User flows

### 2.1 Ask a grounded question (primary)

1. User opens the app; provider and model are visible in the header.
2. Types a product/growth question.
3. UI shows retrieval progress, then streams the answer.
4. Answer renders with inline `[S1]`-style markers; source cards below show
   episode, guest, timestamp and a deep link.
5. User asks a follow-up; prior turns are in context, retrieval runs again for
   the new question.

**Acceptance:** the answer cites at least one retrieved passage; every citation
marker resolves to a real source card; the deep link opens the episode at the
quoted moment.

### 2.2 Insufficient evidence

1. User asks something outside the corpus.
2. The assistant states it has no supporting material, and says what it *did*
   find and why that was not enough.
3. It does not answer from general knowledge.

**Acceptance:** no fabricated citation; response is clearly distinguishable from
a normal answer in the UI.

### 2.3 Turn an answer into a Ship 30 essay

1. After a grounded answer, user requests an essay.
2. The skill produces ~1,250 words: hook, narrative progression, skimmable
   headings and bullets, selective bold, a specific takeaway.
3. Claims trace to the same grounded material; the essay opens in the artifact
   panel.

**Acceptance:** word count within tolerance; required structural elements
present; claims grounded. Verified programmatically, not by eye.

### 2.4 Generate and view an artifact

1. User asks for a checklist, one-pager or landing page.
2. Markdown or HTML/CSS is generated from the conversation.
3. It renders beside the chat in a sandboxed viewer, with a source/preview
   toggle, copy and download.

**Acceptance:** scripts and event handlers are stripped or neutralised; the
viewer cannot reach the parent page, cookies or storage; what is blocked is
documented and demonstrable.

### 2.5 Switch model provider (operator flow)

1. Operator sets `LLM_PROVIDER` and restarts.
2. The UI header reflects the new provider and model.
3. If it is unreachable, the UI says which dependency failed and what to do.

**Acceptance:** no application code changes; failure is explained, not silent.

---

## 3. Acceptance criteria

| Area | Criterion | Verified by |
|---|---|---|
| Ingestion | All upstream transcript formats parse; coverage regressions are loud | `test_ingestion.py`; parse-failure alert |
| Ingestion | Re-running does not duplicate or re-embed unchanged episodes | Idempotency test + live run |
| Retrieval | Relevant passage ranks above irrelevant on a live index | `test_db_retrieval.py` |
| Retrieval | Recall@6 ≥ 85% on the golden set | `app.evals.retrieval_eval` |
| Retrieval | Degrades to lexical when embeddings are unavailable, and reports it | Test with unreachable Ollama |
| Grounding | Every citation marker maps to a retrieved chunk | `test_citations.py`, `test_knowledge_qa.py` |
| Refusal | Out-of-corpus questions refused ≥ 90% | Golden set |
| Sessions | Two sessions never share context | `test_db_sessions.py`, `test_api_chat.py` (live) |
| Persistence | Conversations, sessions, timestamps, metadata in PostgreSQL | Schema tests |
| API | Every error uses one envelope with a stable code | `test_api_health.py` |
| Health | Reports per-dependency status without crashing when deps are down | `test_api_health.py` |
| Ship 30 | ~1,250 words with required structure | Checkpoint 3 |
| Artifacts | Script injection neutralised; viewer isolated | Checkpoint 3 sanitiser suite |
| Model toggle | Provider switch requires no code change | `/api/provider`, `/health/detail` |
| Deployment | Fresh clone runs by documented steps alone | Checkpoint 5 |

---

## 4. Implementation plan

Five checkpoints, ordered by dependency and risk retirement — highest-risk
unknowns first, while there is still time to change approach.

| # | Checkpoint | Why here | Status |
|---|---|---|---|
| 1 | **Knowledge spine** — schema, ingestion, hybrid retrieval, health, errors, logging, eval harness | Nothing downstream is trustworthy if retrieval is weak, and retrieval cannot be rescued later by better prompting | **Complete** |
| 2 | **Provider abstraction, agent runtime, grounded chat** — LLM interface, routing, RAG skill, sessions, streaming | Where a small local model breaks. Retiring that risk early leaves room to adapt | **Complete** |
| 3 | **Ship 30 and artifact skills** — structured essay generation, sanitiser, artifact persistence | Depends on a working grounded answer to build on | |
| 4 | **Frontend** — chat, sources, artifact viewer, states, accessibility | Built against a stable streaming API to avoid rework | |
| 5 | **Operations, documentation, final gate** — hardening, observability, docs, audit, fresh-evaluator test | Verification is worth most when there is a complete system to verify | |

### Checkpoint 1 outcome

Delivered: configuration layer, structured logging with request correlation,
error taxonomy, PostgreSQL schema and migration runner, transcript ingestion
(three upstream formats, pinned commit, idempotent, resumable embedding), hybrid
retrieval with RRF fusion and diversity capping, health endpoints, Docker
Compose, 77 automated tests, and the retrieval evaluation harness.

Notable findings, in full in
[`agent-transcripts/checkpoint-1-knowledge-spine.md`](../agent-transcripts/checkpoint-1-knowledge-spine.md):

- The upstream archive uses **three transcript formats**. Supporting only the
  common one silently dropped 10% of episodes — including Seth Godin, Teresa
  Torres and Ryan Hoover — behind log warnings. Coverage is now 303/303.
- The refusal threshold began as a guess (`0.25`), and the guess confidently
  answered a question about Kubernetes operators for quantum annealing
  hardware. Calibrated against the golden set, the measured optimum is `0.48`,
  which achieves 100% recall@6, 100% in-corpus answer rate and 100%
  out-of-corpus refusal rate (balanced accuracy 1.00) on the full 40-episode
  index. Full numbers in `docs/retrieval-eval-report.json`. This is why the
  eval harness shipped in checkpoint 1 rather than at the end -- it was needed
  to make the decision, not to decorate a report after the fact.

### Checkpoint 2 outcome

Delivered: a provider abstraction (`app/llm/`) for Ollama, Anthropic and
OpenAI with a documented fallback policy; a shared skill contract
(`app/agent/contracts.py`) run through an in-process runtime, after evaluating
and rejecting the Claude Agent SDK for this system; a hybrid intent router;
the grounded Q&A skill with citation extraction, validation and repair;
sessions/messages persistence with proven per-session isolation; SSE streaming
with phase events; and an extension of the eval harness (`grounding_eval.py`)
that measures M1 against a live model instead of leaving it as a target.
56 new automated tests (133 pure/db total, plus 4 live end-to-end tests
against real PostgreSQL and Ollama).

Notable findings, in full in
[`agent-transcripts/checkpoint-2-agent-runtime.md`](../agent-transcripts/checkpoint-2-agent-runtime.md):

- **The Claude Agent SDK was evaluated, not assumed.** Its published package
  (`claude-agent-sdk==0.2.152`) was downloaded and inspected directly: its only
  transport spawns the Claude Code CLI as a subprocess, its wheels vendor that
  CLI binary (94 MB for win_amd64, against a 0.33 MB sdist), and it is
  Claude-only — none of which fit a system whose default path is Ollama and
  whose only capability surface is a fixed retrieve → generate → validate
  pipeline. A ~60-line direct runtime was built instead, behind an interface
  that could still host a Claude-specific runtime later without touching any
  skill or API code.
- **M1 measured 75% on the first live run**, against a 90% target — the
  dominant failure was not a hallucinated citation but an uncited "in summary"
  paragraph tacked onto an otherwise well-cited answer. The citation-retry gate
  was only triggered by a *total absence* of citations, so a properly-cited
  answer with one uncited trailing paragraph passed straight through
  unretried. Widening the retry trigger to "not fully grounded" (while keeping
  the *refusal* trigger at the stricter "no valid citation at all", so a
  partially-uncited-but-real answer is still shown rather than discarded)
  raised the measured rate to **100% (12/12)** on the same golden set and
  model, confirmed by re-running `app.evals.grounding_eval` end to end. Full
  numbers in `docs/grounding-eval-report.json`.
