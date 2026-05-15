from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from clipgen.config_check import run_config_check  # noqa: E402


class ConfigCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write_json(self, name: str, value: object) -> None:
        (self.root / name).write_text(json.dumps(value), encoding="utf-8")

    def run_in_tmp(self):
        old_cwd = Path.cwd()
        try:
            os.chdir(self.root)
            with patch.dict(os.environ, {"YOUTUBE_API_KEY": "key"}, clear=False):
                return run_config_check()
        finally:
            os.chdir(old_cwd)

    def write_valid_files(self) -> None:
        allow_channel = {
            "handle": "@demo",
            "channel_id": "UC1",
            "permission_scope": "primary_source",
        }
        block_channel = {
            "handle": "@blockedchan",
            "channel_id": "UC2",
            "permission_scope": "no_reuse",
        }
        self.write_json("allowlist.json", [allow_channel])
        self.write_json("blocklist.json", [block_channel])
        self.write_json(
            "seed_queries.json",
            {"people": ["a"], "format_words": ["b"], "topics": ["c"]},
        )

    def test_valid_config_has_no_errors(self) -> None:
        self.write_valid_files()

        warnings, errors = self.run_in_tmp()

        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

    def test_invalid_json_is_error(self) -> None:
        self.write_valid_files()
        (self.root / "allowlist.json").write_text("{", encoding="utf-8")

        _, errors = self.run_in_tmp()

        self.assertTrue(any("invalid JSON" in error for error in errors))

    def test_channel_requires_handle_or_channel_id(self) -> None:
        self.write_valid_files()
        self.write_json("allowlist.json", [{"permission_scope": "primary_source"}])

        _, errors = self.run_in_tmp()

        self.assertTrue(any("requires handle or channel_id" in error for error in errors))

    def test_channel_requires_permission_scope(self) -> None:
        self.write_valid_files()
        self.write_json("allowlist.json", [{"handle": "@demo"}])

        _, errors = self.run_in_tmp()

        self.assertTrue(any("permission_scope" in error for error in errors))

    def test_duplicate_channel_id_is_error(self) -> None:
        self.write_valid_files()
        self.write_json(
            "allowlist.json",
            [
                {"handle": "@a", "channel_id": "UC1", "permission_scope": "primary_source"},
                {"handle": "@b", "channel_id": "UC1", "permission_scope": "primary_source"},
            ],
        )

        _, errors = self.run_in_tmp()

        self.assertTrue(any("duplicate channel_id UC1" in error for error in errors))

    def test_seed_queries_require_minimum_lists(self) -> None:
        self.write_valid_files()
        self.write_json("seed_queries.json", {"people": []})

        _, errors = self.run_in_tmp()

        self.assertTrue(any("people" in error for error in errors))
        self.assertTrue(any("format_words" in error for error in errors))
        self.assertTrue(any("topics" in error for error in errors))

    def test_missing_youtube_api_key_is_warning(self) -> None:
        self.write_valid_files()
        old_cwd = Path.cwd()
        try:
            os.chdir(self.root)
            with patch.dict(os.environ, {}, clear=True):
                warnings, errors = run_config_check()
        finally:
            os.chdir(old_cwd)

        self.assertEqual(errors, [])
        self.assertTrue(any("YOUTUBE_API_KEY" in warning for warning in warnings))


if __name__ == "__main__":
    unittest.main()
