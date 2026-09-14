"""The Ship 30 essay skill's orchestration, driven by a scripted model.

The essay's *shape* is tested in `test_essay.py`. This file tests the decisions
the skill makes around generation: when it refuses, how it recovers from an
unusable outline, whether a section that ignores the citation contract is
retried, and whether the repairs fire on the right conditions -- all of which
are only observable by scripting the model's answers.
"""

from __future__ import annotations

from app.agent.contracts import Intent, SkillContext
from app.agent.essay import MAX_BOLD, TAKEAWAY_HEADING, count_words
from app.agent.skills.ship30_essay import Ship30EssaySkill
from app.config import Settings
from app.retrieval.retriever import RetrievalResult
from tests.conftest import make_chunk as _chunk
from tests.test_knowledge_qa import FakeRetriever, ScriptedGateway

GOOD_OUTLINE = (
    '{"title": "Activation is an onboarding problem", '
    '"hook": "open with the drop-off", '
    '"sections": [{"heading": "The real constraint", "angle": "a"}, '
    '{"heading": "What operators did", "angle": "b"}, '
    '{"heading": "Where teams go wrong", "angle": "c"}, '
    '{"heading": "Your first move", "angle": "d"}], '
    '"takeaway": "instrument the first session"}'
)


def section_text(words: int = 240, marker: str = "[S1]") -> str:
    return " ".join(["insight"] * words) + f" {marker}"


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def _result(confidence: float, n_chunks: int = 3) -> RetrievalResult:
    chunks = [_chunk(i, f"passage {i} about activation") for i in range(1, n_chunks + 1)]
    return RetrievalResult(chunks=chunks, method="hybrid", confidence=confidence, query="q")


def _skill(answers: list[str], confidence: float = 0.8) -> tuple[Ship30EssaySkill, ScriptedGateway]:
    gateway = ScriptedGateway(answers)
    skill = Ship30EssaySkill(_settings(), gateway, FakeRetriever(_result(confidence)))
    return skill, gateway


async def _run(skill: Ship30EssaySkill, message: str = "Write a Ship 30 essay on activation"):
    events = [e async for e in skill.run(SkillContext(message=message))]
    result = next(e.result for e in events if e.kind == "result")
    return events, result


BULLETED_SECTION = (
    "The operators who fixed this did three things [S1].\n\n"
    "- **Instrument the first session** before changing anything [S1]\n"
    "- Watch ten real users complete it end to end [S2]\n"
    "- Cut the step with the largest drop-off, not the loudest complaint [S1]\n\n"
)


def full_run_answers(section_words: int = 240) -> list[str]:
    """Outline, hook, four sections, takeaway -- the happy-path call sequence.

    Shaped like plausible model output, not like minimal stubs: a hook of ~100
    words, one section carrying the bullets the outline asked for, and a
    takeaway of ~140 -- so the fixture lands inside the same word band a real
    run has to reach, and the structure assertions mean something.
    """
    bulleted = BULLETED_SECTION + section_text(max(0, section_words - 40))
    return [
        GOOD_OUTLINE,
        section_text(100, "[S1]"),
        section_text(section_words),
        bulleted,
        section_text(section_words),
        section_text(section_words),
        section_text(140, "[S2]"),
    ]


# ------------------------------------------------------------------ refusal --


async def test_refuses_below_the_confidence_threshold_without_generating() -> None:
    """1,250 fluent words off weak evidence is the most expensive kind of
    ungrounded output this system can produce."""
    skill, gateway = _skill(["should never be used"], confidence=0.1)
    _, result = await _run(skill)

    assert result.refused
    assert result.grounding["reason"] == "insufficient_evidence"
    assert gateway.calls == []


async def test_refuses_when_the_finished_essay_has_no_valid_citation() -> None:
    answers = [GOOD_OUTLINE] + ["Words with no citation at all."] * 12
    skill, _ = _skill(answers)
    _, result = await _run(skill)

    assert result.refused
    assert result.grounding["reason"] == "no_valid_citation"


# ------------------------------------------------------------- happy path ---


async def test_produces_a_structurally_valid_essay() -> None:
    skill, _ = _skill(full_run_answers())
    _, result = await _run(skill)

    assert not result.refused
    assert result.intent is Intent.SHIP30_ESSAY
    structure = result.grounding["structure"]
    assert structure["has_title"] and structure["has_takeaway"]
    assert structure["within_word_target"], structure["issues"]
    assert result.content.startswith("# Activation is an onboarding problem")
    assert f"## {TAKEAWAY_HEADING}" in result.content


async def test_the_outline_is_streamed_so_the_ui_can_show_progress() -> None:
    skill, _ = _skill(full_run_answers())
    events, _ = await _run(skill)
    outline_event = next(e for e in events if e.kind == "outline")
    assert outline_event.data["title"] == "Activation is an onboarding problem"
    assert len(outline_event.data["sections"]) == 4


async def test_headings_come_from_the_outline_not_from_the_model() -> None:
    """The model is told not to write headings; the skill writes them, so a
    model that ignores the instruction cannot produce a duplicate."""
    answers = full_run_answers()
    answers[2] = f"## The real constraint\n\n{section_text()}"
    skill, _ = _skill(answers)
    _, result = await _run(skill)
    assert result.content.count("## The real constraint") == 1


async def test_every_section_sees_the_same_passages() -> None:
    """An essay whose sections were grounded in different evidence is not one
    grounded essay -- it is five loosely related ones."""
    skill, gateway = _skill(full_run_answers())
    await _run(skill)
    passage_blocks = [
        m.content for call in gateway.calls for m in call if "Transcript passages" in m.content
    ]
    assert len({b.split("---")[0] for b in passage_blocks}) == 1


# --------------------------------------------------------- outline recovery --


async def test_an_unparseable_outline_is_retried_once() -> None:
    answers = ["I'd be happy to help!", GOOD_OUTLINE, *full_run_answers()[1:]]
    skill, _ = _skill(answers)
    _, result = await _run(skill)

    assert result.grounding["outline"]["source"] == "repaired"
    assert not result.refused


async def test_the_repair_prompt_states_the_actual_validation_failure() -> None:
    """A retry that only says "that was wrong" gets the same answer again."""
    answers = ['{"sections": ["a", "b", "c"]}', GOOD_OUTLINE, *full_run_answers()[1:]]
    skill, gateway = _skill(answers)
    await _run(skill)
    repair_prompt = gateway.calls[1][-1].content
    assert "title" in repair_prompt


async def test_a_twice_invalid_outline_falls_back_deterministically() -> None:
    """A model that cannot plan must not cost the user the essay: the fallback
    outline is generic, but the claims under it are still grounded."""
    answers = ["nonsense", "still nonsense", *full_run_answers()[1:]]
    skill, _ = _skill(answers)
    _, result = await _run(skill)

    assert result.grounding["outline"]["source"] == "fallback"
    assert not result.refused
    assert result.content.startswith("# What operators actually do about")


# ------------------------------------------------------- section validation --


async def test_an_uncited_section_is_retried_once() -> None:
    answers = full_run_answers()
    answers[2] = " ".join(["uncited"] * 200)  # first section, no marker
    answers.insert(3, section_text(240, "[S2]"))  # the retry's answer
    skill, gateway = _skill(answers)
    _, result = await _run(skill)

    assert not result.refused
    retry_prompts = [
        m.content
        for call in gateway.calls
        for m in call
        if "did not cite its sources correctly" in m.content
    ]
    assert len(retry_prompts) == 1


async def test_a_retry_that_regresses_is_discarded() -> None:
    """A rewrite that loses the section's only real citation must not replace a
    usable draft -- the same rule the Q&A skill applies."""
    answers = full_run_answers()
    answers[2] = f"One uncited paragraph here that is quite long indeed.\n\n{section_text(50)}"
    answers.insert(3, " ".join(["worse"] * 100))  # retry: no citation at all
    skill, _ = _skill(answers)
    _, result = await _run(skill)

    assert "insight" in result.content  # the original draft survived
    assert "worse" not in result.content


async def test_a_failed_section_does_not_lose_the_whole_essay() -> None:
    answers = full_run_answers()
    answers[3] = ""  # second section comes back empty
    skill, _ = _skill(answers)
    _, result = await _run(skill)

    assert not result.refused
    assert "## What operators did" not in result.content
    assert "## The real constraint" in result.content


async def test_an_invented_marker_is_stripped_from_the_finished_essay() -> None:
    answers = full_run_answers()
    answers[2] = section_text(240, "[S9]") + " and more [S1]."
    skill, _ = _skill(answers)
    _, result = await _run(skill)

    assert "[S9]" not in result.content
    # Stripped during section generation, before assembly -- but still reported,
    # because a repaired hallucination is something an operator must be able
    # to see.
    assert 9 in result.grounding["invalid_markers"]


# -------------------------------------------------------------- the repairs --


async def test_over_bolding_is_repaired_without_another_model_call() -> None:
    answers = full_run_answers(section_words=240)
    answers[2] = " ".join(f"**bold {i}** filler words here [S1]" for i in range(50))
    skill, gateway = _skill(answers)
    calls_before = len(answers)
    _, result = await _run(skill)

    assert result.grounding["structure"]["bold_count"] <= MAX_BOLD
    assert result.grounding["repairs"]["unbolded"] > 0
    assert len(gateway.calls) <= calls_before  # no extra call was spent on this


async def test_a_short_essay_triggers_one_targeted_expansion() -> None:
    answers = [
        GOOD_OUTLINE,
        "A short opening [S1].",
        *[section_text(90) for _ in range(4)],
        "Do this [S2].",
        section_text(400, "[S1]"),  # the expansion's answer
    ]
    skill, _ = _skill(answers)
    _, result = await _run(skill)

    assert "expanded_section" in result.grounding["repairs"]
    assert result.grounding["repairs"]["words_before_expansion"] < 1000


async def test_an_ungrounded_expansion_is_discarded() -> None:
    answers = [
        GOOD_OUTLINE,
        "A short opening [S1].",
        *[section_text(90) for _ in range(4)],
        "Do this [S2].",
        " ".join(["uncited"] * 400),  # expansion with no marker
    ]
    skill, _ = _skill(answers)
    _, result = await _run(skill)

    assert "uncited" not in result.content
    assert "expanded_section" not in result.grounding["repairs"]


async def test_an_overlong_essay_is_trimmed_to_the_band() -> None:
    skill, _ = _skill(full_run_answers(section_words=500))
    _, result = await _run(skill)

    assert result.grounding["repairs"]["trimmed_paragraphs"] > 0
    assert count_words(result.content) <= result.grounding["structure"]["word_range"][1]


# ----------------------------------------------------------------- reporting --


async def test_the_result_reports_structure_and_citations_for_the_eval_harness() -> None:
    skill, _ = _skill(full_run_answers())
    _, result = await _run(skill)

    grounding = result.grounding
    assert set(grounding) >= {"structure", "outline", "repairs", "cited_indices"}
    assert grounding["structure"]["target_words"] == 1250
    assert result.sources and all(s["label"].startswith("S") for s in result.sources)


async def test_cited_sources_are_flagged_for_the_ui() -> None:
    skill, _ = _skill(full_run_answers())
    _, result = await _run(skill)
    assert any(s["cited"] for s in result.sources)
