"""Retrieval inspector: see exactly what the assistant would be given.

    python -m app.retrieval.inspect "how do I improve activation?"

Prints the fused ranking with each leg's contribution, the confidence score and
the refusal decision. When an answer looks wrong, this is the first tool to
reach for -- it separates "the model reasoned badly" from "the model was handed
the wrong passages", which are very different bugs with very different fixes.
"""

from __future__ import annotations

import argparse
import asyncio

from app.config import get_settings
from app.db.engine import dispose_engine, init_engine
from app.observability import configure_logging
from app.retrieval.retriever import Retriever, knowledge_base_stats


async def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query", help="The question to retrieve for.")
    parser.add_argument("-k", "--top-k", type=int, default=None)
    parser.add_argument("--full", action="store_true", help="Print whole chunks.")
    args = parser.parse_args()

    settings = get_settings()
    configure_logging("WARNING", "console")
    init_engine(settings)
    try:
        stats = await knowledge_base_stats()
        print(
            f"index: {stats['episodes']} episodes / {stats['chunks']} chunks "
            f"({stats['embedded']} embedded)\n"
        )
        result = await Retriever(settings).retrieve(args.query, top_k=args.top_k)

        print(f"query      : {result.query}")
        degraded = f"  ({result.degraded_reason})" if result.degraded_reason else ""
        print(f"method     : {result.method}{degraded}")
        print(
            f"confidence : {result.confidence:.3f}  "
            f"(threshold {settings.retrieval_min_confidence})"
        )
        decision = (
            "ANSWER"
            if result.confidence >= settings.retrieval_min_confidence and result.chunks
            else "REFUSE (insufficient evidence)"
        )
        print(f"decision   : {decision}\n")

        for position, chunk in enumerate(result.chunks, start=1):
            legs = []
            if chunk.lexical_rank:
                legs.append(f"lex#{chunk.lexical_rank}")
            if chunk.semantic_rank:
                legs.append(f"sem#{chunk.semantic_rank}")
            stamp = chunk.timestamp_label or "no timestamp"
            print(
                f"[S{position}] {chunk.episode_title[:64]}  <{chunk.episode_slug}>\n"
                f"      {chunk.guest or 'unknown guest'} | {stamp} "
                f"| rrf={chunk.score:.4f} | {' '.join(legs) or 'n/a'}\n"
                f"      {chunk.citation_url}"
            )
            body = chunk.content if args.full else chunk.content[:300].replace("\n", " ")
            print(f"      {body}\n")
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(_main())
