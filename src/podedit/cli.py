"""podedit CLI — `transcribe` (W1), `cut`/`render` (W2), `serve` (W3)."""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import click
from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn

from .asr import ASRConfig, resolve_device, transcribe
from .asr_eval import (
    compute_cer,
    compute_glossary_recall,
    find_eval_audio,
    kpi_summary_path,
    read_json,
    summarize_kpi_jsonl,
    transcript_to_dict,
    transcript_to_text,
    write_json,
)
from .audio import AudioProbeError, FFmpegMissingError, probe, to_wav_16k_mono
from .bench import measure
from .edit import EditSession, compile_timeline, sha256_of_file
from .render import RenderError, render_cuts, render_segments

console = Console()


ASR_EVAL_PRESETS = {
    "fast": {"model": "tiny", "beam_size": 1},
    "balanced": {"model": "small", "beam_size": 1},
    "quality": {"model": "large-v3-turbo", "beam_size": 5},
}


def _browser_url(host: str, port: int) -> str:
    codespace = os.environ.get("CODESPACE_NAME")
    forwarding_domain = os.environ.get("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN")
    if codespace and forwarding_domain:
        return f"https://{codespace}-{port}.{forwarding_domain}"
    if host in {"0.0.0.0", "::"}:
        return f"http://127.0.0.1:{port}"
    if ":" in host and not host.startswith("["):
        return f"http://[{host}]:{port}"
    return f"http://{host}:{port}"


@click.group()
def cli() -> None:
    """podedit — transcript-driven podcast editor (local-first)."""


@cli.command("asr-eval")
@click.argument("set_name")
@click.option("--model", default=None,
              help="faster-whisper model id (default: ASRConfig.model)")
@click.option("--beam-size", default=None, type=int,
              help="Beam size (default: ASRConfig.beam_size)")
@click.option("--preset", default=None,
              type=click.Choice(["fast", "balanced", "quality"]),
              help="Preset defaults for model/beam-size")
@click.option("--out-dir", type=click.Path(path_type=Path), default=None,
              help="Output directory for predicted transcript and run reports")
@click.option("--initial-prompt", default=None)
@click.option("--hotwords", default=None)
@click.option("--no-vad", is_flag=True, default=False,
              help="Disable VAD filter")
def asr_eval_cmd(
    set_name: str,
    model: str | None,
    beam_size: int | None,
    preset: str | None,
    out_dir: Path | None,
    initial_prompt: str | None,
    hotwords: str | None,
    no_vad: bool,
) -> None:
    """Run ASR on eval/asr/<set_name> and write accuracy/speed metrics."""
    base_cfg = ASRConfig()
    cfg_values = {
        "model": base_cfg.model,
        "beam_size": base_cfg.beam_size,
    }
    if preset is not None:
        cfg_values.update(ASR_EVAL_PRESETS[preset])
    if model is not None:
        cfg_values["model"] = model
    if beam_size is not None:
        cfg_values["beam_size"] = beam_size

    cfg = ASRConfig(
        model=cfg_values["model"],
        beam_size=cfg_values["beam_size"],
        vad_filter=not no_vad,
        initial_prompt=initial_prompt,
        hotwords=hotwords,
    )

    set_dir = Path("eval/asr") / set_name
    if not set_dir.exists():
        _fatal(f"Evaluation set not found: {set_dir}")

    try:
        audio_path = find_eval_audio(set_dir)
        source_info = probe(audio_path)
    except (FileNotFoundError, ValueError, FFmpegMissingError, AudioProbeError) as e:
        _fatal(str(e))

    reference_path = set_dir / "reference.transcript.json"
    meta_path = set_dir / "meta.json"
    if not reference_path.exists():
        _fatal(f"Reference transcript not found: {reference_path}")
    if not meta_path.exists():
        _fatal(f"Eval metadata not found: {meta_path}")

    output_dir = out_dir or set_dir
    runs_dir = output_dir / "runs"
    output_dir.mkdir(parents=True, exist_ok=True)
    runs_dir.mkdir(parents=True, exist_ok=True)

    run_id = (
        f"{datetime.now().strftime('%Y%m%d-%H%M')}-"
        f"{cfg.model.replace('/', '-')}-beam{cfg.beam_size}"
    )
    asr_wav_path = output_dir / f"{audio_path.stem}.16k.wav"
    predicted_path = output_dir / "predicted.transcript.json"
    report_path = runs_dir / f"{run_id}.json"

    total_start = time.perf_counter()
    ffmpeg_start = time.perf_counter()
    try:
        to_wav_16k_mono(audio_path, asr_wav_path)
    except (FFmpegMissingError, AudioProbeError) as e:
        _fatal(str(e))
    wall_sec_ffmpeg = time.perf_counter() - ffmpeg_start

    asr_start = time.perf_counter()
    tx, segments = transcribe(source_info, asr_wav_path, cfg)
    wall_sec_model_load = time.perf_counter() - asr_start
    for _segment in segments:
        pass
    wall_sec_asr = time.perf_counter() - asr_start
    wall_sec_total = time.perf_counter() - total_start

    predicted = transcript_to_dict(tx)
    write_json(predicted_path, predicted)

    reference = read_json(reference_path)
    meta = read_json(meta_path)
    reference_text = transcript_to_text(reference)
    predicted_text = transcript_to_text(predicted)
    cer = compute_cer(reference_text, predicted_text)
    glossary_recall, glossary_details = compute_glossary_recall(
        predicted_text,
        list(meta.get("glossary") or []),
    )
    glossary_found = sum(1 for item in glossary_details if item["found"])
    glossary_total = len(glossary_details)
    glossary_misses = [item["term"] for item in glossary_details if not item["found"]]

    resolved = resolve_device(cfg)
    duration_sec = float(getattr(source_info, "duration_sec", 0.0) or meta.get("duration_sec") or 0.0)
    rtf = wall_sec_asr / duration_sec if duration_sec > 0 else None

    report = {
        "run_id": run_id,
        "set_name": set_name,
        "config": {
            "model": cfg.model,
            "beam_size": cfg.beam_size,
            "vad_filter": cfg.vad_filter,
            "device": resolved.device,
            "compute_type": resolved.compute_type,
            "initial_prompt": cfg.initial_prompt,
            "hotwords": cfg.hotwords,
        },
        "timing": {
            "audio_duration_sec": duration_sec,
            "wall_sec_total": wall_sec_total,
            "wall_sec_ffmpeg": wall_sec_ffmpeg,
            "wall_sec_asr": wall_sec_asr,
            "wall_sec_model_load": wall_sec_model_load,
            "rtf": rtf,
        },
        "accuracy": {
            "cer": cer,
            "glossary_recall": glossary_recall,
            "glossary_details": glossary_details,
        },
        "artifacts": {
            "predicted_transcript": str(predicted_path),
        },
    }
    write_json(report_path, report)

    console.print(f"Source : {audio_path} ({duration_sec:.1f}s)")
    console.print(
        f"Model  : {cfg.model} / beam={cfg.beam_size} / "
        f"vad={'on' if cfg.vad_filter else 'off'} / {resolved.device}/{resolved.compute_type}"
    )
    console.print("")
    console.print("| Metric                 | Value       |")
    console.print("|------------------------|-------------|")
    console.print(f"| wall (total)           | {wall_sec_total:.1f}s      |")
    console.print(f"| wall (ASR only)        | {wall_sec_asr:.1f}s      |")
    console.print(f"| RTF                    | {rtf:.3f}       |" if rtf is not None else "| RTF                    | n/a         |")
    console.print(f"| CER                    | {cer * 100:.1f}%        |")
    console.print(
        f"| Glossary recall        | {glossary_recall * 100:.0f}% "
        f"({glossary_found}/{glossary_total})   |"
    )
    console.print("")
    console.print("Glossary misses: " + (", ".join(glossary_misses) if glossary_misses else "-"))
    console.print(f"Report written: {report_path}")


@cli.command("kpi-summary")
@click.argument("kpi_jsonl", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--audio-duration-sec", default=None, type=float,
              help="Audio duration used for per-hour normalisation")
def kpi_summary_cmd(kpi_jsonl: Path, audio_duration_sec: float | None) -> None:
    """Summarise editor correction clicks from a KPI JSONL log."""
    summary = summarize_kpi_jsonl(kpi_jsonl, audio_duration_sec=audio_duration_sec)
    out_path = kpi_summary_path(kpi_jsonl)
    write_json(out_path, summary)

    duration = summary["audio_duration_sec"]
    wall = summary["session_wall_sec"]
    counts = summary["counts"]
    per_hour = summary["correction_clicks_per_audio_hour"]

    duration_label = "unknown"
    if duration is not None:
        duration_label = f"{duration:.1f}s ({duration / 60.0:.1f} min)"
    wall_label = "unknown"
    if wall is not None:
        wall_label = f"{wall:.1f}s ({wall / 60.0:.1f} min)"

    console.print(f"KPI file       : {kpi_jsonl}")
    console.print(f"Audio duration : {duration_label}")
    console.print(f"Session wall   : {wall_label}")
    console.print("")
    console.print("| Metric                          | Value           |")
    console.print("|---------------------------------|-----------------|")
    console.print(f"| ops.delete                      | {counts['ops.delete']}              |")
    console.print(f"| ops.move                        | {counts['ops.move']}               |")
    console.print(f"| ops.fillers.added (auto)        | {counts['ops.fillers.added']}              |")
    console.print(f"| **correction clicks**           | **{summary['correction_clicks']}**          |")
    if per_hour is None:
        console.print("| **per hour of audio**           | **n/a**         |")
    else:
        console.print(f"| **per hour of audio**           | **{per_hour:.1f} / hr**  |")
    console.print(f"| word clicks (seek)              | {counts['word_clicks']}             |")
    console.print(f"| drag selections                 | {counts['drag_selections']}              |")
    console.print("")
    console.print(f"Summary written: {out_path}")


@cli.command("transcribe")
@click.argument("audio", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("-o", "--out", "out_path", type=click.Path(path_type=Path), default=None,
              help="Output JSON path (default: <work-dir>/<audio.stem>.transcript.json)")
@click.option("--work-dir", type=click.Path(path_type=Path), default=Path(".podedit/work"),
              show_default=True, help="Directory for derived artifacts (16k wav, etc.)")
@click.option("--model", default="small", show_default=True,
              help="faster-whisper model id (tiny|base|small|medium|large-v3|large-v3-turbo)")
@click.option("--lang", default="ja", show_default=True)
@click.option("--device", default="auto", show_default=True,
              type=click.Choice(["auto", "cpu", "cuda"]))
@click.option("--compute-type", default="auto", show_default=True,
              type=click.Choice(["auto", "int8", "int8_float16", "float16", "float32"]))
@click.option("--beam-size", default=5, show_default=True, type=int)
@click.option("--vad/--no-vad", default=True, show_default=True,
              help="VAD filter. Disable if Japanese aizuchi/laughter are being dropped.")
@click.option("--bench-log", type=click.Path(path_type=Path), default=Path("benchmarks.jsonl"),
              show_default=True, help="Append run metrics here")
@click.option("--no-checksum", is_flag=True, default=False,
              help="Skip source SHA-256. Faster; disables tamper detection downstream.")
def transcribe_cmd(
    audio: Path,
    out_path: Path | None,
    work_dir: Path,
    model: str,
    lang: str,
    device: str,
    compute_type: str,
    beam_size: int,
    vad: bool,
    bench_log: Path,
    no_checksum: bool,
) -> None:
    """Transcribe AUDIO and write a JSON transcript."""
    work_dir.mkdir(parents=True, exist_ok=True)
    asr_wav = work_dir / f"{audio.stem}.16k.wav"
    out_path = out_path or work_dir / f"{audio.stem}.transcript.json"

    try:
        info = probe(audio)
    except FFmpegMissingError as e:
        _fatal(str(e))
    except AudioProbeError as e:
        _fatal(f"Could not probe audio: {e}")

    console.print(
        f"[bold]Input[/bold]: {audio.name}  "
        f"({info.duration_sec:.1f}s, {info.sample_rate}Hz, {info.channels}ch, {info.codec})"
    )

    cfg = ASRConfig(
        model=model, language=lang, device=device,
        compute_type=compute_type, beam_size=beam_size, vad_filter=vad,
    )
    resolved = resolve_device(cfg)
    console.print(
        f"[dim]Device: {resolved.device}  Compute: {resolved.compute_type}  "
        f"Model: {model}  VAD: {'on' if vad else 'off'}[/dim]"
    )

    bench_extra = {
        "audio": str(audio),
        "duration_sec": info.duration_sec,
        "model": model,
        "lang": lang,
        "requested_device": device,
        "resolved_device": resolved.device,
        "compute_type": resolved.compute_type,
        "beam_size": beam_size,
        "vad_filter": vad,
    }
    total_wall = 0.0

    with measure("ffmpeg_to_16k", bench_log, extra={**bench_extra, "stage": "ffmpeg_to_16k"}) as rec_ff:
        try:
            to_wav_16k_mono(audio, asr_wav)
        except AudioProbeError as e:
            _fatal(f"ffmpeg resample failed: {e}")
    total_wall += rec_ff["wall_sec"]

    asr_t0 = time.perf_counter()
    with measure("asr_transcribe", bench_log, extra={**bench_extra, "stage": "asr_transcribe"}) as rec_asr:
        try:
            tx, gen = transcribe(info, asr_wav, cfg)
            with Progress(
                TextColumn("[bold blue]ASR"),
                BarColumn(),
                MofNCompleteColumn(),
                TimeElapsedColumn(),
                TextColumn("{task.fields[secs]:.1f}s audio covered"),
                console=console,
            ) as prog:
                task = prog.add_task("ASR", total=None, secs=0.0)
                for seg in gen:
                    prog.update(task, advance=1, secs=seg.end)
        except Exception as e:  # surface a friendlier line, then re-raise so bench logs error
            console.print(f"[red]ASR failed:[/red] {type(e).__name__}: {e}")
            raise
        if not no_checksum:
            tx.source_audio.sha256 = sha256_of_file(audio)
            rec_asr["extra"]["source_sha256"] = tx.source_audio.sha256
        rec_asr["extra"]["segments"] = len(tx.segments)
        rec_asr["extra"]["word_count"] = sum(len(s.words) for s in tx.segments)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(tx.to_dict(), ensure_ascii=False, indent=2))
        rec_asr["extra"]["transcript_path"] = str(out_path)
        rec_asr["extra"]["transcript_bytes"] = out_path.stat().st_size
        rec_asr["extra"]["total_wall_sec"] = rec_ff["wall_sec"] + (time.perf_counter() - asr_t0)
    total_wall = rec_asr["extra"]["total_wall_sec"]

    rtf = rec_asr["wall_sec"] / max(info.duration_sec, 1e-6)
    console.print(
        f"[green]✓[/green] Wrote {out_path}  "
        f"({len(tx.segments)} segments, {sum(len(s.words) for s in tx.segments)} words)"
    )
    console.print(
        f"  ASR wall: {rec_asr['wall_sec']:.1f}s   RTF: {rtf:.2f}x   "
        f"total: {total_wall:.1f}s   peak RSS: {rec_asr['process_peak_rss_mb']:.0f}MB"
    )
    console.print(f"  Bench log: {bench_log}")


def _fatal(msg: str) -> None:
    console.print(f"[red]Error:[/red] {msg}")
    sys.exit(2)


def _parse_time(s: str) -> float:
    """Parse seconds, 'M:SS', or 'H:MM:SS' into float seconds.

    For colon-separated forms, each minutes/seconds field must be < 60. This
    catches typos like "1:75" that would otherwise silently be accepted as
    1m75s = 135s.
    """
    s = s.strip()
    if not s:
        raise ValueError("Empty time string")
    if ":" not in s:
        return float(s)

    parts = s.split(":")
    if len(parts) == 2:
        m = int(parts[0])
        secs = float(parts[1])
        if secs < 0 or secs >= 60:
            raise ValueError(f"Seconds field must be in [0, 60) in {s!r}")
        if m < 0:
            raise ValueError(f"Minutes must be non-negative in {s!r}")
        return m * 60 + secs
    if len(parts) == 3:
        h = int(parts[0])
        m = int(parts[1])
        secs = float(parts[2])
        if m < 0 or m >= 60:
            raise ValueError(f"Minutes field must be in [0, 60) in {s!r}")
        if secs < 0 or secs >= 60:
            raise ValueError(f"Seconds field must be in [0, 60) in {s!r}")
        if h < 0:
            raise ValueError(f"Hours must be non-negative in {s!r}")
        return h * 3600 + m * 60 + secs
    raise ValueError(f"Invalid time format: {s!r}")


def _parse_range(rng: str) -> tuple[float, float]:
    """Parse a 'START-END' range. START and END may be seconds or M:SS / H:MM:SS."""
    s, _, e = rng.partition("-")
    if not s or not e:
        raise ValueError(f"Invalid range {rng!r}; expected START-END")
    s_sec, e_sec = _parse_time(s), _parse_time(e)
    if e_sec <= s_sec:
        raise ValueError(f"Range {rng!r}: END ({e_sec}s) must be greater than START ({s_sec}s)")
    return s_sec, e_sec


@cli.command("cut")
@click.argument("audio", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--delete", "-d", "deletes", multiple=True, required=True,
              help="Delete range 'START-END'. Times in seconds or M:SS / H:MM:SS. Repeatable. Saved sessions can also include move ops.")
@click.option("-o", "--out", "out_path", type=click.Path(path_type=Path), required=True,
              help="Output audio path. Delete and move ops render through the same timeline path.")
@click.option("--save-session", type=click.Path(path_type=Path), default=None,
              help="Also write the EditSession JSON here")
@click.option("--transcript", type=click.Path(exists=True, path_type=Path), default=None,
              help="Link this transcript JSON to the session (optional)")
@click.option("--no-checksum", is_flag=True, default=False,
              help="Skip SHA-256 of source audio (faster, less reproducible)")
@click.option("--crossfade-ms", default=10.0, show_default=True, type=float,
              help="Equal-power crossfade across each cut boundary. 0 = hard splice.")
@click.option("--lufs-target", default=-16.0, show_default=True, type=float,
              help="Integrated loudness target. Use a sentinel like 999 with --no-lufs to skip.")
@click.option("--no-lufs", is_flag=True, default=False,
              help="Skip LUFS normalization entirely.")
@click.option("--seam-analysis/--no-seam-analysis", default=True, show_default=True,
              help="W6: per-seam zero-cross snap + content-aware variable crossfade.")
@click.option("--lufs-two-pass", is_flag=True, default=False,
              help="W7: two-pass loudnorm (slower, ~±0.5 LU accuracy). Recommended for final export.")
def cut_cmd(
    audio: Path,
    deletes: tuple[str, ...],
    out_path: Path,
    save_session: Path | None,
    transcript: Path | None,
    no_checksum: bool,
    crossfade_ms: float,
    lufs_target: float,
    no_lufs: bool,
    seam_analysis: bool,
    lufs_two_pass: bool,
) -> None:
    """Apply --delete ranges to AUDIO and write a wav with those ranges removed.

    W5: sample-precise PCM render with an equal-power crossfade at each cut
    boundary and optional LUFS normalization (default -16 LUFS for podcasts).
    Saved sessions use the same render path and may include move ops.
    """
    try:
        ranges = [_parse_range(r) for r in deletes]
    except ValueError as e:
        _fatal(str(e))

    try:
        info = probe(audio)
    except FFmpegMissingError as e:
        _fatal(str(e))
    except AudioProbeError as e:
        _fatal(f"Could not probe audio: {e}")

    for s, e in ranges:
        if s < 0 or e > info.duration_sec:
            _fatal(f"Range {s}-{e}s falls outside audio duration {info.duration_sec:.2f}s")

    console.print(
        f"[bold]Source[/bold]: {audio.name}  ({info.duration_sec:.1f}s)  "
        f"deletes: {len(ranges)}"
    )

    source_ref = info.to_ref()
    if not no_checksum:
        console.print("[dim]Computing source SHA-256…[/dim]")
        source_ref.sha256 = sha256_of_file(audio)

    session = EditSession.new(
        source_audio=source_ref,
        transcript_ref=str(transcript) if transcript else None,
    )
    for s, e in ranges:
        session.add_delete(s, e)

    try:
        result = render_cuts(
            audio, info.duration_sec, ranges, out_path,
            crossfade_ms=crossfade_ms,
            lufs_target=None if no_lufs else lufs_target,
            seam_analysis=seam_analysis,
            lufs_two_pass=lufs_two_pass,
        )
    except RenderError as e:
        _fatal(str(e))

    console.print(
        f"[green]✓[/green] {out_path} [{result.output_format}]  "
        f"({result.duration_in:.1f}s → {result.duration_out:.1f}s, "
        f"{result.duration_in - result.duration_out:.1f}s cut, "
        f"{len(result.keeps)} keep ranges, max xfade {result.crossfade_ms:.1f}ms)"
    )
    if result.seam_analysis_used and result.seam_analyses:
        klass_counts: dict[str, int] = {}
        for a in result.seam_analyses:
            for side in ("end_class", "start_class"):
                klass_counts[a[side]] = klass_counts.get(a[side], 0) + 1
        summary = ", ".join(f"{v}× {k}" for k, v in sorted(klass_counts.items()))
        console.print(f"  Seam analysis: {len(result.seam_analyses)} seam(s) classified — {summary}")
    if result.lufs_measured_input is not None or result.lufs_out is not None:
        parts = []
        if result.lufs_measured_input is not None:
            parts.append(f"measured-in {result.lufs_measured_input:.1f} LUFS")
        if result.lufs_out is not None:
            parts.append(f"out {result.lufs_out:.1f} LUFS")
        if result.true_peak_dbtp is not None:
            parts.append(f"peak {result.true_peak_dbtp:.1f} dBTP")
        pass_kind = "two-pass" if result.lufs_two_pass else "single-pass"
        console.print(f"  Loudness ({pass_kind}): {' · '.join(parts)}")

    if save_session:
        save_session.parent.mkdir(parents=True, exist_ok=True)
        save_session.write_text(json.dumps(session.to_dict(), ensure_ascii=False, indent=2))
        console.print(f"  Session: {save_session}")


@cli.command("render")
@click.argument("session_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("-o", "--out", "out_path", type=click.Path(path_type=Path), required=True,
              help="Output wav path")
@click.option("--source-override", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              default=None,
              help="Use this audio file instead of the path recorded in the session")
@click.option("--check-checksum/--no-check-checksum", default=True, show_default=True,
              help="Verify the source SHA-256 matches the session record")
@click.option("--crossfade-ms", default=10.0, show_default=True, type=float)
@click.option("--lufs-target", default=-16.0, show_default=True, type=float)
@click.option("--no-lufs", is_flag=True, default=False, help="Skip LUFS normalization.")
@click.option("--seam-analysis/--no-seam-analysis", default=True, show_default=True,
              help="W6: per-seam zero-cross snap + content-aware variable crossfade.")
@click.option("--lufs-two-pass", is_flag=True, default=False,
              help="W7: two-pass loudnorm (slower, ~±0.5 LU accuracy). Recommended for final export.")
def render_cmd(
    session_path: Path,
    out_path: Path,
    source_override: Path | None,
    check_checksum: bool,
    crossfade_ms: float,
    lufs_target: float,
    no_lufs: bool,
    seam_analysis: bool,
    lufs_two_pass: bool,
) -> None:
    """Replay a saved EditSession, including delete and move ops, against its source audio."""
    try:
        session = EditSession.from_dict(json.loads(session_path.read_text()))
    except (KeyError, ValueError) as e:
        _fatal(f"Invalid session: {e}")
    except json.JSONDecodeError as e:
        _fatal(f"Session is not valid JSON: {e}")

    source = source_override or Path(session.source_audio.path)
    if not source.exists():
        _fatal(
            f"Source audio not found: {source}. "
            "Use --source-override PATH if the file has moved."
        )

    if check_checksum and session.source_audio.sha256:
        console.print("[dim]Verifying source SHA-256…[/dim]")
        actual = sha256_of_file(source)
        if actual != session.source_audio.sha256:
            _fatal(
                f"Source SHA-256 mismatch.\n  session: {session.source_audio.sha256}\n  actual:  {actual}\n"
                "Pass --no-check-checksum to render anyway."
            )

    try:
        info = probe(source)
    except (FFmpegMissingError, AudioProbeError) as e:
        _fatal(str(e))

    segments = compile_timeline(info.duration_sec, session.ops)
    move_count = sum(1 for op in session.ops if op.op == "move")
    console.print(
        f"[bold]Session[/bold]: {session_path.name}  "
        f"({len(session.ops)} ops, {move_count} move(s), source {source.name}, {info.duration_sec:.1f}s)"
    )

    try:
        result = render_segments(
            source, segments, out_path,
            source_duration=info.duration_sec,
            move_count=move_count,
            crossfade_ms=crossfade_ms,
            lufs_target=None if no_lufs else lufs_target,
            seam_analysis=seam_analysis,
            lufs_two_pass=lufs_two_pass,
        )
    except RenderError as e:
        _fatal(str(e))

    console.print(
        f"[green]✓[/green] {out_path} [{result.output_format}]  "
        f"({result.duration_in:.1f}s → {result.duration_out:.1f}s, "
        f"{result.duration_in - result.duration_out:.1f}s removed by duration, "
        f"{result.segments_count} segment(s), max xfade {result.crossfade_ms:.1f}ms)"
    )
    if result.seam_analysis_used and result.seam_analyses:
        klass_counts: dict[str, int] = {}
        for a in result.seam_analyses:
            for side in ("end_class", "start_class"):
                klass_counts[a[side]] = klass_counts.get(a[side], 0) + 1
        summary = ", ".join(f"{v}× {k}" for k, v in sorted(klass_counts.items()))
        console.print(f"  Seam analysis: {len(result.seam_analyses)} seam(s) — {summary}")
    if result.lufs_measured_input is not None or result.lufs_out is not None:
        parts = []
        if result.lufs_measured_input is not None:
            parts.append(f"measured-in {result.lufs_measured_input:.1f} LUFS")
        if result.lufs_out is not None:
            parts.append(f"out {result.lufs_out:.1f} LUFS")
        if result.true_peak_dbtp is not None:
            parts.append(f"peak {result.true_peak_dbtp:.1f} dBTP")
        pass_kind = "two-pass" if result.lufs_two_pass else "single-pass"
        console.print(f"  Loudness ({pass_kind}): {' · '.join(parts)}")


@cli.command("eval")
@click.argument("audio", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--delete", "-d", "deletes", multiple=True, required=True,
              help="Delete range 'START-END' (repeatable). Use the same flag as `cut`.")
@click.option("--out-dir", type=click.Path(path_type=Path),
              default=Path(".podedit/eval"), show_default=True)
@click.option("--click-threshold", default=0.25, show_default=True, type=float,
              help="Sample-to-sample delta above which we flag a click candidate")
def eval_cmd(audio: Path, deletes: tuple[str, ...], out_dir: Path, click_threshold: float) -> None:
    """Render `hard`, `fixed_10ms`, and `seam_aware` variants of the same cuts
    side-by-side and report a click-detection summary for each.

    Use this to validate that the W6 pipeline reduces seam clicks vs the
    earlier hard splice and uniform crossfade. The 10-cut evaluation set
    Codex asks for is just `podedit eval <audio> -d ... -d ... -d ...` with
    10 ranges chosen from the user's transcript.
    """
    import time

    try:
        ranges = [_parse_range(r) for r in deletes]
    except ValueError as e:
        _fatal(str(e))
    try:
        info = probe(audio)
    except (FFmpegMissingError, AudioProbeError) as e:
        _fatal(str(e))

    out_dir.mkdir(parents=True, exist_ok=True)
    stem = audio.stem
    variants = [
        ("hard", {"crossfade_ms": 0.0, "seam_analysis": False}),
        ("fixed_10ms", {"crossfade_ms": 10.0, "seam_analysis": False}),
        ("seam_aware", {"crossfade_ms": 50.0, "seam_analysis": True}),
    ]
    # Expected seam timestamps in OUTPUT space, by variant. With a crossfade
    # of length X the output shortens by X per seam, so the seam centers shift
    # cumulatively earlier compared to a hard splice. ``_seam_times`` computes
    # the OUTPUT-time center of each seam given the per-seam xfade vector.
    from .edit import keep_ranges_from_deletes
    keeps = keep_ranges_from_deletes(info.duration_sec, ranges)

    def _seam_times(per_seam_xfade_ms: list[float]) -> list[float]:
        out: list[float] = []
        cursor = 0.0
        for i in range(len(keeps) - 1):
            xs = per_seam_xfade_ms[i] / 1000.0 if i < len(per_seam_xfade_ms) else 0.0
            keep_len = keeps[i][1] - keeps[i][0]
            cursor += keep_len - xs / 2.0  # seam center sits inside the xfade region
            out.append(cursor)
            cursor += xs / 2.0  # second half of the xfade lives in the next keep
        return out

    console.print(f"[bold]Source[/bold]: {audio.name}  ({info.duration_sec:.1f}s)  "
                  f"{len(ranges)} deletes → {len(keeps)} keep ranges, {len(keeps) - 1} seam(s)")

    summary: list[dict] = []
    for name, kwargs in variants:
        out_path = out_dir / f"{stem}.{name}.wav"
        t0 = time.perf_counter()
        try:
            result = render_cuts(
                audio, info.duration_sec, ranges, out_path,
                lufs_target=None,  # eval focuses on seam quality, not loudness
                **kwargs,
            )
        except RenderError as e:
            _fatal(f"{name}: {e}")
        wall = time.perf_counter() - t0

        # Click detection over the output wav. soundfile reads small audio fine;
        # for very large outputs we'd want chunked, but eval typically targets
        # short cut sets.
        import soundfile as sf
        audio_out, sr_out = sf.read(str(out_path), dtype="float32", always_2d=False)
        from .seam_eval import detect_clicks
        # Per-variant expected seam centers — crossfade pulls them earlier.
        per_seam_xfade = result.seam_xfades_ms or ([result.crossfade_ms] * (len(keeps) - 1))
        seam_times = _seam_times(per_seam_xfade)
        # Lengthen the near-seam window enough to cover the xfade region itself
        # plus a few ms of slack (default detect_clicks window_ms=5).
        near_seam_window_ms = max(5.0, (max(per_seam_xfade) if per_seam_xfade else 0.0) + 5.0)
        clicks = detect_clicks(
            audio_out, sr_out, expected_seams_sec=seam_times,
            delta_threshold=click_threshold, window_ms=near_seam_window_ms,
        )
        near_seam = sum(1 for c in clicks if c["near_seam"])
        max_delta = max((c["delta"] for c in clicks), default=0.0)
        max_delta_near_seam = max((c["delta"] for c in clicks if c["near_seam"]), default=0.0)

        summary.append({
            "variant": name,
            "wall_sec": wall,
            "duration_out": result.duration_out,
            "max_xfade_ms": result.crossfade_ms,
            "seam_xfades_ms": per_seam_xfade,
            "seam_times_sec": seam_times,
            "near_seam_window_ms": near_seam_window_ms,
            "clicks_total": len(clicks),
            "clicks_near_seam": near_seam,
            "max_click_delta": max_delta,
            "max_click_delta_near_seam": max_delta_near_seam,
            "seam_analyses": result.seam_analyses,
        })
        console.print(
            f"  [green]✓[/green] {name:11s} wall {wall:5.1f}s  "
            f"dur {result.duration_out:7.2f}s  "
            f"clicks {len(clicks):3d} ({near_seam} near seams, max-near Δ {max_delta_near_seam:.3f})  "
            f"max Δ {max_delta:.3f}"
        )

    summary_path = out_dir / f"{stem}.eval_summary.json"
    summary_path.write_text(json.dumps({
        "audio": str(audio), "deletes": [{"start": s, "end": e} for s, e in ranges],
        "click_threshold": click_threshold,
        "variants": summary,
    }, ensure_ascii=False, indent=2))
    console.print(f"[dim]Summary: {summary_path}[/dim]")


@cli.command("serve")
@click.option("--audio", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None,
              help="Source audio to stream to the UI")
@click.option("--transcript", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None,
              help="Transcript JSON to render")
@click.option("--session", "session_path", type=click.Path(path_type=Path), default=None,
              help="EditSession JSON path. Auto-loaded if exists, auto-saved on UI changes. "
                   "Default: <audio.stem>.session.json next to the transcript.")
@click.option("--kpi-log", type=click.Path(path_type=Path), default=None,
              help="JSONL path for KPI events. Default: <audio.stem>.kpi.jsonl next to the transcript.")
@click.option("--library-dir", type=click.Path(path_type=Path), default=None,
              help="Directory the UI's Open dialog scans for switchable audio files. "
                   "Defaults to the parent dir of --audio.")
@click.option("--work-dir", type=click.Path(path_type=Path), default=None,
              help="Directory holding transcripts and sessions. Defaults to the parent dir of --transcript.")
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8765, show_default=True, type=int)
@click.option("--auth-password", default=None, envvar="PODEDIT_AUTH_PASSWORD",
              help="Require HTTP Basic auth (username: podedit). "
                   "Also reads PODEDIT_AUTH_PASSWORD. Recommended when "
                   "exposing the UI beyond 127.0.0.1 (Codespaces public port, "
                   "shared LAN, etc.). Prefer the env-var form over a literal "
                   "CLI arg — args end up in shell history and `ps`.")
def serve_cmd(
    audio: Path | None,
    transcript: Path | None,
    session_path: Path | None,
    kpi_log: Path | None,
    library_dir: Path | None,
    work_dir: Path | None,
    host: str,
    port: int,
    auth_password: str | None,
) -> None:
    """Start the local web UI on http://host:port."""
    import uvicorn

    from .server.app import AudioTranscriptMismatch, ServeConfig, create_app

    if (audio is None) != (transcript is None):
        _fatal("either both --audio and --transcript, or neither")

    if audio is not None and transcript is not None:
        session_path = session_path or transcript.parent / f"{audio.stem}.session.json"
        kpi_log = kpi_log or transcript.parent / f"{audio.stem}.kpi.jsonl"
    else:
        work_dir = work_dir or Path(".podedit/work")
        session_path = session_path or work_dir / "empty.session.json"
        kpi_log = kpi_log or work_dir / "empty.kpi.jsonl"

    try:
        app = create_app(ServeConfig(
            audio_path=audio,
            transcript_path=transcript,
            session_path=session_path,
            kpi_log_path=kpi_log,
            library_dir=library_dir,
            work_dir=work_dir,
            auth_password=auth_password,
        ))
    except AudioTranscriptMismatch as e:
        _fatal(str(e))
    except FileNotFoundError as e:
        _fatal(str(e))
    browser_url = _browser_url(host, port)
    if audio is not None and transcript is not None:
        console.print(
            f"[green]podedit UI[/green] serving "
            f"[bold]{audio.name}[/bold] + [bold]{transcript.name}[/bold] "
            f"at [link]{browser_url}[/link]"
        )
    else:
        console.print(
            f"[green]podedit UI[/green] serving empty state "
            f"at [link]{browser_url}[/link]"
        )
        console.print("  Open the UI and use Open (O) to upload or select audio.")
    console.print(f"  Session: {session_path}  ({'exists' if session_path.exists() else 'will be created'})")
    console.print(f"  KPI log: {kpi_log}")
    # Friendly hint when binding non-loopback without a password — it's the
    # one combination where someone on the same network (or in the same
    # Codespace with the port public) gets full access by default.
    if auth_password:
        console.print(
            "  [green]Auth:[/green] HTTP Basic enabled (username: [bold]podedit[/bold]). "
            "Share the URL and password with your editing team only."
        )
    elif host not in ("127.0.0.1", "localhost", "::1"):
        console.print(
            "  [yellow]WARNING:[/yellow] binding to non-loopback host without --auth-password. "
            "Anyone who can reach this URL can read and edit your sessions. "
            "Set PODEDIT_AUTH_PASSWORD (or pass --auth-password) before sharing."
        )
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    cli()
