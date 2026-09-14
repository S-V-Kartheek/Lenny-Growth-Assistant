"""Intent routing: which skill should handle this turn.

Hybrid on purpose. Neither half is sufficient:

* **Rules alone** break on phrasing nobody anticipated, and the failure is
  silent -- the wrong skill runs and produces plausible output.
* **An LLM alone** costs 20-40 seconds on a local 7B model *before the real
  work starts*, and a 7B classifier is not reliable enough to be worth that on
  the ~80% of turns whose phrasing is unambiguous.

So: deterministic rules decide when the evidence is unambiguous, and the model
is consulted only when it is not. That keeps the common path instant, keeps the
uncommon path flexible, and -- because `method` is recorded on every decision --
makes it measurable which path is carrying the load.

Weighting: artifact and essay cues outrank Q&A cues because a request can be
both ("How should I think about activation? Put it in an HTML one-pager") and
the *generative* intent is the actionable one. A question with no generative cue
is Q&A.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.agent.contracts import Intent
from app.llm.base import ChatMessage
from app.llm.registry import LLMGateway

log = logging.getLogger("app.agent.router")

# (pattern, weight). Generative cues are weight 1.0, question cues 0.5.
_RULES: dict[Intent, tuple[tuple[str, float], ...]] = {
    Intent.ARTIFACT: (
        (r"\bhtml\b", 1.0),
        (r"\bcss\b", 1.0),
        (r"\blanding page\b", 1.0),
        (r"\bweb ?page\b", 1.0),
        (r"\bchecklist\b", 1.0),
        (r"\bone[- ]pager?\b", 1.0),
        (r"\bmock ?-?up\b", 1.0),
        (r"\bwireframe\b", 1.0),
        (r"\bdashboard\b", 1.0),
        (r"\bmarkdown (?:doc|document|file|page)\b", 1.0),
        (r"\btemplate\b", 0.5),
        (r"\bdiagram\b", 0.5),
    ),
    Intent.SHIP30_ESSAY: (
        (r"\bship ?30\b", 1.5),
        (r"\bessay\b", 1.0),
        (r"\barticle\b", 1.0),
        (r"\bblog post\b", 1.0),
        (r"\blinkedin\b", 1.0),
        (r"\bnewsletter\b", 1.0),
        (r"\b\d{3,4}[- ]word\b", 1.0),
        (r"\bthought leadership\b", 1.0),
        (r"\bpublish\b", 0.5),
    ),
    Intent.KNOWLEDGE_QA: (
        (r"\bwhat (?:do|does|is|are|did|was|were)\b", 0.5),
        (r"\bhow (?:do|does|should|can|did|would)\b", 0.5),
        (r"\bwhy\b", 0.5),
        (r"\bwhen should\b", 0.5),
        (r"\bexplain\b", 0.5),
        (r"\btell me about\b", 0.5),
        (r"\bwho (?:said|talked|discussed)\b", 0.5),
        (r"\badvice\b", 0.5),
    ),
}

_COMPILED = {
    intent: tuple((re.compile(p, re.IGNORECASE), w) for p, w in rules)
    for intent, rules in _RULES.items()
}

_ROUTER_SYSTEM = (
    "You classify a user's request into exactly one category for a product and "
    "growth assistant. Reply with the category name and nothing else.\n\n"
    "knowledge_qa - the user wants an answer, explanation or advice drawn from "
    "podcast transcripts.\n"
    "ship30_essay - the user wants a written essay, article or post to publish.\n"
    "artifact - the user wants a document or web page artifact: markdown, HTML, "
    "a checklist, a one-pager, a mockup.\n\n"
    "Answer with one of: knowledge_qa, ship30_essay, artifact"
)


@dataclass(slots=True)
class RoutingDecision:
    intent: Intent
    method: str  # rules | llm | default
    confidence: float
    matched: list[str] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "intent": str(self.intent),
            "method": self.method,
            "confidence": round(self.confidence, 3),
            "matched": self.matched,
            "scores": {k: round(v, 2) for k, v in self.scores.items()},
        }


def score_rules(text: str) -> tuple[dict[Intent, float], dict[Intent, list[str]]]:
    scores: dict[Intent, float] = dict.fromkeys(Intent, 0.0)
    matched: dict[Intent, list[str]] = {intent: [] for intent in Intent}
    for intent, rules in _COMPILED.items():
        for pattern, weight in rules:
            if found := pattern.search(text):
                scores[intent] += weight
                matched[intent].append(found.group(0).lower())
    return scores, matched


def route_by_rules(text: str) -> RoutingDecision | None:
    """Return a decision only when the rules are unambiguous, else None.

    "Unambiguous" means a single top scorer with a clear margin. A tie between
    two generative intents ("write an essay as a markdown document") is exactly
    the case worth spending a model call on.
    """
    scores, matched = score_rules(text)
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    top_intent, top_score = ranked[0]
    runner_up = ranked[1][1]

    if top_score == 0:
        return None
    if top_score - runner_up < 0.5:
        return None

    # Confidence is the margin, squashed into 0-1. It is a reporting signal, not
    # a threshold: the decision has already been made by the margin test above.
    confidence = min(1.0, 0.5 + (top_score - runner_up) / 4)
    return RoutingDecision(
        intent=top_intent,
        method="rules",
        confidence=confidence,
        matched=matched[top_intent],
        scores={str(k): v for k, v in scores.items()},
    )


class IntentRouter:
    def __init__(self, gateway: LLMGateway | None = None) -> None:
        self._gateway = gateway

    async def route(self, text: str) -> RoutingDecision:
        if decision := route_by_rules(text):
            log.info("routed", extra=decision.as_dict())
            return decision
        decision = await self._route_by_llm(text)
        log.info("routed", extra=decision.as_dict())
        return decision

    async def _route_by_llm(self, text: str) -> RoutingDecision:
        scores, _ = score_rules(text)
        fallback = RoutingDecision(
            intent=Intent.KNOWLEDGE_QA,
            method="default",
            confidence=0.4,
            scores={str(k): v for k, v in scores.items()},
        )
        if self._gateway is None:
            return fallback
        try:
            completion = await self._gateway.complete(
                [
                    ChatMessage(role="system", content=_ROUTER_SYSTEM),
                    ChatMessage(role="user", content=text[:1000]),
                ],
                temperature=0.0,
                # Enough for the longest label plus any stray punctuation; a
                # larger budget only buys the model room to explain itself.
                max_tokens=12,
            )
        except Exception as exc:  # noqa: BLE001 - routing must never fail the turn
            log.warning(
                "router_llm_failed_using_default",
                extra={"error_type": type(exc).__name__},
            )
            return fallback

        answer = completion.text.strip().lower()
        for intent in Intent:
            if str(intent) in answer:
                return RoutingDecision(
                    intent=intent,
                    method="llm",
                    confidence=0.7,
                    scores={str(k): v for k, v in scores.items()},
                )
        log.warning("router_llm_unparseable", extra={"raw": answer[:80]})
        return fallback
