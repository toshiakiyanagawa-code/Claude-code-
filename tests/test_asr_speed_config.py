"""Tests for the P1 ASR speed configuration plumbing.

We don't run real ASR here — that's covered by integration manually. These
tests verify the config / API layer:
  * ASRConfig accepts the new speed knobs
  * BatchedInferencePipeline path is wired up via the ``batched`` flag
  * Cache key for WhisperModel includes cpu_threads / num_workers
  * POST /api/library/transcribe validates ``batched`` / ``batch_size``
  * asr.transcribe() routes to the right faster-whisper API
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from podedit.asr import ASRConfig, _first_temperature
from podedit.server.app import ServeConfig, create_app


def test_asr_config_speed_knob_defaults():
    cfg = ASRConfig()
    assert cfg.cpu_threads == 0  # 0 = let CT2 pick
    assert cfg.num_workers == 1
    assert cfg.batched is False
    assert cfg.batch_size == 4


def test_asr_config_speed_knobs_settable():
    cfg = ASRConfig(cpu_threads=2, num_workers=1, batched=True, batch_size=4)
    assert cfg.cpu_threads == 2
    assert cfg.num_workers == 1
    assert cfg.batched is True
    assert cfg.batch_size == 4


def test_first_temperature_picks_first_of_ladder():
    assert _first_temperature((0.0, 0.2, 0.4)) == 0.0
    assert _first_temperature((0.5,)) == 0.5


def test_first_temperature_passes_scalar_through():
    assert _first_temperature(0.3) == 0.3


def test_first_temperature_rejects_empty_ladder():
    with pytest.raises(ValueError):
        _first_temperature(())


def test_jobs_cache_key_includes_threads_cache_hit():
    """Same key → cache hit, no faster_whisper import needed."""
    from podedit.server.jobs import TranscriptionJobManager

    mgr = TranscriptionJobManager(work_dir=Path("/tmp"))
    sentinel = object()
    mgr._cached_model = sentinel
    mgr._cached_model_key = ("small", "int8", 2, 1)

    cached, hit = mgr._get_or_load_model("small", "int8", 2, 1)

    assert hit is True
    assert cached is sentinel


def test_jobs_cache_key_miss_when_threads_change(monkeypatch):
    """Changing cpu_threads triggers a cache miss + new WhisperModel call."""
    from podedit.server.jobs import TranscriptionJobManager

    calls = []

    class FakeWhisperModel:
        def __init__(self, model, **kwargs):
            calls.append((model, kwargs))

    monkeypatch.setitem(
        sys.modules,
        "faster_whisper",
        types.SimpleNamespace(WhisperModel=FakeWhisperModel),
    )

    mgr = TranscriptionJobManager(work_dir=Path("/tmp"))
    mgr._cached_model = object()
    mgr._cached_model_key = ("small", "int8", 2, 1)

    cached, hit = mgr._get_or_load_model("small", "int8", 4, 1)

    assert hit is False
    assert isinstance(cached, FakeWhisperModel)
    assert mgr._cached_model_key == ("small", "int8", 4, 1)
    assert calls == [
        (
            "small",
            {
                "device": "cpu",
                "compute_type": "int8",
                "cpu_threads": 4,
                "num_workers": 1,
            },
        )
    ]


# ----- API validation -----


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


def test_transcribe_rejects_non_bool_batched(tmp_path: Path):
    client = _make_client(tmp_path)
    r = client.post(
        "/api/library/transcribe",
        json={"name": "nonexistent.wav", "batched": "yes"},
    )
    assert r.status_code == 400
    assert "batched" in r.text


def test_transcribe_rejects_out_of_range_batch_size(tmp_path: Path):
    client = _make_client(tmp_path)
    for bad in [0, 9, -1]:
        r = client.post(
            "/api/library/transcribe",
            json={"name": "nonexistent.wav", "batch_size": bad},
        )
        assert r.status_code == 400, f"expected 400 for batch_size={bad}"
        assert "batch_size" in r.text


@pytest.mark.parametrize("bad", ["4", 4.0, None])
def test_transcribe_rejects_non_int_batch_size(tmp_path: Path, bad):
    """str / float / None for batch_size — all rejected before file existence."""
    client = _make_client(tmp_path)
    r = client.post(
        "/api/library/transcribe",
        json={"name": "nonexistent.wav", "batch_size": bad},
    )
    assert r.status_code == 400
    assert "batch_size" in r.text


# ----- asr.transcribe() routing (via fake faster_whisper) -----


class _FakeSegment:
    def __init__(self, idx: int, text: str = "hi"):
        self.start = float(idx)
        self.end = float(idx) + 0.5
        self.text = text
        self.words = []


class _FakeInfo:
    language = "ja"


class _FakeWhisperModel:
    last_kwargs: dict = {}
    captured_transcribe_kwargs: list[dict] = []

    def __init__(self, model, **kwargs):
        type(self).last_kwargs = kwargs

    def transcribe(self, _audio, **kwargs):
        type(self).captured_transcribe_kwargs.append(kwargs)
        return iter([_FakeSegment(0)]), _FakeInfo()


class _FakeBatchedPipeline:
    captured_transcribe_kwargs: list[dict] = []
    captured_model: object = None

    def __init__(self, model):
        type(self).captured_model = model

    def transcribe(self, _audio, **kwargs):
        type(self).captured_transcribe_kwargs.append(kwargs)
        return iter([_FakeSegment(0)]), _FakeInfo()


def _install_fake_faster_whisper(monkeypatch):
    _FakeWhisperModel.captured_transcribe_kwargs.clear()
    _FakeBatchedPipeline.captured_transcribe_kwargs.clear()
    fake = types.SimpleNamespace(
        WhisperModel=_FakeWhisperModel,
        BatchedInferencePipeline=_FakeBatchedPipeline,
    )
    monkeypatch.setitem(sys.modules, "faster_whisper", fake)
    # Also mock ctranslate2 since resolve_device imports it.
    monkeypatch.setitem(
        sys.modules,
        "ctranslate2",
        types.SimpleNamespace(get_cuda_device_count=lambda: 0),
    )


def _make_audio_info(tmp_path: Path):
    """Build a minimal AudioInfo that to_ref() can call without ffprobe."""
    p = tmp_path / "stub.wav"
    p.write_bytes(b"")  # empty file ok for routing test
    from podedit.audio import AudioInfo

    return AudioInfo(
        path=p,
        duration_sec=1.0,
        sample_rate=16000,
        channels=1,
        codec="pcm_s16le",
    )


def test_transcribe_serial_path_uses_model_transcribe(monkeypatch, tmp_path: Path):
    _install_fake_faster_whisper(monkeypatch)
    # asr.transcribe probes the wav for asr_audio AudioRef — stub probe too.
    monkeypatch.setattr(
        "podedit.audio.probe",
        lambda p: _make_audio_info(tmp_path),
    )

    from podedit.asr import ASRConfig, transcribe

    cfg = ASRConfig(
        model="small", language="ja", device="cpu", compute_type="int8",
        beam_size=5, vad_filter=True, batched=False,
    )
    source = _make_audio_info(tmp_path)
    tx, gen = transcribe(source, tmp_path / "stub.wav", cfg)
    list(gen)  # drive the generator

    assert _FakeWhisperModel.captured_transcribe_kwargs, "model.transcribe must be called"
    assert not _FakeBatchedPipeline.captured_transcribe_kwargs, "batched path must NOT be used"
    call = _FakeWhisperModel.captured_transcribe_kwargs[0]
    assert call["beam_size"] == 5
    assert call["word_timestamps"] is True
    assert call["vad_parameters"]["speech_pad_ms"] == 300
    assert call["condition_on_previous_text"] is True


def test_transcribe_batched_path_uses_pipeline(monkeypatch, tmp_path: Path):
    _install_fake_faster_whisper(monkeypatch)
    monkeypatch.setattr(
        "podedit.audio.probe",
        lambda p: _make_audio_info(tmp_path),
    )

    from podedit.asr import ASRConfig, transcribe

    cfg = ASRConfig(
        model="small", language="ja", device="cpu", compute_type="int8",
        beam_size=5, vad_filter=True, batched=True, batch_size=4,
    )
    source = _make_audio_info(tmp_path)
    tx, gen = transcribe(source, tmp_path / "stub.wav", cfg)
    list(gen)

    assert _FakeBatchedPipeline.captured_transcribe_kwargs, "batched path must be used"
    call = _FakeBatchedPipeline.captured_transcribe_kwargs[0]
    assert call["batch_size"] == 4
    assert call["word_timestamps"] is True
    assert call["vad_parameters"]["speech_pad_ms"] == 300
    # Pipeline doesn't accept condition_on_previous_text — caller must not pass it.
    assert "condition_on_previous_text" not in call
    # Temperature was collapsed from ladder to first value.
    assert call["temperature"] == 0.0
    # Transcript metadata reflects effective values.
    assert tx.model_config.condition_on_previous_text is False
    assert tx.model_config.temperature == 0.0


def test_transcribe_batched_requires_vad(monkeypatch, tmp_path: Path):
    _install_fake_faster_whisper(monkeypatch)
    monkeypatch.setattr(
        "podedit.audio.probe",
        lambda p: _make_audio_info(tmp_path),
    )

    from podedit.asr import ASRConfig, transcribe

    cfg = ASRConfig(
        model="small", device="cpu", compute_type="int8",
        batched=True, vad_filter=False,
    )
    source = _make_audio_info(tmp_path)
    with pytest.raises(ValueError, match="batched.*vad_filter"):
        transcribe(source, tmp_path / "stub.wav", cfg)
