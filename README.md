[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/toshiakiyanagawa-code/Claude-code-)

# podedit

Local-first podcast editor for Japanese podcasts. Edit audio by editing its transcript.

**Status: MVP complete — transcribe, edit, audition, render — all in the browser or from the CLI.**

## Try it in 5 minutes

### Codespaces (1 click, recommended)

Click the **Open in GitHub Codespaces** badge above. The devcontainer installs
Python 3.12, `ffmpeg`, and `uv`, then runs `uv sync` for you.

```bash
uv run podedit serve
```

Open the forwarded port `8765`, click **Open** in the UI, upload an audio file,
click **Transcribe**, choose the **Balanced** preset, then select the file and edit.

> Uploads in Codespaces go through a chunked transfer (512 KB per chunk) to bypass
> the forwarded port's body-size limit. Files up to 500 MB work without leaving the
> browser; progress shows on the upload button.

### Docker

```bash
docker compose up
```

Open `http://localhost:8765`, click **Open**, upload or select an audio file,
transcribe it, then edit. Uploaded files and derived artifacts persist under
`.podedit/work` on the host.

### Local clone

Prerequisites: Python 3.12, `ffmpeg`/`ffprobe`, and `uv`.

```bash
git clone https://github.com/toshiakiyanagawa-code/Claude-code-.git
cd Claude-code-
sudo apt-get install -y ffmpeg  # or: brew install ffmpeg
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv sync
uv run podedit serve
```

Open `http://127.0.0.1:8765`, click **Open**, upload or pick an audio file,
transcribe it, then edit.

## What works today

- **In-browser editor** (`podedit serve`)
  - Click a word to seek; drag across words to select; <kbd>D</kbd> to cut;
    <kbd>⌘X</kbd>/<kbd>⌘V</kbd> to move material; <kbd>⌘Z</kbd>/<kbd>⌘⇧Z</kbd> for undo/redo.
  - Drag a selected range to a new spot to **move** it (the transcript rebuilds
    in edited order, audio follows).
  - **Audition** button renders a sample-precise preview with crossfades + de-click
    + two-pass loudnorm (-16 LUFS, -1 dBTP) and streams it back to the player.
  - **Export** button (or <kbd>E</kbd>) downloads the same render as wav or mp3 —
    no CLI round-trip needed for the common case.
  - **Open dialog** (toolbar **Open** / <kbd>O</kbd>) lists every audio file in the
    library dir and lets you switch the active episode without restarting the server.
  - **Transcribe** button on any library entry that doesn't have a transcript yet —
    runs faster-whisper in the background, shows live RTF and elapsed time, and
    the file becomes selectable once it finishes. A **quality preset** dropdown
    at the top of the Open dialog picks between Fast / Balanced / Quality (see
    table below); the choice persists across sessions.
  - Sessions autosave to JSON; KPI events stream to a JSONL log; both are
    keyed to the audio stem so multiple episodes coexist in one work dir.

- **CLI** (`podedit transcribe|cut|render|serve`)
  - `transcribe` — Japanese ASR via faster-whisper with word-level timestamps;
    appends RTF / peak-RSS / success metrics to `benchmarks.jsonl`.
  - `cut` / `render` — apply delete/move ops from CLI or replay an autosaved
    `EditSession`; verifies the source SHA-256 before rendering.
  - Renderer is sample-precise PCM with **content-aware variable crossfades**
    (zero-cross snap, click detection, per-seam fade lengths) and two-pass
    EBU R128 loudnorm. Exports to wav, mp3, or flac.

## Requirements

- Linux or macOS
- Python 3.12
- ffmpeg / ffprobe on `PATH`
- ~3 GB disk if you fetch `large-v3` (smaller models work fine on CPU)
- GPU optional. CPU works for all models; expect RTF > 1 on `large-v3` without
  CUDA. On a 2-vCPU Codespace (no GPU), measured pure-decode RTF on a 60s JA
  podcast slice with `tiny`:

  | preset (in-UI)                                  |  RTF  | 30-min wall | notes |
  |-------------------------------------------------|------:|------------:|-------|
  | **Fast** (tiny / beam=1)                        | 0.114 | ~3.4 min    | baseline; many JA mis-recognitions |
  | **Balanced** (small / beam=1) — default         | 0.407 | ~12 min     | recognizes "クロード", "認知的", proper nouns |
  | **Quality** (small / beam=5)                    | 0.564 | ~17 min     | crisper names ("スタンフォード"); marginal gain over Balanced |
  | _ablate_ — small / beam=1, no prompt            | 0.385 | ~12 min     | "クロード" regresses to "黒だとか"; prompt earns its keep |

  All in-UI presets are biased by a built-in JA podcast prompt
  (`日本語のポッドキャスト会話。話し言葉、自然な相槌、固有名詞を含みます。`).
  The CLI keeps the legacy defaults (`beam=5`, full temperature ladder,
  `condition_on_previous_text=True`, no prompt biasing) — measured at RTF
  ≈ 0.139 with `tiny`. API callers can override the in-UI defaults per job
  (`POST /api/library/transcribe` accepts optional `model`, `beam_size`,
  `initial_prompt`, `hotwords`; explicit empty string for `initial_prompt`
  disables biasing for ablation).

## Usage details

### Transcribe (W1)

```bash
uv run podedit transcribe path/to/episode.mp3 --model tiny
# Models: tiny | base | small | medium | large-v3 | large-v3-turbo
# Disable VAD if Japanese aizuchi / laughter are being dropped:
uv run podedit transcribe path/to/episode.mp3 --model small --no-vad
# Skip the source SHA-256 (faster, but downstream tools can't verify the file):
uv run podedit transcribe path/to/episode.mp3 --no-checksum
```

Each run appends a record to `benchmarks.jsonl`:

```json
{"label":"asr_transcribe","success":true,"wall_sec":384.69,"process_peak_rss_mb":1858,
 "extra":{"model":"tiny","resolved_device":"cpu","compute_type":"int8",
          "duration_sec":1788.65,"segments":984,"word_count":7869,
          "total_wall_sec":394.1,"transcript_bytes":1319102}}
```

The CLI uses **legacy defaults** (beam=5, temperature fallback ladder,
`condition_on_previous_text=True`) — same behavior as before W7.8. The in-UI
worker opts into **fast mode** (beam=1, ladder kept for safety,
`condition_on_previous_text=False`) for interactive responsiveness.

### Cut (W2)

Apply delete ranges directly via CLI:

```bash
uv run podedit cut path/to/episode.m4a \
  -d "30-40" \
  -d "1:00-1:15" \
  -o out.wav \
  --save-session out.session.json \
  --transcript .podedit/work/episode.transcript.json
```

Ranges accept seconds, `M:SS`, or `H:MM:SS`. The `EditSession` JSON records the
source audio (with SHA-256) and the ops list.

### Render a saved session (W2 + W5–W7)

```bash
uv run podedit render out.session.json -o out.wav
# Verifies source SHA-256 by default. Use --source-override PATH if the file
# has moved; --no-check-checksum to render anyway.
# Output format is inferred from the extension: .wav / .mp3 / .flac.
```

The render command is what the UI's autosaved session feeds into, so anything
you edit in the browser can be replayed deterministically from the CLI. The
renderer:
- decodes PCM and produces sample-precise splices (no atrim drift, W5)
- detects seams that would otherwise click and applies a variable-length
  equal-power crossfade (zero-cross snap, click detection, W6)
- two-pass loudnorm to -16 LUFS / -1 dBTP true peak (EBU R128, W7)
- handles both delete and move ops via `compile_timeline` (W7.5)

### Edit in the browser (W3–W8)

```bash
uv run podedit serve \
  --audio path/to/episode.m4a \
  --transcript .podedit/work/episode.transcript.json
# Default port 8765. The CLI prints the session and KPI log paths.
```

Then open `http://127.0.0.1:8765` (or the Codespaces forwarded URL).

**Controls**:
- Click a word → seek and play from there.
- Drag across words → select a range. <kbd>Shift</kbd>+click → extend.
- <kbd>D</kbd> / <kbd>Delete</kbd> / <kbd>Backspace</kbd> → delete the selection.
- <kbd>⌘X</kbd> → cut selection to clipboard; <kbd>⌘V</kbd> → paste after the
  marked anchor word. (You can also **drag a selected range** onto another
  word to move it.)
- <kbd>⌘Z</kbd> / <kbd>Ctrl+Z</kbd> → undo; add <kbd>Shift</kbd> for redo.
- <kbd>Space</kbd> → play/pause. <kbd>Esc</kbd> → clear selection.
- <kbd>O</kbd> → Open dialog (switch episodes, transcribe new ones).
- <kbd>E</kbd> → Export the current edit as wav or mp3 (format selector in toolbar).
- **Audition** button → render a sample-precise preview of the current edit.

The duration and current-time shown above the transcript reflect the **edited**
timeline. Delete the first 5 seconds and `0:00` becomes the first kept word.
Move a range to a new position and the transcript visually reflows.

### Share with editing team (Codespaces port + password)

To let a few colleagues use the same podedit instance over the internet:

1. **Set a password.** Use the env-var form so the secret doesn't end up in
   shell history or `ps` output:

   ```bash
   read -srp 'podedit password: ' PODEDIT_AUTH_PASSWORD && export PODEDIT_AUTH_PASSWORD
   uv run podedit serve --host 0.0.0.0
   ```

   The server now requires HTTP Basic auth — username is `podedit`, password
   is what you set. Without `PODEDIT_AUTH_PASSWORD`, binding to `0.0.0.0`
   prints a warning because the URL would otherwise be open to anyone who
   can reach it. `--auth-password` is also accepted but discouraged for
   non-throwaway secrets (it shows up in shell history and process listings).

2. **Make the Codespace port public** so colleagues can hit the forwarded URL.
   Either:

   - In the **Ports** panel: right-click port `8765` → Port Visibility → Public.
   - Or `gh codespace ports visibility 8765:public -c <codespace-name>`.

   Copy the forwarded URL (looks like `https://<codespace>-8765.app.github.dev`).
   GitHub serves it over HTTPS, so the Basic Auth password isn't sent in clear.

3. **Share the URL and password out-of-band** (a Slack DM is fine; don't put
   them in the same message). Anyone who has both can log in as `podedit` and
   edit. Sessions and snapshots are stored per audio file under
   `work_dir/<stem>.session.json` and `<stem>.snapshots/`, so two editors
   working on **different** files won't overwrite each other. Coordinating on
   the *same* file is still a manual step for now (use snapshots to fork off
   "draft 1 / draft 2" if you want parallel cuts).

To revoke access: change `PODEDIT_AUTH_PASSWORD` and restart the server, or
flip port visibility back to **Private** in the Ports panel.

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
├── asr.py              # faster-whisper wrapper: word timestamps, VAD, device, fast knobs
├── schema.py           # Transcript / Segment / Word; timestamps anchored to ORIGINAL audio
├── edit.py             # EditSession + DeleteOp + MoveOp + compile_timeline + sha256 helper
├── render.py           # PCM render via ffmpeg pipeline; seam-aware crossfades; loudnorm
├── library.py          # scan_library — list audio + transcript/session status for UI picker
├── bench.py            # wall time + peak RSS context manager, JSONL append
├── seam_eval.py        # offline eval harness for crossfade variants
├── server/
│   ├── app.py          # FastAPI app: /api/{transcript,audio,session,kpi,library,waveform,…}
│   ├── jobs.py         # TranscriptionJobManager — background ASR for the in-UI button
│   └── static/         # index.html + style.css + app.js (vanilla; React deferred)
└── cli.py              # `podedit` entry point: transcribe / cut / render / serve / eval

tests/                  # 57 tests: edit / render / timeline / seam_eval / waveform
```

### Server endpoints

| Method | Path | What |
|---|---|---|
| `GET` | `/api/transcript` | Current transcript JSON |
| `GET` | `/api/audio/info` | Audio metadata + cache-busting URL for `/api/audio` |
| `GET` | `/api/audio` | Raw audio stream (range-supported) |
| `GET` | `/api/session` | Current EditSession |
| `PUT` | `/api/session` | Save EditSession (validates shape, atomic write) |
| `POST` | `/api/preview/render` | Render audition wav, cached by ops-hash, per-key locked |
| `GET` | `/api/preview-audio/{cache_key}` | Stream the rendered preview wav (inline) |
| `GET` | `/api/export/{cache_key}?fmt={wav,mp3}` | Download attachment; mp3 lazy transcode |
| `GET` | `/api/waveform?points=N` | Pre-decoded envelope, cached on disk |
| `GET` | `/api/library` | Library entries + active file |
| `POST` | `/api/library/select` | Switch active (audio, transcript, session) triple |
| `POST` | `/api/library/upload` | Single-shot multipart upload (CLI / local-host friendly; subject to reverse-proxy body limits) |
| `POST` | `/api/library/upload/init` | Open a chunked upload session, returns `{upload_id, chunk_size}` |
| `PUT` | `/api/library/upload/{upload_id}/chunk` | Append one ordered chunk (`X-Chunk-Index` header, raw bytes ≤ 512 KB) |
| `POST` | `/api/library/upload/{upload_id}/finalize` | Commit the chunked upload to `.podedit/work/uploads/<basename>` |
| `POST` | `/api/library/transcribe` | Kick off an ASR job, returns job snapshot |
| `GET` | `/api/library/transcribe/status` | Poll current/last job state |
| `POST` | `/api/kpi/event` | Client KPI append (keepalive-safe) |

All static assets are served with `Cache-Control: no-store`, and the endpoints that
benefit most from freshness (library, transcribe status, audio file) explicitly opt
in too — so a hard refresh reliably pulls the latest UI bundle, important on
Codespaces-forwarded ports where edge caches can sit between you and the dev server.

## Roadmap

- **W1 ✅** Foundation — ASR PoC + bench harness
- **W2 ✅** Edit minimum — `EditSession`, ffmpeg renderer, `cut`/`render` CLI
- **W3 ✅** Local web UI — FastAPI + plain HTML/JS, click-to-seek
- **W4 ✅** Delete ops + preview-skip + Undo/Redo + autosave + KPI + virtual timeline
- **W5 ✅** PCM render via ffmpeg pipeline + fixed 10ms crossfade + server-side audition
- **W6 ✅** Variable crossfade + zero-cross snap + click detection + cut evaluation set
- **W7 ✅** mp3/flac export + two-pass LUFS/true peak + waveform cache + stabilization
- **W7.5 ✅** Move ops — drag-to-move, transcript DOM reorder, selection clamping
- **W7.6 ✅** In-app file picker — switch episodes without restarting the server
- **W7.7 ✅** In-UI Transcribe button — background ASR jobs with live progress
- **W7.8 ✅** ASR speed — beam=1 greedy + WhisperModel cache, 1.65x faster on CPU
- **W8 ✅** MVP completion — in-UI Export (wav/mp3), reproducibility docs, friction polish
- **W9 ✅** ASR accuracy — small-model quality preset + JA podcast prompt biasing; tri-state API
- **W10 ✅** Full-filesystem audio picker — Open dialog can browse audio files beyond the initial library root
- **W11 ✅** UI density polish — tighter transcript layout, copy-transcript button, smaller font, Shift+Arrow selection
- **W12 ✅** Native file picker + upload — choose laptop files via OS dialog and import them into the app
- **W13 ✅** Codespaces-friendly chunked upload — 3-endpoint init/chunk/finalize protocol works around the forwarded-port body-size limit; existing files can be overwritten on demand; the in-UI Open audio strings are also localized to Japanese

Differentiating bet: **Japanese conversation quality** (aizuchi vs filler distinction,
prosody-aware cuts). Voice cloning is staged for v1.0.

## Changelog

| Commit | Week | What |
|---|---|---|
| d1681f0 | W13 | Chunked upload init now supports overwrite from server and UI |
| 4e42ffb | W13 | Restore chunked upload after PR #3 merge and widen file-picker accept types |
| 674a486 | W13 | Fix multipart filename mojibake for Japanese uploads |
| cac261c | W13 | Translate Open audio dialog and surrounding flow to Japanese |
| a8dfd02 | W12 | Native file picker + upload — pick laptop files via OS dialog |
| 53b2696 | W11 | Follow-up: bump transcript line-height 1.15 → 1.25 |
| 94279da | W11 | Follow-up: tighten spacing per user feedback |
| 8a9acce | W11 | UI density, copy-transcript, smaller font, Shift+Arrow selection |
| 2c4892f | W10 | Full-filesystem audio file picker in the Open dialog |
| f72c18d | W9 | ASR accuracy — quality presets and Japanese podcast prompt biasing |
| 2dd09c2 | W7.7-7.8+W8 | In-UI Transcribe button, fast ASR, in-UI Export, MVP completion |
| 32e48c6 | W7.6 | Fix: "Loading library…" stuck — defense against silent fetch failures |
| cb384af | W7.6 | Follow-up: address Codex review on the file-picker refactor |
| 5a5fa23 | W7.6 | In-app file picker — switch audio without restarting the server |
| c5b4701 | W7.5+ | Drag-to-move follow-up: address Codex review on the DOM-reorder fix |
| 1756b6b | W7.5+ | Drag-to-move: rebuild transcript DOM in edited order so moves are visible |
| be38ed3 | W7.5+ | Drag-to-move follow-up: keep drop-caret above the larger ghost |
| b7a2775 | W7.5+ | Drag-to-move: clone the actual selected text into the ghost |
| 3003f0e | W7.5+ | Drag-to-move — grab the selected text and drop it elsewhere |
| a6966d8 | W7.5 | Move ops — drag a range and paste it elsewhere |
| 04a3983 | W7 | Follow-up: address Codex review |
| 9af80ec | W7 | mp3/flac export, two-pass loudnorm, waveform cache, stabilization |
| d5a75e6 | W6 | Follow-up: address Codex review (detect_clicks signed-diff + 3 polish) |
| 77b7677 | W6 | Seam analysis — zero-cross snap + content-aware variable crossfade |
| 9387e73 | W5 | Follow-up: address Codex review feedback |
| 11e3d97 | W5 | PCM render via ffmpeg + server-side preview audition |
| 383b076 | W4 | Refresh README for W4 completion |
| f8c4419 | W4 | Pause at edited end when the tail is deleted |
| 6287425 | W4 | Virtual timeline — edited duration + scrubber + custom player |
| 80910e3 | W4 | Preview-skip fix, server-side session validation, autosave race |
| ee42649 | W4 | Select/delete/undo/redo, autosave, preview-skip, KPI scaffolding |
| fa70b09 | W3 | Validate audio/transcript at serve, harden UI, optional SHA-256 |
| a753125 | W3 | Local web UI with click-to-seek (FastAPI + plain HTML/JS) |
| 1a7ebe4 | W2 | Render command, session round-trip, OOB bugfix |
| ca97ecd | W2 | EditSession schema + ffmpeg atrim+concat renderer + `podedit cut` |
| c9f616e | W1 | Ensure transcript_bytes/total_wall_sec land in bench JSONL |
| ea4c87c | W1 | Foundation + one-way ASR PoC + bench harness |
