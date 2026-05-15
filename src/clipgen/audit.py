from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ALLOWED_EVENTS = {"discover", "filter", "polish", "extract"}


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return value


class AuditLogger:
    def __init__(self, path: Path | None):
        self.path = path

    def log(self, event: str, payload: dict, *, at: datetime | None = None) -> None:
        if event not in ALLOWED_EVENTS:
            raise ValueError(f"unsupported audit event: {event}")

        if self.path is None:
            return

        timestamp = at or datetime.now(timezone.utc)
        record = {
            "schema_version": 1,
            "at": timestamp.isoformat(),
            "event": event,
            "payload": _json_safe(payload),
        }

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
            f.write("\n")


def log_discover(logger: AuditLogger, payload: dict, *, at: datetime | None = None) -> None:
    logger.log("discover", payload, at=at)


def log_filter(logger: AuditLogger, payload: dict, *, at: datetime | None = None) -> None:
    logger.log("filter", payload, at=at)


def log_polish(logger: AuditLogger, payload: dict, *, at: datetime | None = None) -> None:
    logger.log("polish", payload, at=at)


def log_extract(logger: AuditLogger, payload: dict, *, at: datetime | None = None) -> None:
    logger.log("extract", payload, at=at)
