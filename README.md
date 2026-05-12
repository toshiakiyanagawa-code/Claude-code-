# podedit

Local-first podcast editor for Japanese podcasts. Edit audio by editing its transcript.

**Status: W1 (foundation + one-way ASR PoC + bench harness).**

## Requirements

- Linux or macOS
- Python 3.12
- ffmpeg / ffprobe on `PATH`
- ~3 GB disk for models (`large-v3` is the largest default candidate)
- GPU optional. CPU works for small/medium models; expect RTF > 1 on `large-v3` without CUDA.

## Setup

```bash
# 1. Install ffmpeg
sudo apt-get install -y ffmpeg     # Ubuntu/Debian
# brew install ffmpeg              # macOS

# 2. Install uv (https://docs.astral.sh/uv/)
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

# 3. Sync dependencies
uv sync
```

## Usage (W1)

```bash
# Transcribe a podcast episode (writes <audio>.transcript.json next to the input)
uv run podedit transcribe path/to/episode.mp3 --model small

# Pick a model: tiny | base | small | medium | large-v3 | large-v3-turbo
uv run podedit transcribe path/to/episode.mp3 --model large-v3-turbo --device auto
```

Each run appends a JSON line to `benchmarks.jsonl`:

```json
{"label":"asr_transcribe","wall_sec":20.9,"peak_rss_mb":320,"extra":{"model":"tiny","duration_sec":15.2,"segments":3,"word_count":65}}
```

Use these to compare models / devices on **your own** episodes — the W1 hard goal is
"60-min real episode completes end-to-end with metrics logged."

## Layout

```
src/podedit/
├── audio.py      # ffmpeg I/O + 16kHz mono resample
├── asr.py        # faster-whisper wrapper, word timestamps, VAD
├── schema.py     # Transcript / Segment / Word (timestamps anchored to ORIGINAL audio)
├── bench.py      # wall time + peak RSS context manager
└── cli.py        # `podedit` entry point
```

## Roadmap (MVP, 8-9 weeks)

- **W1 ✅ Foundation**: ASR PoC + bench harness
- W2 — Word-level alignment, project schema, CLI delete→render minimum
- W3 — Local web UI + click-to-seek
- W4 — Delete ops + preview + Undo/Redo + save/load (KPI measurement starts here)
- W5 — PCM render + fixed crossfade + wav export
- W6 — Variable crossfade + zero-cross + de-click + cut evaluation set
- W7 — mp3 export + LUFS/true peak + waveform cache + stabilization
- W8 — Real-episode KPI run + friction fixes + reproducibility docs

Differentiating bet: **Japanese conversation quality** (aizuchi vs filler distinction,
prosody-aware cuts). Voice cloning is staged for v1.0.
