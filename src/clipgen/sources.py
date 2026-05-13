"""ホワイトリスト/ブラックリスト読み込みと、チャンネルの切り抜き許諾判定."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

DATA_DIR = Path(__file__).parent / "data"


@dataclass(frozen=True)
class Channel:
    handle: str
    name: str
    category: str
    channel_id: str = ""
    source_url: str = ""
    permission_checked_at: str = ""
    permission_evidence: str = ""
    permission_scope: str = ""
    notes: str = ""


def _normalize_handle(handle: str | None) -> str:
    if not handle:
        return ""
    h = handle.strip()
    if not h.startswith("@"):
        h = "@" + h
    return h.lower()


def _normalize_channel_id(channel_id: str | None) -> str:
    return (channel_id or "").strip()


def _load_channel(c: dict) -> Channel:
    return Channel(
        channel_id=_normalize_channel_id(c.get("channel_id")),
        handle=_normalize_handle(c.get("handle")),
        name=c["name"],
        category=c["category"],
        source_url=c.get("source_url", ""),
        permission_checked_at=c.get("permission_checked_at", ""),
        permission_evidence=c.get("permission_evidence", ""),
        permission_scope=c.get("permission_scope", ""),
        notes=c.get("notes", ""),
    )


def load_allowlist(path: Path = DATA_DIR / "allowlist.json") -> list[Channel]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [_load_channel(c) for c in raw.get("channels", [])]


def load_blocklist(path: Path = DATA_DIR / "blocklist.json") -> tuple[list[Channel], list[str]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    channels = [_load_channel(c) for c in raw.get("channels", [])]
    keywords = list(raw.get("keywords_in_description", []))
    return channels, keywords


def load_seed_queries(path: Path = DATA_DIR / "seed_queries.json") -> dict[str, list[str]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {k: v for k, v in raw.items() if not k.startswith("_") and isinstance(v, list)}


@dataclass
class Permission:
    """切り抜き許諾の判定結果."""

    allowed: bool
    reason: str
    risk_flags: list[str]
    category: str = ""
    scope: str = ""


def _permission_from_channel(
    *,
    channel: Channel,
    allowed: bool,
    reason: str,
    risk_flags: list[str],
) -> Permission:
    return Permission(
        allowed,
        reason,
        risk_flags,
        channel.category,
        channel.permission_scope,
    )


def check_channel_permission(
    *,
    channel_id: str | None = None,
    channel_handle: str | None,
    channel_title: str | None,
    channel_description: str | None,
    allowlist: Iterable[Channel],
    blocklist: Iterable[Channel],
    block_keywords: Iterable[str],
) -> Permission:
    """チャンネル単位の切り抜き許諾を判定する。

    判定順:
      1. blocklist の channel_id にマッチ → 不可
      2. blocklist のハンドルにマッチ → 不可
      3. blocklist の名前にマッチ → 不可
      4. 説明欄に block_keywords のいずれかが含まれる → 不可
      5. allowlist の channel_id にマッチ → 可
      6. allowlist のハンドルにマッチ → 可
      7. allowlist の名前にマッチ → 可
      8. それ以外 → グレー（不可扱い、要手動確認）
    """
    risk_flags: list[str] = []
    cid = _normalize_channel_id(channel_id)
    handle = _normalize_handle(channel_handle)
    title = (channel_title or "").strip()
    desc = channel_description or ""

    for ch in blocklist:
        if cid and ch.channel_id and cid == ch.channel_id:
            risk_flags.append("blocklist_match:channel_id")
            return _permission_from_channel(
                channel=ch,
                allowed=False,
                reason=f"blocklist match: {ch.name} ({ch.category})",
                risk_flags=risk_flags,
            )
    for ch in blocklist:
        if handle and handle == ch.handle:
            risk_flags.append("blocklist_match:handle")
            return _permission_from_channel(
                channel=ch,
                allowed=False,
                reason=f"blocklist match: {ch.name} ({ch.category})",
                risk_flags=risk_flags,
            )
    for ch in blocklist:
        if title and title.lower() == ch.name.lower():
            risk_flags.append("blocklist_match:title")
            return _permission_from_channel(
                channel=ch,
                allowed=False,
                reason=f"blocklist match by name: {ch.name}",
                risk_flags=risk_flags,
            )

    if desc:
        lowered = desc.lower()
        for kw in block_keywords:
            if kw.lower() in lowered:
                risk_flags.append("blocklist_match:description_keyword")
                return Permission(False, f"blocked keyword in description: {kw}", risk_flags)

    for ch in allowlist:
        if cid and ch.channel_id and cid == ch.channel_id:
            if ch.notes:
                risk_flags.append(f"note: {ch.notes}")
            return _permission_from_channel(
                channel=ch,
                allowed=True,
                reason=f"allowlist match: {ch.name} ({ch.category})",
                risk_flags=risk_flags,
            )
    for ch in allowlist:
        if handle and handle == ch.handle:
            if ch.notes:
                risk_flags.append(f"note: {ch.notes}")
            return _permission_from_channel(
                channel=ch,
                allowed=True,
                reason=f"allowlist match: {ch.name} ({ch.category})",
                risk_flags=risk_flags,
            )
    for ch in allowlist:
        if title and title.lower() == ch.name.lower():
            if ch.notes:
                risk_flags.append(f"note: {ch.notes}")
            return _permission_from_channel(
                channel=ch,
                allowed=True,
                reason=f"allowlist match by name: {ch.name} ({ch.category})",
                risk_flags=risk_flags,
            )

    risk_flags.append("not_in_allowlist")
    return Permission(False, "unknown channel; manual review required", risk_flags)


_TV_HINT = re.compile(r"(テレビ|TV|テレ朝|日テレ|テレ東|NHK|TBS|FNN|放送)", re.IGNORECASE)
defamation_review_keywords = ("鼻で笑う", "失言", "炎上", "暴露", "完全論破", "絶句", "激怒", "激詰め")


def looks_like_tv_source(text: str | None) -> bool:
    """タイトル/説明欄に明らかなTV由来っぽい単語が入っていないかの簡易チェック."""
    if not text:
        return False
    return bool(_TV_HINT.search(text))


def looks_defamatory(title: str | None) -> bool:
    """名誉毀損リスクのレビュー対象になりやすい煽り語がタイトルに含まれるかを返す."""
    if not title:
        return False
    return any(keyword in title for keyword in defamation_review_keywords)
