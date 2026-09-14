"""The Ship 30 for 30 essay skill: outline, then write, then verify.

Pipeline, with the reason each stage exists:

    retrieve  -> confidence gate (never write 1,250 fluent words off 0.2
                 confidence: the longer the output, the more expensive an
                 ungrounded one is)
    outline   -> schema-validated plan, one repair attempt, deterministic
                 fallback (PRD 1.6: "schema validation with repair-retry and
                 deterministic fallback")
    sections  -> one bounded call per part, each validated for citations as it
                 lands and retried once if it is not grounded
    assemble  -> headings written by code, not by the model
    repair    -> deterministic fixes for over-bolding and over-length; one
                 targeted model call for under-length
    verify    -> structure + citations measured, reported, and persisted

The skill answers from the **same retrieved material** as grounded Q&A, using
the same retriever, the same passage numbering and the same citation validator,
so an essay's claims are traceable exactly like an answer's are.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator

from app.agent import essay as essay_mod
from app.agent.citations import GroundingReport, validate_and_repair
from app.agent.contracts import Intent, Phase, Skill, SkillContext, SkillEvent, SkillResult
from app.agent.essay import (
    EssayOutline,
    OutlineInvalid,
    SectionPlan,
    assemble,
    build_expansion_messages,
    build_outline_messages,
    build_outline_repair_messages,
    build_section_messages,
    count_words,
    enforce_selective_bold,
    evaluate_structure,
    fallback_outline,
    parse_outline,
    strip_generated_heading,
    trim_to_word_limit,
)
from app.agent.prompts import build_passage_block, estimate_tokens
from app.agent.sources import build_retrieval_query, sources_payload
from app.config import Settings
from app.errors import AppError
from app.llm.base import ChatMessage
from app.llm.registry import LLMGateway
from app.retrieval.retriever import Retriever

log = logging.getLogger("app.agent.skills.ship30_essay")

ESSAY_REFUSAL = (
    "I do not have enough supporting material in the indexed transcripts to write "
    "a grounded essay on that. An essay is 1,250 words of claims -- writing one "
    "without evidence behind it would be exactly the confident, unverifiable "
    "content this assistant exists to avoid. Ask a question I can answer with "
    "citations first, then ask me to turn the answer into an essay."
)

UNGROUNDED_REFUSAL = (
    "I drafted an essay but it did not stay anchored to the retrieved passages, "
    "so I am not going to show it. The sources below are what the search found."
)

# Per-part word budget for the expansion pass. Below this shortfall an essay is
# inside the tolerance band anyway, so the extra call is not worth 30 seconds.
_MIN_SHORTFALL_TO_EXPAND = 60


class Ship30EssaySkill(Skill):
    intent = Intent.SHIP30_ESSAY
    name = "Ship 30 for 30 essay"
    description = (
        "Writes a ~1,250-word Ship 30 for 30 essay -- hook, narrative progression, "
        "skimmable headings and bullets, a specific takeaway -- grounded in the "
        "same cited transcript passages as a normal answer."
    )
    examples = (
        "Turn that into a Ship 30 essay.",
        "Write a 1250 word essay about this.",
        "Draft a LinkedIn post about activation.",
    )

    def __init__(self, settings: Settings, gateway: LLMGateway, retriever: Retriever) -> None:
        self._settings = settings
        self._gateway = gateway
        self._retriever = retriever

    # ------------------------------------------------------------------ run --
    async def run(self, ctx: SkillContext) -> AsyncIterator[SkillEvent]:
        started = time.perf_counter()
        settings = self._settings

        yield SkillEvent(kind="phase", phase=Phase.RETRIEVING)
        # "Turn that into an essay" carries no topic words of its own; the same
        # anaphora heuristic the Q&A skill uses resolves it against the previous
        # turn, which is what makes the essay cover what was just discussed.
        query = build_retrieval_query(ctx.message, ctx.history, drop_format_terms=True)
        retrieval = await self._retriever.retrieve(query)

        if retrieval.is_empty or retrieval.confidence < settings.retrieval_min_confidence:
            log.info(
                "essay_refused_insufficient_evidence",
                extra={"confidence": round(retrieval.confidence, 4)},
            )
            yield SkillEvent(
                kind="result",
                result=SkillResult(
                    content=ESSAY_REFUSAL,
                    intent=self.intent,
                    refused=True,
                    sources=sources_payload(retrieval.chunks[:3], retrieval),
                    grounding={
                        **GroundingReport(source_count=0).as_dict(),
                        "reason": "insufficient_evidence",
                        "retrieval_confidence": round(retrieval.confidence, 4),
                        "threshold": settings.retrieval_min_confidence,
                    },
                    provider=self._gateway.last_provider.name,
                    model=self._gateway.last_provider.model,
                    latency_ms=(time.perf_counter() - started) * 1000,
                ),
            )
            return

        # One passage block, shared by every call. Budgeted against the model's
        # real context window minus the largest single generation this skill
        # makes, so the last section is prompted as completely as the first.
        block, used = build_passage_block(retrieval.chunks, self._passage_budget())
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
        brief = ctx.message if len(ctx.message.split()) > 6 else query
        outline = await self._plan(brief, block)
        yield SkillEvent(kind="outline", data=outline.as_dict())

        hook, sections, takeaway, usage, invalid_seen = await self._write(outline, block, used)

        yield SkillEvent(kind="phase", phase=Phase.VALIDATING)
        essay = assemble(outline, hook, sections, takeaway)
        essay, sections, repairs = await self._repair(
            essay, outline, sections, hook, takeaway, block, used
        )

        text, citation_report = validate_and_repair(essay, used)
        structure = evaluate_structure(text)

        if not citation_report.has_valid_citation:
            log.error("essay_ungrounded", extra={"sources": used})
            yield SkillEvent(
                kind="result",
                result=SkillResult(
                    content=UNGROUNDED_REFUSAL,
                    intent=self.intent,
                    refused=True,
                    sources=sources,
                    grounding={
                        **citation_report.as_dict(),
                        "reason": "no_valid_citation",
                        "structure": structure.as_dict(),
                    },
                    provider=self._gateway.last_provider.name,
                    model=self._gateway.last_provider.model,
                    usage=usage,
                    latency_ms=(time.perf_counter() - started) * 1000,
                ),
            )
            return

        # The essay is streamed once, whole, at the end rather than token by
        # token: a reader cannot follow six out-of-order section generations,
        # and the structure only exists after assembly. Phase events carry
        # progress in the meantime.
        yield SkillEvent(kind="delta", text=text)

        for source in sources:
            source["cited"] = source["index"] in citation_report.cited_indices

        if not structure.ok:
            log.warning("essay_structure_issues", extra={"issues": structure.issues})

        yield SkillEvent(
            kind="result",
            result=SkillResult(
                content=text,
                intent=self.intent,
                refused=False,
                sources=sources,
                grounding={
                    **citation_report.as_dict(),
                    # Union of markers stripped during section generation and at
                    # final assembly: the per-section repair happens first, so
                    # the assembly-time report alone would read as spotless.
                    "invalid_markers": sorted(
                        set(citation_report.invalid_markers) | set(invalid_seen)
                    ),
                    "structure": structure.as_dict(),
                    "outline": outline.as_dict(),
                    "repairs": repairs,
                    "retrieval_confidence": round(retrieval.confidence, 4),
                    "retrieval_method": retrieval.method,
                    "fallback_from": self._gateway.last_fallback_from,
                },
                provider=self._gateway.last_provider.name,
                model=self._gateway.last_provider.model,
                usage=usage,
                latency_ms=(time.perf_counter() - started) * 1000,
            ),
        )

    # ----------------------------------------------------------------- plan --
    async def _plan(self, brief: str, block: str) -> EssayOutline:
        """Outline with one schema-repair attempt, then a deterministic fallback."""
        raw = ""
        try:
            completion = await self._gateway.complete(
                build_outline_messages(brief, block), temperature=0.4, max_tokens=600
            )
            raw = completion.text
            return parse_outline(raw)
        except OutlineInvalid as exc:
            log.warning("essay_outline_invalid_retrying", extra={"reason": str(exc)})
        except AppError as exc:
            log.warning(
                "essay_outline_call_failed", extra={"error_type": type(exc).__name__}
            )
            return fallback_outline(brief)

        try:
            completion = await self._gateway.complete(
                build_outline_repair_messages(brief, block, raw, _last_reason(raw)),
                temperature=0.1,
                max_tokens=600,
            )
            outline = parse_outline(completion.text)
            outline.source = "repaired"
            return outline
        except (OutlineInvalid, AppError) as exc:
            log.warning(
                "essay_outline_fallback",
                extra={"error_type": type(exc).__name__, "reason": str(exc)[:200]},
            )
            return fallback_outline(brief)

    # ---------------------------------------------------------------- write --
    async def _write(
        self, outline: EssayOutline, block: str, used: int
    ) -> tuple[str, list[str], str, dict[str, int], list[int]]:
        """Generate every part, validating citations as each one lands.

        Markers the model invented are stripped *per section*, before assembly,
        so by the time the finished essay is validated there is nothing invalid
        left to find. They are collected and returned anyway: "the model
        hallucinated a citation and we repaired it" is exactly the kind of thing
        an operator needs to see, and a clean final report would hide it.
        """
        usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
        invalid_seen: list[int] = []
        per_section = essay_mod.format_word_budget(len(outline.sections))

        hook = await self._generate_part(
            build_section_messages(
                outline, None, block, used, part="hook", word_target=essay_mod.HOOK_WORDS
            ),
            used,
            label="hook",
            usage=usage,
            invalid_seen=invalid_seen,
        )
        sections: list[str] = []
        for plan in outline.sections:
            body = await self._generate_part(
                build_section_messages(
                    outline, plan, block, used, part="section", word_target=per_section
                ),
                used,
                label=plan.heading,
                usage=usage,
                invalid_seen=invalid_seen,
            )
            sections.append(strip_generated_heading(body, plan.heading))
        takeaway = await self._generate_part(
            build_section_messages(
                outline, None, block, used, part="takeaway", word_target=essay_mod.TAKEAWAY_WORDS
            ),
            used,
            label="takeaway",
            usage=usage,
            invalid_seen=invalid_seen,
        )
        return (
            strip_generated_heading(hook, outline.title),
            sections,
            takeaway,
            usage,
            sorted(set(invalid_seen)),
        )

    async def _generate_part(
        self,
        messages: list[ChatMessage],
        used: int,
        *,
        label: str,
        usage: dict[str, int],
        invalid_seen: list[int],
    ) -> str:
        """One part, with one corrective retry if it comes back ungrounded.

        Validating per part rather than only at the end is what makes the retry
        affordable: regenerating one 240-word section costs a fraction of
        regenerating a 1,250-word essay, and it localises the fault to the
        section that actually broke the contract.
        """
        text, report = await self._complete_and_validate(messages, used, usage)
        invalid_seen.extend(report.invalid_markers)
        if report.is_grounded or not text.strip():
            return text

        log.warning(
            "essay_section_not_grounded_retrying",
            extra={
                "section": label[:60],
                "uncited_paragraphs": report.uncited_paragraphs,
                "invalid_markers": report.invalid_markers,
            },
        )
        retry_messages = [
            *messages,
            ChatMessage(role="assistant", content=text[:2000]),
            ChatMessage(
                role="user",
                content=(
                    f"That draft did not cite its sources correctly. Rewrite it using "
                    f"ONLY the passages above, with at least one marker from "
                    f"[S1]-[S{used}] in every paragraph and every bullet. Output the "
                    f"section body only."
                ),
            ),
        ]
        retried, retried_report = await self._complete_and_validate(
            retry_messages, used, usage
        )
        invalid_seen.extend(retried_report.invalid_markers)
        # Only keep a retry that is a strict improvement -- a rewrite that loses
        # the section's only real citation must not replace a usable draft.
        if retried.strip() and (
            retried_report.is_grounded
            or (retried_report.has_valid_citation and not report.has_valid_citation)
        ):
            return retried
        return text

    async def _complete_and_validate(
        self, messages: list[ChatMessage], used: int, usage: dict[str, int]
    ) -> tuple[str, GroundingReport]:
        try:
            completion = await self._gateway.complete(messages, temperature=0.55)
        except AppError as exc:
            # One failed part must not lose the whole essay: an essay missing a
            # section still assembles, and the structure report will say so.
            log.warning(
                "essay_part_generation_failed", extra={"error_type": type(exc).__name__}
            )
            return "", GroundingReport(source_count=used)
        usage["input_tokens"] += completion.usage.input_tokens
        usage["output_tokens"] += completion.usage.output_tokens
        return validate_and_repair(completion.text.strip(), used)

    # --------------------------------------------------------------- repair --
    async def _repair(
        self,
        essay: str,
        outline: EssayOutline,
        sections: list[str],
        hook: str,
        takeaway: str,
        block: str,
        used: int,
    ) -> tuple[str, list[str], dict[str, object]]:
        """Bring the draft inside the contract, cheapest repair first."""
        repairs: dict[str, object] = {}

        essay, unbolded = enforce_selective_bold(essay)
        if unbolded:
            repairs["unbolded"] = unbolded

        words = count_words(essay)
        shortfall = essay_mod.MIN_WORDS - words
        if shortfall >= _MIN_SHORTFALL_TO_EXPAND:
            index = _shortest_section_index(sections)
            if index is not None:
                expanded = await self._expand_section(
                    outline.sections[index], sections[index], block, used, shortfall
                )
                if count_words(expanded) > count_words(sections[index]):
                    sections[index] = expanded
                    essay, _ = enforce_selective_bold(
                        assemble(outline, hook, sections, takeaway)
                    )
                    repairs["expanded_section"] = outline.sections[index].heading
                    repairs["words_before_expansion"] = words

        essay, trimmed = trim_to_word_limit(essay)
        if trimmed:
            repairs["trimmed_paragraphs"] = trimmed
        return essay, sections, repairs

    async def _expand_section(
        self,
        plan: SectionPlan,
        existing: str,
        block: str,
        used: int,
        shortfall: int,
    ) -> str:
        outline_stub = EssayOutline(plan.heading, "", [plan], "")
        try:
            completion = await self._gateway.complete(
                build_expansion_messages(
                    outline_stub, plan, block, used, existing, shortfall + 40
                ),
                temperature=0.5,
            )
        except AppError as exc:
            log.warning("essay_expansion_failed", extra={"error_type": type(exc).__name__})
            return existing
        expanded, report = validate_and_repair(completion.text.strip(), used)
        expanded = strip_generated_heading(expanded, plan.heading)
        # An expansion that arrives ungrounded is worse than a short essay.
        return expanded if report.has_valid_citation else existing

    # --------------------------------------------------------------- budget --
    def _passage_budget(self) -> int:
        """Context left for passages once the largest prompt overhead is reserved.

        Reserved against the *outline* system prompt plus a generous section
        prompt, not against the smallest one, so no single call in the pipeline
        can overflow the window that the others were budgeted for.
        """
        context = self._gateway.primary.context_tokens
        reserve = (
            self._settings.llm_max_output_tokens
            + estimate_tokens(essay_mod.SECTION_SYSTEM)
            + estimate_tokens(essay_mod.OUTLINE_SYSTEM)
            + 1024
        )
        return max(512, context - reserve)


def _shortest_section_index(sections: list[str]) -> int | None:
    """Expand the thinnest section: it is the one most likely to be underdeveloped."""
    candidates = [(count_words(s), i) for i, s in enumerate(sections) if s.strip()]
    if not candidates:
        return None
    return min(candidates)[1]


def _last_reason(raw: str) -> str:
    """Re-derive the validation failure so the repair prompt can state it."""
    try:
        parse_outline(raw)
    except OutlineInvalid as exc:
        return str(exc)
    return "the outline did not match the required shape"
