"""Measure the Ship 30 essay skill against a live model.

    python -m app.evals.essay_eval
    python -m app.evals.essay_eval --json report.json --limit 2

PRD 2.3's acceptance criterion is "word count within tolerance; required
structural elements present; claims grounded -- **verified programmatically, not
by eye**". This is that verification: it runs the real `Ship30EssaySkill`
against the live index and the configured model, then applies
`app.agent.essay.evaluate_structure` and the same citation validator the skill
itself uses.

Two rates are reported, and they are deliberately separate:

* **structure pass rate** -- essays meeting every element of the contract
  (length band, title, hook, sections, bullets, selective bold, takeaway).
* **grounded rate** -- essays with at least one resolvable citation and no
  uncited substantive paragraph. This is the essay analogue of M1, and it is the
  one that must not regress: a badly-shaped essay is disappointing, an
  ungrounded one is the failure this product exists to prevent.

Essays are slow on a local 7B -- seven model calls each, minutes per essay -- so
`--limit` exists to run a subset while iterating. The recorded numbers are from
a full run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app.agent.contracts import SkillContext
from app.agent.essay import MAX_WORDS, MIN_WORDS, TARGET_WORDS
from app.agent.skills.ship30_essay import Ship30EssaySkill
from app.config import get_settings
from app.db.engine import dispose_engine, init_engine
from app.evals.dataset import GoldenQuestion, load_golden_set
from app.llm.registry import LLMGateway
from app.observability import configure_logging
from app.retrieval.retriever import Retriever


@dataclass
class EssayOutcome:
    id: str
    question: str
    refused: bool
    reason: str | None = None
    word_count: int = 0
    within_word_target: bool = False
    structure_ok: bool = False
    issues: list[str] = field(default_factory=list)
    heading_count: int = 0
    bullet_count: int = 0
    bold_count: int = 0
    has_hook: bool = False
    has_takeaway: bool = False
    grounded: bool = False
    citation_count: int = 0
    invalid_markers: list[int] = field(default_factory=list)
    uncited_paragraphs: int = 0
    outline_source: str | None = None
    repairs: dict = field(default_factory=dict)
    latency_ms: float = 0.0


async def _evaluate_one(skill: Ship30EssaySkill, item: GoldenQuestion) -> EssayOutcome:
    # Phrased as an explicit essay request so the skill is exercised the way a
    # user reaches it, rather than being handed a bare question.
    message = f"Write a Ship 30 for 30 essay answering: {item.question}"
    result = None
    async for event in skill.run(SkillContext(message=message)):
        if event.kind == "result":
            result = event.result
    assert result is not None, "the essay skill must always yield a result"

    grounding = result.grounding or {}
    if result.refused:
        return EssayOutcome(
            id=item.id,
            question=item.question,
            refused=True,
            reason=grounding.get("reason"),
            latency_ms=result.latency_ms,
        )

    structure = grounding.get("structure", {})
    return EssayOutcome(
        id=item.id,
        question=item.question,
        refused=False,
        word_count=int(structure.get("word_count", 0)),
        within_word_target=bool(structure.get("within_word_target")),
        structure_ok=bool(structure.get("structure_ok")),
        issues=list(structure.get("issues", [])),
        heading_count=int(structure.get("heading_count", 0)),
        bullet_count=int(structure.get("bullet_count", 0)),
        bold_count=int(structure.get("bold_count", 0)),
        has_hook=bool(structure.get("has_hook")),
        has_takeaway=bool(structure.get("has_takeaway")),
        grounded=bool(grounding.get("grounded")),
        citation_count=int(grounding.get("citation_count", 0)),
        invalid_markers=list(grounding.get("invalid_markers", [])),
        uncited_paragraphs=int(grounding.get("uncited_paragraphs", 0)),
        outline_source=(grounding.get("outline") or {}).get("source"),
        repairs=dict(grounding.get("repairs", {})),
        latency_ms=result.latency_ms,
    )


async def run(limit: int | None = None) -> dict[str, object]:
    settings = get_settings()
    gateway = LLMGateway(settings)
    skill = Ship30EssaySkill(settings, gateway, Retriever(settings))
    questions = load_golden_set().in_corpus[: limit or None]

    outcomes = [await _evaluate_one(skill, q) for q in questions]
    written = [o for o in outcomes if not o.refused]
    structural = [o for o in written if o.structure_ok]
    grounded = [o for o in written if o.grounded]
    in_band = [o for o in written if o.within_word_target]

    return {
        "provider": gateway.describe,
        "contract": {
            "target_words": TARGET_WORDS,
            "word_range": [MIN_WORDS, MAX_WORDS],
        },
        "summary": {
            "total": len(outcomes),
            "refused": len(outcomes) - len(written),
            "written": len(written),
            "structure_ok": len(structural),
            "structure_rate": _rate(len(structural), len(written)),
            "within_word_target": len(in_band),
            "word_target_rate": _rate(len(in_band), len(written)),
            "grounded": len(grounded),
            "grounded_rate": _rate(len(grounded), len(written)),
            "mean_words": (
                round(sum(o.word_count for o in written) / len(written)) if written else 0
            ),
            "mean_latency_ms": (
                round(sum(o.latency_ms for o in written) / len(written)) if written else 0
            ),
            "fallback_outlines": len([o for o in written if o.outline_source == "fallback"]),
        },
        "outcomes": [asdict(o) for o in outcomes],
    }


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _print_report(report: dict) -> None:
    provider = report["provider"]
    summary = report["summary"]
    low, high = report["contract"]["word_range"]

    print(f"\nprovider: {provider['provider']} / {provider['model']}")
    print(f"target   : {report['contract']['target_words']} words ({low}-{high} accepted)\n")
    for label, rate_key, count_key in (
        ("Ship 30 structure pass rate", "structure_rate", "structure_ok"),
        ("  within word target      ", "word_target_rate", "within_word_target"),
        ("  grounded (essay M1)     ", "grounded_rate", "grounded"),
    ):
        rate = summary[rate_key]
        shown = f"{rate:.0%}" if rate is not None else "n/a"
        print(f"{label}: {shown} ({summary[count_key]}/{summary['written']})")
    print(
        f"\nmean length {summary['mean_words']} words, "
        f"mean {summary['mean_latency_ms'] / 1000:.0f}s per essay, "
        f"{summary['refused']} refused, "
        f"{summary['fallback_outlines']} fallback outlines"
    )

    failures = [o for o in report["outcomes"] if not o["refused"] and not o["structure_ok"]]
    if failures:
        print("\nstructure failures:")
        for o in failures:
            print(f"  {o['id']:<22} {'; '.join(o['issues'])}")

    ungrounded = [o for o in report["outcomes"] if not o["refused"] and not o["grounded"]]
    if ungrounded:
        print("\nungrounded essays:")
        for o in ungrounded:
            print(
                f"  {o['id']:<22} citations={o['citation_count']} "
                f"uncited_paragraphs={o['uncited_paragraphs']} "
                f"invalid={o['invalid_markers']}"
            )
    print()


async def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Measure Ship 30 essay structure and grounding against a live model."
    )
    parser.add_argument("--json", type=Path, help="Also write the full report to this path.")
    parser.add_argument(
        "--limit", type=int, help="Only run the first N in-corpus questions (essays are slow)."
    )
    args = parser.parse_args()

    configure_logging("WARNING", "console")
    init_engine(get_settings())
    try:
        report = await run(limit=args.limit)
    finally:
        await dispose_engine()

    _print_report(report)
    if args.json:
        args.json.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"full report written to {args.json}")


if __name__ == "__main__":
    asyncio.run(_main())
