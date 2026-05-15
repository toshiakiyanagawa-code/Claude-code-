"""Tests for DELETE /api/library — remove an audio + all derivatives."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from podedit.server.app import ServeConfig, create_app


def _make_setup(tmp_path: Path) -> tuple[TestClient, Path, Path]:
    library_dir = tmp_path / "library"
    library_dir.mkdir()
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    cfg = ServeConfig(
        audio_path=None,
        transcript_path=None,
        session_path=None,
        kpi_log_path=work_dir / "kpi.jsonl",
        library_dir=library_dir,
        work_dir=work_dir,
    )
    return TestClient(create_app(cfg)), library_dir, work_dir


def _seed_audio(library_dir: Path, work_dir: Path, name: str = "ep1.wav") -> str:
    """Drop a fake audio + a full set of derived artifacts on disk."""
    stem = Path(name).stem
    (library_dir / name).write_bytes(b"RIFF\x00\x00\x00\x00WAVE")
    (work_dir / f"{stem}.transcript.json").write_text("{}", encoding="utf-8")
    (work_dir / f"{stem}.transcript.dict-ops.jsonl").write_text("", encoding="utf-8")
    (work_dir / f"{stem}.session.json").write_text("{}", encoding="utf-8")
    (work_dir / f"{stem}.kpi.jsonl").write_text("", encoding="utf-8")
    (work_dir / "_podedit_asr").mkdir(exist_ok=True)
    (work_dir / "_podedit_asr" / f"{stem}.16k.wav").write_bytes(b"")
    for suffix in (".m4a", ".mp4", ".mov"):
        (work_dir / f"{stem}.faststart{suffix}").write_bytes(b"")
    snap_dir = work_dir / f"{stem}.snapshots"
    snap_dir.mkdir()
    (snap_dir / "draft1.json").write_text("{}", encoding="utf-8")
    return name


def test_delete_removes_audio_and_all_derivatives(tmp_path: Path):
    client, library_dir, work_dir = _make_setup(tmp_path)
    name = _seed_audio(library_dir, work_dir)
    stem = Path(name).stem

    r = client.request("DELETE", "/api/library", json={"name": name})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["audio_existed"] is True
    assert body["deleted_count"] >= 7  # at least 6 files + 1 dir

    # All paths gone.
    assert not (library_dir / name).exists()
    assert not (work_dir / f"{stem}.transcript.json").exists()
    assert not (work_dir / f"{stem}.transcript.dict-ops.jsonl").exists()
    assert not (work_dir / f"{stem}.session.json").exists()
    assert not (work_dir / f"{stem}.kpi.jsonl").exists()
    assert not (work_dir / "_podedit_asr" / f"{stem}.16k.wav").exists()
    assert not (work_dir / f"{stem}.faststart.m4a").exists()
    assert not (work_dir / f"{stem}.faststart.mp4").exists()
    assert not (work_dir / f"{stem}.faststart.mov").exists()
    assert not (work_dir / f"{stem}.snapshots").exists()


def test_delete_is_idempotent_for_already_missing(tmp_path: Path):
    """Deleting an audio that doesn't exist returns 200 (idempotent DELETE)."""
    client, library_dir, _ = _make_setup(tmp_path)
    r = client.request("DELETE", "/api/library", json={"name": "ghost.wav"})
    assert r.status_code == 200
    body = r.json()
    assert body["audio_existed"] is False
    assert body["deleted_count"] == 0


def test_delete_rejects_traversal_name(tmp_path: Path):
    client, _, _ = _make_setup(tmp_path)
    for bad in ["../etc.wav", "a/b.wav", "a\\b.wav", ".hidden.wav", ""]:
        r = client.request("DELETE", "/api/library", json={"name": bad})
        assert r.status_code == 400, f"expected 400 for name={bad!r}"


def test_delete_rejects_non_string_name(tmp_path: Path):
    client, _, _ = _make_setup(tmp_path)
    r = client.request("DELETE", "/api/library", json={"name": 123})
    assert r.status_code == 400


def test_delete_rejects_unsupported_suffix(tmp_path: Path):
    client, _, _ = _make_setup(tmp_path)
    r = client.request("DELETE", "/api/library", json={"name": "evil.exe"})
    assert r.status_code == 400


def test_delete_partial_only_removes_what_exists(tmp_path: Path):
    """If only the audio exists (no transcripts), DELETE still succeeds."""
    client, library_dir, work_dir = _make_setup(tmp_path)
    (library_dir / "lonely.wav").write_bytes(b"")

    r = client.request("DELETE", "/api/library", json={"name": "lonely.wav"})
    assert r.status_code == 200
    body = r.json()
    assert body["audio_existed"] is True
    assert body["deleted_count"] == 1  # only the audio file
    assert not (library_dir / "lonely.wav").exists()


def test_delete_logs_kpi_event(tmp_path: Path):
    client, library_dir, work_dir = _make_setup(tmp_path)
    _seed_audio(library_dir, work_dir, "ep2.wav")

    client.request("DELETE", "/api/library", json={"name": "ep2.wav"})

    kpi_log = work_dir / "kpi.jsonl"
    assert kpi_log.exists()
    events = [json.loads(line) for line in kpi_log.read_text(encoding="utf-8").splitlines() if line]
    delete_events = [e for e in events if e.get("type") == "server.library.deleted"]
    assert len(delete_events) == 1
    assert delete_events[0]["name"] == "ep2.wav"
    assert delete_events[0]["audio_existed"] is True


def test_delete_via_path_works(tmp_path: Path):
    """Path-based deletion also works (used when audio is in a subdir)."""
    client, library_dir, work_dir = _make_setup(tmp_path)
    _seed_audio(library_dir, work_dir, "ep3.wav")
    target = library_dir / "ep3.wav"

    r = client.request("DELETE", "/api/library", json={"path": str(target)})
    assert r.status_code == 200
    assert not target.exists()


def test_delete_rejects_active_audio(tmp_path: Path, monkeypatch):
    """Currently-loaded audio cannot be deleted (409).

    We can't ``load_active`` a fake wav without ffprobe accepting it, so we
    mark the audio as active by directly setting ``state.audio_path``. The
    state object lives in the route handler's closure — we reach it by
    walking the app's routes for one of our handlers and reading its
    ``__closure__``.
    """
    client, library_dir, work_dir = _make_setup(tmp_path)
    audio_path = library_dir / "ep4.wav"
    audio_path.write_bytes(b"")

    # Find the closure-captured ``state`` via any route handler.
    app = client.app
    state = None
    for route in app.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None:
            continue
        closure = getattr(endpoint, "__closure__", None) or ()
        for cell in closure:
            value = cell.cell_contents
            if hasattr(value, "audio_path") and hasattr(value, "library_dir"):
                state = value
                break
        if state is not None:
            break
    assert state is not None, "could not locate ServeState"
    state.audio_path = audio_path.resolve()

    r = client.request("DELETE", "/api/library", json={"name": "ep4.wav"})
    assert r.status_code == 409
    assert "currently loaded" in r.text


def test_delete_tolerates_missing_active_audio_path(tmp_path: Path):
    """If state.audio_path points at a vanished file, resolve() FileNotFoundError
    is caught and the unrelated delete still succeeds."""
    client, library_dir, work_dir = _make_setup(tmp_path)
    (library_dir / "target.wav").write_bytes(b"")

    # Reach into state via closure (same trick as the active-audio test).
    state = None
    for route in client.app.routes:
        endpoint = getattr(route, "endpoint", None)
        for cell in getattr(endpoint, "__closure__", None) or ():
            value = cell.cell_contents
            if hasattr(value, "audio_path") and hasattr(value, "library_dir"):
                state = value
                break
        if state is not None:
            break
    assert state is not None
    state.audio_path = library_dir / "already-gone.wav"

    r = client.request("DELETE", "/api/library", json={"name": "target.wav"})
    assert r.status_code == 200


def test_delete_does_not_touch_other_files(tmp_path: Path):
    """Removing ep_a.wav must not touch ep_b.wav and its derivatives."""
    client, library_dir, work_dir = _make_setup(tmp_path)
    _seed_audio(library_dir, work_dir, "ep_a.wav")
    _seed_audio(library_dir, work_dir, "ep_b.wav")

    r = client.request("DELETE", "/api/library", json={"name": "ep_a.wav"})
    assert r.status_code == 200

    # ep_b artifacts intact.
    assert (library_dir / "ep_b.wav").exists()
    assert (work_dir / "ep_b.transcript.json").exists()
    assert (work_dir / "ep_b.session.json").exists()
    assert (work_dir / "ep_b.snapshots" / "draft1.json").exists()
    assert (work_dir / "_podedit_asr" / "ep_b.16k.wav").exists()
