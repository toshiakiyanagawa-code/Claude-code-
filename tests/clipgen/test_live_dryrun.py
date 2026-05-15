"""live --dry-run 経路のテスト (fixtures 経由)."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from clipgen.live_fixtures import run_pipeline_dryrun  # noqa: E402


NOW = datetime(2026, 5, 13, 12, 0, tzinfo=timezone.utc)


def test_dryrun_returns_candidates_and_blocks_tv():
    cands = run_pipeline_dryrun(now=NOW, include_blocked=True)
    assert cands, "no candidates returned"
    assert any(c.video_id == "fx001" and c.usage_status == "cleared" for c in cands)
    assert any(c.video_id == "fx003" and c.usage_status == "blocked" for c in cands)


def test_dryrun_long_format_prefers_long_videos():
    short = run_pipeline_dryrun(now=NOW, target_format="short")
    long = run_pipeline_dryrun(now=NOW, target_format="long")
    # short では fx004 (2時間中継) は score 低め、long では duration_fit でブースト
    short_score = next((c.score for c in short if c.video_id == "fx004"), 0.0)
    long_score = next((c.score for c in long if c.video_id == "fx004"), 0.0)
    assert long_score > short_score
