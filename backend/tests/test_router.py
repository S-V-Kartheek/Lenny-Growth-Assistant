"""Intent routing against the golden routing fixtures.

These fixtures (`app/evals/data/golden_questions.yaml`, `routing:` section) were
written in checkpoint 1 specifically for this test and were not yet consumed by
anything -- this is what closes that loop.
"""

from __future__ import annotations

import pytest

from app.agent.contracts import Intent
from app.agent.router import IntentRouter, route_by_rules
from app.evals.dataset import load_golden_set

golden = load_golden_set()


@pytest.mark.parametrize(
    "case", golden.routing, ids=[c.id for c in golden.routing]
)
async def test_routes_match_the_golden_fixture(case) -> None:
    router = IntentRouter(gateway=None)  # no LLM fallback needed: rules must cover these
    decision = await router.route(case.text)
    assert decision.intent == Intent(case.intent), (
        f"{case.text!r} routed to {decision.intent} via {decision.method}, "
        f"expected {case.intent}"
    )


def test_phrasing_with_no_rule_cues_is_not_forced_through_rules() -> None:
    """Nothing recognisable should fall through to the LLM step, not guess."""
    decision = route_by_rules("So yeah, that is interesting stuff overall.")
    assert decision is None


async def test_router_falls_back_to_default_intent_when_no_gateway_and_rules_are_silent() -> None:
    router = IntentRouter(gateway=None)
    decision = await router.route("hmm, interesting")
    assert decision.intent == Intent.KNOWLEDGE_QA
    assert decision.method == "default"


async def test_router_survives_a_broken_llm_fallback() -> None:
    """Routing must never be the reason a turn fails; a bad classifier answer defaults safely."""

    class ExplodingGateway:
        async def complete(self, *args, **kwargs):
            raise RuntimeError("model is on fire")

    router = IntentRouter(gateway=ExplodingGateway())
    decision = await router.route("Something ambiguous that rules do not cover at all")
    assert decision.intent == Intent.KNOWLEDGE_QA
    assert decision.method == "default"


def test_rules_prefer_generative_intent_over_a_bare_question_cue() -> None:
    decision = route_by_rules("How should I think about activation? Put it in an HTML one-pager.")
    assert decision is not None
    assert decision.intent == Intent.ARTIFACT


@pytest.mark.parametrize(
    "case",
    [c for c in golden.routing if c.kind],
    ids=[c.id for c in golden.routing if c.kind],
)
def test_artifact_format_matches_the_golden_fixture(case) -> None:
    """Routing has two levels once artifacts are real: which skill runs, and
    which format that skill produces. The second level is a routing decision
    too, so it is measured against the same golden set rather than left to a
    unit test of the regex.
    """
    from app.agent.skills.artifact import choose_kind

    assert choose_kind(case.text) == case.kind, (
        f"{case.text!r} chose {choose_kind(case.text)}, expected {case.kind}"
    )


def test_every_intent_has_a_real_skill_registered() -> None:
    """Checkpoint 2 registered truthful placeholders for the two unbuilt
    skills. This asserts they are gone: an intent that routes somewhere useless
    would make the routing numbers above meaningless.
    """
    from app.agent.orchestrator import Agent
    from app.agent.skills.artifact import ArtifactSkill
    from app.agent.skills.knowledge_qa import KnowledgeQASkill
    from app.agent.skills.ship30_essay import Ship30EssaySkill
    from app.config import Settings

    agent = Agent(Settings(_env_file=None), gateway=None, retriever=None)
    registered = {d["intent"] for d in agent.describe_skills()}
    assert registered == {str(i) for i in Intent}
    assert {type(s) for s in agent._skills.values()} == {
        KnowledgeQASkill,
        Ship30EssaySkill,
        ArtifactSkill,
    }
