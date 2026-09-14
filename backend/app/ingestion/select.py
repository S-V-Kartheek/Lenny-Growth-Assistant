"""Choose which episodes to index.

The full archive is ~390 episodes / ~30MB. Embedding all of it on a laptop takes
far longer than an evaluator should wait, so ingestion indexes a bounded,
*deterministic* subset by default and can be widened with one setting.

Selection rules, in order:
  1. Drop anything shorter than INGEST_MIN_DURATION_SECONDS. The archive mixes
     3-minute promo clips with 90-minute interviews; the clips add noise without
     adding answerable depth.
  2. Rank by view count descending, then slug ascending. Most-watched episodes
     are the ones a growth team is most likely to be asking about, and the tie
     break on slug makes the selection byte-identical on every machine.
  3. Take INGEST_MAX_EPISODES (or everything when INGEST_ALL=true).

INGEST_EPISODES overrides all of this with an explicit slug allow-list, which is
what the test fixtures and the evaluation harness use.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.ingestion.parse import EpisodeDocument


@dataclass(slots=True)
class SelectionResult:
    selected: list[EpisodeDocument]
    skipped_short: int
    skipped_over_limit: int


def select_episodes(
    documents: list[EpisodeDocument],
    *,
    min_duration_seconds: int = 1200,
    max_episodes: int = 40,
    ingest_all: bool = False,
    allow_list: list[str] | None = None,
) -> SelectionResult:
    if allow_list:
        wanted = {slug.strip() for slug in allow_list if slug.strip()}
        chosen = [d for d in documents if d.slug in wanted]
        return SelectionResult(chosen, 0, 0)

    long_enough = [
        d
        for d in documents
        if d.duration_seconds is None or d.duration_seconds >= min_duration_seconds
    ]
    skipped_short = len(documents) - len(long_enough)

    ranked = sorted(long_enough, key=lambda d: (-(d.view_count or 0), d.slug))
    if ingest_all or max_episodes <= 0:
        return SelectionResult(ranked, skipped_short, 0)

    return SelectionResult(
        ranked[:max_episodes], skipped_short, max(0, len(ranked) - max_episodes)
    )
