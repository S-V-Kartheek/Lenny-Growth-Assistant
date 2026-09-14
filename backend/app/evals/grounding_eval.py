"""Measure M1 (grounded answer rate) end to end.

    python -m app.evals.grounding_eval
    python -m app.evals.grounding_eval --json report.json

`retrieval_eval.py` measures whether the *right passages* are found. This
measures the step after it: given those passages, does the model produce an
answer that is actually grounded -- at least one citation that resolves to a
retrieved chunk, and no paragraph left uncited? That is only knowable by
running the real skill against a real model, which is why this is a separate,
slower harness rather than a unit test.

A question counts as "grounded" (M1's predicate) when the skill did not refuse
and `GroundingReport.is_grounded` is true. A question the skill correctly
refused (confidence below threshold) is excluded from the M1 denominator --
refusing is scored by M2, not this metric -- and reported separately so a
refusal is never invisible in this report.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app.agent.contracts import SkillContext
from app.agent.skills.knowledge_qa import KnowledgeQASkill
from app.config import get_settings
from app.db.engine import dispose_engine, init_engine
from app.evals.dataset import GoldenQuestion, load_golden_set
from app.llm.registry import LLMGateway
from app.observability import configure_logging
from app.retrieval.retriever import Retriever


@dataclass
class GroundingOutcome:
    id: str
    question: str
    refused: bool
    grounded: bool | None  # None when refused -- M1 does not apply
    citation_count: int
    invalid_markers: list[int]
    uncited_paragraphs: int
    source_count: int
    provider: str | None = None
    model: str | None = None
    latency_ms: float = 0.0
    reason: str | None = None


@dataclass
class GroundingReportSummary:
    total: int
    refused: int
    answered: int
    grounded: int
    rate: float | None
    outcomes: list[GroundingOutcome] = field(default_factory=list)


async def _evaluate_one(
    skill: KnowledgeQASkill, item: GoldenQuestion
) -> GroundingOutcome:
    ctx = SkillContext(message=item.question)
    result = None
    async for event in skill.run(ctx):
        if event.kind == "result":
            result = event.result
    assert result is not None, "knowledge_qa skill must always yield a result"

    grounding = result.grounding or {}
    return GroundingOutcome(
        id=item.id,
        question=item.question,
        refused=result.refused,
        grounded=None if result.refused else bool(grounding.get("grounded")),
        citation_count=int(grounding.get("citation_count", 0)),
        invalid_markers=list(grounding.get("invalid_markers", [])),
        uncited_paragraphs=int(grounding.get("uncited_paragraphs", 0)),
        source_count=int(grounding.get("source_count", 0)),
        provider=result.provider,
        model=result.model,
        latency_ms=result.latency_ms,
        reason=grounding.get("reason"),
    )


async def run() -> dict[str, object]:
    settings = get_settings()
    gateway = LLMGateway(settings)
    retriever = Retriever(settings)
    skill = KnowledgeQASkill(settings, gateway, retriever)
    golden = load_golden_set()

    outcomes = [await _evaluate_one(skill, q) for q in golden.in_corpus]

    answered = [o for o in outcomes if not o.refused]
    grounded = [o for o in answered if o.grounded]
    summary = GroundingReportSummary(
        total=len(outcomes),
        refused=len(outcomes) - len(answered),
        answered=len(answered),
        grounded=len(grounded),
        rate=round(len(grounded) / len(answered), 4) if answered else None,
        outcomes=outcomes,
    )
    return {
        "provider": gateway.describe,
        "summary": {k: v for k, v in asdict(summary).items() if k != "outcomes"},
        "outcomes": [asdict(o) for o in outcomes],
    }


def _print_report(report: dict) -> None:
    provider = report["provider"]
    print(f"\nprovider: {provider['provider']} / {provider['model']}")
    summary = report["summary"]
    rate = f"{summary['rate']:.0%}" if summary["rate"] is not None else "n/a"
    print(
        f"M1 grounded answer rate : {rate} "
        f"({summary['grounded']}/{summary['answered']} answered questions)"
    )
    print(
        f"  {summary['total']} in-corpus questions total, "
        f"{summary['refused']} refused (excluded from M1, see M2)"
    )

    failures = [
        o
        for o in report["outcomes"]
        if o["grounded"] is False
    ]
    if failures:
        print("\nungrounded answers:")
        for o in failures:
            print(
                f"  {o['id']:<22} invalid_markers={o['invalid_markers']} "
                f"uncited_paragraphs={o['uncited_paragraphs']}"
            )

    refusals = [o for o in report["outcomes"] if o["refused"]]
    if refusals:
        print("\nrefused (in-corpus, but confidence was below threshold):")
        for o in refusals:
            print(f"  {o['id']:<22} {o['question'][:60]}")
    print()


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Measure M1 against a live model.")
    parser.add_argument("--json", type=Path, help="Also write the full report to this path.")
    args = parser.parse_args()

    settings = get_settings()
    configure_logging("WARNING", "console")
    init_engine(settings)
    try:
        report = await run()
    finally:
        await dispose_engine()

    _print_report(report)
    if args.json:
        args.json.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"full report written to {args.json}")


if __name__ == "__main__":
    asyncio.run(_main())
