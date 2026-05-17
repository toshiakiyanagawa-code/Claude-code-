from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from clipgen import youtube_client  # noqa: E402


def test_curl_backend_reads_json_without_putting_url_on_command_line(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["input"] = kwargs["input"]
        return subprocess.CompletedProcess(cmd, 0, stdout='{"items":[]}\n200', stderr="")

    monkeypatch.setattr(youtube_client.shutil, "which", lambda name: "curl.exe")
    monkeypatch.setattr(youtube_client.subprocess, "run", fake_run)

    payload = youtube_client._curl_get_json("https://example.test/?key=secret")

    assert payload == {"items": []}
    assert "https://example.test/?key=secret" not in captured["cmd"]
    assert 'url = "https://example.test/?key=secret"' in captured["input"]


def test_http_get_json_uses_curl_backend_on_windows_auto(monkeypatch):
    called = {}

    monkeypatch.setattr(youtube_client.os, "name", "nt")
    monkeypatch.setenv("CLIPGEN_HTTP_BACKEND", "auto")
    monkeypatch.setattr(youtube_client, "_curl_get_json", lambda url: called.setdefault("url", url))

    youtube_client._http_get_json("https://example.test")

    assert called["url"] == "https://example.test"
