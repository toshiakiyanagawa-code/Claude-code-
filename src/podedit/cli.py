"""podedit CLI — W1: `transcribe` only."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn

from .asr import ASRConfig, resolve_device, transcribe
from .audio import AudioProbeError, FFmpegMissingError, probe, to_wav_16k_mono
from .bench import measure

console = Console()


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
        rec_asr["extra"]["segments"] = len(tx.segments)
        rec_asr["extra"]["word_count"] = sum(len(s.words) for s in tx.segments)
    total_wall += rec_asr["wall_sec"]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(tx.to_dict(), ensure_ascii=False, indent=2))
    rec_asr["extra"]["transcript_bytes"] = out_path.stat().st_size
    rec_asr["extra"]["total_wall_sec"] = total_wall

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


if __name__ == "__main__":
    cli()
