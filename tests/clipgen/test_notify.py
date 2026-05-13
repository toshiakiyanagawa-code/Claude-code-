from __future__ import annotations

import sys
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from clipgen import notify  # noqa: E402


def test_build_digest_includes_title_usage_and_highlights():
    message = notify.build_digest(
        [
            {
                "title_candidates": ["Best title"],
                "usage_status": "ok",
                "highlights": [{"summary": "first highlight"}],
            }
        ],
        date="2026-05-13",
    )

    assert "*ClipGen Daily Digest - 2026-05-13*" in message
    assert "*Best title*" in message
    assert "usage_status: `ok`" in message
    assert "first highlight" in message


def test_build_digest_empty_plans_is_safe():
    message = notify.build_digest([], date="2026-05-13")

    assert "Plans: 0" in message
    assert "No plans generated." in message


def test_build_digest_respects_top_n():
    plans = [{"title": f"title {i}", "usage_status": "ok"} for i in range(3)]

    message = notify.build_digest(plans, date="2026-05-13", top_n=2)

    assert "title 0" in message
    assert "title 1" in message
    assert "title 2" not in message
    assert "...and 1 more." in message


def test_post_slack_dry_run_prints_and_returns_true(capsys):
    result = notify.post_slack("https://example.invalid", "hello", dry_run=True)

    assert result is True
    assert "hello" in capsys.readouterr().out


def test_post_slack_http_error_returns_false(monkeypatch):
    def fail(*args, **kwargs):
        raise urllib.error.HTTPError("url", 500, "bad", hdrs=None, fp=None)

    monkeypatch.setattr(notify.urllib.request, "urlopen", fail)

    assert notify.post_slack("https://example.invalid", "hello") is False


def test_post_slack_success(monkeypatch):
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def getcode(self):
            return self.status

    seen = {}

    def fake_urlopen(request, timeout):
        seen["body"] = request.data
        seen["timeout"] = timeout
        return Response()

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)

    assert notify.post_slack("https://example.invalid", "hello", timeout=1.5) is True
    assert b"hello" in seen["body"]
    assert seen["timeout"] == 1.5
