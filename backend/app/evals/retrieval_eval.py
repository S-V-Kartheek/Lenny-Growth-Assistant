"""Measure retrieval quality and calibrate the refusal threshold.

    python -m app.evals.retrieval_eval
    python -m app.evals.retrieval_eval --json report.json

Three numbers matter, and they trade off against each other:

* **recall@k**  -- for answerable questions, did an expected episode appear in
  the top-k? If this is low, no amount of prompt engineering will help: the
  model is never shown the right passage.
* **answer rate on in-corpus questions** -- how often the system is willing to
  answer something it *can* answer. Refusing a good question is a real failure,
  just a quieter one than hallucinating.
* **refusal rate on out-of-corpus questions** -- how often it declines to invent
  an answer. This is the safety-critical direction.

The threshold sweep exists because RETRIEVAL_MIN_CONFIDENCE was originally a
guess, and a guessed threshold answered "which Kubernetes operator should I use
for quantum annealing hardware?" with a confident, fully-cited response. The
sweep picks the threshold that best separates the two populations, and reports
the separation so a human can see whether the signal is strong enough to trust.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app.config import get_settings
from app.db.engine import dispose_engine, init_engine
from app.evals.dataset import GoldenQuestion, load_golden_set
from app.observability import configure_logging
from app.retrieval.retriever import Retriever, knowledge_base_stats


@dataclass
class QuestionOutcome:
    id: str
    question: str
    answerable: bool
    confidence: float
    method: str
    returned: int
    episodes: list[str] = field(default_factory=list)
    expected: list[str] = field(default_factory=list)
    recall_hit: bool | None = None


@dataclass
class ThresholdPoint:
    threshold: float
    answered_in_corpus: int
    refused_out_of_corpus: int
    total_in_corpus: int
    total_out_of_corpus: int

    @property
    def score(self) -> float:
        """Balanced accuracy: answering good questions and refusing bad ones."""
        if not self.total_in_corpus or not self.total_out_of_corpus:
            return 0.0
        return 0.5 * (
            self.answered_in_corpus / self.total_in_corpus
            + self.refused_out_of_corpus / self.total_out_of_corpus
        )


async def _evaluate_one(retriever: Retriever, item: GoldenQuestion) -> QuestionOutcome:
    result = await retriever.retrieve(item.question)
    episodes = list(dict.fromkeys(c.episode_slug for c in result.chunks))
    outcome = QuestionOutcome(
        id=item.id,
        question=item.question,
        answerable=item.answerable,
        confidence=round(result.confidence, 4),
        method=result.method,
        returned=len(result.chunks),
        episodes=episodes,
        expected=list(item.expect_any),
    )
    if item.answerable and item.expect_any:
        outcome.recall_hit = any(slug in episodes for slug in item.expect_any)
    return outcome


def _sweep(outcomes: list[QuestionOutcome]) -> list[ThresholdPoint]:
    in_corpus = [o for o in outcomes if o.answerable]
    out_corpus = [o for o in outcomes if not o.answerable]
    points: list[ThresholdPoint] = []
    for step in range(0, 101, 2):
        threshold = step / 100
        points.append(
            ThresholdPoint(
                threshold=threshold,
                answered_in_corpus=sum(
                    1 for o in in_corpus if o.returned and o.confidence >= threshold
                ),
                refused_out_of_corpus=sum(
                    1 for o in out_corpus if not (o.returned and o.confidence >= threshold)
                ),
                total_in_corpus=len(in_corpus),
                total_out_of_corpus=len(out_corpus),
            )
        )
    return points


async def run() -> dict[str, object]:
    settings = get_settings()
    retriever = Retriever(settings)
    golden = load_golden_set()

    outcomes = [await _evaluate_one(retriever, q) for q in golden.all_questions]

    in_corpus = [o for o in outcomes if o.answerable]
    out_corpus = [o for o in outcomes if not o.answerable]
    measurable = [o for o in in_corpus if o.recall_hit is not None]

    sweep = _sweep(outcomes)
    best = max(sweep, key=lambda p: (p.score, -p.threshold))
    current = min(sweep, key=lambda p: abs(p.threshold - settings.retrieval_min_confidence))

    def mean(values: list[float]) -> float:
        return round(sum(values) / len(values), 4) if values else 0.0

    return {
        "knowledge_base": await knowledge_base_stats(),
        "retrieval_top_k": settings.retrieval_top_k,
        "recall_at_k": {
            "hits": sum(1 for o in measurable if o.recall_hit),
            "measurable": len(measurable),
            "rate": round(
                sum(1 for o in measurable if o.recall_hit) / len(measurable), 4
            )
            if measurable
            else None,
        },
        "confidence": {
            "mean_in_corpus": mean([o.confidence for o in in_corpus]),
            "mean_out_of_corpus": mean([o.confidence for o in out_corpus]),
            "min_in_corpus": min((o.confidence for o in in_corpus), default=0.0),
            "max_out_of_corpus": max((o.confidence for o in out_corpus), default=0.0),
        },
        "current_threshold": {
            "value": settings.retrieval_min_confidence,
            **{k: v for k, v in asdict(current).items() if k != "threshold"},
            "balanced_accuracy": round(current.score, 4),
        },
        "best_threshold": {
            "value": best.threshold,
            "balanced_accuracy": round(best.score, 4),
            "answered_in_corpus": best.answered_in_corpus,
            "refused_out_of_corpus": best.refused_out_of_corpus,
        },
        "outcomes": [asdict(o) for o in outcomes],
    }


def _print_report(report: dict) -> None:
    kb = report["knowledge_base"]
    print(f"\nknowledge base : {kb['episodes']} episodes, {kb['chunks']} chunks "
          f"({kb['embedded']} embedded)")
    recall = report["recall_at_k"]
    rate = f"{recall['rate']:.0%}" if recall["rate"] is not None else "n/a"
    print(f"recall@{report['retrieval_top_k']}       : {rate} "
          f"({recall['hits']}/{recall['measurable']} questions)")

    conf = report["confidence"]
    print("\nconfidence separation")
    print(f"  in-corpus      mean {conf['mean_in_corpus']:.3f}   min {conf['min_in_corpus']:.3f}")
    print(
        f"  out-of-corpus  mean {conf['mean_out_of_corpus']:.3f}   "
        f"max {conf['max_out_of_corpus']:.3f}"
    )
    gap = conf["min_in_corpus"] - conf["max_out_of_corpus"]
    verdict = "clean separation" if gap > 0 else "OVERLAP - some refusals will be wrong"
    print(f"  gap            {gap:+.3f}  ({verdict})")

    cur, best = report["current_threshold"], report["best_threshold"]
    print(
        f"\ncurrent threshold {cur['value']}:  "
        f"answers {cur['answered_in_corpus']}/{cur['total_in_corpus']} in-corpus, "
        f"refuses {cur['refused_out_of_corpus']}/{cur['total_out_of_corpus']} out-of-corpus "
        f"(balanced acc {cur['balanced_accuracy']:.2f})"
    )
    print(f"best threshold    {best['value']}:  balanced acc {best['balanced_accuracy']:.2f}")

    failures = [
        o for o in report["outcomes"]
        if o["answerable"] and o["recall_hit"] is False
    ]
    if failures:
        print("\nrecall misses (expected episode not in top-k):")
        for o in failures:
            print(f"  {o['id']:<22} expected {o['expected']}  got {o['episodes'][:3]}")

    leaks = sorted(
        (o for o in report["outcomes"] if not o["answerable"]),
        key=lambda o: -o["confidence"],
    )[:3]
    print("\nhighest-confidence out-of-corpus questions (these drive the threshold):")
    for o in leaks:
        print(f"  {o['confidence']:.3f}  {o['question'][:70]}")
    print()


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate retrieval against the golden set.")
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
