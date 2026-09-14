"""Retrieval behaviour: query normalisation, RRF fusion and confidence."""

from __future__ import annotations

import pytest

from app.retrieval.retriever import Retriever, normalise_query

# -------------------------------------------------------- query normalising --

@pytest.mark.parametrize(
    ("raw", "must_contain", "must_not_contain"),
    [
        ("Can you tell me what Lenny says about activation?", "activation", "Lenny"),
        ("Please help me with retention loops", "retention", "Please"),
        ("What does the podcast say about pricing?", "pricing", "podcast"),
    ],
)
def test_domain_filler_is_stripped(raw: str, must_contain: str, must_not_contain: str) -> None:
    out = normalise_query(raw)
    assert must_contain in out
    assert must_not_contain not in out


def test_ordinary_stopwords_are_left_to_postgres() -> None:
    # to_tsvector drops "what"/"is"; stripping them here too would risk removing
    # meaningful words in other phrasings for no retrieval benefit.
    assert "product-market" in normalise_query("What is product-market fit?")


def test_query_of_only_filler_falls_back_to_raw_text() -> None:
    # Never send an empty tsquery: that returns nothing and looks like
    # "no evidence" when the real problem is over-aggressive filtering.
    assert normalise_query("tell me about lenny").strip()


def test_punctuation_does_not_break_tsquery_input() -> None:
    assert "@" not in normalise_query("what about growth@scale?!")


# -------------------------------------------------------------------- fusion --

def test_rrf_rewards_agreement_between_both_legs() -> None:
    lexical = [("a", 0.9), ("b", 0.5), ("c", 0.1)]
    semantic = [("c", 0.8), ("a", 0.7), ("d", 0.6)]
    fused = Retriever._fuse(lexical, semantic, 60)
    ids = [cid for cid, _ in fused]
    # "a" is ranked highly by both legs, so it must win outright.
    assert ids[0] == "a"
    assert set(ids) == {"a", "b", "c", "d"}


def test_fusion_uses_rank_not_raw_score_scale() -> None:
    # ts_rank and cosine live on different scales; a huge lexical score must not
    # be able to dominate purely because of its magnitude.
    lexical = [("a", 999.0), ("b", 1.0)]
    semantic = [("b", 0.9), ("a", 0.1)]
    ids = [cid for cid, _ in Retriever._fuse(lexical, semantic, 60)]
    assert set(ids) == {"a", "b"}
    scores = dict(Retriever._fuse(lexical, semantic, 60))
    assert scores["a"] == pytest.approx(scores["b"])  # rank 1+2 vs 2+1


def test_fusion_with_one_empty_leg_preserves_the_other_order() -> None:
    lexical = [("a", 0.9), ("b", 0.5)]
    assert [cid for cid, _ in Retriever._fuse(lexical, [], 60)] == ["a", "b"]


def test_fusion_of_two_empty_legs_is_empty() -> None:
    assert Retriever._fuse([], [], 60) == []


def test_fusion_is_deterministic_for_tied_scores() -> None:
    lexical = [("b", 0.5), ("a", 0.5)]
    first = Retriever._fuse(lexical, [], 60)
    second = Retriever._fuse(lexical, [], 60)
    assert first == second


# ---------------------------------------------------------------- confidence --

def test_no_chunks_means_zero_confidence() -> None:
    assert Retriever._confidence([], [("a", 5.0)], None) == 0.0


def test_strong_semantic_match_yields_high_confidence() -> None:
    chunks = [object()]  # only emptiness is inspected
    value = Retriever._confidence(chunks, [], [("a", 0.80)])  # type: ignore[arg-type]
    assert value > 0.8


def test_weak_semantic_match_yields_low_confidence() -> None:
    value = Retriever._confidence([object()], [], [("a", 0.36)])  # type: ignore[arg-type]
    assert value < 0.1


def test_confidence_is_clamped_to_unit_range() -> None:
    high = Retriever._confidence([object()], [("a", 10_000.0)], [("a", 0.99)])  # type: ignore[arg-type]
    low = Retriever._confidence([object()], [("a", 0.0)], [("a", 0.0)])  # type: ignore[arg-type]
    assert 0.0 <= low <= high <= 1.0


def test_lexical_only_still_produces_usable_confidence() -> None:
    # Degraded mode must not permanently pin confidence below the refusal
    # threshold, or lexical-only deployments could never answer anything.
    value = Retriever._confidence([object()], [("a", 0.5)], None)  # type: ignore[arg-type]
    assert value > 0.25
