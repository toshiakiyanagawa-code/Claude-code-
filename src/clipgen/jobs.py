from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .pipeline import run_pipeline_live, run_pipeline_mock

try:
    from .clip_extract import plan_to_extract, write_extract_plan
except ImportError:  # pragma: no cover
    plan_to_extract = None  # type: ignore[assignment]
    write_extract_plan = None  # type: ignore[assignment]


TARGET_SHORT = "short"
VALID_FORMATS = {"short", "long", "both"}


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


def _formats(target_format: str) -> list[str]:
    if target_format not in VALID_FORMATS:
        raise ValueError(f"target_format must be one of {sorted(VALID_FORMATS)}, got {target_format!r}")
    return ["short", "long"] if target_format == "both" else [target_format]


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_to_plain(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _error_dict(stage: str, message: str) -> dict[str, str]:
    return {"error": stage, "message": message}


def _load_srt_text(
    *,
    srt_path: str | None,
    from_youtube: str | None,
) -> tuple[str | None, str | None]:
    if srt_path:
        return Path(srt_path).read_text(encoding="utf-8"), "srt"
    if from_youtube:
        from .transcripts import fetch_youtube_transcript, transcript_to_srt

        cues = fetch_youtube_transcript(from_youtube)
        if not cues:
            return None, "no_transcript"
        return transcript_to_srt(cues), "youtube"
    return None, None


def _discover_candidates(
    source: str,
    *,
    now: datetime,
    include_blocked: bool,
    target_format: str,
    live_dry_run: bool,
    lookback_days: int,
    max_per_query: int,
    query_limit: int | None,
    min_views: int,
) -> list[Any]:
    if source == "live":
        if live_dry_run:
            from .live_fixtures import run_pipeline_dryrun

            return list(
                run_pipeline_dryrun(
                    now=now,
                    include_blocked=include_blocked,
                    target_format=target_format,
                )
            )
        return list(
            run_pipeline_live(
                lookback_days=lookback_days,
                max_per_query=max_per_query,
                query_limit=query_limit,
                min_views=min_views,
                now=now,
                include_blocked=include_blocked,
                target_format=target_format,
            )
        )

    return list(
        run_pipeline_mock(
            _default_mock_path(source),
            now=now,
            include_blocked=include_blocked,
            target_format=target_format,
        )
    )


def _legacy_candidate_plan(
    candidate: Any,
    *,
    srt_text: str | None = None,
    target_format: str = TARGET_SHORT,
    aggressiveness: int | None = None,
    provider: Any = None,
) -> dict[str, Any]:
    plain = _to_plain(candidate)
    title = _get(candidate, "title") or plain.get("title") or plain.get("video_title") or "Untitled"
    usage_status = plain.get("usage_status") or plain.get("rights") or "manual_review"

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
        "highlight_status": "no_srt" if srt_text is None else ("ok" if highlights else "no_highlight"),
    }

    for key in (
        "video_id",
        "url",
        "channel",
        "channel_title",
        "channel_handle",
        "channel_id",
        "published_at",
        "score",
        "risk_flags",
        "permission_scope",
    ):
        value = plain.get(key)
        if value is not None:
            plan[key] = value

    if srt_text:
        plan["srt_text"] = srt_text
    if provider is not None:
        plan["provider"] = str(provider)
    return plan


def _candidate_plan(
    candidate: Any,
    *,
    srt_text: str | None = None,
    target_format: str = TARGET_SHORT,
    aggressiveness: int | None = None,
    provider: Any = None,
    highlight_status_override: str | None = None,
    min_score: float = 0.3,
    selection_mode: str = "legacy",
    llm_model: str = "claude-haiku-4-5",
    llm_top_k: int = 12,
    min_composite_score: float = 0.0,
) -> dict[str, Any]:
    if isinstance(candidate, dict):
        plan = _legacy_candidate_plan(
            candidate,
            srt_text=srt_text,
            target_format=target_format,
            aggressiveness=aggressiveness,
            provider=provider,
        )
    else:
        from .cli import _candidate_plan as build_candidate_plan

        plan = build_candidate_plan(
            candidate,
            srt_text=srt_text,
            target_format=target_format,
            aggressiveness=aggressiveness,
            provider=provider,
            highlight_status_override=highlight_status_override,
            min_score=min_score,
            selection_mode=selection_mode,
            llm_model=llm_model,
            llm_top_k=llm_top_k,
            min_composite_score=min_composite_score,
        )

    if highlight_status_override is not None:
        plan["highlight_status"] = highlight_status_override
    return plan


def _extract_one(plan: dict[str, Any], extract_dir: Path | None, dry_run: bool) -> Any:
    if plan_to_extract is None:
        return {"plan": plan, "status": "skipped", "reason": "plan_to_extract_unavailable"}

    output_root = extract_dir if extract_dir is not None else Path("_dryrun_extract")
    extract = plan_to_extract(plan, output_root=output_root)  # type: ignore[misc]
    if not dry_run:
        if write_extract_plan is None:
            return {"plan": plan, "status": "skipped", "reason": "write_extract_plan_unavailable"}
        write_extract_plan(extract, output_root)  # type: ignore[misc]
    return extract


def _load_takedowns(path: str | None):
    if not path:
        return None
    from .compliance import load_takedown_list

    return load_takedown_list(Path(path))


def _apply_takedowns(plans: list[dict[str, Any]], takedown_list: str | None) -> tuple[list[dict[str, Any]], dict[str, int]]:
    takedowns = _load_takedowns(takedown_list)
    if not takedowns:
        return plans, {"passed": len(plans), "blocked": 0}

    from .compliance import apply_takedown

    passed, blocked = apply_takedown(plans, takedowns)
    return [*passed, *blocked], {"passed": len(passed), "blocked": len(blocked)}


def _review_plans(plans: list[dict[str, Any]], *, score_threshold: float) -> list[dict[str, Any]]:
    from .review import review_candidates

    return review_candidates(plans, score_threshold=score_threshold)


def run_daily_job(
    date: str,
    out_dir: Path,
    *,
    dry_run: bool = False,
    source: str = "mock",
    include_blocked: bool = False,
    polish_provider: Any = None,
    aggressiveness: int | None = None,
    target_format: str = TARGET_SHORT,
    top: int = 5,
    srt_path: str | None = None,
    from_youtube: str | None = None,
    min_score: float = 0.3,
    selection_mode: str = "legacy",
    llm_model: str = "claude-haiku-4-5",
    llm_top_k: int = 12,
    min_composite_score: float = 0.0,
    review_threshold: float = 0.0,
    takedown_list: str | None = None,
    lookback_days: int = 14,
    max_per_query: int = 10,
    query_limit: int | None = None,
    min_views: int = 30_000,
    digest_top_n: int = 5,
    webhook_url: str | None = None,
) -> dict:
    errors: list[dict[str, str]] = []
    warnings: list[str] = []
    now = datetime.now(timezone.utc)
    day_dir = out_dir / date
    extract_dir = day_dir / "extract"
    candidates_by_format: dict[str, list[Any]] = {}
    plans: list[dict[str, Any]] = []
    extracts: list[Any] = []

    try:
        srt_text, srt_source = _load_srt_text(srt_path=srt_path, from_youtube=from_youtube)
        if srt_source == "no_transcript":
            warnings.append(f"no transcript found for YouTube video: {from_youtube}")
    except Exception as exc:
        srt_text = None
        errors.append(_error_dict("transcript", str(exc)))

    formats = _formats(target_format)

    for fmt in formats:
        try:
            cands = _discover_candidates(
                source,
                now=now,
                include_blocked=include_blocked,
                target_format=fmt,
                live_dry_run=dry_run,
                lookback_days=lookback_days,
                max_per_query=max_per_query,
                query_limit=query_limit,
                min_views=min_views,
            )
            candidates_by_format[fmt] = cands
        except Exception as exc:
            candidates_by_format[fmt] = []
            stage = "pipeline" if len(formats) == 1 else f"pipeline:{fmt}"
            errors.append(_error_dict(stage, str(exc)))
            continue

        usable = cands if include_blocked else [c for c in cands if _get(c, "usage_status") != "blocked"]
        for candidate in usable[:top]:
            try:
                plans.append(
                    _candidate_plan(
                        candidate,
                        srt_text=srt_text,
                        target_format=fmt,
                        aggressiveness=aggressiveness,
                        provider=polish_provider,
                        min_score=min_score,
                        selection_mode=selection_mode,
                        llm_model=llm_model,
                        llm_top_k=llm_top_k,
                        min_composite_score=min_composite_score,
                    )
                )
            except Exception as exc:
                stage = "plan" if len(formats) == 1 else f"plan:{fmt}"
                errors.append(_error_dict(stage, str(exc)))

    plans, compliance_summary = _apply_takedowns(plans, takedown_list)
    reviewed = _review_plans(plans, score_threshold=review_threshold)

    for plan in plans:
        try:
            extracts.append(_extract_one(plan, None if dry_run else extract_dir, dry_run))
        except Exception as exc:
            errors.append(_error_dict("extract", str(exc)))

    digest_text = ""
    try:
        from .notify import build_digest, post_slack

        review_summary = {
            "total": len(reviewed),
            "review_required": sum(1 for item in reviewed if item.get("review_required")),
        }
        digest_text = build_digest(plans, date=date, top_n=digest_top_n, reviewed=review_summary)
        if webhook_url and not dry_run:
            if not post_slack(webhook_url, digest_text):
                errors.append(_error_dict("digest", "failed to post Slack digest"))
    except Exception as exc:
        errors.append(_error_dict("digest", str(exc)))

    files: dict[str, str] = {}
    if not dry_run:
        extract_dir.mkdir(parents=True, exist_ok=True)
        all_candidates: list[Any] = []
        for fmt, cands in candidates_by_format.items():
            all_candidates.extend(cands)
            path = day_dir / f"candidates_{fmt}.json"
            _write_json(path, cands)
            files[f"candidates_{fmt}"] = str(path)
        candidates_path = day_dir / "candidates.json"
        _write_json(candidates_path, all_candidates)
        files["candidates"] = str(candidates_path)

        plan_path = day_dir / "plan.json"
        review_json = day_dir / "review.json"
        review_tsv = day_dir / "review.tsv"
        digest_path = day_dir / "digest.txt"

        _write_json(plan_path, {"generated_at": now.isoformat(), "plans": plans})
        _write_json(review_json, reviewed)
        from .review import write_tsv_report

        write_tsv_report(review_tsv, reviewed)
        digest_path.write_text(digest_text + ("\n" if digest_text else ""), encoding="utf-8")

        files.update(
            {
                "plan": str(plan_path),
                "review_json": str(review_json),
                "review_tsv": str(review_tsv),
                "digest": str(digest_path),
                "extract_dir": str(extract_dir),
            }
        )

    return {
        "date": date,
        "source": source,
        "target_format": target_format,
        "formats": formats,
        "candidates": sum(len(items) for items in candidates_by_format.values()),
        "candidates_by_format": {fmt: len(items) for fmt, items in candidates_by_format.items()},
        "plans": len(plans),
        "extracts": len(extracts),
        "review_required": sum(1 for item in reviewed if item.get("review_required")),
        "compliance": compliance_summary,
        "warnings": warnings,
        "errors": errors,
        "files": files,
    }
