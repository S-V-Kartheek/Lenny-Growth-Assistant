# Checkpoint 2 — Provider abstraction, agent runtime, grounded chat

**Scope:** LLM provider abstraction (Ollama/Anthropic/OpenAI), the Claude
Agent SDK evaluation, a shared skill contract with an in-process runtime,
hybrid intent routing, the grounded Q&A skill with citation validation and
repair, sessions/messages persistence, SSE streaming with phase events, and
extending the eval harness to measure M1 against a live model.

**Why this was next:** checkpoint 1 proved retrieval works; this checkpoint is
where a small local model either honours the grounding contract or does not.
Retiring that risk here — rather than after building the essay and artifact
skills on top of it — leaves room to change the prompting or validation
strategy if a 7B model turned out not to cooperate.

---

## Decision: Claude Agent SDK vs. a direct runtime

Investigated before writing any agent code, per the assignment. Findings from
the actual published `claude-agent-sdk==0.2.152` package (sdist + wheels
pulled from PyPI and inspected directly, not assumed from documentation):

- Its only transport (`_internal/transport/subprocess_cli.py`) spawns the
  **Claude Code CLI** as a subprocess and speaks a JSON control protocol over
  stdin/stdout. It is not an HTTP client for the Messages API — it requires
  the `claude` binary to exist (bundled in the wheel, or found on `PATH`), not
  merely an `ANTHROPIC_API_KEY`.
- The CLI binary is vendored per-platform: the win_amd64 wheel is **94 MB**
  against a **0.33 MB** sdist. That is the weight it would add to every image
  that imports it.
- It is Claude-only by construction. The demo's default and primary path is
  Ollama (assumption A7), so this system needs an Ollama-driving runtime
  regardless of what happens on the Anthropic path — the SDK could not be
  "the" runtime even if adopted.
- Its value proposition is agentic tool use (filesystem, bash, MCP, permission
  prompts, multi-turn planning). This system has exactly one capability
  surface — retrieval over a pinned corpus — and deliberately wants a fixed
  retrieve → generate → validate pipeline, not a model choosing tools. Nothing
  here needs what the SDK is for.

**Decision:** a direct, in-process runtime (`app/agent/runtime.py`,
`DirectRuntime`, ~60 lines) that calls the provider abstraction directly.
Skills are defined once against a shared contract (`app/agent/contracts.py`)
and `select_runtime()` is the single seam where a different runtime could be
substituted per-provider later — the interface exists so this decision is
revisable without touching skills or the API. The trade-off is written down in
the runtime module's docstring, not just here.

`claude-agent-sdk` was **not installed** — nothing in `pyproject.toml`
changed to add it, so there was nothing to ask permission for.

---

## Provider abstraction

`app/llm/base.py` defines one interface (`health`, `stream`, `complete` built
on `stream`) implemented three times: `ollama.py`, `anthropic.py`,
`openai.py`. Deliberately built on raw `httpx` rather than the vendor SDKs —
recorded as a stated trade-off in `app/llm/transport.py`'s docstring: the
official SDKs would give free retries and forward-compatibility, at the cost
of three different retry/error/streaming shapes to normalise instead of one.
Anthropic and OpenAI were both testable end-to-end (request shape, SSE
parsing, error translation) without an API key, which is the only way those
paths get exercised in this engagement.

Fallback policy (`app/llm/registry.py`, `LLMGateway`) is a documented, tested
contract, not "it falls back": only on `ProviderUnavailable`/`ProviderTimeout`
(never on a 4xx, which is a configuration bug to fix, not retry), and only
**before the first token is emitted** — switching mid-stream would splice two
models' prose together. `test_llm.py` proves both halves of that rule with a
scripted fake provider.

Provider and model are visible in every API response: `/api/provider`,
`/health/detail`'s `llm` block, and every persisted message row
(`provider`/`model` columns already existed in the checkpoint-1 schema).

---

## Failures and corrections

### 1. Ruff caught a real off-by-nothing: word-count threshold too tight in a test, not the code

**Symptom.** `test_each_bullet_is_checked_independently` failed:
`count_uncited_paragraphs` returned 0, expected 1.

**Cause.** Not a bug in the citation validator — the test's second bullet was
11 words, one short of `MIN_CLAIM_WORDS = 12`. The threshold itself was chosen
deliberately (to exclude short transitional lines like "Here's what I found:")
and was correct; the test fixture just happened to land exactly on the wrong
side of it.

**Correction.** Lengthened the test bullet to be unambiguously a substantive
claim, rather than lowering the threshold to fit a borderline example — the
same principle as checkpoint 1's refusal threshold: tune the code to a
measured/deliberate bar, not to make one example pass.

### 2. A fallback test asserted a scenario it did not actually construct

**Symptom.** `test_gateway_does_not_fall_back_once_a_token_was_emitted` failed
with "DID NOT RAISE" — the scripted failure never fired.

**Cause.** The fake provider's `raise_after` index was checked inside
`enumerate(self._chunks)`, but the test supplied only one chunk, so index 1
(where the failure was scripted) was never reached — the loop ended after
index 0 with no error raised at all.

**Correction.** Gave the fake provider two chunks so the scripted failure
(after the first token is yielded) is actually reachable. Caught immediately
by the test itself failing loudly rather than silently passing — this is
exactly the kind of test bug that a green suite can hide.

### 3. Router "ambiguous" test wasn't actually ambiguous

**Symptom.** `test_ambiguous_phrasing_is_not_forced_through_rules` expected
`route_by_rules` to return `None` for "What do you think about this in
general?" — it returned a confident `knowledge_qa` decision instead.

**Cause.** The phrase matches the `\bwhat (?:do|does|is|...)\b` rule cleanly.
It was never actually ambiguous between intents; it was unambiguous
`knowledge_qa` all along, which is the correct routing behaviour. The test's
premise was wrong, not the router.

**Correction.** Rewrote the test around a phrase that genuinely matches no
rule at all ("So yeah, that is interesting stuff overall."), which is the real
case the None-return path exists for — a request the rules have no opinion
on, which is when the LLM fallback earns its keep.

### 4. M1 measured 75%, not the target 90% -- and the fix was in the retry gate, not the prompt

**Symptom.** The first live run of `app.evals.grounding_eval` against the full
40-episode index and `qwen2.5:7b-instruct` reported **75% (9/12)**. All 12
in-corpus questions were answered (0 refused, so this was not a confidence
threshold issue) but 3 were not fully grounded.

**Investigation.** Inspected the three failures directly (`invalid_markers`
was empty on all three -- so citations were never hallucinated) and found a
single consistent pattern: 5-6 well-cited paragraphs, followed by one
uncited concluding paragraph ("In summary, the most important thing is...",
"Beyond that, teams should also focus on..."). The model was following the
citation contract for the substance of its answer and then adding an
uncited wrap-up.

**Cause.** The retry gate only fired on `not report.has_valid_citation` --
literally zero resolvable citations anywhere in the answer. An answer with
five valid citations and one uncited trailing paragraph sailed straight past
that gate, because it manifestly had a valid citation; `is_grounded`
(which also requires zero uncited paragraphs) was never consulted before the
answer was returned.

**Correction.** Widened the retry trigger to `not report.is_grounded`, so a
partially-uncited answer gets the same one corrective pass a fully-uncited one
already got. The *refusal* trigger stayed at the stricter
`has_valid_citation` check -- a retry that still leaves one uncited paragraph
but keeps its real citations is more useful shown than discarded, and refusing
it would trade a grounding nitpick for a much larger drop in answer rate.
Also fixed a related latent bug the investigation surfaced:
`GroundingReport.is_grounded` required `invalid_markers` to be empty, but
`invalid_markers` records markers that *were* stripped during repair -- so a
successfully repaired answer (hallucinated marker removed, real citations
intact) was being permanently marked ungrounded even after the repair fixed
it. `is_grounded` now evaluates only the displayed text (valid citation
present, no uncited paragraph); `repaired` remains in the report as
observability, not as a grounding gate.

**Result, re-measured on the same golden set and model:** **100% (12/12)**.
Also re-confirmed M2/M3 were unaffected by the change (`retrieval_eval.py`:
recall@6 100%, balanced accuracy 1.00 at threshold 0.48 -- this change only
touches post-generation validation, not retrieval).

### 5. Test expectations didn't match the actual prompt wording

**Symptom.** `test_build_grounded_messages_reports_the_true_passage_count`
asserted the substring `"S1-S6"`, but the generated instruction text was
`"[S1]-[S6]"` (with brackets, matching the citation marker format used
everywhere else).

**Correction.** Fixed the assertion to match the real, correct format rather
than changing the prompt — the bracketed form is what the citation validator
also expects, so consistency was on the code's side.

---

## Verification performed

| Check | Result |
|---|---|
| Pure + db test suite (checkpoint 1 + 2) | 77 → **133 passed** (56 new: 112 pure, 21 db) |
| Live db+llm suite (real Postgres + real Ollama, `qwen2.5:7b-instruct`) | **4 passed** in ~22s: provider info, a full grounded SSE turn, two-sessions-do-not-share-context over the real API, 404 on an unowned session |
| Ruff | clean (`ruff check app tests`) — two auto-fixed (dict comprehension, import order), one line-length fixed by hand |
| M1 (grounded answer rate), live model | see below |
| Routing golden fixtures | all 9 `routing:` fixtures in `golden_questions.yaml` now consumed and passing |

### M1 — grounded answer rate, measured

`python -m app.evals.grounding_eval` runs the real `KnowledgeQASkill` against
the live 40-episode index and `qwen2.5:7b-instruct`, for every in-corpus golden
question, and checks each answer's `GroundingReport.is_grounded` — at least
one citation that resolves to a retrieved chunk, and no uncited substantive
paragraph.

```
provider: ollama / qwen2.5:7b-instruct
M1 grounded answer rate : 100% (12/12 answered questions)
  12 in-corpus questions total, 0 refused (excluded from M1, see M2)
```

First run measured 75% (9/12) before the retry-gate correction (failure 4
above); re-run after the fix, same golden set and model, measured 100%
(12/12). Full per-question numbers in `docs/grounding-eval-report.json`.
Re-running `app.evals.retrieval_eval` confirmed M2/M3 were unaffected: recall@6
100% (11/11), balanced accuracy 1.00 at threshold 0.48.

---

## What was deliberately not built here

- **No Claude Agent SDK.** See the decision section above.
- **No separate runtime per cloud provider today.** `select_runtime()` returns
  `DirectRuntime` for all three providers; the seam exists for a future
  tool-using Claude runtime, but nothing in this checkpoint needs one.
- **No query-rewriting model call for follow-ups.** `build_retrieval_query`
  is a regex heuristic (anaphora detection + previous-turn concatenation), not
  a model call — on a local 7B, a rewrite costs more latency than the
  retrieval it feeds, for a class of question the heuristic already gets
  right often enough.
- **Ship 30 and artifact skills are placeholders**, not stubs that silently
  misroute: `PlannedSkill` is a real, routable, testable skill that says
  plainly which checkpoint implements it, so the routing evaluation is honest
  about what each intent currently does.
