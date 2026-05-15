from __future__ import annotations

import csv
import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_SCORE_THRESHOLD = 60.0
TV_HINTS = ("tv", "テレビ", "番組", "放送", "出演", "地上波", "日テレ", "tbs", "フジテレビ", "nhk")
DEFAMATION_HINTS = ("疑惑", "告発", "詐欺", "逮捕", "犯罪", "不倫", "炎上", "暴露", "反社")


def _plain(value: Any) -> Any:
    if is_dataclass(value):
        return _plain(asdict(value))
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except TypeError:
            pass
    if hasattr(value, "to_dict"):
        return _plain(value.to_dict())
    if hasattr(value, "model_dump"):
        return _plain(value.model_dump())
    return value


def _text(candidate: dict[str, Any]) -> str:
    values: list[str] = []
    for key in ("title", "description", "text", "transcript", "channel", "summary"):
        value = candidate.get(key)
        if value:
            values.append(str(value))
    for item in candidate.get("highlights") or []:
        if isinstance(item, dict):
            values.extend(str(v) for v in item.values() if v)
        elif item:
            values.append(str(item))
    return " ".join(values).lower()


def _score(candidate: dict[str, Any], now: datetime) -> tuple[float, dict[str, Any]]:
    breakdown = candidate.get("score_breakdown")
    if isinstance(breakdown, dict):
        for key in ("score", "total", "final_score"):
            if isinstance(breakdown.get(key), (int, float)):
                return float(breakdown[key]), dict(breakdown)
        numeric = [float(v) for v in breakdown.values() if isinstance(v, (int, float))]
        if numeric:
            return float(sum(numeric)), dict(breakdown)

    value = candidate.get("score", 0)
    try:
        return float(value), {"score": float(value)}
    except (TypeError, ValueError):
        return 0.0, {"score": 0.0}


def _reasons(candidate: dict[str, Any], score: float, default_score_threshold: float) -> list[str]:
    reasons: list[str] = []
    threshold = candidate.get("score_threshold", default_score_threshold)
    try:
        threshold_value = float(threshold)
    except (TypeError, ValueError):
        threshold_value = default_score_threshold

    haystack = _text(candidate)
    if score < threshold_value:
        reasons.append("score_below_threshold")
    if any(hint in haystack for hint in DEFAMATION_HINTS):
        reasons.append("defamation_review_required")
    if any(hint in haystack for hint in TV_HINTS):
        reasons.append("tv_hint_in_text")
    if candidate.get("blocked") or candidate.get("block_reason"):
        reasons.append("blocked_source")
    if candidate.get("usage_status") in {"ng", "blocked", "forbidden"}:
        reasons.append("usage_not_clear")
    return reasons


def review_candidates(
    candidates: list[dict],
    *,
    now: datetime | None = None,
    score_threshold: float = DEFAULT_SCORE_THRESHOLD,
) -> list[dict]:
    review_now = now or datetime.now(timezone.utc)
    reviewed: list[dict[str, Any]] = []

    for candidate in candidates:
        item = _plain(candidate)
        if not isinstance(item, dict):
            item = {"value": item}

        score, breakdown = _score(item, review_now)
        reasons = _reasons(item, score, score_threshold)

        reviewed_item = dict(item)
        reviewed_item["review_score"] = score
        reviewed_item["score_breakdown"] = breakdown
        reviewed_item["reason"] = reasons
        reviewed_item["review_required"] = bool(reasons)
        reviewed.append(reviewed_item)

    return reviewed


def write_tsv_report(path: Path, reviewed: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = (
        "id",
        "video_id",
        "url",
        "title",
        "channel_title",
        "channel_handle",
        "review_score",
        "score_breakdown_json",
        "usage_status",
        "reason",
        "review_required",
    )
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        for item in reviewed:
            row = dict(item)
            row["reason"] = ",".join(str(v) for v in item.get("reason", []))
            row["score_breakdown_json"] = json.dumps(_plain(item.get("score_breakdown", {})), ensure_ascii=False, sort_keys=True)
            writer.writerow(row)


def write_json_report(path: Path, reviewed: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_plain(reviewed), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
