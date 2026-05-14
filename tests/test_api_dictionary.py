"""Tests for GET/PUT /api/dictionary endpoints."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from podedit.server.app import ServeConfig, create_app


def _make_client(tmp_path: Path) -> TestClient:
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    kpi_log = work_dir / "kpi.jsonl"
    cfg = ServeConfig(
        audio_path=None,
        transcript_path=None,
        session_path=None,
        kpi_log_path=kpi_log,
        library_dir=tmp_path,
        work_dir=work_dir,
    )
    app = create_app(cfg)
    return TestClient(app)


def test_get_dictionary_empty(tmp_path: Path):
    client = _make_client(tmp_path)
    r = client.get("/api/dictionary")
    assert r.status_code == 200
    assert r.json() == {"version": 1, "entries": []}


def test_put_dictionary_persists_and_get_returns(tmp_path: Path):
    client = _make_client(tmp_path)
    r = client.put(
        "/api/dictionary",
        json={"entries": [{"from": "黒だ", "to": "クロード"}]},
    )
    assert r.status_code == 200
    got = r.json()
    assert len(got["entries"]) == 1
    assert got["entries"][0]["from"] == "黒だ"
    assert got["entries"][0]["to"] == "クロード"
    assert got["entries"][0]["enabled"] is True
    # Persisted to disk.
    dict_file = (tmp_path / "work" / "dictionary.json")
    assert dict_file.exists()
    payload = json.loads(dict_file.read_text())
    assert payload["entries"][0]["from"] == "黒だ"

    r2 = client.get("/api/dictionary")
    assert r2.status_code == 200
    assert r2.json()["entries"][0]["from"] == "黒だ"


def test_put_dictionary_rejects_non_list(tmp_path: Path):
    client = _make_client(tmp_path)
    r = client.put("/api/dictionary", json={"entries": "not a list"})
    assert r.status_code == 400


def test_put_dictionary_rejects_empty_from(tmp_path: Path):
    client = _make_client(tmp_path)
    r = client.put("/api/dictionary", json={"entries": [{"from": "", "to": "X"}]})
    assert r.status_code == 400


def test_put_dictionary_rejects_missing_from(tmp_path: Path):
    client = _make_client(tmp_path)
    r = client.put("/api/dictionary", json={"entries": [{"to": "X"}]})
    assert r.status_code == 400


def test_put_dictionary_rejects_too_long(tmp_path: Path):
    client = _make_client(tmp_path)
    long = "あ" * 201
    r = client.put("/api/dictionary", json={"entries": [{"from": long, "to": "X"}]})
    assert r.status_code == 400


def test_put_dictionary_replaces_existing(tmp_path: Path):
    client = _make_client(tmp_path)
    client.put("/api/dictionary", json={"entries": [{"from": "A", "to": "a"}]})
    client.put("/api/dictionary", json={"entries": [{"from": "B", "to": "b"}]})
    r = client.get("/api/dictionary")
    entries = r.json()["entries"]
    assert len(entries) == 1
    assert entries[0]["from"] == "B"


def test_put_dictionary_rejects_duplicate_id(tmp_path: Path):
    client = _make_client(tmp_path)
    r = client.put(
        "/api/dictionary",
        json={
            "entries": [
                {"id": "same", "from": "A", "to": "a"},
                {"id": "same", "from": "B", "to": "b"},
            ]
        },
    )
    assert r.status_code == 400


def test_put_dictionary_rejects_invalid_max_conf(tmp_path: Path):
    client = _make_client(tmp_path)
    # In-range invalid values that httpx will serialize without complaint.
    for bad in [-0.1, 1.1]:
        r = client.put(
            "/api/dictionary",
            json={"entries": [{"from": "X", "to": "x", "max_conf": bad}]},
        )
        assert r.status_code == 400, f"expected 400 for max_conf={bad}"
    # NaN / inf need to bypass httpx's strict JSON encoder — send raw text.
    for bad_token in ["Infinity", "-Infinity", "NaN"]:
        raw = '{"entries":[{"from":"X","to":"x","max_conf":%s}]}' % bad_token
        r = client.put(
            "/api/dictionary",
            content=raw,
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 400, f"expected 400 for max_conf={bad_token}"


def test_put_dictionary_accepts_valid_max_conf(tmp_path: Path):
    client = _make_client(tmp_path)
    r = client.put(
        "/api/dictionary",
        json={"entries": [{"from": "X", "to": "x", "max_conf": 0.5}]},
    )
    assert r.status_code == 200
    assert r.json()["entries"][0]["max_conf"] == 0.5
