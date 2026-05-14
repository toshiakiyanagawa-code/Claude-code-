from types import SimpleNamespace

from cms_entry_assistant.istock_search import build_query_plan
from cms_entry_assistant.web.app import diversify_case_candidates


def _hit(asset_id: str) -> SimpleNamespace:
    return SimpleNamespace(asset_id=asset_id)


def test_build_query_plan_normal_keeps_primary_without_duplicate_attempts() -> None:
    plan = build_query_plan(
        "睡眠 健康",
        context="健康的な睡眠",
        h4_text="睡眠 健康",
        slot_key="h4_1",
    )

    assert plan == ["睡眠 健康"]


def test_build_query_plan_proper_noun_uses_stock_safe_fallback() -> None:
    plan = build_query_plan(
        "孫正義 逆境",
        context="孫正義氏は逆境に立つ",
        h4_text="",
        slot_key="hero",
    )

    assert plan[0] == "孫正義 逆境"
    assert plan[1] == "課題 解決 ビジネス"
    assert all("孫正義" not in query for query in plan[1:])


def test_build_query_plan_press_sensitive_uses_diplomacy_fallback() -> None:
    plan = build_query_plan(
        "習近平 外交",
        context="習近平氏と日本外交の緊張",
        h4_text="",
        slot_key="hero",
    )

    assert plan[0] == "習近平 外交"
    assert plan[1] == "国旗 会談 ビジネス"
    assert all("習近平" not in query for query in plan[1:])


def test_build_query_plan_lower_back_pain_keeps_specific_scene_query() -> None:
    plan = build_query_plan(
        "シニア 腰痛",
        context="腰痛に悩むシニアが医師に相談する",
        h4_text="シニア 腰痛",
        slot_key="h4_1",
    )

    assert plan == ["シニア 腰痛"]


def test_build_query_plan_tower_mansion_keeps_specific_scene_query() -> None:
    plan = build_query_plan(
        "タワーマンション 都市 街並み",
        context="タワマンと都市の再開発、建設規制緩和",
        h4_text="タワーマンション 都市 街並み",
        slot_key="h4_2",
    )

    assert plan == ["タワーマンション 都市 街並み"]


def test_diversify_case_candidates_moves_duplicate_hero_asset_from_h4_head() -> None:
    out = {
        "hero": [_hit("asset-1")],
        "h4_1": [_hit("asset-1"), _hit("asset-2"), _hit("asset-1")],
        "h4_2": [_hit("asset-3")],
    }

    result = diversify_case_candidates(out)

    assert result is out
    assert [hit.asset_id for hit in result["h4_1"]] == [
        "asset-2",
        "asset-1",
        "asset-1",
    ]
    assert result["h4_2"][0].asset_id == "asset-3"


# --- Phase 4 追加: 全 slot 横断 dedupe + URL identity + fallback 到達性 ---


def _phase4_hit(asset_id="", detail_url=None, thumbnail_url=None):
    suffix = asset_id or "blank"
    return SimpleNamespace(
        asset_id=asset_id,
        detail_url=detail_url
        if detail_url is not None
        else f"https://example.test/detail/{suffix}",
        thumbnail_url=thumbnail_url
        if thumbnail_url is not None
        else f"https://example.test/thumb/{suffix}.jpg",
    )


def test_diversify_case_candidates_deduplicates_h4_leads_across_h4_slots() -> None:
    """h4_1 と h4_2 の先頭が同じ asset_id の場合、h4_2 側を入れ替える。"""
    out = {
        "hero": [_phase4_hit(asset_id="hero")],
        "h4_1": [_phase4_hit(asset_id="shared"), _phase4_hit(asset_id="h4-1-alt")],
        "h4_2": [_phase4_hit(asset_id="shared"), _phase4_hit(asset_id="h4-2-alt")],
    }
    diversify_case_candidates(out)
    assert out["h4_1"][0].asset_id == "shared"  # 先発は維持
    assert out["h4_2"][0].asset_id == "h4-2-alt"  # 後発は入れ替え
    assert out["h4_2"][1].asset_id == "shared"


def test_diversify_case_candidates_uses_detail_url_when_asset_id_is_empty() -> None:
    """asset_id が空でも detail_url が同じなら重複と判定。"""
    shared_detail_url = "https://example.test/detail/shared"
    alt_detail_url = "https://example.test/detail/alt"
    out = {
        "hero": [
            _phase4_hit(
                asset_id="",
                detail_url=shared_detail_url,
                thumbnail_url="https://example.test/thumb/hero.jpg",
            )
        ],
        "h4_1": [
            _phase4_hit(
                asset_id="",
                detail_url=shared_detail_url,
                thumbnail_url="https://example.test/thumb/h4.jpg",
            ),
            _phase4_hit(asset_id="", detail_url=alt_detail_url),
        ],
    }
    diversify_case_candidates(out)
    assert out["h4_1"][0].detail_url == alt_detail_url
    assert out["h4_1"][1].detail_url == shared_detail_url


def test_fetch_candidates_collects_until_hits_per_slot_plus_two(monkeypatch) -> None:
    """primary が hits_per_slot 件取れても、後段 diversify 用に +2 件まで貪欲に集める。

    `_fetch_candidates` のシグネチャは (suggestions, *, hits_per_slot=5) なので、
    primary で 3 件のみ取れたら fallback も呼ばれる (collect_until = 5+2 = 7)。
    """
    from cms_entry_assistant.istock_crawler import IstockSearchHit
    from cms_entry_assistant.models import IstockSearchSuggestion
    from cms_entry_assistant.web import app as app_module

    calls: list[str] = []

    def fake_crawl(query, limit=8):
        calls.append(query)
        if query == "primary":
            return [
                IstockSearchHit(asset_id="p1", thumbnail_url="", alt="", photographer_username="", detail_url=""),
                IstockSearchHit(asset_id="p2", thumbnail_url="", alt="", photographer_username="", detail_url=""),
                IstockSearchHit(asset_id="p3", thumbnail_url="", alt="", photographer_username="", detail_url=""),
            ]
        if query == "fallback":
            return [
                IstockSearchHit(asset_id="f1", thumbnail_url="", alt="", photographer_username="", detail_url=""),
            ]
        return []

    monkeypatch.setattr(app_module, "_crawl_search_safe", fake_crawl)
    monkeypatch.setattr(app_module, "is_available", lambda: True)
    monkeypatch.setattr(
        app_module, "rank_hits",
        lambda hits, *, preferences, history, limit: hits[:limit],
    )

    suggestion = IstockSearchSuggestion(
        slot_key="hero",
        slot_label="カンバン",
        query_ja="primary",
        query_plan=["primary", "fallback"],
    )
    out = app_module._fetch_candidates([suggestion], hits_per_slot=5)
    # primary 3 件では collect_until=7 に届かないので fallback も呼ばれる
    assert calls == ["primary", "fallback"]
    # 最終的に rank_hits の limit=5 で切られる
    assert len(out["hero"]) <= 5
