from __future__ import annotations

import csv
import json
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


@dataclass(frozen=True)
class TakedownEntry:
    video_id: str | None = None
    channel_handle: str | None = None
    channel_id: str | None = None
    reason: str = ""


def _empty_to_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_handle(value: Any) -> str | None:
    text = _empty_to_none(value)
    if text is None:
        return None

    text = text.strip().rstrip("/")
    parsed = urlparse(text)
    if parsed.scheme and parsed.netloc:
        parts = [part for part in parsed.path.split("/") if part]
        handle_part = next((part for part in parts if part.startswith("@")), parts[-1] if parts else "")
        text = handle_part.strip().rstrip("/")

    text = text.strip().rstrip("/").lower()
    return text if text.startswith("@") else f"@{text}"


def _validate_reason(reason: str) -> str:
    text = reason.strip()
    if not text:
        raise ValueError("takedown entry requires non-empty reason")
    return text


def _normalize_channel_id(value: Any) -> str | None:
    channel_id = _empty_to_none(value)
    if channel_id and not channel_id.startswith("UC"):
        warnings.warn(f"channel_id should start with UC: {channel_id}", UserWarning, stacklevel=2)
    return channel_id


def _entry_from_dict(item: dict[str, Any]) -> TakedownEntry:
    return TakedownEntry(
        video_id=_empty_to_none(item.get("video_id") or item.get("id")),
        channel_handle=_normalize_handle(item.get("channel_handle") or item.get("handle")),
        channel_id=_normalize_channel_id(item.get("channel_id")),
        reason=_validate_reason(str(item.get("reason") or "")),
    )


def load_takedown_list(path: Path) -> list[TakedownEntry]:
    suffix = path.suffix.lower()
    if suffix == ".json":
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)

        if isinstance(raw, dict):
            raw = raw.get("takedowns", raw.get("entries", []))

        if not isinstance(raw, list):
            raise ValueError(f"{path}: JSON takedown list must be a list")

        return [_entry_from_dict(item) for item in raw if isinstance(item, dict)]

    with path.open("r", encoding="utf-8", newline="") as f:
        rows = csv.reader(f, delimiter="\t")
        entries: list[TakedownEntry] = []
        for row in rows:
            if not row or all(not cell.strip() for cell in row):
                continue
            if row[0].strip().lower() in {"id", "video_id"}:
                continue
            padded = row + [""] * (4 - len(row))
            entries.append(
                TakedownEntry(
                    video_id=_empty_to_none(padded[0]),
                    channel_handle=_normalize_handle(padded[1]),
                    channel_id=_normalize_channel_id(padded[2]),
                    reason=_validate_reason(padded[3]),
                )
            )
        return entries


def _candidate_video_id(candidate: dict[str, Any]) -> str | None:
    return _empty_to_none(candidate.get("video_id") or candidate.get("id"))


def _candidate_handle(candidate: dict[str, Any]) -> str | None:
    return _normalize_handle(candidate.get("channel_handle") or candidate.get("handle"))


def _matches(candidate: dict[str, Any], entry: TakedownEntry) -> bool:
    video_id = _candidate_video_id(candidate)
    channel_handle = _candidate_handle(candidate)
    channel_id = _empty_to_none(candidate.get("channel_id"))

    return any(
        (
            entry.video_id is not None and entry.video_id == video_id,
            entry.channel_handle is not None and entry.channel_handle == channel_handle,
            entry.channel_id is not None and entry.channel_id == channel_id,
        )
    )


def apply_takedown(
    candidates: list[dict],
    takedowns: list[TakedownEntry],
) -> tuple[list[dict], list[dict]]:
    passed: list[dict] = []
    blocked: list[dict] = []

    for candidate in candidates:
        match = next((entry for entry in takedowns if _matches(candidate, entry)), None)
        if match is None:
            passed.append(candidate)
            continue

        updated = dict(candidate)
        updated["usage_status"] = "blocked"
        updated["permission_reason"] = f"takedown: {match.reason}"
        blocked.append(updated)

    return passed, blocked


def load_candidates(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8") as f:
        raw = json.load(f)

    if isinstance(raw, list):
        candidates = raw
    elif isinstance(raw, dict):
        candidates = raw.get("candidates", raw.get("plans", []))
    else:
        candidates = []

    return [item for item in candidates if isinstance(item, dict)]


def write_compliance_result(path: Path, passed: list[dict], blocked: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"passed": passed, "blocked": blocked}
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
