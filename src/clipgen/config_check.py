from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


DATA_DIR = Path("src/clipgen/data")
ALLOWED_PERMISSION_SCOPES = {
    # Actively used in this repo's allowlist/blocklist data files
    "public_press_conferences",
    "official_speech",
    "parliament_recording",
    "tv_broadcast",
    "news_agency_clip",
    # Reserved scope vocabulary for future entries
    "public_press_conference",
    "member_clip_allowed",
    "public_speech",
    "governmental_release",
    "primary_source",
    "takedown_only",
    "manual_review",
    "no_reuse",
    "third_party_managed",
}


def _first_existing(*paths: Path) -> Path:
    for path in paths:
        if path.exists():
            return path
    return paths[0]


def _load_json(path: Path, errors: list[str]) -> Any:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        errors.append(f"{path}: missing")
    except json.JSONDecodeError as exc:
        errors.append(f"{path}: invalid JSON: {exc.msg}")
    except OSError as exc:
        errors.append(f"{path}: cannot read: {exc}")
    return None


def _extract_channels(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("channels", "allowlist", "blocklist"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _channel_identity_sets(data: Any) -> tuple[set[str], set[str]]:
    channel_ids: set[str] = set()
    handles: set[str] = set()

    for channel in _extract_channels(data):
        channel_id = str(channel.get("channel_id") or "").strip()
        handle = str(channel.get("handle") or "").strip()
        if channel_id:
            channel_ids.add(channel_id)
        if handle:
            handles.add(handle)

    return channel_ids, handles


def _check_channel_file(path: Path, data: Any, warnings: list[str], errors: list[str]) -> None:
    channels = _extract_channels(data)
    seen_channel_ids: set[str] = set()

    if data is not None and not channels:
        errors.append(f"{path}: no channel entries found")

    for index, channel in enumerate(channels):
        label = channel.get("handle") or channel.get("channel_id") or f"#{index}"

        handle = str(channel.get("handle") or "").strip()
        channel_id = str(channel.get("channel_id") or "").strip()
        if not (handle or channel_id):
            errors.append(f"{path}: channel {label} requires handle or channel_id")

        if handle and not handle.startswith("@"):
            warnings.append(f'{path}: channel {label} handle should start with "@" (normalize recommended)')

        permission_scope = str(channel.get("permission_scope") or "").strip()
        if not permission_scope:
            errors.append(f"{path}: channel {label} requires non-empty permission_scope")
        elif permission_scope not in ALLOWED_PERMISSION_SCOPES:
            errors.append(f"{path}: channel {label} has invalid permission_scope {permission_scope}")

        if channel_id:
            if channel_id in seen_channel_ids:
                errors.append(f"{path}: duplicate channel_id {channel_id}")
            seen_channel_ids.add(channel_id)


def _check_allow_block_overlap(allowlist: Any, blocklist: Any, errors: list[str]) -> None:
    allow_channel_ids, allow_handles = _channel_identity_sets(allowlist)
    block_channel_ids, block_handles = _channel_identity_sets(blocklist)

    for channel_id in sorted(allow_channel_ids & block_channel_ids):
        errors.append(f"allowlist/blocklist: duplicate channel_id {channel_id}")

    for handle in sorted(allow_handles & block_handles):
        errors.append(f"allowlist/blocklist: duplicate handle {handle}")


def _check_seed_queries(path: Path, data: Any, errors: list[str]) -> None:
    if not isinstance(data, dict):
        if data is not None:
            errors.append(f"{path}: expected object")
        return

    for key in ("people", "format_words", "topics"):
        value = data.get(key)
        if not isinstance(value, list) or not value:
            errors.append(f"{path}: {key} must be a non-empty list")


def _check_output_root(path: Path, errors: list[str]) -> None:
    marker = path / ".clipgen_config_check"
    created_root = False

    try:
        if not path.exists():
            path.mkdir(parents=True)
            created_root = True
        if not path.is_dir():
            errors.append(f"{path}: output root is not a directory")
            return
        marker.write_text("ok", encoding="utf-8")
        marker.unlink()
        if created_root:
            path.rmdir()
    except OSError as exc:
        errors.append(f"{path}: output root is not writable: {exc}")


def run_config_check() -> tuple[list[str], list[str]]:
    warnings: list[str] = []
    errors: list[str] = []

    allowlist_path = _first_existing(Path("allowlist.json"), DATA_DIR / "allowlist.json")
    blocklist_path = _first_existing(Path("blocklist.json"), DATA_DIR / "blocklist.json")
    seed_queries_path = _first_existing(Path("seed_queries.json"), DATA_DIR / "seed_queries.json")

    allowlist = _load_json(allowlist_path, errors)
    blocklist = _load_json(blocklist_path, errors)
    seed_queries = _load_json(seed_queries_path, errors)

    _check_channel_file(allowlist_path, allowlist, warnings, errors)
    _check_channel_file(blocklist_path, blocklist, warnings, errors)
    _check_allow_block_overlap(allowlist, blocklist, errors)
    _check_seed_queries(seed_queries_path, seed_queries, errors)

    if not os.environ.get("YOUTUBE_API_KEY"):
        warnings.append("YOUTUBE_API_KEY is not set")

    _check_output_root(Path("output"), errors)

    return warnings, errors


def main() -> int:
    warnings, errors = run_config_check()

    for warning in warnings:
        print(f"WARNING: {warning}")
    for error in errors:
        print(f"ERROR: {error}")

    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
