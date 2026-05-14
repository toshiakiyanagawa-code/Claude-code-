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


def test_rerank_hard_blocks_explicit_policy_violations():
    """Policy-3 hard-filter: 笑顔・ポートレート・カメラ目線・白人・黒人 を含む
    候補は最終リストから完全に除外される。"""
    plan = _Plan(
        intent_terms=["ビジネスマン"],
        query_terms=["businessman"],
        avoid_terms=[],
    )
    hits = [
        _Hit("ok-japanese", alt="日本人 ビジネスマン 後ろ姿 階段"),
        _Hit("bad-portrait", alt="カメラを見ている一人のビジネスウーマンスタジオの肖像画"),
        _Hit("bad-smile", alt="白い背景に腕を組んで微笑む実業家"),
        _Hit("bad-black", alt="職場でラップトップ上のグラフを分析する黒人起業家"),
        _Hit("bad-foreign", alt="幸せな中年のビジネスウーマンマネージャーが握手する"),
        _Hit("ok-abstract", alt="ビジネス 戦略 グラフ 矢印 デジタル"),
    ]

    ranked = rerank_candidates(hits, plan)

    asset_ids = [r.candidate.asset_id for r in ranked]
    # 明示違反は除外される
    assert "bad-portrait" not in asset_ids
    assert "bad-smile" not in asset_ids
    assert "bad-black" not in asset_ids
    # ポリシー OK の候補は残る
    assert "ok-japanese" in asset_ids
    assert "ok-abstract" in asset_ids


def test_rerank_demotes_ambiguous_person_candidates():
    """Policy-3 soft-demote: 人物指標があるが日本人/顔なし指標が無い候補は、
    抽象/日本人/顔なし候補より下に来る。"""
    plan = _Plan(intent_terms=["ビジネス"], query_terms=[], avoid_terms=[])
    hits = [
        _Hit("ambiguous", alt="ビジネス街を歩くビジネスマン"),       # 人物だが指標なし → -6
        _Hit("japanese-back", alt="日本人 ビジネスマン 後ろ姿"),    # +8 +8 = +16
        _Hit("graph", alt="経済 グラフ 矢印 上昇"),                  # +10 (abstract)
    ]
    ranked = rerank_candidates(hits, plan)
    asset_ids = [r.candidate.asset_id for r in ranked]
    # ambiguous が最下位
    assert asset_ids[-1] == "ambiguous"
    # japanese-back と graph がより上
    assert asset_ids.index("japanese-back") < asset_ids.index("ambiguous")
    assert asset_ids.index("graph") < asset_ids.index("ambiguous")


def test_rerank_real_world_alts_filter_policy_violations():
    """codex policy review §4 fixture: 実 case 99d805d99323 で表示された alt 30 件で、
    新ロジックが明示違反 4 件 (肖像画/微笑む/黒人/幸せな) を top10 から除外することを検証。"""
    plan = _Plan(
        intent_terms=["japanese", "businessman", "back view"],
        query_terms=["japanese businessman", "business"],
        avoid_terms=["face", "smiling", "portrait", "caucasian", "外国人"],
    )
    real_alts = [
        ("a1", "ビジネス3dタブレット仮想成長矢印財務グラフ"),
        ("a2", "成長グラフ"),
        ("a3", "手の触れるグラフの成長の計画を上に移動"),
        ("a4", "矢印は強調表示されたターゲットに当たります"),
        ("a5", "ウェブ分析とデジタルマーケティング。ラップトップを使用してビジネスマンのトップビュー"),
        ("a6", "ビジネスを管理するためにオフィスビルに足を踏み入れるアジアの成人男性"),
        ("a7", "ビジネスマンのエスカレーター"),
        ("a8", "ビジネス街を歩くビジネスマン"),
        ("a9", "エスカレーターでの日本のビジネス"),
        ("a10", "エレガントな服装でモダンな階段を上る自信に満ちた女性"),
        ("VIOLATION-portrait", "カメラを見ている一人のビジネスウーマンスタジオの肖像画"),
        ("a11", "光を用いたロケット形ドア"),
        ("VIOLATION-smile", "白い背景に腕を組んで微笑む実業家"),
        ("a12", "目に見えないステップを上りブリーフケースを持ったビジネスマンの背面図"),
        ("a13", "日本人男性ビジネスマン"),
        ("a14", "不可能を可能にする日本人男性ビジネスマン"),
        ("a15", "日本のコンサルタントが説明をする"),
        ("a16", "ビジネスの成功"),
        ("a17", "成功した実業家の赤い矢印目標"),
        ("VIOLATION-black", "職場でラップトップ上のグラフを分析する黒人起業家"),
        ("a18", "ラップトップ コンピュータ上のグラフ"),
        ("a19", "パートナーと握手するビジネスマン"),
        ("a20", "オフィスでのビジネスパートナーシップ会議"),
        ("a21", "オフィスで手を重ねた同僚"),
        ("VIOLATION-happy", "幸せな中年のビジネスウーマンマネージャーが握手をしてクライアント"),
        ("a22", "手の積み重ね統一とチームワークのコンセプト"),
    ]
    hits = [_Hit(aid, alt=alt) for aid, alt in real_alts]

    ranked = rerank_candidates(hits, plan)

    asset_ids = [r.candidate.asset_id for r in ranked]
    # 明示違反 4 件はすべて完全除外
    for violation_id in (
        "VIOLATION-portrait",   # 「カメラを見ている / 肖像画」
        "VIOLATION-smile",      # 「微笑む」
        "VIOLATION-black",      # 「黒人」
        "VIOLATION-happy",      # 「幸せな」
    ):
        assert violation_id not in asset_ids, f"{violation_id} should be hard-blocked"
    # a13/a14 (日本人明示) が top10 に入る
    top10 = asset_ids[:10]
    assert "a13" in top10 or "a14" in top10


def test_rerank_preserves_candidate_object_identity():
    plan = _Plan(intent_terms=["x"], query_terms=[], avoid_terms=[])
    hits = [_Hit("a", alt="x match"), _Hit("b", alt="other")]
    ranked = rerank_candidates(hits, plan)
    # 返ってくる candidate は元のオブジェクトそのもの
    assert ranked[0].candidate is hits[0]
    assert ranked[1].candidate is hits[1]
