"""clipgen CLI.

例:
    # mock データで動作確認
    uv run python -m clipgen.cli discover --source mock \
        --mock src/clipgen/data/mock_search.json --out output/candidates.json

    # 実API呼び出し
    YOUTUBE_API_KEY=xxx uv run python -m clipgen.cli discover --source live --out output/candidates.json
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .highlights import detect_highlights, parse_srt
from .pipeline import (
    DEFAULT_LOOKBACK_DAYS,
    DEFAULT_MIN_VIEWS,
    candidates_to_dict,
    run_pipeline_live,
    run_pipeline_mock,
    write_json,
)
from .scoring import Candidate
from .titles import generate_thumbnails, generate_titles


def _rights_label(status: str) -> str:
    if status == "cleared":
        return "RIGHTS=CLEARED"
    if status == "blocked":
        return "RIGHTS=BLOCKED"
    return "RIGHTS=REVIEW"


def _defamation_label(risk_flags: list[str]) -> str:
    return "DEF=Y" if "defamation_review_required" in risk_flags else "DEF=-"


def _scope_label(scope: str) -> str:
    if not scope:
        return "SCOPE=-"
    return f"SCOPE={scope[:18]}"


def _print_table(cands, limit: int = 30) -> None:
    print(
        f"{'rank':>4}  {'score':>6}  {'views':>9}  {'rights':<14}  "
        f"{'def':<5}  {'scope':<24}  channel / title"
    )
    print("-" * 144)
    for i, c in enumerate(cands[:limit], start=1):
        title = c.title if len(c.title) <= 50 else c.title[:49] + "…"
        ch = c.channel_title[:18]
        print(
            f"{i:>4}  {c.score:>6.3f}  {c.view_count:>9}  {_rights_label(c.usage_status):<14}  "
            f"{_defamation_label(c.risk_flags):<5}  {_scope_label(c.permission_scope):<24}  "
            f"{ch:<18}  {title}"
        )


def _make_audit_logger(path: str | None):
    from .audit import AuditLogger

    return AuditLogger(Path(path) if path else None)


def _audit_call(logger: Any, method_name: str, payload: dict[str, Any]) -> None:
    if logger is None:
        return
    method = getattr(logger, method_name, None)
    if method is not None:
        try:
            method(payload)
            return
        except TypeError:
            try:
                method(**payload)
                return
            except TypeError:
                pass
    from . import audit as _audit

    func = getattr(_audit, method_name, None)
    if func is not None:
        try:
            func(logger, payload)
        except Exception:
            pass


def _discover_once(
    args: argparse.Namespace,
    *,
    target_format: str,
    now: datetime,
):
    if args.source == "mock":
        mock_path = Path(args.mock)
        if not mock_path.exists():
            print(f"mock file not found: {mock_path}", file=sys.stderr)
            return None
        return run_pipeline_mock(
            mock_path,
            now=now,
            include_blocked=args.include_blocked,
            target_format=target_format,
        )
    if args.dry_run:
        from .live_fixtures import run_pipeline_dryrun

        return run_pipeline_dryrun(
            now=now,
            include_blocked=args.include_blocked,
            target_format=target_format,
        )
    return run_pipeline_live(
        lookback_days=args.lookback_days,
        min_views=args.min_views,
        now=now,
        include_blocked=args.include_blocked,
        target_format=target_format,
    )


def _resolve_out_path(base: str | None, target_format: str, *, suffix: bool) -> Path | None:
    if not base:
        return None
    p = Path(base)
    if not suffix:
        return p
    return p.with_name(f"{p.stem}_{target_format}{p.suffix}")


def cmd_discover(args: argparse.Namespace) -> int:
    now = datetime.now(timezone.utc) if args.now is None else datetime.fromisoformat(args.now)
    formats = ["short", "long"] if args.target_format == "both" else [args.target_format]
    audit = _make_audit_logger(args.audit_log)

    for fmt in formats:
        cands = _discover_once(args, target_format=fmt, now=now)
        if cands is None:
            return 2

        out_path = _resolve_out_path(args.out, fmt, suffix=len(formats) > 1)
        if out_path:
            write_json(cands, out_path)
        _audit_call(
            audit,
            "log_discover",
            {
                "phase": "discover",
                "target_format": fmt,
                "count": len(cands),
                "out": str(out_path) if out_path else None,
                "source": args.source,
                "include_blocked": args.include_blocked,
            },
        )
        if not args.quiet:
            print(f"\n## format={fmt}")
            _print_table(cands)
            if not out_path:
                print()
                print(json.dumps(candidates_to_dict(cands)[:5], ensure_ascii=False, indent=2))
    return 0


def _candidate_plan(
    candidate: Candidate,
    *,
    srt_text: str | None,
    target_format: str,
    aggressiveness: int | None,
    provider=None,
    highlight_status_override: str | None = None,
    min_score: float = 0.3,
    selection_mode: str = "legacy",
    llm_model: str = "claude-haiku-4-5",
    llm_top_k: int = 12,
    min_composite_score: float = 0.0,
    candidate_dump_path: str | None = None,
) -> dict:
    """1候補に対し、ハイライト/タイトル/サムネをまとめて生成する.

    selection_mode:
      - "legacy"        : 旧キーワードベース (default、後方互換)
      - "political"     : 政治密度 + 多軸スコア (codex 2026-05 リサーチ)
      - "political_llm" : political → 上位を Claude API で rerank
    """
    highlights = []
    if srt_text is not None:
        cues = parse_srt(srt_text)
        if selection_mode == "legacy":
            highlights = detect_highlights(cues, target_format=target_format, min_score=min_score)
        else:
            from .political_scoring import select_political_highlights
            highlights, all_features = select_political_highlights(
                cues,
                target_format=target_format,
                min_composite_score=min_composite_score,
            )
            if selection_mode == "political_llm":
                from .llm_rerank import rerank_candidates
                from .political_scoring import apply_overlap_filter
                from .highlights import Highlight

                rerank = rerank_candidates(
                    all_features,
                    program_title=candidate.title,
                    target_format=target_format,
                    top_k=llm_top_k,
                    model=llm_model,
                    cues=cues,
                )
                if rerank:
                    publishable_items = [r for r in rerank if r.publishable]
                    if not publishable_items:
                        # 全件 publishable=false → 文脈安全違反として政治スコアの highlights を維持
                        # (rerank しない、または no_highlight)
                        import sys
                        print(
                            "warning: LLM judged no candidates as publishable; keeping deterministic highlights",
                            file=sys.stderr,
                        )
                    elif target_format == "short":
                        top = max(publishable_items, key=lambda r: r.llm_score)
                        highlights = [
                            Highlight(
                                start_sec=top.start_sec,
                                end_sec=top.end_sec,
                                score=round(top.llm_score, 3),
                                rationale=[f"llm_score={top.llm_score:.1f}", top.reasoning],
                                keywords=[],
                            )
                        ]
                    else:
                        # long: スコア降順 → overlap filter で重複除外 → 時系列ソート
                        sorted_pub = sorted(publishable_items, key=lambda r: r.llm_score, reverse=True)
                        filtered = apply_overlap_filter(sorted_pub, max_windows=5)
                        filtered.sort(key=lambda r: r.start_sec)
                        highlights = [
                            Highlight(
                                start_sec=r.start_sec,
                                end_sec=r.end_sec,
                                score=round(r.llm_score, 3),
                                rationale=[f"llm_score={r.llm_score:.1f}", r.reasoning],
                                keywords=[],
                            )
                            for r in filtered
                        ]
            if candidate_dump_path:
                from pathlib import Path as _P
                _P(candidate_dump_path).parent.mkdir(parents=True, exist_ok=True)
                _dump = {
                    "video_id": candidate.video_id,
                    "target_format": target_format,
                    "selection_mode": selection_mode,
                    "candidates": [
                        f.as_dict()
                        for f in sorted(all_features, key=lambda w: w.composite_score(), reverse=True)[:30]
                    ],
                }
                _P(candidate_dump_path).write_text(
                    json.dumps(_dump, ensure_ascii=False, indent=2), encoding="utf-8"
                )
    highlight_status = "no_srt" if srt_text is None else ("no_highlight" if not highlights else "ok")
    if highlight_status_override is not None:
        highlight_status = highlight_status_override
    top_highlight = highlights[0] if highlights else None
    titles = generate_titles(
        candidate,
        top_highlight,
        target_format=target_format,
        aggressiveness=aggressiveness,
    )
    if provider is not None:
        from .llm import polish_titles

        effective_aggressiveness = aggressiveness if aggressiveness is not None else 2
        titles = polish_titles(
            titles,
            candidate,
            top_highlight,
            provider=provider,
            aggressiveness=effective_aggressiveness,
        )
    thumbs = generate_thumbnails(
        candidate,
        top_highlight,
        target_format=target_format,
        aggressiveness=aggressiveness,
    )
    return {
        "video_id": candidate.video_id,
        "title": candidate.title,
        "channel_title": candidate.channel_title,
        "url": candidate.url,
        "score": candidate.score,
        "usage_status": candidate.usage_status,
        "permission_scope": candidate.permission_scope,
        "target_format": target_format,
        "highlight_status": highlight_status,
        "highlights": [h.as_dict() for h in highlights],
        "title_candidates": [t.as_dict() for t in titles],
        "thumbnail_candidates": [th.as_dict() for th in thumbs],
        "risk_flags": candidate.risk_flags,
    }


def cmd_plan(args: argparse.Namespace) -> int:
    """discover の出力をもとに、上位候補ごとにハイライト/タイトル/サムネを作る."""
    now = datetime.now(timezone.utc) if args.now is None else datetime.fromisoformat(args.now)
    formats = ["short", "long"] if args.target_format == "both" else [args.target_format]
    audit = _make_audit_logger(args.audit_log)

    srt_text: str | None = None
    highlight_status_override: str | None = None
    if args.srt:
        srt_path = Path(args.srt)
        if not srt_path.exists():
            print(f"srt file not found: {srt_path}", file=sys.stderr)
            return 2
        srt_text = srt_path.read_text(encoding="utf-8")
    elif args.from_youtube:
        from .transcripts import fetch_youtube_transcript, transcript_to_srt

        cues = fetch_youtube_transcript(args.from_youtube)
        if cues:
            srt_text = transcript_to_srt(cues)
        else:
            print(
                f"warning: no transcript found for YouTube video: {args.from_youtube}",
                file=sys.stderr,
            )
            highlight_status_override = "no_transcript"

    aggressiveness = args.aggressiveness

    provider = None
    if getattr(args, "polish", False):
        if os.environ.get("ANTHROPIC_API_KEY"):
            from .llm import AnthropicProvider

            provider = AnthropicProvider(model=args.polish_model)
        else:
            print(
                "warning: --polish was requested but ANTHROPIC_API_KEY is not set; continuing without LLM polish",
                file=sys.stderr,
            )

    plans: list[dict] = []
    for fmt in formats:
        cands = _discover_once(args, target_format=fmt, now=now)
        if cands is None:
            return 2
        usable = cands if args.include_blocked else [c for c in cands if c.usage_status != "blocked"]
        for c in usable[: args.top]:
            plans.append(
                _candidate_plan(
                    c,
                    srt_text=srt_text,
                    target_format=fmt,
                    aggressiveness=aggressiveness,
                    provider=provider,
                    highlight_status_override=highlight_status_override,
                    min_score=getattr(args, "min_score", 0.3),
                    selection_mode=getattr(args, "selection_mode", "legacy"),
                    llm_model=getattr(args, "llm_model", "claude-haiku-4-5"),
                    llm_top_k=getattr(args, "llm_top_k", 12),
                    min_composite_score=getattr(args, "min_composite_score", 0.0),
                    candidate_dump_path=getattr(args, "dump_candidates", None),
                )
            )

    output = {
        "generated_at": now.isoformat(),
        "plans": plans,
    }
    out_path = Path(args.out) if args.out else None
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    _audit_call(
        audit,
        "log_polish",
        {
            "phase": "plan",
            "polish_requested": bool(getattr(args, "polish", False)),
            "provider_enabled": provider is not None,
            "plans": len(plans),
            "out": str(out_path) if out_path else None,
        },
    )
    if not args.quiet:
        print(f"plans: {len(plans)} (format={args.target_format}, top={args.top})")
        for p in plans:
            print(
                f"  [{p['target_format']}] {p['usage_status']:<13} score={p['score']:.3f} "
                f"highlight={p['highlight_status']:<12} {p['channel_title']:<18} {p['title']}"
            )
            for t in p["title_candidates"][:3]:
                print(f"      title: {t['text']}")
    return 0


def _first_output_line(result: subprocess.CompletedProcess) -> str:
    text = (result.stdout or result.stderr or "").strip()
    if not text:
        return "出力なし"
    return text.splitlines()[0].strip()


def _run_version_command(label: str, command: list[str]) -> tuple[str | None, str | None]:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except FileNotFoundError:
        return None, f"{label} が見つかりません。PATH または仮想環境を確認してください。"
    except subprocess.TimeoutExpired:
        return None, f"{label} のバージョン取得が 10 秒でタイムアウトしました。"
    except OSError as exc:
        return None, f"{label} のバージョン取得に失敗しました: {exc}"

    line = _first_output_line(result)
    if result.returncode != 0:
        return None, f"{label} のバージョン取得に失敗しました (終了コード {result.returncode}): {line}"
    return line, None


def _check_runtime_versions() -> tuple[list[str], list[str]]:
    infos: list[str] = []
    errors: list[str] = []
    commands = [
        ("python", [sys.executable, "--version"]),
        ("ffmpeg", ["ffmpeg", "-version"]),
        ("yt-dlp", ["yt-dlp", "--version"]),
    ]

    for label, command in commands:
        version, error = _run_version_command(label, command)
        if error is not None:
            errors.append(error)
        else:
            infos.append(f"{label} バージョン: {version}")

    return infos, errors


def _check_job_dir(job_dir: str | None) -> tuple[list[str], list[str], list[str]]:
    infos: list[str] = []
    warnings: list[str] = []
    errors: list[str] = []

    if not job_dir:
        return infos, warnings, errors

    root = Path(job_dir)
    if not root.exists():
        errors.append(f"ジョブディレクトリが見つかりません: {root}")
        return infos, warnings, errors
    if not root.is_dir():
        errors.append(f"ジョブディレクトリではありません: {root}")
        return infos, warnings, errors

    source_mp4 = root / "source.mp4"
    if not source_mp4.exists():
        errors.append(f"source.mp4 が見つかりません: {source_mp4}")
    elif not source_mp4.is_file():
        errors.append(f"source.mp4 が通常ファイルではありません: {source_mp4}")
    else:
        try:
            size = source_mp4.stat().st_size
        except OSError as exc:
            errors.append(f"source.mp4 のサイズ確認に失敗しました: {exc}")
        else:
            mb = size / (1024 * 1024)
            if size <= 0:
                errors.append(f"source.mp4 が空です: {source_mp4}")
            elif size < 100 * 1024 * 1024:
                warnings.append(
                    f"source.mp4 のサイズが小さい可能性 (要 100MB 以上、実測 {mb:.1f}MB): {source_mp4}"
                )
            else:
                infos.append(f"source.mp4 を確認しました: {source_mp4} ({mb:.1f}MB)")

    source_srt = root / "source.ja.srt"
    if not source_srt.exists():
        errors.append(f"source.ja.srt が見つかりません: {source_srt}")
    elif not source_srt.is_file():
        errors.append(f"source.ja.srt が通常ファイルではありません: {source_srt}")
    else:
        try:
            srt_text = source_srt.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError as exc:
            errors.append(f"source.ja.srt を UTF-8 として読めません: {exc}")
        except OSError as exc:
            errors.append(f"source.ja.srt の読み込みに失敗しました: {exc}")
        else:
            try:
                cues = parse_srt(srt_text)
            except Exception as exc:
                errors.append(f"source.ja.srt の SRT 解析に失敗しました: {exc}")
            else:
                if not cues:
                    errors.append(f"source.ja.srt を解析できましたが字幕区間が 0 件です: {source_srt}")
                else:
                    infos.append(f"source.ja.srt を解析しました: {len(cues)} 区間")

    return infos, warnings, errors


def cmd_config_check(args: argparse.Namespace) -> int:
    from .config_check import run_config_check

    warnings, errors = run_config_check()

    infos, version_errors = _check_runtime_versions()
    errors.extend(version_errors)

    job_infos, job_warnings, job_errors = _check_job_dir(args.job_dir)
    infos.extend(job_infos)
    warnings.extend(job_warnings)
    errors.extend(job_errors)

    for info in infos:
        print(f"情報: {info}")
    for w in warnings:
        print(f"警告: {w}", file=sys.stderr)
    for e in errors:
        print(f"エラー: {e}", file=sys.stderr)

    if not warnings and not errors:
        print("情報: 設定検証は正常に完了しました。")

    return 1 if errors else 0


def cmd_compliance_check(args) -> int:
    from .compliance import apply_takedown, load_candidates, load_takedown_list, write_compliance_result

    candidates = load_candidates(Path(args.input))
    takedowns = load_takedown_list(Path(args.takedown_list))

    passed, blocked = apply_takedown(candidates, takedowns)
    payload = {"passed": passed, "blocked": blocked}
    if args.out:
        write_compliance_result(Path(args.out), passed, blocked)
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"passed: {len(passed)}, blocked: {len(blocked)}", file=sys.stderr)
    return 0


def cmd_extract(args) -> int:
    from .clip_extract import plan_to_extract, write_extract_plan

    audit = _make_audit_logger(args.audit_log)
    plan_path = Path(args.plan)
    payload = json.loads(plan_path.read_text(encoding="utf-8"))

    if isinstance(payload, list):
        plans = payload
    elif isinstance(payload, dict):
        if isinstance(payload.get("plans"), list):
            plans = payload["plans"]
        elif isinstance(payload.get("candidates"), list):
            plans = payload["candidates"]
        elif isinstance(payload.get("items"), list):
            plans = payload["items"]
        else:
            plans = [payload]
    else:
        raise SystemExit("plan JSON must be an object or list")

    out_root = Path(args.out_root)
    extracts = [plan_to_extract(plan, output_root=out_root) for plan in plans[: args.top]]

    _audit_call(
        audit,
        "log_extract",
        {
            "phase": "extract",
            "plan": str(plan_path),
            "out_root": str(out_root),
            "count": len(extracts),
            "dry_run": args.dry_run,
        },
    )

    if args.dry_run:
        for extract in extracts:
            print(f"# {extract.video_id} {extract.target_format}")
            if extract.blocked_reason is not None:
                print(f"BLOCKED: {extract.blocked_reason}")
                print()
                continue
            if extract.download_cmd:
                print(extract.download_cmd)
            for cmd in extract.cut_cmds:
                print(cmd)
            if extract.combine_cmd:
                print(extract.combine_cmd)
            print()
        return 0

    for extract in extracts:
        write_extract_plan(extract, out_root)
    return 0


def cmd_run_job(args: argparse.Namespace) -> int:
    from .jobs import run_daily_job

    provider = None
    if args.polish:
        if os.environ.get("ANTHROPIC_API_KEY"):
            from .llm import AnthropicProvider

            provider = AnthropicProvider(model=args.polish_model)
        else:
            print(
                "warning: --polish was requested but ANTHROPIC_API_KEY is not set; continuing without LLM polish",
                file=sys.stderr,
            )

    result = run_daily_job(
        args.date,
        Path(args.out_root),
        dry_run=args.dry_run,
        source=args.source,
        include_blocked=args.include_blocked,
        polish_provider=provider,
        aggressiveness=args.aggressiveness,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result.get("errors") else 0


def _load_items_payload(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return {"items": payload}, payload
    if not isinstance(payload, dict):
        raise SystemExit("input JSON must be an object or list")

    for key in ("plans", "candidates", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return payload, value
    return payload, [payload]


def _score_as_percent(value: Any) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    if 0.0 <= score <= 1.0:
        return score * 100.0
    return score


def _review_items(items: list[dict[str, Any]], threshold: float) -> dict[str, Any]:
    reviewed = []
    for item in items:
        score = _score_as_percent(item.get("score"))
        reviewed.append(
            {
                "video_id": item.get("video_id"),
                "title": item.get("title"),
                "channel_title": item.get("channel_title"),
                "score": score,
                "usage_status": item.get("usage_status"),
                "target_format": item.get("target_format"),
                "passed": score >= threshold,
                "risk_flags": item.get("risk_flags", []),
            }
        )
    passed = [item for item in reviewed if item["passed"]]
    return {
        "score_threshold": threshold,
        "total": len(reviewed),
        "passed": len(passed),
        "items": reviewed,
    }


def _write_review_tsv(path: Path, items: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["video_id", "title", "channel_title", "score", "usage_status", "target_format", "passed"]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(items)


def cmd_review(args: argparse.Namespace) -> int:
    input_path = Path(args.input)
    _, items = _load_items_payload(input_path)
    threshold = args.score_threshold * 100.0 if 0.0 <= args.score_threshold <= 1.0 else args.score_threshold
    result = _review_items(items, threshold)

    wrote = False
    if args.out_json:
        out_json = Path(args.out_json)
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        wrote = True
    if args.out_tsv:
        _write_review_tsv(Path(args.out_tsv), result["items"])
        wrote = True
    if not wrote:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _digest_plans(plans: list[dict[str, Any]], *, date: str, top_n: int) -> dict[str, Any]:
    ranked = sorted(plans, key=lambda p: _score_as_percent(p.get("score")), reverse=True)[:top_n]
    items = [
        {
            "rank": i,
            "video_id": plan.get("video_id"),
            "title": plan.get("title"),
            "channel_title": plan.get("channel_title"),
            "score": _score_as_percent(plan.get("score")),
            "target_format": plan.get("target_format"),
            "url": plan.get("url"),
        }
        for i, plan in enumerate(ranked, start=1)
    ]
    return {
        "date": date,
        "top_n": top_n,
        "items": items,
        "text": "\n".join(
            f"{item['rank']}. {item.get('title') or '-'} ({item.get('score', 0):.1f})"
            for item in items
        ),
    }


def cmd_digest(args: argparse.Namespace) -> int:
    from .notify import build_digest, post_slack

    _, plans = _load_items_payload(Path(args.plans))
    digest = _digest_plans(plans, date=args.date, top_n=args.top_n)
    digest["text"] = build_digest(plans, date=args.date, top_n=args.top_n)

    if args.dry_run or not args.webhook_url:
        print(json.dumps(digest, ensure_ascii=False, indent=2))
        return 0

    posted = post_slack(args.webhook_url, digest["text"])
    return 0 if posted else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="clipgen", description="政治系切り抜き動画 素材候補抽出ツール")
    sub = p.add_subparsers(dest="cmd", required=True)

    cc = sub.add_parser("config-check", help="設定ファイル・環境変数・出力先を検証")
    cc.add_argument("--job-dir", help="source.mp4/source.ja.srt を検証するジョブディレクトリ")
    cc.set_defaults(func=cmd_config_check)

    cp = sub.add_parser("compliance-check", help="削除依頼/権利リストで候補を再フィルタ")
    cp.add_argument("--input", required=True, help="candidates または plans の JSON")
    cp.add_argument("--takedown-list", required=True, help="JSON または TSV の takedown リスト")
    cp.add_argument("--out", help="結果 JSON 出力先 (省略時は stdout)")
    cp.set_defaults(func=cmd_compliance_check)

    pp = sub.add_parser("plan", help="候補ごとにハイライト/タイトル/サムネ案を生成する")
    pp.add_argument("--source", choices=["mock", "live"], default="mock")
    pp.add_argument(
        "--mock",
        default="src/clipgen/data/mock_search.json",
        help="--source mock のときの入力 JSON",
    )
    pp.add_argument("--out", help="プラン JSON 出力先")
    pp.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    pp.add_argument("--min-views", type=int, default=DEFAULT_MIN_VIEWS)
    pp.add_argument("--include-blocked", action="store_true")
    pp.add_argument(
        "--format",
        dest="target_format",
        choices=["short", "long", "both"],
        default="both",
        help="出力フォーマット (short / long / both)",
    )
    pp.add_argument("--dry-run", action="store_true")
    transcript_group = pp.add_mutually_exclusive_group()
    transcript_group.add_argument("--srt", help="ハイライト検出に使う SRT/VTT ファイル")
    transcript_group.add_argument("--from-youtube", help="YouTube video ID から字幕を取得して使う")
    pp.add_argument("--top", type=int, default=5, help="各フォーマットで処理する候補数")
    pp.add_argument(
        "--aggressiveness",
        type=int,
        choices=[0, 1, 2, 3],
        default=None,
        help="煽り強度 0..3 (デフォルト=環境変数 CLIPGEN_AGGRESSIVENESS or 2)",
    )
    pp.add_argument("--now", help="ISO8601 で『現在時刻』を上書き")
    pp.add_argument(
        "--min-score",
        type=float,
        default=0.3,
        help="ハイライト検出の最低スコア閾値 (default 0.3、煽り少ない素材は 0.0 で全区間採用)",
    )
    pp.add_argument(
        "--selection-mode",
        choices=["legacy", "political", "political_llm"],
        default="legacy",
        help="ハイライト選定方式: legacy (旧キーワード), political (政治密度多軸), political_llm (政治密度+Claude API rerank)",
    )
    pp.add_argument(
        "--llm-model",
        default="claude-haiku-4-5",
        help="political_llm の rerank モデル (default claude-haiku-4-5)",
    )
    pp.add_argument(
        "--llm-top-k",
        type=int,
        default=12,
        help="LLM rerank に渡す上位候補数 (default 12)",
    )
    pp.add_argument(
        "--min-composite-score",
        type=float,
        default=0.0,
        help="political モードの最低 composite score (default 0.0)",
    )
    pp.add_argument(
        "--dump-candidates",
        help="political モードの全候補窓スコアを JSON でダンプ (デバッグ用)",
    )
    pp.add_argument("--quiet", action="store_true")
    pp.add_argument("--polish", action="store_true", help="LLM(Claude API)でタイトル品質を向上 (要 ANTHROPIC_API_KEY)")
    pp.add_argument("--polish-model", default="claude-opus-4-7", help="LLM ポリッシュ用モデル")
    pp.add_argument("--audit-log", help="監査ログ出力先 (JSONL)")
    pp.set_defaults(func=cmd_plan)

    d = sub.add_parser("discover", help="候補動画を抽出してランキング表示する")
    d.add_argument("--source", choices=["mock", "live"], default="mock")
    d.add_argument(
        "--mock",
        default="src/clipgen/data/mock_search.json",
        help="--source mock のときの入力 JSON",
    )
    d.add_argument("--out", help="候補リストの JSON 出力先")
    d.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    d.add_argument("--min-views", type=int, default=DEFAULT_MIN_VIEWS)
    d.add_argument("--include-blocked", action="store_true", help="RIGHTS=BLOCKED の候補も出力する")
    d.add_argument(
        "--format",
        dest="target_format",
        choices=["short", "long", "both"],
        default="short",
        help="出力フォーマット (short=≤60秒, long=≥8分, both=両方)",
    )
    d.add_argument("--dry-run", action="store_true", help="live 経路を fixture/スタブで動作させる (M1)")
    d.add_argument("--now", help="ISO8601 で『現在時刻』を上書き (テスト用)")
    d.add_argument("--quiet", action="store_true")
    d.add_argument("--audit-log", help="監査ログ出力先 (JSONL)")
    d.set_defaults(func=cmd_discover)

    d2 = sub.add_parser("extract", help="generate yt-dlp/ffmpeg extraction commands from plan JSON")
    d2.add_argument("--plan", required=True, help="path to plan.json generated by the plan command")
    d2.add_argument("--out-root", default="output/extract", help="output root directory")
    d2.add_argument("--top", type=int, default=5, help="number of plans to process")
    d2.add_argument("--dry-run", action="store_true", help="print commands instead of writing files")
    d2.add_argument("--audit-log", help="監査ログ出力先 (JSONL)")
    d2.set_defaults(func=cmd_extract)

    rj = sub.add_parser("run-job", help="日次ジョブを実行する")
    rj.add_argument("--date", required=True, help="対象日 YYYY-MM-DD")
    rj.add_argument("--out-root", default="output", help="出力ルート")
    rj.add_argument("--source", default="mock", help='"mock" または mock JSON の path')
    rj.add_argument("--include-blocked", action="store_true")
    rj.add_argument("--aggressiveness", type=int, choices=[0, 1, 2, 3], default=None)
    rj.add_argument("--polish", action="store_true")
    rj.add_argument("--polish-model", default="claude-opus-4-7")
    rj.add_argument("--dry-run", action="store_true")
    rj.set_defaults(func=cmd_run_job)

    rv = sub.add_parser("review", help="candidates/plans JSON をレビューする")
    rv.add_argument("--input", required=True, help="candidates または plans JSON")
    rv.add_argument("--out-json", help="JSON 出力先")
    rv.add_argument("--out-tsv", help="TSV 出力先")
    rv.add_argument("--score-threshold", type=float, default=60.0)
    rv.set_defaults(func=cmd_review)

    dg = sub.add_parser("digest", help="plans JSON から日次 digest を生成/投稿する")
    dg.add_argument("--plans", required=True, help="plan JSON、または run-job 出力の plan.json")
    dg.add_argument("--date", required=True, help="対象日 YYYY-MM-DD")
    dg.add_argument("--top-n", type=int, default=5)
    dg.add_argument("--webhook-url", help="Slack incoming webhook URL")
    dg.add_argument("--dry-run", action="store_true")
    dg.set_defaults(func=cmd_digest)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
