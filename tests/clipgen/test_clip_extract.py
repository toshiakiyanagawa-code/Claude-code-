from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from clipgen.clip_extract import (  # noqa: E402
    build_yt_dlp_command,
    plan_to_extract,
    write_extract_plan,
)


def _plan(**overrides):
    plan = {
        "video_id": "abc123",
        "url": "https://www.youtube.com/watch?v=abc123",
        "usage_status": "cleared",
        "permission_scope": "clip extraction",
        "permission_reason": "primary source",
        "target_format": "short",
        "highlight_status": "ok",
        "highlights": [
            {"start_sec": 10.0, "end_sec": 20.0, "score": 0.9},
        ],
    }
    plan.update(overrides)
    return plan


def test_blocked_plan_writes_blocked_notice_and_manifest_only(tmp_path):
    extract = plan_to_extract(
        _plan(
            usage_status="blocked",
            permission_scope="no reuse",
            permission_reason="not allowed",
            blocked_reason="rights unavailable",
        ),
        output_root=tmp_path,
    )

    assert extract.blocked_reason == "rights unavailable"
    assert extract.download_cmd == ""
    assert extract.cut_cmds == []
    assert extract.combine_cmd is None
    assert extract.manifest["usage_status"] == "blocked"
    assert extract.manifest["permission_scope"] == "no reuse"
    assert extract.manifest["permission_reason"] == "not allowed"
    assert extract.manifest["blocked_reason"] == "rights unavailable"

    out_dir = write_extract_plan(extract, tmp_path)

    assert (out_dir / "BLOCKED_NOTICE.txt").exists()
    assert (out_dir / "manifest.json").exists()
    assert not (out_dir / "download.sh").exists()
    assert not (out_dir / "cut.sh").exists()
    assert not (out_dir / "combine.sh").exists()

    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["manifest"]["usage_status"] == "blocked"
    assert manifest["manifest"]["permission_scope"] == "no reuse"
    assert manifest["manifest"]["permission_reason"] == "not allowed"
    assert manifest["manifest"]["blocked_reason"] == "rights unavailable"


def test_short_plan_with_one_highlight_builds_one_cut(tmp_path):
    extract = plan_to_extract(_plan(target_format="short"), output_root=tmp_path)

    assert len(extract.cut_cmds) == 1
    assert "ffmpeg" in extract.cut_cmds[0]
    assert extract.combine_cmd is None


def test_long_plan_with_three_highlights_builds_three_cuts_and_concat(tmp_path):
    highlights = [
        {"start_sec": 1.0, "end_sec": 5.0, "score": 0.9},
        {"start_sec": 10.0, "end_sec": 15.0, "score": 0.8},
        {"start_sec": 20.0, "end_sec": 25.0, "score": 0.7},
    ]
    extract = plan_to_extract(
        _plan(target_format="long", highlights=highlights),
        output_root=tmp_path,
    )

    assert len(extract.cut_cmds) == 3
    assert extract.combine_cmd is not None
    assert "concat" in extract.combine_cmd


def test_long_plan_writes_concat_with_parts_prefix(tmp_path):
    highlights = [
        {"start_sec": 1.0, "end_sec": 5.0, "score": 0.9},
        {"start_sec": 10.0, "end_sec": 15.0, "score": 0.8},
    ]
    extract = plan_to_extract(
        _plan(target_format="long", highlights=highlights),
        output_root=tmp_path,
    )

    out_dir = write_extract_plan(extract, tmp_path)

    assert (out_dir / "concat.txt").read_text(encoding="utf-8") == (
        "file parts/part_001.mp4\nfile parts/part_002.mp4"
    )


def test_empty_highlights_sets_no_highlight_manifest(tmp_path):
    extract = plan_to_extract(
        _plan(highlights=[], highlight_status="no_highlight"),
        output_root=tmp_path,
    )

    assert extract.cut_cmds == []
    assert extract.manifest["highlight_status"] == "no_highlight"


@pytest.mark.parametrize("video_id", ["../etc/passwd", "a/b"])
def test_invalid_video_id_raises_value_error(tmp_path, video_id):
    with pytest.raises(ValueError):
        plan_to_extract(_plan(video_id=video_id), output_root=tmp_path)


@pytest.mark.parametrize("video_id", ["abc123", "ABC_123-xyz", "a", "a" * 32])
def test_valid_video_id_passes(tmp_path, video_id):
    extract = plan_to_extract(_plan(video_id=video_id), output_root=tmp_path)

    assert extract.video_id == video_id


def test_invalid_target_format_raises_value_error(tmp_path):
    with pytest.raises(ValueError):
        plan_to_extract(_plan(target_format="medium"), output_root=tmp_path)


def test_highlight_with_start_at_or_after_end_raises_value_error(tmp_path):
    with pytest.raises(ValueError):
        plan_to_extract(
            _plan(highlights=[{"start_sec": 20.0, "end_sec": 20.0, "score": 0.9}]),
            output_root=tmp_path,
        )

    with pytest.raises(ValueError):
        plan_to_extract(
            _plan(highlights=[{"start_sec": 21.0, "end_sec": 20.0, "score": 0.9}]),
            output_root=tmp_path,
        )


@pytest.mark.parametrize("usage_status", ["cleared", "manual_review"])
def test_cleared_and_manual_review_both_generate_download_and_cut(tmp_path, usage_status):
    extract = plan_to_extract(_plan(usage_status=usage_status), output_root=tmp_path)

    out_dir = write_extract_plan(extract, tmp_path)

    assert extract.manifest["usage_status"] == usage_status
    assert (out_dir / "download.sh").exists()
    assert (out_dir / "cut.sh").exists()
    assert extract.download_cmd
    assert extract.cut_cmds


def test_missing_usage_status_falls_back_to_manual_review(tmp_path):
    plan = _plan()
    plan.pop("usage_status")

    extract = plan_to_extract(plan, output_root=tmp_path)

    assert extract.manifest["usage_status"] == "manual_review"


def test_unknown_usage_status_falls_back_to_manual_review(tmp_path):
    extract = plan_to_extract(_plan(usage_status="allowed"), output_root=tmp_path)

    assert extract.manifest["usage_status"] == "manual_review"


def test_build_yt_dlp_command_includes_video_id_in_url():
    cmd = build_yt_dlp_command("abc123", Path("out/source.mp4"))

    assert "https://www.youtube.com/watch?v=abc123" in cmd


def test_shell_metacharacters_are_quoted_for_video_id_and_path():
    cmd = build_yt_dlp_command("abc;rm -rf /", Path("out dir/source file.mp4"))

    assert "'https://www.youtube.com/watch?v=abc;rm -rf /'" in cmd
    assert "'out dir/source file.mp4'" in cmd
