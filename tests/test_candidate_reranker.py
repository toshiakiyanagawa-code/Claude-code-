"""Unit tests for cms_entry_assistant.candidate_reranker.

LLM プランは _ReRankPlanView (intent_terms / query_terms / avoid_terms) と
互換のダミーオブジェクトで投げる。
"""

from __future__ import annotations

from dataclasses import dataclass

from cms_entry_assistant.candidate_reranker import rerank_candidates


@dataclass
class _Plan:
    intent_terms: list[str]
    query_terms: list[str]
    avoid_terms: list[str]


@dataclass
class _Hit:
    asset_id: str
    alt: str
    detail_url: str = ""
    title: str = ""
    keywords: str = ""


def test_rerank_prefers_candidate_matching_intent():
    plan = _Plan(intent_terms=["shanghai skyline"], query_terms=[], avoid_terms=[])
    good = _Hit("1", alt="shanghai skyline at dusk")
    bad = _Hit("2", alt="cute cats playing")

    ranked = rerank_candidates([bad, good], plan)

    assert [r.candidate.asset_id for r in ranked] == ["1", "2"]
    assert ranked[0].intent_score > ranked[1].intent_score


def test_rerank_query_terms_drive_secondary_score():
    plan = _Plan(intent_terms=[], query_terms=["tokyo tower"], avoid_terms=[])
    direct = _Hit("a", alt="tokyo tower at night")
    other = _Hit("b", alt="osaka castle")

    ranked = rerank_candidates([other, direct], plan)

    assert ranked[0].candidate.asset_id == "a"
    assert ranked[0].query_score > ranked[1].query_score


def test_rerank_avoid_terms_penalize_candidate():
    plan = _Plan(
        intent_terms=["protest"],
        query_terms=[],
        avoid_terms=["beijing"],
    )
    clean = _Hit("clean", alt="protest crowd")
    flagged = _Hit("flagged", alt="protest crowd beijing")

    ranked = rerank_candidates([flagged, clean], plan)

    # flagged は penalty で順位が下がる
    assert ranked[0].candidate.asset_id == "clean"
    by_id = {r.candidate.asset_id: r for r in ranked}
    assert by_id["flagged"].penalty > 0
    assert "beijing" in by_id["flagged"].avoided_terms


def test_rerank_handles_empty_candidate_list():
    plan = _Plan(intent_terms=["foo"], query_terms=[], avoid_terms=[])
    assert rerank_candidates([], plan) == []


def test_rerank_preserves_candidate_object_identity():
    plan = _Plan(intent_terms=["x"], query_terms=[], avoid_terms=[])
    hits = [_Hit("a", alt="x match"), _Hit("b", alt="other")]
    ranked = rerank_candidates(hits, plan)
    # 返ってくる candidate は元のオブジェクトそのもの
    assert ranked[0].candidate is hits[0]
    assert ranked[1].candidate is hits[1]
