"""Local dictionary for iStock asset_id -> photographer username."""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.request import Request, urlopen

from cms_entry_assistant.models import PhotographerEntry

DEFAULT_DB_PATH = Path("data/photographer_lookup.json")
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_MIN_INTERVAL_S = 1.5
_LAST_FETCH_AT = 0.0


def _walk_json(value: Any) -> Iterator[Any]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


class PhotographerLookup:
    DEFAULT_DB_PATH = DEFAULT_DB_PATH
    _ASSET_URL_RE = re.compile(r"gm([0-9]{6,12})")

    def __init__(self, path: Path | str = DEFAULT_DB_PATH):
        self.path = Path(path)
        self.entries: dict[str, PhotographerEntry] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self.entries = {}
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            self.entries = {}
            return
        entries: dict[str, PhotographerEntry] = {}
        for asset_id, value in raw.items():
            if isinstance(value, str):
                entries[asset_id] = PhotographerEntry(asset_id=asset_id, photographer_username=value)
            elif isinstance(value, dict):
                entries[asset_id] = PhotographerEntry(asset_id=asset_id, **{k: v for k, v in value.items() if k != "asset_id"})
        self.entries = entries

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {asset_id: asdict(entry) for asset_id, entry in sorted(self.entries.items())},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def get(self, asset_id: str) -> PhotographerEntry | None:
        return self.entries.get((asset_id or "").strip())

    def upsert(
        self,
        asset_id: str,
        photographer_username: str,
        *,
        source_url: str = "",
        note: str = "",
        registered_by: str = "",
        review_status: str = "auto",
    ) -> PhotographerEntry:
        asset_id = (asset_id or "").strip()
        photographer_username = (photographer_username or "").strip()
        if not asset_id or not photographer_username:
            raise ValueError("asset_id and photographer_username are required")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        existing = self.entries.get(asset_id)
        if existing:
            existing.photographer_username = photographer_username
            existing.last_used_at = now
            existing.usage_count += 1
            if source_url:
                existing.source_url = source_url
            if note:
                existing.note = note
            if registered_by:
                existing.registered_by = registered_by
            existing.review_status = review_status or existing.review_status
            return existing
        entry = PhotographerEntry(
            asset_id=asset_id,
            photographer_username=photographer_username,
            source_url=source_url,
            first_seen_at=now,
            last_used_at=now,
            usage_count=1,
            note=note,
            registered_by=registered_by,
            review_status=review_status,
        )
        self.entries[asset_id] = entry
        return entry

    @staticmethod
    def _iter_cms_image_blocks(text: str) -> Iterator[tuple[str, str]]:
        """Yield (asset_id, photographer) pairs from pasted CMS source."""

        for match in re.finditer(r"gm([0-9]{6,12}).{0,300}?iStock\.com\s*[／/]\s*([^<\s）),]+)", text, re.S):
            yield match.group(1), match.group(2)
        for match in re.finditer(r"iStock\.com\s*[／/]\s*([^<\s）),]+).{0,300}?gm([0-9]{6,12})", text, re.S):
            yield match.group(2), match.group(1)

    def seed_from_cms_files(self, paths: Iterable[Path | str]) -> int:
        count = 0
        for path_like in paths:
            path = Path(path_like)
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                text = path.read_text(encoding="cp932", errors="ignore")
            except OSError:
                continue
            for asset_id, username in self._iter_cms_image_blocks(text):
                self.upsert(
                    asset_id,
                    username,
                    source_url=str(path),
                    note="seeded from CMS source",
                )
                count += 1
        return count

    def known_usernames(self) -> set[str]:
        return {
            entry.photographer_username
            for entry in self.entries.values()
            if entry.photographer_username
        }

    def _build_istock_urls(self, asset_id: str) -> list[str]:
        asset_id = asset_id.strip()
        return [
            f"https://www.istockphoto.com/jp/photo/-gm{asset_id}",
            f"https://www.istockphoto.com/photo/-gm{asset_id}",
        ]

    @staticmethod
    def _extract_username_from_html(html: str) -> str:
        patterns = [
            r"iStock\.com\s*[／/]\s*([^<\s）),]+)",
            r'"artist"\s*:\s*"([^"]+)"',
            r'"contributor"\s*:\s*\{[^}]*"name"\s*:\s*"([^"]+)"',
            r'itemprop="author"[^>]*>\s*<[^>]*itemprop="name"[^>]*content="([^"]+)"',
        ]
        for pattern in patterns:
            m = re.search(pattern, html, re.S)
            if m:
                return re.sub(r"\s+", " ", m.group(1)).strip()
        return ""

    @staticmethod
    def _extract_image_meta_from_html(html: str) -> dict[str, str]:
        username = PhotographerLookup._extract_username_from_html(html)
        title = ""
        m = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
        if m:
            title = re.sub(r"\s+", " ", m.group(1)).strip()
        return {"photographer_username": username, "title": title}

    def fetch_asset_meta(self, asset_id: str) -> PhotographerEntry | None:
        """Fetch iStock metadata for one asset and persist the username if found."""

        asset_id = (asset_id or "").strip()
        if not asset_id:
            return None
        existing = self.get(asset_id)
        if existing and existing.photographer_username:
            return existing

        global _LAST_FETCH_AT
        wait = _MIN_INTERVAL_S - (time.monotonic() - _LAST_FETCH_AT)
        if wait > 0:
            time.sleep(wait)
        _LAST_FETCH_AT = time.monotonic()

        for url in self._build_istock_urls(asset_id):
            req = Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "ja,en;q=0.8"})
            try:
                with urlopen(req, timeout=20) as response:
                    html = response.read().decode("utf-8", errors="ignore")
            except Exception:
                continue
            meta = self._extract_image_meta_from_html(html)
            username = meta.get("photographer_username", "")
            if username:
                return self.upsert(asset_id, username, source_url=url, note=meta.get("title", ""))
        return None
