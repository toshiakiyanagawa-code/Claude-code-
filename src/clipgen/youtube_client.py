"""YouTube Data API v3 の薄いラッパー.

APIキーは環境変数 YOUTUBE_API_KEY を読む。
ネットワーク到達性がない/キーがない環境向けに、--source mock で
src/clipgen/data/mock_search.json を読むモードも提供する。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .scoring import Candidate

API_BASE = "https://www.googleapis.com/youtube/v3"
DEFAULT_TIMEOUT = 20
_RETRY_DELAYS = (0.5, 1.0, 2.0)
_RETRYABLE_STATUSES = {403, 429, 500, 502, 503, 504}


class YouTubeAPIError(RuntimeError):
    def __init__(self, status: int | None, body_summary: str) -> None:
        self.status = status
        self.body_summary = body_summary
        status_text = "network" if status is None else str(status)
        super().__init__(f"YouTube API error {status_text}: {body_summary}")


@dataclass
class SearchParams:
    query: str
    max_results: int = 25
    published_after: datetime | None = None
    region_code: str = "JP"
    relevance_language: str = "ja"
    order: str = "viewCount"  # date / rating / relevance / title / videoCount / viewCount


def _summarize_body(body: bytes | str, limit: int = 500) -> str:
    if isinstance(body, bytes):
        text = body.decode("utf-8", errors="replace")
    else:
        text = body
    text = " ".join(text.split())
    return text[:limit]


def _curl_get_json(url: str) -> dict[str, Any]:
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if not curl:
        raise YouTubeAPIError(None, "curl executable not found")

    last_error: YouTubeAPIError | None = None
    config = f'url = "{url}"\n'
    for attempt, delay in enumerate(_RETRY_DELAYS, start=1):
        cmd = [
            curl,
            "--config",
            "-",
            "--silent",
            "--show-error",
            "--location",
            "--max-time",
            str(DEFAULT_TIMEOUT),
            "--write-out",
            "\n%{http_code}",
        ]
        if os.name == "nt":
            cmd.append("--ssl-no-revoke")
        try:
            proc = subprocess.run(
                cmd,
                input=config,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=DEFAULT_TIMEOUT + 5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            err = YouTubeAPIError(None, _summarize_body(str(exc)))
        else:
            body, _, status_text = proc.stdout.rpartition("\n")
            status = int(status_text) if status_text.isdigit() else None
            if proc.returncode == 0 and status is not None and 200 <= status < 300:
                try:
                    return json.loads(body)
                except json.JSONDecodeError as exc:
                    raise YouTubeAPIError(None, f"invalid JSON response: {exc.msg}") from exc
            summary = _summarize_body(body or proc.stderr)
            err = YouTubeAPIError(status, summary)

        if err.status not in _RETRYABLE_STATUSES and err.status is not None:
            raise err
        if attempt == len(_RETRY_DELAYS):
            raise err
        last_error = err
        time.sleep(delay)

    if last_error is not None:
        raise last_error
    raise YouTubeAPIError(None, "request failed")


def _urllib_get_json(url: str) -> dict[str, Any]:
    last_error: YouTubeAPIError | None = None
    for attempt, delay in enumerate(_RETRY_DELAYS, start=1):
        req = Request(url, headers={"User-Agent": "clipgen/0.1 (+politics-clip)"})
        try:
            with urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:  # noqa: S310 — limited to googleapis
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            body = exc.read()
            summary = _summarize_body(body)
            err = YouTubeAPIError(exc.code, summary)
            if exc.code not in _RETRYABLE_STATUSES or attempt == len(_RETRY_DELAYS):
                raise err from exc
            last_error = err
        except URLError as exc:
            err = YouTubeAPIError(None, _summarize_body(str(exc.reason)))
            if attempt == len(_RETRY_DELAYS):
                raise err from exc
            last_error = err
        except TimeoutError as exc:
            err = YouTubeAPIError(None, "request timed out")
            if attempt == len(_RETRY_DELAYS):
                raise err from exc
            last_error = err
        time.sleep(delay)
    if last_error is not None:
        raise last_error
    raise YouTubeAPIError(None, "request failed")


def _http_get_json(url: str) -> dict[str, Any]:
    backend = os.environ.get("CLIPGEN_HTTP_BACKEND", "auto").lower()
    if backend == "curl" or (backend == "auto" and os.name == "nt"):
        return _curl_get_json(url)
    return _urllib_get_json(url)


def _parse_duration_iso8601(s: str | None) -> int | None:
    """PT1H2M3S → 秒 を返す簡易パーサ."""
    if not s or not s.startswith("PT"):
        return None
    s = s[2:]
    total = 0
    num = ""
    for ch in s:
        if ch.isdigit():
            num += ch
        else:
            if not num:
                continue
            val = int(num)
            num = ""
            if ch == "H":
                total += val * 3600
            elif ch == "M":
                total += val * 60
            elif ch == "S":
                total += val
    return total


class YouTubeClient:
    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or os.environ.get("YOUTUBE_API_KEY")
        if not self.api_key:
            raise YouTubeAPIError(None, "YOUTUBE_API_KEY is not set")

    def search(self, params: SearchParams) -> list[dict[str, Any]]:
        q: dict[str, Any] = {
            "key": self.api_key,
            "part": "snippet",
            "q": params.query,
            "type": "video",
            "maxResults": params.max_results,
            "regionCode": params.region_code,
            "relevanceLanguage": params.relevance_language,
            "order": params.order,
        }
        if params.published_after:
            q["publishedAfter"] = params.published_after.strftime("%Y-%m-%dT%H:%M:%SZ")
        url = f"{API_BASE}/search?{urlencode(q)}"
        data = _http_get_json(url)
        return data.get("items", [])

    def videos(self, video_ids: list[str]) -> list[dict[str, Any]]:
        if not video_ids:
            return []
        q = {
            "key": self.api_key,
            "part": "snippet,statistics,contentDetails,status",
            "id": ",".join(video_ids),
            "maxResults": 50,
        }
        url = f"{API_BASE}/videos?{urlencode(q)}"
        return _http_get_json(url).get("items", [])

    def channels(self, channel_ids: list[str]) -> list[dict[str, Any]]:
        if not channel_ids:
            return []
        q = {
            "key": self.api_key,
            "part": "snippet,statistics,brandingSettings",
            "id": ",".join(channel_ids),
            "maxResults": 50,
        }
        url = f"{API_BASE}/channels?{urlencode(q)}"
        return _http_get_json(url).get("items", [])

    def channel_for_handle(self, handle: str) -> dict[str, Any] | None:
        if not handle:
            return None
        q = {
            "key": self.api_key,
            "part": "snippet",
            "forHandle": handle.lstrip("@"),
            "maxResults": 1,
        }
        url = f"{API_BASE}/channels?{urlencode(q)}"
        items = _http_get_json(url).get("items", [])
        return items[0] if items else None

    def handles_to_channel_ids(self, handles: list[str]) -> dict[str, str]:
        resolved: dict[str, str] = {}
        for handle in handles:
            if not handle:
                continue
            item = self.channel_for_handle(handle)
            if item and item.get("id"):
                resolved[handle.lower() if handle.startswith("@") else f"@{handle.lower()}"] = item["id"]
        return resolved


def candidate_from_video_item(item: dict[str, Any], channel_handle: str | None = None) -> Candidate:
    snippet = item.get("snippet", {})
    stats = item.get("statistics", {})
    details = item.get("contentDetails", {})
    published = snippet.get("publishedAt", "")
    try:
        pub_dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
    except ValueError:
        pub_dt = datetime.now().astimezone()
    return Candidate(
        video_id=item["id"] if isinstance(item.get("id"), str) else item.get("id", {}).get("videoId", ""),
        title=snippet.get("title", ""),
        channel_id=snippet.get("channelId", ""),
        channel_handle=channel_handle,
        channel_title=snippet.get("channelTitle", ""),
        published_at=pub_dt,
        duration_sec=_parse_duration_iso8601(details.get("duration")),
        view_count=int(stats.get("viewCount", 0)),
        like_count=int(stats["likeCount"]) if "likeCount" in stats else None,
        comment_count=int(stats["commentCount"]) if "commentCount" in stats else None,
        description=snippet.get("description", ""),
    )


def load_mock_search(path: Path) -> list[Candidate]:
    """mock_search.json から Candidate のリストを構築する。"""
    items = json.loads(path.read_text(encoding="utf-8"))
    out: list[Candidate] = []
    for it in items:
        out.append(
            Candidate(
                video_id=it["video_id"],
                title=it["title"],
                channel_id=it["channel_id"],
                channel_handle=it.get("channel_handle"),
                channel_title=it["channel_title"],
                published_at=datetime.fromisoformat(it["published_at"].replace("Z", "+00:00")),
                duration_sec=it.get("duration_sec"),
                view_count=int(it["view_count"]),
                like_count=it.get("like_count"),
                comment_count=it.get("comment_count"),
                description=it.get("description", ""),
            )
        )
    return out
