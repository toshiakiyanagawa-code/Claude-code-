from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from clipgen.compliance import TakedownEntry, apply_takedown, load_candidates, load_takedown_list  # noqa: E402


class ComplianceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_load_json_takedown_list(self) -> None:
        path = self.root / "takedowns.json"
        path.write_text(
            json.dumps([{"video_id": "v1", "handle": "demo", "reason": "owner request"}]),
            encoding="utf-8",
        )

        entries = load_takedown_list(path)

        self.assertEqual(entries, [TakedownEntry(video_id="v1", channel_handle="@demo", reason="owner request")])

    def test_load_tsv_takedown_list(self) -> None:
        path = self.root / "takedowns.tsv"
        path.write_text("id\thandle\tchannel_id\treason\nv1\t@demo\tUC1\tclaim\n", encoding="utf-8")

        entries = load_takedown_list(path)

        self.assertEqual(entries, [TakedownEntry(video_id="v1", channel_handle="@demo", channel_id="UC1", reason="claim")])

    def test_apply_takedown_matches_video_id(self) -> None:
        candidates = [{"video_id": "v1", "permission_reason": "old"}]

        passed, blocked = apply_takedown(candidates, [TakedownEntry(video_id="v1", reason="claim")])

        self.assertEqual(passed, [])
        self.assertEqual(blocked[0]["usage_status"], "blocked")
        self.assertEqual(blocked[0]["permission_reason"], "takedown: claim")

    def test_apply_takedown_matches_channel_handle(self) -> None:
        candidates = [{"video_id": "v1", "channel_handle": "@demo"}]

        passed, blocked = apply_takedown(candidates, [TakedownEntry(channel_handle="@demo", reason="handle")])

        self.assertEqual(passed, [])
        self.assertEqual(blocked[0]["video_id"], "v1")

    def test_apply_takedown_matches_channel_id(self) -> None:
        candidates = [{"video_id": "v1", "channel_id": "UC1"}]

        passed, blocked = apply_takedown(candidates, [TakedownEntry(channel_id="UC1", reason="channel")])

        self.assertEqual(passed, [])
        self.assertEqual(blocked[0]["permission_reason"], "takedown: channel")

    def test_apply_takedown_match_none_passes_candidate(self) -> None:
        candidates = [{"video_id": "v1", "channel_id": "UC1"}]

        passed, blocked = apply_takedown(candidates, [TakedownEntry(video_id="v2", reason="other")])

        self.assertEqual(passed, candidates)
        self.assertEqual(blocked, [])

    def test_load_candidates_accepts_candidates_dict(self) -> None:
        path = self.root / "input.json"
        path.write_text(json.dumps({"candidates": [{"video_id": "v1"}]}), encoding="utf-8")

        self.assertEqual(load_candidates(path), [{"video_id": "v1"}])

    def test_load_candidates_accepts_plans_dict(self) -> None:
        path = self.root / "input.json"
        path.write_text(json.dumps({"plans": [{"video_id": "v1"}]}), encoding="utf-8")

        self.assertEqual(load_candidates(path), [{"video_id": "v1"}])

    def test_load_candidates_accepts_list(self) -> None:
        path = self.root / "input.json"
        path.write_text(json.dumps([{"video_id": "v1"}]), encoding="utf-8")

        self.assertEqual(load_candidates(path), [{"video_id": "v1"}])


if __name__ == "__main__":
    unittest.main()
