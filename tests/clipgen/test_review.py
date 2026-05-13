from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from clipgen import review  # noqa: E402


def test_review_candidates_adds_score_breakdown_and_reason():
    result = review.review_candidates(
        [{"title": "plain", "score": 50}],
        now=datetime(2026, 5, 13, tzinfo=timezone.utc),
    )

    assert result[0]["review_score"] == 50.0
    assert result[0]["reason"] == ["score_below_threshold"]
    assert result[0]["review_required"] is True


def test_review_candidates_detects_defamation_hint():
    result = review.review_candidates([{"title": "詐欺 疑惑を検証", "score": 95}])

    assert "defamation_review_required" in result[0]["reason"]


def test_review_candidates_detects_tv_hint_in_text():
    result = review.review_candidates([{"title": "地上波テレビ出演の裏側", "score": 95}])

    assert "tv_hint_in_text" in result[0]["reason"]


def test_review_candidates_marks_clean_candidate_not_required():
    result = review.review_candidates([{"title": "clean topic", "score": 90}])

    assert result[0]["reason"] == []
    assert result[0]["review_required"] is False


def test_write_json_report(tmp_path):
    path = tmp_path / "review" / "report.json"
    reviewed = [{"title": "x", "review_score": 80, "reason": []}]

    review.write_json_report(path, reviewed)

    assert json.loads(path.read_text(encoding="utf-8")) == reviewed


def test_write_tsv_report(tmp_path):
    path = tmp_path / "review.tsv"
    reviewed = [
        {
            "id": "1",
            "video_id": "v1",
            "title": "Title",
            "review_score": 80,
            "usage_status": "ok",
            "reason": ["tv_hint_in_text"],
            "review_required": True,
        }
    ]

    review.write_tsv_report(path, reviewed)

    text = path.read_text(encoding="utf-8")
    assert text.splitlines()[0] == "id\tvideo_id\turl\ttitle\tchannel_title\tchannel_handle\treview_score\tscore_breakdown_json\tusage_status\treason\treview_required"
    assert "tv_hint_in_text" in text
