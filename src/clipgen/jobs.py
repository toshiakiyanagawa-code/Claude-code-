from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .pipeline import run_pipeline_mock

try:
    from .clip_extract import plan_to_extract
except ImportError:  # pragma: no cover
    plan_to_extract = None  # type: ignore[assignment]


TARGET_SHORT = "short"


def _to_plain(value: Any) -> Any:
    if is_dataclass(value):
        return _to_plain(asdict(value))
    if isinstance(value, dict):
        return {str(k): _to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_plain(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except TypeError:
            pass
    if hasattr(value, "to_dict"):
        return _to_plain(value.to_dict())
    if hasattr(value, "model_dump"):
        return _to_plain(value.model_dump())
    return value


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _default_mock_path(source: str) -> Path:
    if source and source != "mock":
        return Path(source)

    candidates = (
        Path("src/clipgen/data/mock_search.json"),
        Path("tests/fixtures/mock_sources.json"),
        Path("fixtures/mock_sources.json"),
        Path("mock_sources.json"),
    )
    for path in candidates:
        if path.exists():
            return path
    return candidates[0]


def _candidate_plan(
    candidate: Any,
    *,
    srt_text: str | None = None,
    target_format: str = TARGET_SHORT,
    aggressiveness: int | None = None,
    provider: Any = None,
) -> dict[str, Any]:
    plain = _to_plain(candidate)
    title = _get(candidate, "title") or plain.get("title") or plain.get("video_title") or "Untitled"
    usage_status = plain.get("usage_status") or plain.get("rights") or "review_required"

    highlights = plain.get("highlights") or plain.get("highlight_summary") or []
    if isinstance(highlights, str):
        highlights = [{"summary": highlights}]

    title_candidates = plain.get("title_candidates") or [title]
    if provider is not None:
        from .llm import polish_titles

        title_candidates = polish_titles(
            title_candidates,
            candidate,
            None,
            provider=provider,
            aggressiveness=aggressiveness if aggressiveness is not None else 2,
        )

    plan: dict[str, Any] = {
        "candidate": plain,
        "title": title,
        "title_candidates": title_candidates,
        "usage_status": usage_status,
        "target_format": target_format,
        "aggressiveness": aggressiveness,
        "highlights": highlights,
    }

    for key in ("video_id", "url", "channel", "published_at", "score"):
        value = plain.get(key)
        if value is not None:
            plan[key] = value

    if srt_text:
        plan["srt_text"] = srt_text
    if provider is not None:
        plan["provider"] = str(provider)
    return plan


def _extract_one(plan: dict[str, Any], extract_dir: Path | None, dry_run: bool) -> Any:
    if plan_to_extract is None:
        return {"plan": plan, "status": "skipped", "reason": "plan_to_extract_unavailable"}

    if dry_run:
        try:
            return plan_to_extract(plan, output_root=Path("/tmp/_dryrun"))  # type: ignore[misc]
        except (TypeError, ValueError):
            return {"plan": plan, "status": "dry_run"}

    try:
        return plan_to_extract(plan, output_root=extract_dir)  # type: ignore[misc]
    except (TypeError, ValueError) as exc:
        return {"plan": plan, "status": "error", "reason": str(exc)}


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(_to_plain(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _error_dict(stage: str, message: str) -> dict[str, str]:
    return {"error": stage, "message": message}


def run_daily_job(
    date: str,
    out_dir: Path,
    *,
    dry_run: bool = False,
    source: str = "mock",
    include_blocked: bool = False,
    polish_provider: Any = None,
    aggressiveness: int | None = None,
) -> dict:
    errors: list[dict[str, str]] = []
    candidates: list[Any] = []
    plans: list[dict[str, Any]] = []
    extracts: list[Any] = []

    try:
        candidates = list(
            run_pipeline_mock(
                _default_mock_path(source),
                now=datetime.now(timezone.utc),
                include_blocked=include_blocked,
                target_format=TARGET_SHORT,
            )
        )
    except Exception as exc:
        errors.append(_error_dict("pipeline", str(exc)))

    for candidate in candidates:
        try:
            plans.append(
                _candidate_plan(
                    candidate,
                    srt_text=_get(candidate, "srt_text"),
                    target_format=TARGET_SHORT,
                    aggressiveness=aggressiveness,
                    provider=polish_provider,
                )
            )
        except Exception as exc:
            errors.append(_error_dict("plan", str(exc)))

    extract_dir = out_dir / date / "extract"
    for plan in plans:
        try:
            extracts.append(_extract_one(plan, None if dry_run else extract_dir, dry_run))
        except Exception as exc:
            errors.append(_error_dict("extract", str(exc)))

    if not dry_run:
        day_dir = out_dir / date
        extract_dir.mkdir(parents=True, exist_ok=True)
        _write_json(day_dir / "candidates.json", candidates)
        _write_json(day_dir / "plan.json", plans)

    return {
        "date": date,
        "candidates": len(candidates),
        "plans": len(plans),
        "extracts": len(extracts),
        "errors": errors,
    }
