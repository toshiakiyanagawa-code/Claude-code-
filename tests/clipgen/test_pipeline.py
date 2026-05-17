"""clipgen の許諾判定とスコアリングの基本テスト."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from clipgen.pipeline import candidates_to_dict, filter_and_score, run_pipeline_live, run_pipeline_mock  # noqa: E402
from clipgen.youtube_client import SearchParams  # noqa: E402
from clipgen.scoring import Candidate, score_candidate  # noqa: E402
from clipgen.sources import (  # noqa: E402
    check_channel_permission,
    load_allowlist,
    load_blocklist,
    looks_defamatory,
)


NOW = datetime(2026, 5, 13, 12, 0, tzinfo=timezone.utc)


def _cand(**kw) -> Candidate:
    base = dict(
        video_id="x",
        title="dummy",
        channel_id="UC_jimin",
        channel_handle="@jimin",
        channel_title="自民党公式",
        published_at=datetime(2026, 5, 13, 6, 0, tzinfo=timezone.utc),
        duration_sec=60,
        view_count=100_000,
        like_count=3_000,
        comment_count=500,
        description="",
    )
    base.update(kw)
    return Candidate(**base)


def test_blocklist_blocks_tv_channel():
    allow = load_allowlist()
    blocks, kws = load_blocklist()
    perm = check_channel_permission(
        channel_id="UC_tbs",
        channel_handle="@tbsnewsdig",
        channel_title="TBS NEWS DIG",
        channel_description="",
        allowlist=allow,
        blocklist=blocks,
        block_keywords=kws,
    )
    assert perm.allowed is False
    assert "blocklist" in perm.reason
    assert any(flag.startswith("blocklist_match") for flag in perm.risk_flags)


def test_allowlist_allows_party_official():
    allow = load_allowlist()
    blocks, kws = load_blocklist()
    perm = check_channel_permission(
        channel_id="UC_jimin",
        channel_handle="@jimin",
        channel_title="自民党公式",
        channel_description="",
        allowlist=allow,
        blocklist=blocks,
        block_keywords=kws,
    )
    assert perm.allowed is True
    assert "allowlist" in perm.reason
    assert perm.category == "party_official"


def test_channel_handle_none_is_allowed_by_channel_id():
    allow = load_allowlist()
    blocks, kws = load_blocklist()
    perm = check_channel_permission(
        channel_id="UC_dpfp",
        channel_handle=None,
        channel_title="国民民主党",
        channel_description="",
        allowlist=allow,
        blocklist=blocks,
        block_keywords=kws,
    )
    assert perm.allowed is True
    assert "allowlist" in perm.reason


def test_unknown_channel_is_grey():
    allow = load_allowlist()
    blocks, kws = load_blocklist()
    perm = check_channel_permission(
        channel_id="UC_random",
        channel_handle="@somerandom",
        channel_title="知らないチャンネル",
        channel_description="",
        allowlist=allow,
        blocklist=blocks,
        block_keywords=kws,
    )
    assert perm.allowed is False
    assert "not_in_allowlist" in perm.risk_flags


def test_description_block_keyword_blocks():
    allow = load_allowlist()
    blocks, kws = load_blocklist()
    perm = check_channel_permission(
        channel_id="UC_random",
        channel_handle="@somerandom",
        channel_title="未確認",
        channel_description="本動画の切り抜き禁止です",
        allowlist=allow,
        blocklist=blocks,
        block_keywords=kws,
    )
    assert perm.allowed is False
    assert "切り抜き禁止" in perm.reason
    assert "blocklist_match:description_keyword" in perm.risk_flags


def test_suspicious_low_comment_density_flagged():
    c = _cand(view_count=1_500_000, like_count=600, comment_count=12)
    scored = score_candidate(c, now=NOW)
    assert "suspicious:low_comment_density" in scored.risk_flags
    assert scored.score_breakdown["comment_density"] < 0


def test_format_boost_and_keyword_boost():
    c = _cand(title="【完全論破】高市早苗が予算委員会で激詰め 切り抜き")
    scored = score_candidate(c, now=NOW)
    assert scored.score_breakdown["format_boost"] > 0
    assert scored.score_breakdown["keyword_boost"] >= 0.08
    assert "defamation_review_required" in scored.risk_flags


def test_looks_defamatory_for_shock_title():
    assert looks_defamatory("【絶句】安住淳氏、親族の訴えを鼻で笑う")


def test_defamation_title_forces_manual_review():
    allow = load_allowlist()
    blocks, kws = load_blocklist()
    cands = filter_and_score(
        [_cand(title="【絶句】高市早苗 記者会見ハイライト")],
        allowlist=allow,
        blocklist=blocks,
        block_keywords=kws,
        now=NOW,
    )
    assert len(cands) == 1
    assert cands[0].allowed is True
    assert cands[0].usage_status == "manual_review"
    assert "defamation_review_required" in cands[0].risk_flags


def test_permission_scope_propagates():
    allow = load_allowlist()
    blocks, kws = load_blocklist()
    cands = filter_and_score(
        [_cand(title="高市早苗 記者会見ハイライト")],
        allowlist=allow,
        blocklist=blocks,
        block_keywords=kws,
        now=NOW,
    )
    assert len(cands) == 1
    assert cands[0].usage_status == "cleared"
    assert cands[0].permission_scope == "public_press_conferences"


def test_audit_block_in_output():
    out = candidates_to_dict([_cand()])
    assert "audit" in out[0]
    assert out[0]["audit"] == {
        "reviewed_by": "",
        "decision": "",
        "decided_at": "",
        "notes": "",
    }


def test_duration_short_bonus():
    short = score_candidate(_cand(duration_sec=60), now=NOW)
    long = score_candidate(_cand(duration_sec=61), now=NOW)
    assert short.score_breakdown["duration_fit"] == 0.1
    assert short.score > long.score


def test_tv_hint_lowers_score():
    base = _cand(title="高市早苗 記者会見ハイライト")
    hinted = _cand(title="高市早苗 記者会見ハイライト")
    hinted.risk_flags.append("tv_hint_in_text")
    scored_base = score_candidate(base, now=NOW)
    scored_hinted = score_candidate(hinted, now=NOW)
    assert scored_hinted.score < scored_base.score
    assert scored_hinted.score_breakdown["tv_hint_multiplier"] == 0.7


def test_pipeline_mock_end_to_end():
    mock_path = ROOT / "src" / "clipgen" / "data" / "mock_search.json"
    cands = run_pipeline_mock(mock_path, now=NOW, include_blocked=True)
    assert cands, "no candidates returned"
    allowed = [c for c in cands if c.allowed]
    blocked = [c for c in cands if c.usage_status == "blocked"]
    # 自民党/国民民主党/参政党/石丸/衆議院 などは allowlist
    assert any(c.channel_handle == "@jimin" for c in allowed)
    # TBS NEWS DIG は blocklist
    assert any(c.channel_handle == "@tbsnewsdig" for c in blocked)
    # ハンドルがない動画も channel_id で allowlist 判定される
    assert any(c.video_id == "mock011" and c.allowed for c in allowed)
    assert {c.usage_status for c in cands} <= {"cleared", "manual_review", "blocked"}
    assert {"cleared", "manual_review", "blocked"} <= {c.usage_status for c in cands}


def test_pipeline_excludes_blocked_by_default():
    mock_path = ROOT / "src" / "clipgen" / "data" / "mock_search.json"
    cands = run_pipeline_mock(mock_path, now=NOW)
    assert all(c.usage_status != "blocked" for c in cands)


def test_run_pipeline_live_respects_query_limit_and_max_per_query():
    class FakeClient:
        def __init__(self):
            self.searches = []

        def handles_to_channel_ids(self, handles):
            return {}

        def search(self, params: SearchParams):
            self.searches.append(params)
            return [{"id": {"videoId": f"v{len(self.searches)}"}}]

        def videos(self, video_ids):
            return [
                {
                    "id": video_id,
                    "snippet": {
                        "title": "高市早苗 国会 ハイライト",
                        "channelId": "UC_unknown",
                        "channelTitle": "Unknown",
                        "publishedAt": "2026-05-13T00:00:00Z",
                        "description": "",
                    },
                    "statistics": {"viewCount": "1000"},
                    "contentDetails": {"duration": "PT1M"},
                }
                for video_id in video_ids
            ]

    client = FakeClient()

    run_pipeline_live(
        client=client,
        now=NOW,
        max_per_query=1,
        query_limit=2,
        min_views=0,
        include_blocked=True,
    )

    assert len(client.searches) == 2
    assert all(params.max_results == 1 for params in client.searches)
