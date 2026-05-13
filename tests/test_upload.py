"""Tests for the chunked upload protocol introduced in W12.1.

The single-shot multipart upload was replaced by a 3-stage protocol so that
each chunk fits under the Codespaces forwarded-port nginx body limit:

    POST /api/library/uploads                  -> upload_id
    PUT  /api/library/uploads/{id}             with Content-Range  (repeat)
    POST /api/library/uploads/{id}/commit      atomic rename
    DELETE /api/library/uploads/{id}           cancel + cleanup

These tests cover the happy path plus the error paths the client relies on:
out-of-order chunk -> 409, incomplete commit -> 409, bad Content-Range -> 400,
oversize init -> 413, name collision -> 409 with the temp cleaned up.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from podedit.server.app import ServeConfig, create_app


def _make_client(tmp_path: Path) -> tuple[TestClient, Path]:
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
    return TestClient(create_app(config)), work_dir


def _chunked_put(client: TestClient, upload_id: str, payload: bytes, *, chunk_size: int):
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


def test_chunked_upload_roundtrip(tmp_path: Path) -> None:
    client, work_dir = _make_client(tmp_path)
    payload = os.urandom(3 * 1024 * 1024 + 17)  # 3 MiB + a few bytes — exercises a non-aligned final chunk

    init = client.post(
        "/api/library/uploads",
        json={"filename": "episode.m4a", "total_size": len(payload)},
    )
    assert init.status_code == 200, init.text
    init_reply = init.json()
    assert init_reply["basename"] == "episode.m4a"
    upload_id = init_reply["upload_id"]
    chunk_size = init_reply["chunk_size_hint"]
    assert chunk_size > 0

    _chunked_put(client, upload_id, payload, chunk_size=1024 * 1024)  # 1 MiB per chunk

    commit = client.post(f"/api/library/uploads/{upload_id}/commit")
    assert commit.status_code == 200, commit.text
    reply = commit.json()
    assert reply["bytes"] == len(payload)
    assert reply["basename"] == "episode.m4a"
    final = work_dir / "uploads" / "episode.m4a"
    assert final.read_bytes() == payload

    # Session must be gone after commit.
    poll = client.get(f"/api/library/uploads/{upload_id}")
    assert poll.status_code == 404

    # And no .part residue under uploads/.
    leftovers = [p.name for p in (work_dir / "uploads").iterdir() if p.name.startswith(".upload-")]
    assert leftovers == []


def test_init_rejects_oversize(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)
    too_big = 500 * 1024 * 1024 + 1
    r = client.post(
        "/api/library/uploads",
        json={"filename": "big.wav", "total_size": too_big},
    )
    assert r.status_code == 413
    assert "500" in r.json()["detail"]


def test_init_rejects_hidden_or_path_traversal(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)
    for bad in (".hidden.m4a", "foo/bar.m4a", "../escape.m4a", ".."):
        r = client.post(
            "/api/library/uploads",
            json={"filename": bad, "total_size": 100},
        )
        assert r.status_code == 400, (bad, r.text)


def test_init_rejects_unsupported_suffix(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)
    r = client.post(
        "/api/library/uploads",
        json={"filename": "evil.exe", "total_size": 100},
    )
    assert r.status_code == 400
    assert "suffix" in r.json()["detail"]


def test_chunk_out_of_order_returns_409_with_state(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)
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


def test_chunk_total_mismatch_returns_409(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)
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


def test_chunk_body_length_mismatch_returns_400(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)
    init = client.post(
        "/api/library/uploads",
        json={"filename": "x.wav", "total_size": 100},
    )
    upload_id = init.json()["upload_id"]
    # Claim bytes 0-9 (10 bytes) but send only 5.
    r = client.put(
        f"/api/library/uploads/{upload_id}",
        content=b"hello",
        headers={"Content-Range": "bytes 0-9/100"},
    )
    assert r.status_code == 400


def test_chunk_invalid_content_range_returns_400(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)
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


def test_commit_incomplete_returns_409(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)
    init = client.post(
        "/api/library/uploads",
        json={"filename": "x.wav", "total_size": 100},
    )
    upload_id = init.json()["upload_id"]
    # Send only the first 50 bytes.
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


def test_commit_filename_collision_cleans_up(tmp_path: Path) -> None:
    client, work_dir = _make_client(tmp_path)
    uploads = work_dir / "uploads"
    uploads.mkdir()
    (uploads / "dup.wav").write_bytes(b"old contents")

    init = client.post(
        "/api/library/uploads",
        json={"filename": "dup.wav", "total_size": 4},
    )
    upload_id = init.json()["upload_id"]
    client.put(
        f"/api/library/uploads/{upload_id}",
        content=b"new!",
        headers={"Content-Range": "bytes 0-3/4"},
    )
    r = client.post(f"/api/library/uploads/{upload_id}/commit")
    assert r.status_code == 409
    assert "already exists" in r.json()["detail"]
    # Original file is untouched.
    assert (uploads / "dup.wav").read_bytes() == b"old contents"
    # No orphan .upload-*.part left behind.
    leftovers = [p.name for p in uploads.iterdir() if p.name.startswith(".upload-")]
    assert leftovers == []


def test_cancel_removes_session_and_tempfile(tmp_path: Path) -> None:
    client, work_dir = _make_client(tmp_path)
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
    # Temp file exists pre-cancel.
    parts = list((work_dir / "uploads").glob(".upload-*.part"))
    assert len(parts) == 1

    r = client.delete(f"/api/library/uploads/{upload_id}")
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"

    # Idempotent: second cancel still 200.
    r2 = client.delete(f"/api/library/uploads/{upload_id}")
    assert r2.status_code == 200
    assert r2.json()["status"] == "not_found"

    # Session is gone.
    assert client.get(f"/api/library/uploads/{upload_id}").status_code == 404
    # Temp file is gone.
    parts = list((work_dir / "uploads").glob(".upload-*.part"))
    assert parts == []


def test_status_endpoint_reflects_progress(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)
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


def test_zero_byte_file_init_and_commit(tmp_path: Path) -> None:
    """Zero-byte uploads should be allowed (init succeeds, commit immediately)."""
    client, work_dir = _make_client(tmp_path)
    init = client.post(
        "/api/library/uploads",
        json={"filename": "empty.wav", "total_size": 0},
    )
    assert init.status_code == 200
    upload_id = init.json()["upload_id"]

    # No chunks sent — commit right away.
    r = client.post(f"/api/library/uploads/{upload_id}/commit")
    assert r.status_code == 200
    assert r.json()["bytes"] == 0
    final = work_dir / "uploads" / "empty.wav"
    assert final.exists()
    assert final.read_bytes() == b""


def test_parallel_sessions_do_not_interfere(tmp_path: Path) -> None:
    """Different upload_ids can be in flight concurrently without crosstalk."""
    client, work_dir = _make_client(tmp_path)
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

    # Interleave the writes.
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


def test_chunk_after_commit_returns_404(tmp_path: Path) -> None:
    """After commit, the session is gone — additional chunks must 404."""
    client, _ = _make_client(tmp_path)
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
    # Late chunk — session is gone.
    r = client.put(
        f"/api/library/uploads/{upload_id}",
        content=b"e",
        headers={"Content-Range": "bytes 4-4/4"},
    )
    assert r.status_code == 404


def test_old_multipart_endpoint_is_removed(tmp_path: Path) -> None:
    """The single-shot /api/library/upload no longer exists.

    If we ever bring it back as a compatibility shim, change this test
    accordingly — until then, its absence prevents accidental reintroduction.
    """
    client, _ = _make_client(tmp_path)
    r = client.post(
        "/api/library/upload",
        files={"file": ("x.m4a", b"hello", "audio/m4a")},
    )
    assert r.status_code in (404, 405)
