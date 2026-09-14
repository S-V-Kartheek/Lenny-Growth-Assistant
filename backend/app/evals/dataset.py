"""Loader for the golden evaluation set."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

DATA_PATH = Path(__file__).parent / "data" / "golden_questions.yaml"


@dataclass(frozen=True, slots=True)
class GoldenQuestion:
    id: str
    question: str
    answerable: bool
    expect_any: tuple[str, ...] = ()
    intent: str = "knowledge_qa"


@dataclass(frozen=True, slots=True)
class RoutingCase:
    id: str
    text: str
    intent: str


@dataclass(frozen=True, slots=True)
class GoldenSet:
    in_corpus: list[GoldenQuestion] = field(default_factory=list)
    out_of_corpus: list[GoldenQuestion] = field(default_factory=list)
    routing: list[RoutingCase] = field(default_factory=list)

    @property
    def all_questions(self) -> list[GoldenQuestion]:
        return [*self.in_corpus, *self.out_of_corpus]


@lru_cache(maxsize=1)
def load_golden_set(path: Path | None = None) -> GoldenSet:
    raw = yaml.safe_load((path or DATA_PATH).read_text(encoding="utf-8")) or {}
    return GoldenSet(
        in_corpus=[
            GoldenQuestion(
                id=item["id"],
                question=item["question"],
                answerable=True,
                expect_any=tuple(item.get("expect_any", ())),
                intent=item.get("intent", "knowledge_qa"),
            )
            for item in raw.get("in_corpus", [])
        ],
        out_of_corpus=[
            GoldenQuestion(id=item["id"], question=item["question"], answerable=False)
            for item in raw.get("out_of_corpus", [])
        ],
        routing=[
            RoutingCase(id=item["id"], text=item["text"], intent=item["intent"])
            for item in raw.get("routing", [])
        ],
    )
