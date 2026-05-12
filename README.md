# podedit

Local-first podcast editor for Japanese podcasts. Edit audio by editing its transcript.

**Status: W4 complete — full transcript-driven editor in the browser.**
W5 (PCM crossfade / de-click / server-side preview render) is next.

## What works today

- `podedit transcribe` — Japanese ASR via faster-whisper, word-level timestamps,
  bench-logged (RTF, peak RSS, success/error) to `benchmarks.jsonl`.
- `podedit cut` — apply delete ranges directly via CLI, write a wav, save an
  `EditSession` JSON.
- `podedit render` — replay a saved `EditSession` against its source; verifies
  the source SHA-256 before rendering.
- `podedit serve` — local web UI: scroll the transcript, drag to select words,
  press <kbd>D</kbd> to delete, <kbd>⌘Z</kbd> to undo. The scrubber and time
  displays reflect the **edited** timeline (deletions remove their span from
  the visible duration); audio playback skips deleted ranges automatically.
  Sessions autosave; KPI events stream to a JSONL log.

## Requirements

- Linux or macOS
- Python 3.12
- ffmpeg / ffprobe on `PATH`
- ~3 GB disk if you fetch `large-v3` (smaller models work fine on CPU)
- GPU optional. CPU works for small/medium models; expect RTF > 1 on `large-v3`
  without CUDA. Measured on a 2-vCPU Codespace: `tiny` 0.21x RTF, `base` 0.24x,
  `small` 0.65x.

## Setup

```bash
sudo apt-get install -y ffmpeg                          # or: brew install ffmpeg
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv sync
```

## Usage

### Transcribe (W1)

```bash
# Writes <stem>.transcript.json under .podedit/work/ by default.
uv run podedit transcribe path/to/episode.mp3 --model small

# Pick a model: tiny | base | small | medium | large-v3 | large-v3-turbo
uv run podedit transcribe path/to/episode.mp3 --model large-v3-turbo

# Disable VAD if Japanese aizuchi / laughter are being dropped.
uv run podedit transcribe path/to/episode.mp3 --model small --no-vad

# Skip the source SHA-256 (faster, but downstream tools can't verify the file).
uv run podedit transcribe path/to/episode.mp3 --no-checksum
```

Each run appends a record to `benchmarks.jsonl`:

```json
{"label":"asr_transcribe","success":true,"wall_sec":384.69,"process_peak_rss_mb":1858,
 "extra":{"model":"tiny","resolved_device":"cpu","compute_type":"int8",
          "duration_sec":1788.65,"segments":984,"word_count":7869,
          "total_wall_sec":394.1,"transcript_bytes":1319102}}
```

### Cut (W2)

Apply one or more delete ranges directly via CLI:

```bash
uv run podedit cut path/to/episode.m4a \
  -d "30-40" \
  -d "1:00-1:15" \
  -o out.wav \
  --save-session out.session.json \
  --transcript .podedit/work/episode.transcript.json
```

Ranges accept seconds, `M:SS`, or `H:MM:SS`. The `EditSession` JSON records the
source audio (with SHA-256) and the ops list. W2 cuts are hard splices (no
crossfade); proper sample-precise PCM with de-click arrives in W5.

### Render a saved session (W2 follow-up)

```bash
uv run podedit render out.session.json -o out.wav
# Verifies source SHA-256 by default. Use --source-override PATH if the file
# has moved; --no-check-checksum to render anyway.
```

The render command is what the UI's autosaved session feeds into, so anything
you edit in the browser can be replayed deterministically from the CLI.

### Edit in the browser (W3 + W4)

```bash
uv run podedit serve \
  --audio path/to/episode.m4a \
  --transcript .podedit/work/episode.transcript.json
# Default port 8765. The CLI prints the session and KPI log paths.
```

Then open `http://127.0.0.1:8765` (or the Codespaces forwarded URL).

**Controls**:
- Click a word → seek and play from there.
- Drag across words → select a range.
- Shift+click → extend an existing selection.
- <kbd>D</kbd> / <kbd>Delete</kbd> / <kbd>Backspace</kbd> → delete the selection.
- <kbd>⌘Z</kbd> / <kbd>Ctrl+Z</kbd> → undo; add <kbd>Shift</kbd> for redo.
- <kbd>Space</kbd> → play/pause. <kbd>Esc</kbd> → clear selection.

The duration and current-time shown above the transcript reflect the **edited**
timeline. Delete the first 5 seconds and `0:00` becomes the first kept word.

## How the audio actually plays past cuts

`<audio>` keeps streaming the source m4a/wav; the UI runs a small mapping layer
that bridges between "source seconds" (what the audio element knows) and "edited
seconds" (what the scrubber shows). When the source playhead enters a deleted
range, a `pause → currentTime = next-keep → play` cycle skips it. The original
audio is never modified — every edit is just an entry in `EditSession.ops`.

For m4a/mp4 inputs the server checks the `moov` atom location at startup and
faststart-remuxes once into `.podedit/work/<stem>.faststart.m4a` if the moov
sits at the tail of the file (otherwise the browser can't seek reliably).

## Project layout

```
src/podedit/
├── audio.py            # ffmpeg I/O + 16kHz mono resample + duration probe
├── asr.py              # faster-whisper wrapper: word timestamps, VAD, device resolution
├── schema.py           # Transcript / Segment / Word; timestamps anchored to ORIGINAL audio
├── edit.py             # EditSession + DeleteOp + keep_ranges_from_deletes + sha256 helper
├── render.py           # ffmpeg atrim+concat renderer (W2; W5 swaps in PCM)
├── bench.py            # wall time + peak RSS context manager, JSONL append
├── server/
│   ├── app.py          # FastAPI app: /api/{transcript,audio,session,kpi/event}, validation
│   └── static/         # index.html + style.css + app.js (vanilla; React deferred)
└── cli.py              # `podedit` entry point: transcribe / cut / render / serve

tests/test_edit.py      # 17 tests: keep_ranges_from_deletes + EditSession round-trip
```

## Roadmap (MVP, 8-9 weeks)

- **W1 ✅** Foundation — ASR PoC + bench harness
- **W2 ✅** Edit minimum — `EditSession`, ffmpeg renderer, `cut`/`render` CLI
- **W3 ✅** Local web UI — FastAPI + plain HTML/JS, click-to-seek
- **W4 ✅** Delete ops + preview-skip + Undo/Redo + autosave + KPI + virtual timeline
- **W5** PCM render + fixed crossfade + wav export + server-side preview render
- **W6** Variable crossfade + zero-cross + de-click + cut evaluation set
- **W7** mp3 export + LUFS/true peak + waveform cache + stabilization
- **W8** Real-episode KPI run + friction fixes + reproducibility docs

Differentiating bet: **Japanese conversation quality** (aizuchi vs filler distinction,
prosody-aware cuts). Voice cloning is staged for v1.0.

## Changelog

| Commit | Week | What |
|---|---|---|
| f8c4419 | W4 | Pause at edited end when the tail is deleted |
| 6287425 | W4 | Virtual timeline — edited duration + scrubber + custom player |
| 84ab5f3 | W4 | Improve drag-select sensitivity |
| befc8e1 | W4 | Harden preview-skip — pause/seek/play + cache-bust + diagnostics |
| f726c82 | W4 | Faststart-remux m4a so preview-skip seeks actually take effect |
| 6ce910a | W4 | Make deletions unmissable + preview-skip indicator |
| 94fd512 | W4 | Switch to drag-to-select; preserve click=seek |
| 2ee6a05 | W4 | Hotfix: word click handler captured idx by reference |
| 80910e3 | W4 | Preview-skip fix, server-side session validation, autosave race |
| ee42649 | W4 | Select/delete/undo/redo, autosave, preview-skip, KPI scaffolding |
| fa70b09 | W3 | Validate audio/transcript at serve, harden UI, optional SHA-256 |
| a753125 | W3 | Local web UI with click-to-seek (FastAPI + plain HTML/JS) |
| 1a7ebe4 | W2 | Render command, session round-trip, OOB bugfix |
| ca97ecd | W2 | EditSession schema + ffmpeg atrim+concat renderer + `podedit cut` |
| c9f616e | W1 | Ensure transcript_bytes/total_wall_sec land in bench JSONL |
| ea4c87c | W1 | Foundation + one-way ASR PoC + bench harness |
