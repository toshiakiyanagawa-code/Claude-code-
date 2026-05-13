"""タイトル/サムネ生成のテスト."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from clipgen.highlights import Highlight  # noqa: E402
from clipgen.scoring import Candidate  # noqa: E402
from clipgen.titles import generate_thumbnails, generate_titles  # noqa: E402


def _cand(**kw) -> Candidate:
    base = dict(
        video_id="x",
        title="高市早苗 国会答弁 切り抜き",
        channel_id="UC_jimin",
        channel_handle="@jimin",
        channel_title="自民党公式",
        published_at=datetime(2026, 5, 13, 6, 0, tzinfo=timezone.utc),
        duration_sec=58,
        view_count=500_000,
        like_count=20_000,
        comment_count=1_500,
        usage_status="cleared",
    )
    base.update(kw)
    return Candidate(**base)


def test_short_titles_at_each_aggressiveness():
    h = Highlight(start_sec=0, end_sec=50, score=5, keywords=["論破"])
    for level in (0, 1, 2, 3):
        out = generate_titles(_cand(), h, target_format="short", aggressiveness=level)
        assert out, f"empty for level={level}"
        styles = {t.style for t in out}
        assert len(styles) == 1


def test_review_prefix_for_defamation():
    h = Highlight(start_sec=0, end_sec=50, score=5, keywords=["完全論破"])
    c = _cand(risk_flags=["defamation_review_required"], usage_status="manual_review")
    out = generate_titles(c, h, target_format="short", aggressiveness=2)
    assert all(t.text.startswith("[REVIEW] ") for t in out)
    assert all("REVIEW" in t.flags for t in out)


def test_defamation_forces_neutral_or_mild_even_when_aggressive_requested():
    h = Highlight(start_sec=0, end_sec=50, score=5, keywords=["完全論破"])
    c = _cand(risk_flags=["defamation_review_required"], usage_status="manual_review")
    titles = generate_titles(c, h, target_format="short", aggressiveness=3)
    thumbs = generate_thumbnails(c, h, target_format="short", aggressiveness=3)

    assert {t.style for t in titles} <= {"neutral", "mild"}
    assert {t.style for t in thumbs} <= {"neutral", "mild"}
    assert all("FORCED_DOWNGRADE" in t.flags for t in titles)
    assert all("FORCED_DOWNGRADE" in t.flags for t in thumbs)


def test_sensational_flag_is_added_when_generated_text_contains_hot_word():
    h = Highlight(start_sec=0, end_sec=50, score=5, keywords=["完全論破"])
    titles = generate_titles(_cand(), h, target_format="short", aggressiveness=3)
    thumbs = generate_thumbnails(_cand(), h, target_format="short", aggressiveness=3)

    assert any("SENSATIONAL" in t.flags for t in titles)
    assert any("SENSATIONAL" in t.flags for t in thumbs)


def test_state_topic_duplication_is_resolved():
    h = Highlight(start_sec=0, end_sec=50, score=5, keywords=["論破"])
    out = generate_titles(_cand(), h, target_format="short", aggressiveness=2)

    assert all("論破論破" not in t.text for t in out)
    assert all("論破】" not in t.text for t in out if "【" in t.text)


def test_unknown_person_uses_speaker_label():
    h = Highlight(start_sec=0, end_sec=50, score=5, keywords=["注目"])
    c = _cand(title="国会答弁 切り抜き", channel_title="政治チャンネル公式")
    out = generate_titles(c, h, target_format="short", aggressiveness=1)

    assert any("発言者" in t.text for t in out)
    assert all("政治チャンネル" not in t.text for t in out)


def test_long_titles_longer_than_short():
    h = Highlight(start_sec=0, end_sec=120, score=8, keywords=["論破"])
    short = generate_titles(_cand(), h, target_format="short", aggressiveness=2)
    long = generate_titles(_cand(), h, target_format="long", aggressiveness=2)
    # 長尺タイトルは平均長が長い
    avg_short = sum(len(t.text) for t in short) / len(short)
    avg_long = sum(len(t.text) for t in long) / len(long)
    assert avg_long > avg_short


def test_person_name_detected_in_title():
    h = Highlight(start_sec=0, end_sec=50, score=5, keywords=["絶句"])
    out = generate_titles(_cand(), h, target_format="short", aggressiveness=2)
    assert any("高市早苗" in t.text for t in out)


def test_thumbnails_two_lines_with_length_caps():
    h = Highlight(start_sec=0, end_sec=50, score=5, keywords=["論破"])
    short = generate_thumbnails(_cand(), h, target_format="short", aggressiveness=2)
    long = generate_thumbnails(_cand(), h, target_format="long", aggressiveness=2)
    for t in short:
        assert len(t.line1) <= 12
        assert len(t.line2) <= 12
    for t in long:
        assert len(t.line1) <= 18
        assert len(t.line2) <= 18
