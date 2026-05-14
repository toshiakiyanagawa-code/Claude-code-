"""podedit CLI — `transcribe` (W1), `cut`/`render` (W2), `serve` (W3)."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import click
from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn

from .asr import ASRConfig, resolve_device, transcribe
from .audio import AudioProbeError, FFmpegMissingError, probe, to_wav_16k_mono
from .bench import measure
from .edit import EditSession, compile_timeline, sha256_of_file
from .render import RenderError, render_cuts, render_segments

console = Console()


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
def serve_cmd(
    audio: Path | None,
    transcript: Path | None,
    session_path: Path | None,
    kpi_log: Path | None,
    library_dir: Path | None,
    work_dir: Path | None,
    host: str,
    port: int,
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
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    cli()
