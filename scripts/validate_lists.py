#!/usr/bin/env python3
"""allowlist/blocklist の必須項目を検証する."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REQUIRED_KEYS = ("name", "category", "permission_scope", "permission_checked_at")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"{path}: file not found") from None
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}: invalid JSON: {exc}") from exc


def _validate_channel(path: Path, index: int, channel: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    label = f"{path}: channels[{index}]"

    if not channel.get("channel_id") and not channel.get("handle"):
        errors.append(f"{label}: channel_id or handle is required")

    for key in REQUIRED_KEYS:
        if not channel.get(key):
            errors.append(f"{label}: missing required key: {key}")

    return errors


def validate_list(path: Path) -> list[str]:
    try:
        raw = _load_json(path)
    except ValueError as exc:
        return [str(exc)]

    channels = raw.get("channels")
    if not isinstance(channels, list):
        return [f"{path}: channels must be a list"]

    errors: list[str] = []
    for index, channel in enumerate(channels):
        if not isinstance(channel, dict):
            errors.append(f"{path}: channels[{index}] must be an object")
            continue
        errors.extend(_validate_channel(path, index, channel))
    return errors


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate clipgen allowlist/blocklist metadata.")
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        default=[
            Path("src/clipgen/data/allowlist.json"),
            Path("src/clipgen/data/blocklist.json"),
        ],
        help="JSON list files to validate",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    errors: list[str] = []
    for path in args.paths:
        errors.extend(validate_list(path))

    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
