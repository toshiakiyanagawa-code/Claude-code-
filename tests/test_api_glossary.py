"""Tests for GET/PUT /api/glossary endpoints."""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from podedit.server.app import ServeConfig, create_app


def _make_client(tmp_path: Path) -> TestClient:
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    cfg = ServeConfig(
        audio_path=None,
        transcript_path=None,
        session_path=None,
        kpi_log_path=work_dir / "kpi.jsonl",
        library_dir=tmp_path,
        work_dir=work_dir,
    )
    return TestClient(create_app(cfg))


def test_get_glossary_empty(tmp_path: Path):
    r = _make_client(tmp_path).get("/api/glossary")
    assert r.status_code == 200
    assert r.json() == {"version": 1, "terms": []}


def test_put_glossary_persists(tmp_path: Path):
    client = _make_client(tmp_path)
    r = client.put("/api/glossary", json={"terms": ["クロード", "Anthropic"]})
    assert r.status_code == 200
    assert r.json()["terms"] == ["クロード", "Anthropic"]
    # File written.
    p = tmp_path / "work" / "glossary.txt"
    assert p.exists()
    assert p.read_text(encoding="utf-8").splitlines() == ["クロード", "Anthropic"]
    # Round-trip via GET.
    r2 = client.get("/api/glossary")
    assert r2.json()["terms"] == ["クロード", "Anthropic"]


def test_put_glossary_dedupes_and_strips(tmp_path: Path):
    client = _make_client(tmp_path)
    r = client.put(
        "/api/glossary",
        json={"terms": ["クロード", "", "  ", "クロード", "Anthropic"]},
    )
    assert r.status_code == 200
    assert r.json()["terms"] == ["クロード", "Anthropic"]


def test_put_glossary_rejects_non_list(tmp_path: Path):
    r = _make_client(tmp_path).put("/api/glossary", json={"terms": "クロード"})
    assert r.status_code == 400


def test_put_glossary_rejects_non_string_term(tmp_path: Path):
    r = _make_client(tmp_path).put("/api/glossary", json={"terms": [123]})
    assert r.status_code == 400


def test_put_glossary_rejects_too_many(tmp_path: Path):
    r = _make_client(tmp_path).put(
        "/api/glossary", json={"terms": ["a"] * 2001}
    )
    assert r.status_code == 400


def test_put_glossary_rejects_embedded_newlines(tmp_path: Path):
    """A term with `\\n` would silently split into two on the next GET — reject up front."""
    client = _make_client(tmp_path)
    r = client.put("/api/glossary", json={"terms": ["A\nB"]})
    assert r.status_code == 400
    r2 = client.put("/api/glossary", json={"terms": ["A\rB"]})
    assert r2.status_code == 400


def test_put_glossary_rejects_unknown_version(tmp_path: Path):
    r = _make_client(tmp_path).put(
        "/api/glossary", json={"version": 999, "terms": ["A"]}
    )
    assert r.status_code == 400


def test_put_glossary_accepts_matching_version(tmp_path: Path):
    r = _make_client(tmp_path).put(
        "/api/glossary", json={"version": 1, "terms": ["A"]}
    )
    assert r.status_code == 200
