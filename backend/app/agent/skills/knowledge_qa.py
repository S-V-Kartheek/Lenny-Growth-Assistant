"""Grounded question answering -- the skill the product exists for.

The shape of this skill is dictated by one requirement: *never produce a fluent
answer that is not supported by the corpus.* Three gates enforce it, in order,
and each is cheaper than the one after it:

1. **Confidence gate (no model call).** If fused retrieval confidence is below
   the calibrated `RETRIEVAL_MIN_CONFIDENCE`, the skill refuses before spending
   a single token. The threshold is measured, not guessed -- see checkpoint 1.
2. **Prompt contract.** The model is given numbered passages and told, as hard
   rules, to cite every paragraph and to never invent a marker.
3. **Post-hoc validation.** Markers are checked against the passages actually
   sent. Unresolvable markers are stripped; an answer left with no valid
   citation at all is retried once and then refused rather than shown.

Gate 3 is the one that matters, because 1 and 2 are both probabilistic and this
one is not.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator

from app.agent.citations import GroundingReport, validate_and_repair
from app.agent.contracts import Intent, Phase, Skill, SkillContext, SkillEvent, SkillResult
from app.agent.prompts import build_grounded_messages, build_refusal_messages

# Re-exported below for the tests and callers that already import them from here.
from app.agent.sources import build_retrieval_query, sources_payload
from app.config import Settings
from app.errors import AppError
from app.llm.base import ChatMessage
from app.llm.registry import LLMGateway
from app.retrieval.retriever import RetrievalResult, Retriever

log = logging.getLogger("app.agent.skills.knowledge_qa")

__all__ = ["KnowledgeQASkill", "build_retrieval_query", "sources_payload"]

STATIC_REFUSAL = (
    "I could not find anything in the indexed Lenny's Podcast transcripts that "
    "supports an answer to that. Rather than answer from general knowledge, I am "
    "telling you the corpus does not cover it. The corpus covers product "
    "management, growth, startups and operating topics from the podcast."
)

UNGROUNDED_REFUSAL = (
    "I found related passages but could not produce an answer that stays "
    "anchored to them, so I am not going to show one. The sources below are what "
    "the search returned -- they may still be worth reading directly."
)


class KnowledgeQASkill(Skill):
    intent = Intent.KNOWLEDGE_QA
    name = "Grounded Q&A"
    description = (
        "Answers a product or growth question from indexed podcast transcripts, "
        "with citations, or refuses when the corpus does not support an answer."
    )
    examples = (
        "How do I improve activation?",
        "What does the podcast say about pricing?",
    )

    def __init__(
        self, settings: Settings, gateway: LLMGateway, retriever: Retriever
    ) -> None:
        self._settings = settings
        self._gateway = gateway
        self._retriever = retriever

    async def run(self, ctx: SkillContext) -> AsyncIterator[SkillEvent]:
        started = time.perf_counter()
        settings = self._settings

        yield SkillEvent(kind="phase", phase=Phase.RETRIEVING)
        query = build_retrieval_query(ctx.message, ctx.history)
        retrieval = await self._retriever.retrieve(query)

        # --- gate 1: refuse before spending a single generation token --------
        if retrieval.is_empty or retrieval.confidence < settings.retrieval_min_confidence:
            async for event in self._refuse(ctx, retrieval, started):
                yield event
            return

        messages, used = build_grounded_messages(
            ctx.message,
            retrieval.chunks,
            ctx.history,
            context_tokens=self._gateway.primary.context_tokens,
            max_output_tokens=settings.llm_max_output_tokens,
        )
        sources = sources_payload(retrieval.chunks[:used], retrieval)
        yield SkillEvent(
            kind="sources",
            data={
                "sources": sources,
                "retrieval": {
                    "method": retrieval.method,
                    "confidence": round(retrieval.confidence, 4),
                    "query": retrieval.query,
                    "degraded_reason": retrieval.degraded_reason,
                },
            },
        )

        yield SkillEvent(kind="phase", phase=Phase.GENERATING)
        parts: list[str] = []
        usage: dict[str, int] = {}
        async for event in self._gateway.stream(messages):
            if event.text:
                parts.append(event.text)
                yield SkillEvent(kind="delta", text=event.text)
            if event.usage:
                usage = event.usage.as_dict()

        yield SkillEvent(kind="phase", phase=Phase.VALIDATING)
        text, report = validate_and_repair("".join(parts).strip(), used)

        # --- gate 3: retry once if the answer is not fully grounded -----------
        # Measured against the golden set (see app.evals.grounding_eval), the
        # dominant failure was not a hallucinated marker -- it was a trailing,
        # uncited "in summary" paragraph tacked onto an otherwise well-cited
        # answer. Retrying on `not report.is_grounded` (not just "no citation
        # at all") catches that case too. Refusal below stays gated on the
        # stricter `has_valid_citation`: a partially-uncited answer that still
        # has real citations is more useful shown than refused.
        if not report.is_grounded:
            log.warning(
                "answer_not_fully_grounded_retrying",
                extra={
                    "invalid_markers": report.invalid_markers,
                    "uncited_paragraphs": report.uncited_paragraphs,
                    "sources": used,
                },
            )
            retried_text, retried_report = await self._retry_grounded(messages, used)
            # Keep the retry only if it is a strict improvement -- a retry that
            # regresses (e.g. loses its only valid citation) must not overwrite
            # a usable first answer.
            if retried_report.is_grounded or (
                retried_report.has_valid_citation and not report.has_valid_citation
            ):
                text, report = retried_text, retried_report

        if not report.has_valid_citation:
            log.error("answer_ungrounded_after_retry", extra={"sources": used})
            yield SkillEvent(
                kind="result",
                result=SkillResult(
                    content=UNGROUNDED_REFUSAL,
                    intent=self.intent,
                    refused=True,
                    sources=sources,
                    grounding={
                        **report.as_dict(),
                        "reason": "no_valid_citation_after_retry",
                        "retrieval_confidence": round(retrieval.confidence, 4),
                        "retrieval_method": retrieval.method,
                    },
                    provider=self._gateway.last_provider.name,
                    model=self._gateway.last_provider.model,
                    usage=usage,
                    latency_ms=(time.perf_counter() - started) * 1000,
                ),
            )
            return

        for source in sources:
            source["cited"] = source["index"] in report.cited_indices

        yield SkillEvent(
            kind="result",
            result=SkillResult(
                content=text,
                intent=self.intent,
                refused=False,
                sources=sources,
                grounding={
                    **report.as_dict(),
                    "retrieval_confidence": round(retrieval.confidence, 4),
                    "retrieval_method": retrieval.method,
                    "degraded_reason": retrieval.degraded_reason,
                    "fallback_from": self._gateway.last_fallback_from,
                },
                provider=self._gateway.last_provider.name,
                model=self._gateway.last_provider.model,
                usage=usage,
                latency_ms=(time.perf_counter() - started) * 1000,
            ),
        )

    # ------------------------------------------------------------- refusal ---
    async def _refuse(
        self, ctx: SkillContext, retrieval: RetrievalResult, started: float
    ) -> AsyncIterator[SkillEvent]:
        """Explain the refusal, and show what was found anyway.

        The near-miss passages are returned deliberately (PRD A2): "I have
        nothing" is far more useful when the user can see *what* was closest and
        judge for themselves that it really is off-topic.
        """
        nearest = sources_payload(retrieval.chunks[:3], retrieval, cited=False)
        yield SkillEvent(
            kind="sources",
            data={
                "sources": nearest,
                "retrieval": {
                    "method": retrieval.method,
                    "confidence": round(retrieval.confidence, 4),
                    "query": retrieval.query,
                    "degraded_reason": retrieval.degraded_reason,
                },
            },
        )
        yield SkillEvent(kind="phase", phase=Phase.GENERATING)

        parts: list[str] = []
        try:
            async for event in self._gateway.stream(
                build_refusal_messages(ctx.message, retrieval.chunks, retrieval.confidence),
                max_tokens=200,
            ):
                if event.text:
                    parts.append(event.text)
                    yield SkillEvent(kind="delta", text=event.text)
        except AppError as exc:
            # A refusal must not depend on the model being up: the honest answer
            # is already known, only its wording was being delegated.
            log.warning(
                "refusal_generation_failed_using_static",
                extra={"error_type": type(exc).__name__},
            )
            parts = []

        content = "".join(parts).strip() or STATIC_REFUSAL
        # Strip any marker the model invented while refusing; there is nothing
        # for it to point at.
        content, _ = validate_and_repair(content, 0)
        yield SkillEvent(
            kind="result",
            result=SkillResult(
                content=content,
                intent=self.intent,
                refused=True,
                sources=nearest,
                grounding={
                    **GroundingReport(source_count=0).as_dict(),
                    "reason": "insufficient_evidence",
                    "retrieval_confidence": round(retrieval.confidence, 4),
                    "retrieval_method": retrieval.method,
                    "threshold": self._settings.retrieval_min_confidence,
                    "degraded_reason": retrieval.degraded_reason,
                },
                provider=self._gateway.last_provider.name,
                model=self._gateway.last_provider.model,
                latency_ms=(time.perf_counter() - started) * 1000,
            ),
        )

    async def _retry_grounded(
        self, messages: list[ChatMessage], used: int
    ) -> tuple[str, GroundingReport]:
        """One corrective pass, with the violated rule restated.

        Exactly one: on a local model each attempt is tens of seconds, and a
        model that ignores the citation contract twice will ignore it a third
        time. The refusal path is a better outcome than a third try.
        """
        retry = [
            *messages,
            ChatMessage(
                role="user",
                content=(
                    f"That answer did not cite its sources correctly. Rewrite it using "
                    f"ONLY the passages above, and put at least one marker from "
                    f"[S1]-[S{used}] in every paragraph. Output only the rewritten answer."
                ),
            ),
        ]
        try:
            completion = await self._gateway.complete(retry)
        except AppError as exc:
            log.warning("citation_retry_failed", extra={"error_type": type(exc).__name__})
            return "", GroundingReport(source_count=used)
        return validate_and_repair(completion.text.strip(), used)
