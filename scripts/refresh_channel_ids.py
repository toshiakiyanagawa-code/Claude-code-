#!/usr/bin/env python3
"""allowlist/blocklist の handle → channel_id を YouTube Data API で一括解決する.

使い方:
    YOUTUBE_API_KEY=xxxx python scripts/refresh_channel_ids.py --diff
    YOUTUBE_API_KEY=xxxx python scripts/refresh_channel_ids.py --write

`UC` で始まらない、または 24 文字でない channel_id は怪しいので警告する。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from clipgen.youtube_client import YouTubeAPIError, YouTubeClient  # noqa: E402

TARGETS = [
    ROOT / "src" / "clipgen" / "data" / "allowlist.json",
    ROOT / "src" / "clipgen" / "data" / "blocklist.json",
]


def _looks_plausible_channel_id(cid: str) -> bool:
    return cid.startswith("UC") and len(cid) == 24


def _resolve_channels(
    client: YouTubeClient,
    channels: list[dict[str, Any]],
    *,
    force_replace: bool,
) -> tuple[list[str], bool]:
    """channels をその場で更新し、変更ログと部分失敗の有無を返す."""
    logs: list[str] = []
    had_error = False

    for ch in channels:
        handle = ch.get("handle", "")
        if not handle:
            continue

        cid = ch.get("channel_id", "")
        if cid and _looks_plausible_channel_id(cid):
            continue

        try:
            item = client.channel_for_handle(handle)
        except YouTubeAPIError as exc:
            logs.append(f"WARN {handle}: API error: {exc}")
            had_error = True
            continue

        if not item:
            logs.append(f"WARN {handle}: not found")
            had_error = True
            continue

        new_cid = item.get("id", "")
        if not new_cid:
            logs.append(f"WARN {handle}: empty channel_id in response")
            had_error = True
            continue

        old = ch.get("channel_id", "")
        if old and old != new_cid:
            if not force_replace:
                logs.append(f"SKIP {handle}: existing channel_id differs ({old} -> {new_cid})")
                continue
            logs.append(f"DIFF {handle}: {old} -> {new_cid}")
        elif not old:
            logs.append(f"SET  {handle}: {new_cid}")

        ch["channel_id"] = new_cid

    return logs, had_error


def process(
    path: Path,
    *,
    client: YouTubeClient,
    write: bool,
    force_replace: bool,
) -> tuple[list[str], bool]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    channels = raw.get("channels", [])
    logs, had_error = _resolve_channels(client, channels, force_replace=force_replace)
    if write:
        path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return [f"{path.name}: {line}" for line in logs], had_error


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--write", action="store_true", help="変更を JSON に書き戻す")
    p.add_argument("--diff", action="store_true", help="変更ログを表示するだけ (--write と同時指定可)")
    p.add_argument("--allow-partial", action="store_true", help="API error/not found があっても exit 0 にする")
    p.add_argument("--force-replace", action="store_true", help="既存 channel_id と解決結果が異なる場合も上書きする")
    p.add_argument("paths", nargs="*", type=Path, default=TARGETS)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        client = YouTubeClient()
    except YouTubeAPIError as exc:
        print(f"YouTubeClient init failed: {exc}", file=sys.stderr)
        return 2

    all_logs: list[str] = []
    had_error = False
    for path in args.paths:
        logs, path_had_error = process(
            path,
            client=client,
            write=args.write,
            force_replace=args.force_replace,
        )
        all_logs.extend(logs)
        had_error = had_error or path_had_error

    if args.diff or not args.write:
        for line in all_logs:
            print(line)
    elif args.write:
        print(f"updated: {len(all_logs)} entries")

    if had_error and not args.allow_partial:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
