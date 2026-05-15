from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _title(plan: dict[str, Any]) -> str:
    titles = plan.get("title_candidates") or plan.get("titles")
    if isinstance(titles, list) and titles:
        first = titles[0]
        if isinstance(first, dict):
            return str(first.get("title") or first.get("text") or plan.get("title") or "Untitled")
        return str(first)
    return str(plan.get("title") or plan.get("video_title") or "Untitled")


def _highlight_summary(plan: dict[str, Any]) -> str:
    highlights = _as_list(plan.get("highlights") or plan.get("highlight_summary"))
    parts: list[str] = []
    for item in highlights[:3]:
        if isinstance(item, dict):
            text = item.get("summary") or item.get("text") or item.get("title")
            if text:
                parts.append(str(text))
        elif item:
            parts.append(str(item))
    return "; ".join(parts) if parts else "No highlights"


def build_digest(plans: list[dict], *, date: str, top_n: int = 5, reviewed: dict | None = None) -> str:
    lines = [f"*ClipGen Daily Digest - {date}*", f"Plans: {len(plans)}"]

    if reviewed is not None:
        review_required = reviewed.get("review_required", 0)
        total = reviewed.get("total", 0)
        lines.append(f"review_required: {review_required} / total: {total}")

    if not plans:
        lines.append("No plans generated.")
        return "\n".join(lines)

    for index, plan in enumerate(plans[: max(0, top_n)], start=1):
        usage_status = plan.get("usage_status") or "unknown"
        title = _title(plan)
        highlights = _highlight_summary(plan)
        lines.append(f"{index}. *{title}*")
        lines.append(f"   usage_status: `{usage_status}`")
        lines.append(f"   highlights: {highlights}")

    remaining = len(plans) - max(0, top_n)
    if remaining > 0:
        lines.append(f"...and {remaining} more.")
    return "\n".join(lines)


def post_slack(
    webhook_url: str,
    message: str,
    *,
    dry_run: bool = False,
    timeout: float = 5.0,
    max_retries: int = 2,
) -> bool:
    if dry_run:
        print(message)
        return True

    payload = json.dumps({"text": message}).encode("utf-8")

    for attempt in range(max(0, max_retries) + 1):
        request = urllib.request.Request(
            webhook_url,
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = int(getattr(response, "status", response.getcode()))
                if 200 <= status < 300:
                    return True
                print(f"slack post failed: status {status}", file=sys.stderr)
                if status == 429 or status >= 500:
                    if attempt < max_retries:
                        time.sleep(0.5 * (2**attempt))
                        continue
                return False
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            print(f"slack post failed: status {status}", file=sys.stderr)
            if status == 429 or status >= 500:
                if attempt < max_retries:
                    time.sleep(0.5 * (2**attempt))
                    continue
            return False
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            print(f"slack post failed: {exc}", file=sys.stderr)
            return False

    return False
