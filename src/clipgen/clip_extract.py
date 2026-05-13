from __future__ import annotations

import json
import re
import shlex
from dataclasses import asdict, dataclass
from pathlib import Path


_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_ALLOWED_TARGET_FORMATS = {"short", "long"}
_ALLOWED_USAGE_STATUSES = {"cleared", "manual_review", "blocked"}


@dataclass
class ExtractPlan:
    video_id: str
    target_format: str
    source_url: str
    download_cmd: str
    cut_cmds: list[str]
    combine_cmd: str | None
    output_dir: str
    manifest: dict
    blocked_reason: str | None


def _quote(value: object) -> str:
    return shlex.quote(str(value))


def _usage_status(value: object) -> str:
    status = str(value or "manual_review")
    return status if status in _ALLOWED_USAGE_STATUSES else "manual_review"


def _validate_video_id(video_id: str) -> None:
    if not _VIDEO_ID_RE.fullmatch(video_id):
        raise ValueError("video_id must match [A-Za-z0-9_-]{1,32}")


def _validate_target_format(target_format: str) -> None:
    if target_format not in _ALLOWED_TARGET_FORMATS:
        raise ValueError("target_format must be one of: short, long")


def _validate_highlights(highlights: list[dict]) -> None:
    for highlight in highlights:
        if highlight["start_sec"] >= highlight["end_sec"]:
            raise ValueError("highlight start_sec must be less than end_sec")


def build_yt_dlp_command(video_id, output_path, *, with_subs=True) -> str:
    url = f"https://www.youtube.com/watch?v={video_id}"
    parts = [
        "yt-dlp",
        "-f",
        "bestvideo+bestaudio",
        "--merge-output-format",
        "mp4",
    ]
    if with_subs:
        parts.extend(["--write-auto-sub", "--sub-lang", "ja"])
    parts.extend([url, "-o", str(output_path)])
    return " ".join(_quote(part) for part in parts)


def build_ffmpeg_cut(input_file, start_sec, end_sec, output_file) -> str:
    parts = [
        "ffmpeg",
        # Keep -y intentionally: generated plans are rerunnable operation artifacts.
        "-y",
        "-ss",
        start_sec,
        "-to",
        end_sec,
        "-i",
        input_file,
        "-c:v",
        "libx264",
        "-c:a",
        "aac",
        "-preset",
        "fast",
        output_file,
    ]
    return " ".join(_quote(part) for part in parts)


def build_ffmpeg_concat(parts_file, output_file) -> str:
    parts = [
        "ffmpeg",
        # Keep -y intentionally: generated plans are rerunnable operation artifacts.
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        parts_file,
        "-c",
        "copy",
        output_file,
    ]
    return " ".join(_quote(part) for part in parts)


def plan_to_extract(plan: dict, *, output_root: Path) -> ExtractPlan:
    video_id = str(plan.get("video_id", ""))
    target_format = str(plan.get("target_format", "short"))
    _validate_video_id(video_id)
    _validate_target_format(target_format)

    source_url = str(plan.get("url") or f"https://www.youtube.com/watch?v={video_id}")
    output_dir = output_root / f"{video_id}_{target_format}"
    usage_status = _usage_status(plan.get("usage_status"))
    highlight_status = plan.get("highlight_status")
    highlights = list(plan.get("highlights") or [])
    _validate_highlights(highlights)

    manifest = {
        "video_id": video_id,
        "target_format": target_format,
        "source_url": source_url,
        "usage_status": usage_status,
        "highlight_status": highlight_status or ("ok" if highlights else "no_highlight"),
        "highlights": highlights,
        "outputs": {
            "input": str(output_dir / "source.mp4"),
            "parts_dir": str(output_dir / "parts"),
        },
    }

    if usage_status == "blocked":
        blocked_reason = (
            plan.get("blocked_reason")
            or plan.get("permission_reason")
            or "usage_status is blocked"
        )
        manifest["permission_scope"] = plan.get("permission_scope")
        manifest["permission_reason"] = plan.get("permission_reason")
        manifest["blocked_reason"] = blocked_reason
        return ExtractPlan(
            video_id=video_id,
            target_format=target_format,
            source_url=source_url,
            download_cmd="",
            cut_cmds=[],
            combine_cmd=None,
            output_dir=str(output_dir),
            manifest=manifest,
            blocked_reason=blocked_reason,
        )

    input_file = output_dir / "source.mp4"
    download_cmd = build_yt_dlp_command(video_id, input_file)

    selected_highlights = highlights[:1] if target_format == "short" else highlights
    cut_cmds: list[str] = []
    part_files: list[Path] = []

    for index, highlight in enumerate(selected_highlights, start=1):
        output_file = output_dir / "parts" / f"part_{index:03d}.mp4"
        part_files.append(output_file)
        cut_cmds.append(
            build_ffmpeg_cut(
                input_file,
                highlight["start_sec"],
                highlight["end_sec"],
                output_file,
            )
        )

    combine_cmd = None
    if target_format == "long" and part_files:
        parts_file = output_dir / "concat.txt"
        output_file = output_dir / "combined.mp4"
        combine_cmd = build_ffmpeg_concat(parts_file, output_file)
        manifest["outputs"]["concat_list"] = str(parts_file)
        manifest["outputs"]["combined"] = str(output_file)

    manifest["outputs"]["parts"] = [str(path) for path in part_files]

    return ExtractPlan(
        video_id=video_id,
        target_format=target_format,
        source_url=source_url,
        download_cmd=download_cmd,
        cut_cmds=cut_cmds,
        combine_cmd=combine_cmd,
        output_dir=str(output_dir),
        manifest=manifest,
        blocked_reason=None,
    )


def _write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def write_extract_plan(extract: ExtractPlan, root: Path) -> Path:
    output_dir = Path(extract.output_dir)
    if not output_dir.is_absolute():
        output_dir = root / output_dir

    output_dir.mkdir(parents=True, exist_ok=True)

    if extract.blocked_reason is not None:
        _write_text(output_dir / "BLOCKED_NOTICE.txt", extract.blocked_reason + "\n")
        _write_text(
            output_dir / "manifest.json",
            json.dumps(asdict(extract), ensure_ascii=False, indent=2) + "\n",
        )
        return output_dir

    parts_dir = output_dir / "parts"
    parts_dir.mkdir(parents=True, exist_ok=True)

    _write_text(output_dir / "download.sh", extract.download_cmd + "\n")
    _write_text(output_dir / "cut.sh", "\n".join(extract.cut_cmds) + ("\n" if extract.cut_cmds else ""))

    if extract.combine_cmd is not None:
        _write_text(output_dir / "combine.sh", extract.combine_cmd + "\n")
        concat_list = "\n".join(
            f"file parts/{shlex.quote(str(Path(part).name))}"
            for part in extract.manifest.get("outputs", {}).get("parts", [])
        )
        _write_text(output_dir / "concat.txt", concat_list)

    _write_text(
        output_dir / "manifest.json",
        json.dumps(asdict(extract), ensure_ascii=False, indent=2) + "\n",
    )
    return output_dir
