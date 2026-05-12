"""Audio I/O via ffmpeg subprocess. No PyAV/torchaudio deps for portability."""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .schema import AudioRef


class FFmpegMissingError(RuntimeError):
    pass


class AudioProbeError(RuntimeError):
    pass


def _require_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        raise FFmpegMissingError(
            "ffmpeg/ffprobe not found on PATH. Install ffmpeg "
            "(e.g. `sudo apt-get install ffmpeg` or `brew install ffmpeg`)."
        )


@dataclass(frozen=True, slots=True)
class AudioInfo:
    path: Path
    duration_sec: float
    sample_rate: int
    channels: int
    codec: str

    def to_ref(self) -> AudioRef:
        return AudioRef(
            path=str(self.path),
            duration_sec=self.duration_sec,
            sample_rate=self.sample_rate,
            channels=self.channels,
            codec=self.codec,
        )


def probe(path: Path) -> AudioInfo:
    """Probe an audio file. Falls back to stream duration if format duration is missing."""
    _require_ffmpeg()
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error", "-print_format", "json",
                "-show_streams", "-show_format", str(path),
            ],
            capture_output=True, text=True, check=True,
        )
    except subprocess.CalledProcessError as e:
        raise AudioProbeError(f"ffprobe failed for {path}: {e.stderr.strip()}") from e

    data = json.loads(out.stdout)
    astream = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    if astream is None:
        raise AudioProbeError(f"No audio stream in {path}")

    fmt = data.get("format", {})
    duration = fmt.get("duration") or astream.get("duration")
    if duration is None:
        raise AudioProbeError(
            f"Could not determine duration for {path} "
            "(neither format.duration nor stream.duration present)."
        )

    return AudioInfo(
        path=path,
        duration_sec=float(duration),
        sample_rate=int(astream["sample_rate"]),
        channels=int(astream["channels"]),
        codec=astream["codec_name"],
    )


def to_wav_16k_mono(src: Path, dst: Path) -> Path:
    """Convert to 16kHz mono PCM wav (Whisper-friendly).

    The ASR pipeline operates on this 16k mono copy; the original audio stays
    untouched and remains the source of truth for editing/rendering timestamps.
    """
    _require_ffmpeg()
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", str(src),
                "-ac", "1", "-ar", "16000",
                "-acodec", "pcm_s16le",
                str(dst),
            ],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as e:
        raise AudioProbeError(f"ffmpeg resample failed: {e.stderr.strip()}") from e
    return dst
