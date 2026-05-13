"""Tests for the chunked upload protocol introduced in W12.1.

The single-shot multipart upload was replaced by a 3-stage protocol so that
each chunk fits under the Codespaces forwarded-port nginx body limit:

    POST /api/library/uploads                  -> upload_id
    PUT  /api/library/uploads/{id}             with Content-Range  (repeat)
    POST /api/library/uploads/{id}/commit      atomic rename
    DELETE /api/library/uploads/{id}           cancel + cleanup

These tests cover the happy path plus the error paths the client relies on:
out-of-order chunk -> 409, incomplete commit -> 409, bad Content-Range -> 400,
chunk size cap -> 413, oversize init -> 413, name-collision-at-init -> 409.

Uses ``with TestClient(app)`` so startup/shutdown lifespans run, and tests
involving concurrency drive parallel uploads through a thread pool to verify
the dict + per-session locks isolate sessions correctly.
"""
from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from podedit.server.app import (
    UPLOAD_CHUNK_SIZE_MAX,
    ServeConfig,
    create_app,
)


@pytest.fixture
def app_client(tmp_path: Path):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    library_dir = tmp_path / "library"
    library_dir.mkdir()
    config = ServeConfig(
        audio_path=None,
        transcript_path=None,
        session_path=None,
        kpi_log_path=work_dir / "kpi.jsonl",
        library_dir=library_dir,
        work_dir=work_dir,
    )
    with TestClient(create_app(config)) as client:
        yield client, work_dir


def _chunked_put(client: TestClient, upload_id: str, payload: bytes, *, chunk_size: int) -> None:
    total = len(payload)
    offset = 0
    while offset < total:
        end = min(offset + chunk_size, total)
        body = payload[offset:end]
        r = client.put(
            f"/api/library/uploads/{upload_id}",
            content=body,
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Range": f"bytes {offset}-{end - 1}/{total}",
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["received_bytes"] == end
        offset = end


def test_chunked_upload_roundtrip(app_client) -> None:
    client, work_dir = app_client
    payload = os.urandom(3 * 1024 * 1024 + 17)  # exercises a non-aligned final chunk

    init = client.post(
        "/api/library/uploads",
        json={"filename": "episode.m4a", "total_size": len(payload)},
    )
    assert init.status_code == 200, init.text
    init_reply = init.json()
    assert init_reply["basename"] == "episode.m4a"
    upload_id = init_reply["upload_id"]
    assert init_reply["chunk_size_hint"] > 0

    _chunked_put(client, upload_id, payload, chunk_size=1024 * 1024)

    commit = client.post(f"/api/library/uploads/{upload_id}/commit")
    assert commit.status_code == 200, commit.text
    reply = commit.json()
    assert reply["bytes"] == len(payload)
    assert reply["basename"] == "episode.m4a"
    final = work_dir / "uploads" / "episode.m4a"
    assert final.read_bytes() == payload

    # Session is gone after commit.
    assert client.get(f"/api/library/uploads/{upload_id}").status_code == 404
    leftovers = [p.name for p in (work_dir / "uploads").iterdir() if p.name.startswith(".upload-")]
    assert leftovers == []


def test_init_rejects_oversize(app_client) -> None:
    client, _ = app_client
    too_big = 500 * 1024 * 1024 + 1
    r = client.post(
        "/api/library/uploads",
        json={"filename": "big.wav", "total_size": too_big},
    )
    assert r.status_code == 413
    assert "500" in r.json()["detail"]


def test_init_rejects_hidden_or_path_traversal(app_client) -> None:
    client, _ = app_client
    for bad in (".hidden.m4a", "foo/bar.m4a", "../escape.m4a", ".."):
        r = client.post(
            "/api/library/uploads",
            json={"filename": bad, "total_size": 100},
        )
        assert r.status_code == 400, (bad, r.text)


def test_init_rejects_unsupported_suffix(app_client) -> None:
    client, _ = app_client
    r = client.post(
        "/api/library/uploads",
        json={"filename": "evil.exe", "total_size": 100},
    )
    assert r.status_code == 400
    assert "suffix" in r.json()["detail"]


def test_init_fails_fast_on_name_collision(app_client) -> None:
    """Codex review (W12.1) — fail at init instead of after a 500 MB stream."""
    client, work_dir = app_client
    uploads = work_dir / "uploads"
    uploads.mkdir()
    (uploads / "dup.wav").write_bytes(b"existing")
    r = client.post(
        "/api/library/uploads",
        json={"filename": "dup.wav", "total_size": 100},
    )
    assert r.status_code == 409
    assert "already exists" in r.json()["detail"]


def test_commit_collision_when_final_race_created(app_client) -> None:
    """init wins the existence check, then something else races to create the
    final file before commit. Commit must 409 AND clean up the temp + session.

    Models the case where a sibling process / external tool drops the same
    filename into uploads/ between our init and our commit.
    """
    client, work_dir = app_client
    uploads = work_dir / "uploads"
    init = client.post(
        "/api/library/uploads",
        json={"filename": "race.wav", "total_size": 4},
    )
    assert init.status_code == 200, init.text
    upload_id = init.json()["upload_id"]
    client.put(
        f"/api/library/uploads/{upload_id}",
        content=b"abcd",
        headers={"Content-Range": "bytes 0-3/4"},
    )

    # Simulate the race: drop the final file just before commit.
    (uploads / "race.wav").write_bytes(b"raced-in")

    r = client.post(f"/api/library/uploads/{upload_id}/commit")
    assert r.status_code == 409
    assert "already exists" in r.json()["detail"]

    # The raced-in file is untouched.
    assert (uploads / "race.wav").read_bytes() == b"raced-in"
    # The session is gone (committed handler popped it).
    assert client.get(f"/api/library/uploads/{upload_id}").status_code == 404
    # The temp file is cleaned up.
    leftovers = [p.name for p in uploads.iterdir() if p.name.startswith(".upload-")]
    assert leftovers == []


def test_chunk_out_of_order_returns_409_with_state(app_client) -> None:
    client, _ = app_client
    payload = b"abcdefghij" * 100  # 1000 bytes
    init = client.post(
        "/api/library/uploads",
        json={"filename": "x.wav", "total_size": len(payload)},
    )
    upload_id = init.json()["upload_id"]

    # Skip the first chunk and try to PUT bytes 100..199.
    skip_start = 100
    skip_end = 199
    r = client.put(
        f"/api/library/uploads/{upload_id}",
        content=payload[skip_start : skip_end + 1],
        headers={"Content-Range": f"bytes {skip_start}-{skip_end}/{len(payload)}"},
    )
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "out_of_order"
    assert body["expected_start"] == 0
    assert body["received_bytes"] == 0


def test_chunk_total_mismatch_returns_409(app_client) -> None:
    client, _ = app_client
    init = client.post(
        "/api/library/uploads",
        json={"filename": "x.wav", "total_size": 100},
    )
    upload_id = init.json()["upload_id"]
    r = client.put(
        f"/api/library/uploads/{upload_id}",
        content=b"x" * 10,
        headers={"Content-Range": "bytes 0-9/200"},  # lies about total
    )
    assert r.status_code == 409
    assert r.json()["error"] == "total_size_mismatch"


def test_chunk_body_length_mismatch_returns_400(app_client) -> None:
    client, _ = app_client
    init = client.post(
        "/api/library/uploads",
        json={"filename": "x.wav", "total_size": 100},
    )
    upload_id = init.json()["upload_id"]
    # Claim bytes 0-9 (10 bytes) but send only 5. httpx will set Content-Length
    # to 5, so the server's CL-vs-range check trips first (still 400).
    r = client.put(
        f"/api/library/uploads/{upload_id}",
        content=b"hello",
        headers={"Content-Range": "bytes 0-9/100"},
    )
    assert r.status_code == 400


def test_chunk_invalid_content_range_returns_400(app_client) -> None:
    client, _ = app_client
    init = client.post(
        "/api/library/uploads",
        json={"filename": "x.wav", "total_size": 100},
    )
    upload_id = init.json()["upload_id"]
    for bad in ("", "0-9/100", "bytes 0-9", "bytes garbage"):
        r = client.put(
            f"/api/library/uploads/{upload_id}",
            content=b"x" * 10,
            headers={"Content-Range": bad},
        )
        assert r.status_code == 400, (bad, r.text)


def test_chunk_size_cap_rejected_with_413(app_client) -> None:
    """Codex review (W12.1) — the server must reject an oversized chunk BEFORE
    buffering it, regardless of how generous the client tries to be."""
    client, _ = app_client
    total = UPLOAD_CHUNK_SIZE_MAX + 1
    init = client.post(
        "/api/library/uploads",
        json={"filename": "x.wav", "total_size": total},
    )
    upload_id = init.json()["upload_id"]
    # Don't actually send the body — Content-Range claim alone should trip
    # the 413, before request.body() buffers anything.
    r = client.put(
        f"/api/library/uploads/{upload_id}",
        content=b"",  # body length 0
        headers={"Content-Range": f"bytes 0-{total - 1}/{total}"},
    )
    assert r.status_code == 413
    assert "exceeds" in r.json()["detail"]


def test_commit_incomplete_returns_409(app_client) -> None:
    client, _ = app_client
    init = client.post(
        "/api/library/uploads",
        json={"filename": "x.wav", "total_size": 100},
    )
    upload_id = init.json()["upload_id"]
    client.put(
        f"/api/library/uploads/{upload_id}",
        content=b"x" * 50,
        headers={"Content-Range": "bytes 0-49/100"},
    )
    r = client.post(f"/api/library/uploads/{upload_id}/commit")
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "incomplete"
    assert body["received_bytes"] == 50
    assert body["total_size"] == 100


def test_cancel_removes_session_and_tempfile(app_client) -> None:
    client, work_dir = app_client
    init = client.post(
        "/api/library/uploads",
        json={"filename": "x.wav", "total_size": 1000},
    )
    upload_id = init.json()["upload_id"]
    client.put(
        f"/api/library/uploads/{upload_id}",
        content=b"x" * 100,
        headers={"Content-Range": "bytes 0-99/1000"},
    )
    parts = list((work_dir / "uploads").glob(".upload-*.part"))
    assert len(parts) == 1

    r = client.delete(f"/api/library/uploads/{upload_id}")
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"

    # Idempotent: second cancel returns ok+not_found.
    r2 = client.delete(f"/api/library/uploads/{upload_id}")
    assert r2.status_code == 200
    assert r2.json()["status"] == "not_found"

    assert client.get(f"/api/library/uploads/{upload_id}").status_code == 404
    assert list((work_dir / "uploads").glob(".upload-*.part")) == []


def test_status_endpoint_reflects_progress(app_client) -> None:
    client, _ = app_client
    init = client.post(
        "/api/library/uploads",
        json={"filename": "x.wav", "total_size": 1000},
    )
    upload_id = init.json()["upload_id"]
    client.put(
        f"/api/library/uploads/{upload_id}",
        content=b"x" * 300,
        headers={"Content-Range": "bytes 0-299/1000"},
    )
    r = client.get(f"/api/library/uploads/{upload_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["received_bytes"] == 300
    assert body["total_size"] == 1000


def test_zero_byte_file_init_and_commit(app_client) -> None:
    """Zero-byte uploads should be allowed (init succeeds, commit immediately)."""
    client, work_dir = app_client
    init = client.post(
        "/api/library/uploads",
        json={"filename": "empty.wav", "total_size": 0},
    )
    assert init.status_code == 200
    upload_id = init.json()["upload_id"]
    r = client.post(f"/api/library/uploads/{upload_id}/commit")
    assert r.status_code == 200
    assert r.json()["bytes"] == 0
    final = work_dir / "uploads" / "empty.wav"
    assert final.exists()
    assert final.read_bytes() == b""


def test_parallel_sessions_do_not_interfere(app_client) -> None:
    """Different upload_ids in flight at once don't cross-write each other's bytes."""
    client, work_dir = app_client
    a_payload = b"A" * 200
    b_payload = b"B" * 200
    init_a = client.post(
        "/api/library/uploads",
        json={"filename": "a.wav", "total_size": len(a_payload)},
    )
    init_b = client.post(
        "/api/library/uploads",
        json={"filename": "b.wav", "total_size": len(b_payload)},
    )
    aid = init_a.json()["upload_id"]
    bid = init_b.json()["upload_id"]
    assert aid != bid

    client.put(
        f"/api/library/uploads/{aid}",
        content=a_payload[:100],
        headers={"Content-Range": f"bytes 0-99/{len(a_payload)}"},
    )
    client.put(
        f"/api/library/uploads/{bid}",
        content=b_payload[:100],
        headers={"Content-Range": f"bytes 0-99/{len(b_payload)}"},
    )
    client.put(
        f"/api/library/uploads/{aid}",
        content=a_payload[100:],
        headers={"Content-Range": f"bytes 100-199/{len(a_payload)}"},
    )
    client.put(
        f"/api/library/uploads/{bid}",
        content=b_payload[100:],
        headers={"Content-Range": f"bytes 100-199/{len(b_payload)}"},
    )

    assert client.post(f"/api/library/uploads/{aid}/commit").status_code == 200
    assert client.post(f"/api/library/uploads/{bid}/commit").status_code == 200
    assert (work_dir / "uploads" / "a.wav").read_bytes() == a_payload
    assert (work_dir / "uploads" / "b.wav").read_bytes() == b_payload


def test_concurrent_uploads_through_thread_pool(app_client) -> None:
    """Codex review (W12.1) — exercise the dict + per-session locks under
    real parallel pressure (TestClient is thread-safe; we fire N uploads
    from a pool and verify all complete with the right bytes)."""
    client, work_dir = app_client

    def _upload(name: str, content: bytes) -> dict:
        init = client.post(
            "/api/library/uploads",
            json={"filename": name, "total_size": len(content)},
        )
        assert init.status_code == 200, init.text
        uid = init.json()["upload_id"]
        # One chunk per file for simplicity; we're testing inter-session
        # isolation, not intra-session resumption.
        r = client.put(
            f"/api/library/uploads/{uid}",
            content=content,
            headers={"Content-Range": f"bytes 0-{len(content) - 1}/{len(content)}"},
        )
        assert r.status_code == 200, r.text
        r = client.post(f"/api/library/uploads/{uid}/commit")
        assert r.status_code == 200, r.text
        return r.json()

    files = [(f"par_{i}.wav", os.urandom(100 * 1024)) for i in range(6)]
    with ThreadPoolExecutor(max_workers=6) as ex:
        replies = list(ex.map(lambda args: _upload(*args), files))

    assert len({r["basename"] for r in replies}) == len(files)
    for (name, content), reply in zip(files, replies):
        assert reply["basename"] == name
        assert reply["bytes"] == len(content)
        assert (work_dir / "uploads" / name).read_bytes() == content


def test_chunk_after_commit_returns_404(app_client) -> None:
    """After commit, the session is gone — additional chunks must 404."""
    client, _ = app_client
    init = client.post(
        "/api/library/uploads",
        json={"filename": "x.wav", "total_size": 4},
    )
    upload_id = init.json()["upload_id"]
    client.put(
        f"/api/library/uploads/{upload_id}",
        content=b"abcd",
        headers={"Content-Range": "bytes 0-3/4"},
    )
    client.post(f"/api/library/uploads/{upload_id}/commit")
    r = client.put(
        f"/api/library/uploads/{upload_id}",
        content=b"e",
        headers={"Content-Range": "bytes 4-4/4"},
    )
    assert r.status_code == 404


def test_old_multipart_endpoint_is_removed(app_client) -> None:
    """The single-shot /api/library/upload no longer exists.

    Its absence prevents accidental reintroduction. If you intentionally bring
    it back as a shim, update this test.
    """
    client, _ = app_client
    r = client.post(
        "/api/library/upload",
        files={"file": ("x.m4a", b"hello", "audio/m4a")},
    )
    assert r.status_code in (404, 405)
