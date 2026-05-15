"""live 経路のドライラン fixtures.

YouTube Data API キーが手元にない開発環境でも `--source live --dry-run` で
パイプライン全体を回せるよう、`YouTubeClient` を fixture でスタブ化する。

fixture は `src/clipgen/data/fixtures/` 配下に置く:
  - search.json:  search.list の items 配列を模した配列
  - videos.json:  videos.list の items 配列を模した配列
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .pipeline import DEFAULT_LOOKBACK_DAYS, DEFAULT_MIN_VIEWS, run_pipeline_live
from .scoring import TARGET_SHORT, Candidate
from .youtube_client import SearchParams, YouTubeClient

FIXTURE_DIR = Path(__file__).parent / "data" / "fixtures"


@dataclass
class StubYouTubeClient:
    """YouTubeClient と同シグネチャの fixture クライアント.

    test/dry-run 専用。実 HTTP は呼ばない。
    """

    fixture_dir: Path = FIXTURE_DIR

    def _load(self, name: str) -> list[dict[str, Any]]:
        path = self.fixture_dir / name
        if not path.exists():
            return []
        return json.loads(path.read_text(encoding="utf-8"))

    def search(self, params: SearchParams) -> list[dict[str, Any]]:
        return self._load("search.json")

    def videos(self, video_ids: list[str]) -> list[dict[str, Any]]:
        items = self._load("videos.json")
        if not video_ids:
            return items
        wanted = set(video_ids)
        out = []
        for it in items:
            vid = it.get("id") if isinstance(it.get("id"), str) else it.get("id", {}).get("videoId")
            if vid in wanted:
                out.append(it)
        return out

    def channels(self, channel_ids: list[str]) -> list[dict[str, Any]]:
        return self._load("channels.json")

    def channel_for_handle(self, handle: str) -> dict[str, Any] | None:
        return None

    def handles_to_channel_ids(self, handles: list[str]) -> dict[str, str]:
        # fixture では allowlist の channel_id をそのまま使う前提で空辞書を返す
        return {}


def run_pipeline_dryrun(
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    min_views: int = DEFAULT_MIN_VIEWS,
    now: datetime | None = None,
    include_blocked: bool = False,
    target_format: str = TARGET_SHORT,
    fixture_dir: Path | None = None,
) -> list[Candidate]:
    """`run_pipeline_live` を fixture でドライランする."""
    stub: YouTubeClient = StubYouTubeClient(fixture_dir=fixture_dir or FIXTURE_DIR)  # type: ignore[assignment]
    return run_pipeline_live(
        api_key="dryrun",
        lookback_days=lookback_days,
        min_views=min_views,
        now=now,
        include_blocked=include_blocked,
        target_format=target_format,
        client=stub,
    )
