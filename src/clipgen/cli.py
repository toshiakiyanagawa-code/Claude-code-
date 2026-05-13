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
import sys
from datetime import datetime, timezone
from pathlib import Path

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

    for fmt in formats:
        cands = _discover_once(args, target_format=fmt, now=now)
        if cands is None:
            return 2

        out_path = _resolve_out_path(args.out, fmt, suffix=len(formats) > 1)
        if out_path:
            write_json(cands, out_path)
        if not args.quiet:
            print(f"\n## format={fmt}")
            _print_table(cands)
            if not out_path:
                import json as _json

                print()
                print(_json.dumps(candidates_to_dict(cands)[:5], ensure_ascii=False, indent=2))
    return 0


def _candidate_plan(
    candidate: Candidate,
    *,
    srt_text: str | None,
    target_format: str,
    aggressiveness: int | None,
    provider=None,
) -> dict:
    """1候補に対し、ハイライト/タイトル/サムネをまとめて生成する."""
    highlights = []
    if srt_text is not None:
        cues = parse_srt(srt_text)
        highlights = detect_highlights(cues, target_format=target_format)
    highlight_status = "no_srt" if srt_text is None else ("no_highlight" if not highlights else "ok")
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

    srt_text: str | None = None
    if args.srt:
        srt_path = Path(args.srt)
        if not srt_path.exists():
            print(f"srt file not found: {srt_path}", file=sys.stderr)
            return 2
        srt_text = srt_path.read_text(encoding="utf-8")

    aggressiveness = args.aggressiveness

    provider = None
    if getattr(args, "polish", False):
        import os as _os

        if _os.environ.get("ANTHROPIC_API_KEY"):
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
                )
            )

    output = {
        "generated_at": now.isoformat(),
        "plans": plans,
    }
    out_path = Path(args.out) if args.out else None
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(__import__("json").dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
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


def cmd_config_check(args) -> int:
    from .config_check import run_config_check

    warnings, errors = run_config_check()
    for w in warnings:
        print(f"WARNING: {w}")
    for e in errors:
        print(f"ERROR: {e}")
    return 1 if errors else 0


def cmd_compliance_check(args) -> int:
    import json as _json
    from pathlib import Path

    from .compliance import apply_takedown, load_candidates, load_takedown_list, write_compliance_result

    candidates = load_candidates(Path(args.input))
    takedowns = load_takedown_list(Path(args.takedown_list))
    passed, blocked = apply_takedown(candidates, takedowns)

    payload = {"passed": passed, "blocked": blocked}
    if args.out:
        write_compliance_result(Path(args.out), passed, blocked)
    else:
        print(_json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"passed: {len(passed)}, blocked: {len(blocked)}", file=sys.stderr)
    return 0


def cmd_extract(args) -> int:
    import json as _json
    from pathlib import Path

    from .clip_extract import plan_to_extract, write_extract_plan

    plan_path = Path(args.plan)
    payload = _json.loads(plan_path.read_text(encoding="utf-8"))

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
    extracts = [
        plan_to_extract(plan, output_root=out_root)
        for plan in plans[: args.top]
    ]

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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="clipgen", description="政治系切り抜き動画 素材候補抽出ツール")
    sub = p.add_subparsers(dest="cmd", required=True)

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
    d.set_defaults(func=cmd_discover)

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
    pp.add_argument("--srt", help="ハイライト検出に使う SRT/VTT ファイル")
    pp.add_argument("--top", type=int, default=5, help="各フォーマットで処理する候補数")
    pp.add_argument(
        "--aggressiveness",
        type=int,
        choices=[0, 1, 2, 3],
        default=None,
        help="煽り強度 0..3 (デフォルト=環境変数 CLIPGEN_AGGRESSIVENESS or 2)",
    )
    pp.add_argument("--now", help="ISO8601 で『現在時刻』を上書き")
    pp.add_argument("--quiet", action="store_true")
    pp.add_argument("--polish", action="store_true", help="LLM(Claude API)でタイトル品質を向上 (要 ANTHROPIC_API_KEY)")
    pp.add_argument("--polish-model", default="claude-opus-4-7", help="LLM ポリッシュ用モデル")
    pp.set_defaults(func=cmd_plan)

    cc = sub.add_parser("config-check", help="設定ファイル・環境変数・出力先を検証")
    cc.set_defaults(func=cmd_config_check)

    cp = sub.add_parser("compliance-check", help="削除依頼/権利リストで候補を再フィルタ")
    cp.add_argument("--input", required=True, help="candidates または plans の JSON")
    cp.add_argument("--takedown-list", required=True, help="JSON または TSV の takedown リスト")
    cp.add_argument("--out", help="結果 JSON 出力先 (省略時は stdout)")
    cp.set_defaults(func=cmd_compliance_check)

    d2 = sub.add_parser("extract", help="generate yt-dlp/ffmpeg extraction commands from plan JSON")
    d2.add_argument("--plan", required=True, help="path to plan.json generated by the plan command")
    d2.add_argument("--out-root", default="output/extract", help="output root directory")
    d2.add_argument("--top", type=int, default=5, help="number of plans to process")
    d2.add_argument("--dry-run", action="store_true", help="print commands instead of writing files")
    d2.set_defaults(func=cmd_extract)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
