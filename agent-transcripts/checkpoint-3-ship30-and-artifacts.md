# Checkpoint 3 — Ship 30 essay, artifact generation, sanitisation

**Scope:** the Ship 30 for 30 essay skill (structured outline → section-wise
generation, not a single free-form prompt), Markdown/HTML artifact generation,
a from-scratch HTML sanitiser with an independent verification pass, artifact
persistence on the existing `artifacts` table, artifact API endpoints
including a sandboxed-iframe document view, and extending the eval harness to
measure the essay's structural and grounding contract against a live model.

**Why this was next:** checkpoint 2 proved the grounded Q&A skill and the
skill contract hold up against a live 7B model. This checkpoint builds two more
skills on that same contract and, for the first time, produces content this
system does not fully control the rendering of — HTML that a browser will
execute. That made the sanitiser the highest-risk single component in the
checkpoint, and it was built and adversarially tested first, before either
skill that would call it.

---

## Decision: nh3 over a hand-rolled sanitiser

Recorded here because it was an explicit choice with a real alternative, not a
default. Considered:

1. **Hand-rolled, `html.parser`-based.** No new dependency, fully auditable
   (~200 lines). Rejected as the *primary* layer: `html.parser` is not an
   HTML5-spec tokenizer, so it does not reliably reproduce how a browser
   actually parses malformed or foreign-content markup (`<svg><style><img
   src=x onerror=...></style></svg>`-class mutation XSS payloads exist
   specifically because a sanitiser's parse of a document differs from the
   browser's).
2. **nh3** (Rust bindings to `ammonia`, built on `html5ever` — the same
   tokenizer Servo/Firefox use). Chosen as the primary layer for exactly the
   property (1) lacks: it parses the way a browser parses.
3. **Both.** nh3 sanitises; a second, independent `html.parser`-based pass
   (`verify_sanitised`) re-checks the *output* against the same allow-list
   policy and the artifact is rejected outright (`ArtifactRejected`, not
   served) if the two disagree. This is what shipped — asked and approved by
   the user before installing anything (`nh3>=0.2.18`, resolved to `0.3.7`;
   no other dependency changed).

The independent verifier is not decorative: it is the thing that would have
caught a hypothetical nh3 bypass, and the tests prove that by handing it
strings that never came out of `sanitise_html` and confirming it flags them
(`test_verifier_catches_a_policy_violation_it_is_handed`), plus a
`monkeypatch`-driven test that makes `nh3.clean` itself return something
forbidden and confirms the artifact is rejected rather than served
(`test_artifact_is_rejected_rather_than_served_when_layers_disagree`).

## Decision: isolation contract ships now, not in checkpoint 4

The PRD (2.4, 3) lists "sandboxed iframe, no `allow-same-origin`, CSP inside
the document" as a checkpoint-3 acceptance criterion, and the user confirmed
(via `AskUserQuestion`) that the document-serving endpoint should ship now
rather than wait for the frontend. `GET /api/artifacts/{id}/document` serves
the sanitised HTML with `Content-Security-Policy: default-src 'none'` (no
`script-src` at all — nothing grants script, so nothing can run even past a
sanitiser bypass), `X-Frame-Options: SAMEORIGIN`, and the exact `sandbox`
string an embedding iframe must use (`ARTIFACT_SANDBOX`,
`allow-popups allow-popups-to-escape-sandbox` — deliberately excluding
`allow-scripts` and `allow-same-origin`). `test_sandbox_contract_never_grants_
same_origin_or_scripts` asserts those two strings are absent from both the
sandbox attribute and the CSP, because that is the single line that would undo
every other layer if it regressed.

---

## What was built

- **`app/agent/sanitize.py`** — the allow-list policy (tags, attributes, URL
  schemes, CSS properties), `sanitise_html`/`sanitise_markdown`, the
  independent verifier, and `build_artifact_document` (the model never
  authors `<head>`, so it cannot inject a `<meta http-equiv>` or `<base>` no
  matter what it emits).
- **`app/agent/essay.py`** — the Ship 30 output contract as data, not prose:
  word-count band, structural requirements, `evaluate_structure()` (the one
  place "correct" is defined, used by the skill, the eval harness and the
  tests), outline schema parsing with a stated failure reason for the
  repair-retry, a deterministic fallback outline, and two deterministic
  repairs (un-bolding past a cap, trimming whole paragraphs never sentences).
- **`app/agent/skills/ship30_essay.py`** — retrieve → confidence gate → plan
  (outline, schema-validated, one repair, deterministic fallback) → write
  (hook / N sections / takeaway, each validated for citations as it lands and
  retried once if not grounded) → assemble (headings written by code) →
  repair → verify.
- **`app/agent/skills/artifact.py`** — format chosen from explicit cues before
  generation (ties go to Markdown, the lower-risk path); citations requested
  and validated when retrieval clears the confidence threshold, otherwise the
  artifact is still built from the conversation and honestly labelled
  `grounded: false` rather than refused (a checklist is a work product, not a
  transcript claim).
- **`app/agent/sources.py`** — `build_retrieval_query` and `sources_payload`,
  extracted from the Q&A skill so both new skills share the exact same
  passage-numbering logic instead of each growing a slightly different copy.
- **`app/services/artifacts.py` + `app/api/artifacts.py`** — persistence with
  an atomically-computed per-session version, and three endpoints (list, get,
  document). `ArtifactRow` has no `raw_content` field at all — reaching the
  unsanitised text requires calling `get_raw_for_debugging()` by name, so a
  careless "just serialise everything" change cannot leak it.
- **`app/evals/essay_eval.py`** — runs the real skill against the live index
  and model, applies `evaluate_structure` and the citation validator, and
  reports a structure pass rate and a grounded rate (the essay analogue of
  M1) separately, because a badly-shaped essay and an ungrounded one are not
  the same severity of failure.
- Golden routing fixtures extended with an artifact-format dimension
  (`kind: html` / `kind: markdown`), since routing now has two levels once
  artifacts are real: which skill, and which format that skill produces.
- `PlannedSkill` and `planned.py` deleted; the orchestrator registers the two
  real skills.

---

## Failures and corrections

### 1. `nh3` rejected the exact `rel` attribute it adds itself

**Symptom.** Every test asserting on a sanitised `<a href>` failed:
`ValueError: "rel" attribute is not allowed for tag "a" when link_rel is set`.

**Cause.** `link_rel="noopener noreferrer nofollow"` makes nh3 *write*
`rel` unconditionally; nh3 refuses to also see it in the caller's
`attributes` allow-list, on the reasonable theory that the two would
conflict. The independent verifier's allow-list didn't know `rel` was ever
going to appear, so it flagged nh3's own hardening as a violation on every
link.

**Correction.** Split the policy into two maps: `ALLOWED_ATTRIBUTES` (what the
*model* may request) and `INJECTED_ATTRIBUTES` (what nh3 adds regardless,
`{"a": {"rel"}}`), and the verifier checks the union. A model-supplied `rel`
is still not in `ALLOWED_ATTRIBUTES` and is still stripped — only the one nh3
itself writes is recognised.

### 2. Title extraction round-tripped through `nh3.clean_text`, which escapes spaces

**Symptom.** `extract_title("<title>Launch Checklist</title>...")` returned
`"Launch&#32;Checklist"` instead of `"Launch Checklist"`.

**Cause.** `nh3.clean_text` is built for embedding text inside an HTML
attribute, where a literal space is fine but the function escapes it anyway
as a defensive default; using it to produce a plain-text title carried that
escaping into a string nothing was going to re-parse as HTML.

**Correction.** Switched to stripping tags with a regex and decoding entities
with the stdlib `html.unescape`, then re-collapsing whitespace — appropriate
for a value that becomes a JSON string and a `<title>` text node (itself
escaped separately by `build_artifact_document`), not an attribute value.

### 3. A heading-cleanup regex order bug: `**1. First point**` → `**First point`

**Symptom.** `test_outline_headings_are_cleaned_of_numbering_and_markup`
failed: stripping `**` before stripping the leading `1. ` left `**First
point` — the `**` immediately after the numbering was never at the *start*
of the string when the emphasis-strip ran.

**Correction.** Reordered to numbering-then-emphasis, and added a second
emphasis-strip pass after, to also catch `**1. Heading**` (numbering nested
inside the emphasis) without special-casing it. Documented in the function
docstring so the ordering dependency doesn't get "simplified" back later.

### 4. Two skill-level test fixtures asserted on essay shapes the code was never asked to produce

**Symptom.** `test_produces_a_structurally_valid_essay` failed on a scripted
essay that came in at 990 words with zero bullets and zero bold spans — both
requirements the outline and prompts explicitly ask for, but the fixture's
scripted model answers didn't include any.

**Correction.** Rewrote `full_run_answers()` to script a hook, four sections
(one carrying the outline's bulleted list requirement), and a takeaway shaped
like plausible model output rather than minimal stubs — the same principle as
checkpoint 2's "tune the fixture to be a real example, not to pass by
construction."

### 5. A hallucinated marker stripped mid-section was invisible in the final report

**Symptom.** Writing `test_an_invented_marker_is_stripped_from_the_finished_
essay`, `[S9]` was correctly removed from the displayed essay but
`result.grounding["invalid_markers"]` was empty.

**Cause.** `validate_and_repair` runs once per section during generation (so
a hallucinated marker never reaches assembly) and once more on the assembled
essay for the final citation report. By the time the second pass runs, the
first pass has already deleted the marker — so the final report, which is
what gets persisted and shown, said nothing invalid was ever produced, even
though the model did hallucinate one and it was silently fixed.

**Correction.** `_write()` now collects `invalid_markers` from every
per-section validation pass (including retries) and the skill unions that set
into the final `grounding` payload. "The model hallucinated a citation and it
was repaired" is exactly the kind of thing an operator debugging output
quality needs to see, and a spotless-looking final report was hiding it.

### 6. Two real product-logic bugs, found by a scripted worst-case format cue

**Symptom.** `test_html_is_chosen_when_asked_for["Give me a styled one-pager
in HTML and CSS"]` failed: the request routed to `markdown`.

**Cause.** `_MARKDOWN_CUES` originally included `"one-pager"`, `"doc"`,
`"template"` alongside genuine format words like `"markdown"` — but those are
document-*type* words, not format words; a one-pager can legitimately be
either format. Counting them as Markdown votes meant an explicit HTML request
that also happened to name a document type ("an HTML **one-pager**") tied
1-to-1 and fell back to Markdown, the opposite of what was asked.

**Correction.** Narrowed `_MARKDOWN_CUES` to actual format words
(`markdown`/`md`) and moved document-type words out of the cue system
entirely — they carry no format signal. An explicit `"markdown"` still wins
over an HTML cue in the same sentence (the lower-risk path stays the
tie-breaker), but "HTML one-pager" now correctly selects `html`.

A second bug surfaced alongside it: `_unwrap()` decided whether a
fenced-block response was "the whole artifact" by the fence's share of total
characters (`> 60%`), which failed on `test_a_fenced_response_is_unwrapped`
because a one-line artifact inside a fence with a two-line wrapper ("Here you
go!\n```html\n...\n```") is obviously the whole artifact, but wasn't a 60%
majority of characters. Fixed by changing the discriminator: unwrap when
everything *outside* the single fence is short (≤200 chars) and heading-free,
rather than by the fence's proportion of the text — this also fixes the
inverse case (a Markdown guide containing one long, legitimate `bash` code
sample, which must **not** be reduced to that sample), verified by
`test_a_markdown_doc_keeps_its_own_code_sample`.

### 7. Live verification found a real bug the test suite could not: format instructions diluted retrieval below the refusal threshold

**Symptom.** End-to-end against the real API: a grounded follow-up question
("How do I improve activation for a B2B SaaS product?") scored retrieval
confidence 0.4976 and answered normally. The very next turn in the same
session — "Turn that into an HTML landing page with CSS." — should have
built on the same evidence, but instead the artifact came back
`grounding.reason: "conversation_only"`, with zero sources and zero citations.

**Investigation.** Reproduced directly against the live retriever, isolating
the query construction from everything else:

```
previous question alone                              conf=0.4976  answers=True
previous + "Turn that into an HTML landing page with CSS."  conf=0.4177  answers=False
```

The anaphora heuristic (`build_retrieval_query`, checkpoint 2) correctly
detected "that" as a follow-up reference and concatenated the previous
question with the new message — exactly as designed for a real follow-up
question. But an artifact/essay request is not a follow-up *question*; it is
almost entirely an instruction about *form*. Words like "html", "css",
"landing page" are strong, real lexical signals in the corpus (there are
transcript passages that discuss actual web/CSS engineering topics), so
appending them pulled the hybrid retriever toward genuinely unrelated
passages and dropped the fused confidence 0.08 below where the same question
alone had scored — crossing the 0.48 refusal threshold. Asking for a landing
page about a topic made the topic itself unanswerable.

**First correction attempt was incomplete.** Added `strip_format_terms()` to
remove format/command vocabulary before concatenation. That fixed the
confidence (0.4976 → 0.4740) but not enough — still below 0.48 — because the
*residue* left after stripping ("that into an with") was still being
appended as noise, even though it carried no subject.

**Second correction.** Count *content* words in the stripped residue (after
dropping stopwords: "that", "into", "an", "with", …), and when fewer than 2
remain, use the previous question alone rather than concatenating debris onto
it. Re-measured directly against the live index:

```
"Turn that into an HTML landing page with CSS."  -> "How do I improve activation..."  conf=0.4976  answers=True
"Turn that into a Ship 30 essay."                -> "How do I improve activation..."  conf=0.4976  answers=True
"Turn that into a one-pager about onboarding."   -> "How do I improve activation... about onboarding"  conf=0.5009  answers=True
```

A follow-up that *does* add a real subject ("about onboarding") still keeps
it — only pure format instructions fall back to the bare previous question.
Re-ran the exact two-turn conversation against the live API after the fix:
the same artifact request now returns `grounding.grounded: true`,
`cited_indices: [1, 4, 5, 6]`, 6 real sources, and a landing page whose
`<h2>` sections trace to the same transcript passages the first turn's answer
did — inspected directly, not just asserted in a test.

Five regression tests added (`tests/test_sources.py`), including the exact
failing phrasing as a named case with the measured numbers in its docstring,
so a future change that reintroduces this cannot pass silently.

### 8. Live verification found a second real bug: the hook's generation target exceeded its own acceptance cap

**Symptom.** A full, real 7-call essay generation against qwen2.5:7b-
instruct (topic: "How do I improve activation for a B2B SaaS product?")
produced a essay that was well-formed by every other measure — 1,097 words
(inside the 1,000-1,500 band), 5 headings, 6 bullets, 3 bold spans, 22
citations, zero invalid markers, a real title and a real takeaway — but
`structure_ok: false`, with the single issue `"hook is 91 words (max 90)"`.

**Investigation.** Read the actual hook text rather than just the number: a
coherent, substantive 91-word opening paragraph, not a wandering one. Traced
the numbers that produced the failure: the prompt asks the model for
`HOOK_WORDS = 140` words ("Length: about 140 words"), while
`evaluate_structure` only accepted up to `MAX_HOOK_WORDS = 90` — the
generation target was 50 words *past* the validator's cap. A hook that landed
close to what it was actually asked to write was structurally guaranteed to
fail its own contract on a non-trivial fraction of runs; this was the first
live essay run, and it hit the mismatch on the first try.

**Correction.** Two changes, not one, because either alone would have left
the other broken: `HOOK_WORDS` (the generation target) lowered to `100`,
comfortably under the cap; `MAX_HOOK_WORDS` (the acceptance threshold) raised
from `90` to `120`, both to match the corrected target with headroom and
because 90 was never a PRD requirement — the PRD asks for *a* hook, not a
specific word count — and turned out tighter than a good hook naturally
lands. Re-evaluated the *same* real essay output against the corrected
contract without regenerating it: `has_hook: true`, `structure_ok: true`,
zero issues. `MAX_BOLD`/bullet/section thresholds were left alone — nothing
in the live run suggested they were miscalibrated the way the hook pair was.

---

## Verification performed

| Check | Result |
|---|---|
| Sanitiser adversarial suite (`test_sanitize.py`) | **129 tests** — script tags (12 variants), event-handler attributes (10), unsafe URL schemes incl. entity/whitespace/NUL obfuscation (13 payloads × 2 attribute contexts), embedding/navigation elements (12), form elements (6), CSS-based exfiltration incl. `expression()`/`-moz-binding`/obfuscated `url()` (10), mutation-XSS/foreign-content payloads (9), the independent verifier catching violations it never sanitised itself, a forced-bypass rejection test, the served document's CSP/sandbox contract, and Markdown's separate HTML-escaping path |
| Full pure + db suite | **385 passed**, 0 failed, 0 skipped-unexpectedly |
| Ruff | clean (`ruff check app tests`) after 6 real fixes (see Failures 1-3, 6, 8) |
| Live artifact generation (real Postgres + real Ollama) | A real 2-turn session: grounded Q&A answer (confidence 0.4976, 6 citations), then "Turn that into an HTML landing page with CSS." in the same session — initially broke grounding (Failure 7), fixed and re-verified: `grounded: true`, 6 sources, 4 cited, served document inspected directly (`curl`) — correct CSP header, no `<script>`/`onerror`/`onclick`/`javascript:` anywhere in ~3.6 KB of served HTML, correct `<h1>`/`<h2>` structure |
| Live essay generation (real Postgres + real Ollama) | One full 7-call run (~18 minutes on CPU), topic "How do I improve activation for a B2B SaaS product?": 1,097 words, 5 headings, 6 bullets, 3 bold spans, 22 citations, 0 invalid markers, title/hook/takeaway present — found and fixed Failure 8, re-evaluated the same real output against the corrected contract: `structure_ok: true` |
| Artifact persistence + API (real Postgres) | `test_db_artifacts.py`, 16 tests: atomic per-session versioning, session-scoped listing and 404-on-wrong-session, oversized-content truncation, cascade delete, **`raw_content` absent from every API response verified by substring search on the raw response body**, sandbox string never contains `allow-same-origin`/`allow-scripts`, Markdown artifacts correctly have no `document_url` and the `/document` route 404s for them |
| Routing, with the new format dimension | `test_router.py`: golden routing fixtures now include `kind: html`/`kind: markdown` cases distinguishing document-*type* words from format words; `test_every_intent_has_a_real_skill_registered` asserts the two placeholder skills are gone |

---

## What was deliberately not built here

- **A frontend viewer.** The document-serving endpoint and its isolation
  headers ship now (per the PRD's checkpoint-3 acceptance criterion and the
  user's explicit choice), but the sandboxed `<iframe>` itself, the
  source/preview toggle and copy/download affordances are checkpoint 4.
- **Regeneration / editing of an existing artifact.** `version` is already
  atomic per session so a future "regenerate" action has somewhere to write a
  new row, but no endpoint triggers one yet — nothing in this checkpoint's
  scope needed it.
- **SVG or MathML support in artifacts.** Excluded entirely from the
  sanitiser's tag allow-list rather than sanitised, because they are the
  documented source of most real-world mutation-XSS research and a checklist
  or one-pager has no real need for them.
- **A model call to resolve which retrieval query to use.** The format-term
  stripping added in Failure 7 is, like checkpoint 2's follow-up heuristic, a
  regex-based heuristic rather than a model call, for the same latency
  argument recorded there.
