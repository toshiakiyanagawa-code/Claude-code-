from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from clipgen.audit import AuditLogger, log_discover, log_extract, log_filter, log_polish  # noqa: E402


class AuditLoggerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "audit.jsonl"

    def read_records(self) -> list[dict]:
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()]

    def test_none_path_is_noop(self) -> None:
        logger = AuditLogger(None)

        logger.log("discover", {"value": 1})

    def test_log_writes_json_line(self) -> None:
        logger = AuditLogger(self.path)
        at = datetime(2026, 5, 13, 1, 2, 3, tzinfo=timezone.utc)

        logger.log("discover", {"count": 2}, at=at)

        records = self.read_records()
        self.assertEqual(records[0]["event"], "discover")
        self.assertEqual(records[0]["at"], at.isoformat())
        self.assertEqual(records[0]["payload"], {"count": 2})

    def test_log_appends_multiple_events(self) -> None:
        logger = AuditLogger(self.path)

        logger.log("discover", {})
        logger.log("filter", {})

        self.assertEqual([record["event"] for record in self.read_records()], ["discover", "filter"])

    def test_log_rejects_unknown_event(self) -> None:
        logger = AuditLogger(self.path)

        with self.assertRaises(ValueError):
            logger.log("unknown_event", {})

    def test_records_include_schema_version(self) -> None:
        logger = AuditLogger(self.path)

        logger.log("discover", {})

        self.assertEqual(self.read_records()[0]["schema_version"], 1)

    def test_tuple_set_and_datetime_are_json_safe(self) -> None:
        logger = AuditLogger(self.path)
        at = datetime(2026, 5, 13, 1, 2, 3)

        logger.log("extract", {"tuple": (1, 2), "set": {"b", "a"}, "time": at}, at=at)

        payload = self.read_records()[0]["payload"]
        self.assertEqual(payload["tuple"], [1, 2])
        self.assertEqual(sorted(payload["set"]), ["a", "b"])
        self.assertEqual(payload["time"], at.isoformat())

    def test_helper_event_names(self) -> None:
        logger = AuditLogger(self.path)

        log_discover(logger, {})
        log_filter(logger, {})
        log_polish(logger, {})
        log_extract(logger, {})

        self.assertEqual(
            [record["event"] for record in self.read_records()],
            ["discover", "filter", "polish", "extract"],
        )

    def test_cli_audit_log_integration_is_deferred(self) -> None:
        # CLI --audit-log wiring is intentionally deferred to the next ticket.
        self.assertTrue(callable(log_discover))


if __name__ == "__main__":
    unittest.main()
