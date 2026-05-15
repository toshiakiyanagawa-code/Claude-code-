"""ASR evaluation metrics and KPI-log summarisation helpers."""
from __future__ import annotations

import json
import unicodedata
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

AUDIO_EXTENSIONS = (".wav", ".mp3", ".m4a")


def _nfkc(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def _normalise_for_cer(text: str) -> str:
    return "".join(_nfkc(text).split())


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)

    if len(b) > len(a):
        a, b = b, a

    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            insert_cost = current[j - 1] + 1
            delete_cost = previous[j] + 1
            replace_cost = previous[j - 1] + (ca != cb)
            current.append(min(insert_cost, delete_cost, replace_cost))
        previous = current
    return previous[-1]


def compute_cer(reference: str, hypothesis: str) -> float:
    """Character Error Rate using Levenshtein distance / len(reference).

    Both strings are NFKC-normalised + whitespace-stripped before compare.
    """
    ref = _normalise_for_cer(reference)
    hyp = _normalise_for_cer(hypothesis)

    if not ref:
        return 0.0 if not hyp else 1.0

    distance = _levenshtein(ref, hyp)
    return min(1.0, distance / len(ref))


def compute_glossary_recall(
    hypothesis_text: str, glossary: list[str]
) -> tuple[float, list[dict[str, Any]]]:
    """For each glossary term, check if it appears in hypothesis_text."""
    hyp = _nfkc(hypothesis_text).lower()
    details: list[dict[str, Any]] = []

    for term in glossary:
        normalised_term = _nfkc(term).lower()
        occurrences = hyp.count(normalised_term) if normalised_term else 0
        details.append(
            {
                "term": term,
                "found": occurrences > 0,
                "occurrences": occurrences,
            }
        )

    if not glossary:
        return 0.0, details

    found = sum(1 for item in details if item["found"])
    return found / len(glossary), details


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def transcript_to_text(transcript: dict[str, Any]) -> str:
    """Concatenate every word.text in segment order, separator '' (ja).

    Tolerates the schema used by ``podedit transcribe`` output.
    """
    parts: list[str] = []
    for segment in _get(transcript, "segments", []) or []:
        for word in _get(segment, "words", []) or []:
            text = _get(word, "text")
            if text is not None:
                parts.append(str(text))
    return "".join(parts)


def transcript_to_dict(transcript: Any) -> dict[str, Any]:
    if isinstance(transcript, dict):
        return transcript
    if hasattr(transcript, "model_dump"):
        return transcript.model_dump(mode="json")
    if hasattr(transcript, "dict"):
        return transcript.dict()
    if is_dataclass(transcript):
        return asdict(transcript)
    raise TypeError(f"Unsupported transcript object: {type(transcript)!r}")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serialisable")


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=_json_default) + "\n",
        encoding="utf-8",
    )


def find_eval_audio(set_dir: Path) -> Path:
    matches = [
        path
        for path in set_dir.iterdir()
        if path.is_file() and path.stem == "audio" and path.suffix.lower() in AUDIO_EXTENSIONS
    ]
    if not matches:
        raise FileNotFoundError(f"No audio.wav/audio.mp3/audio.m4a found in {set_dir}")
    if len(matches) > 1:
        names = ", ".join(path.name for path in matches)
        raise ValueError(f"Multiple evaluation audio files found in {set_dir}: {names}")
    return matches[0]


def _event_name(row: dict[str, Any]) -> str | None:
    value = row.get("event") or row.get("name") or row.get("type")
    return str(value) if value is not None else None


def _nested_dicts(row: dict[str, Any]) -> list[dict[str, Any]]:
    dicts = [row]
    for key in ("payload", "data", "details", "meta"):
        value = row.get(key)
        if isinstance(value, dict):
            dicts.append(value)
    return dicts


def _number_from(row: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for container in _nested_dicts(row):
        for key in keys:
            value = container.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, int | float):
                return float(value)
            if isinstance(value, str):
                try:
                    return float(value)
                except ValueError:
                    continue
    return None


def _event_count(row: dict[str, Any], *, default: int = 1) -> int:
    value = _number_from(row, ("count", "n", "value"))
    if value is None:
        return default
    return int(value)


def _fillers_added_count(row: dict[str, Any]) -> int:
    value = _number_from(row, ("added", "fillers_added", "fillersAdded", "count", "n", "value"))
    if value is None:
        return 1
    return int(value)


def _audio_duration_from(row: dict[str, Any]) -> float | None:
    value = _number_from(
        row,
        ("audio_duration_sec", "audioDurationSec", "duration_sec", "durationSec"),
    )
    if value is not None:
        return value

    for container in _nested_dicts(row):
        audio = container.get("audio") or container.get("source_audio")
        if isinstance(audio, dict):
            value = _number_from(audio, ("duration_sec", "durationSec"))
            if value is not None:
                return value
    return None


def _timestamp(row: dict[str, Any]) -> float | None:
    value: Any = None
    for container in _nested_dicts(row):
        for key in ("ts", "t", "time", "timestamp", "created_at"):
            if key in container:
                value = container[key]
                break
        if value is not None:
            break

    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            pass
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(text).timestamp()
        except ValueError:
            return None
    return None


def summarize_kpi_jsonl(
    path: Path,
    *,
    audio_duration_sec: float | None = None,
) -> dict[str, Any]:
    counts = {
        "ops.delete": 0,
        "ops.move": 0,
        "word_clicks": 0,
        "drag_selections": 0,
        "ops.fillers.added": 0,
    }
    events_read = 0
    events_skipped = 0
    first_loaded_ts: float | None = None
    last_event_ts: float | None = None
    inferred_duration = audio_duration_sec

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        try:
            row = json.loads(raw_line)
        except json.JSONDecodeError:
            events_skipped += 1
            continue
        if not isinstance(row, dict):
            events_skipped += 1
            continue

        events_read += 1
        name = _event_name(row)
        ts = _timestamp(row)
        if ts is not None:
            last_event_ts = ts

        if name == "ui.loaded":
            if first_loaded_ts is None and ts is not None:
                first_loaded_ts = ts
            if inferred_duration is None:
                inferred_duration = _audio_duration_from(row)
        elif name == "ui.op.delete":
            counts["ops.delete"] += _event_count(row)
        elif name == "ui.op.move":
            counts["ops.move"] += _event_count(row)
        elif name in {"ui.click.word", "ui.dblclick.word"}:
            counts["word_clicks"] += _event_count(row)
        elif name == "ui.drag.select":
            counts["drag_selections"] += _event_count(row)
        elif name == "ui.annotation.fillers.added":
            counts["ops.fillers.added"] += _fillers_added_count(row)

    correction_clicks = (
        counts["ops.delete"] + counts["ops.move"] + counts["ops.fillers.added"]
    )
    per_hour = None
    if inferred_duration and inferred_duration > 0:
        per_hour = correction_clicks / (inferred_duration / 3600.0)

    session_wall_sec = None
    if first_loaded_ts is not None and last_event_ts is not None:
        session_wall_sec = max(0.0, last_event_ts - first_loaded_ts)

    return {
        "kpi_file": str(path),
        "audio_duration_sec": inferred_duration,
        "session_wall_sec": session_wall_sec,
        "counts": counts,
        "correction_clicks": correction_clicks,
        "correction_clicks_per_audio_hour": per_hour,
        "events_read": events_read,
        "events_skipped": events_skipped,
    }


def kpi_summary_path(path: Path) -> Path:
    if path.name.endswith(".kpi.jsonl"):
        return path.with_name(path.name[: -len(".kpi.jsonl")] + ".kpi-summary.json")
    return path.with_name(path.stem + ".kpi-summary.json")
