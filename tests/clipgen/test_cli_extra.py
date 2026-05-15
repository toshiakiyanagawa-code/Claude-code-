from __future__ import annotations

import json
import sys
import types
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from clipgen import cli  # noqa: E402


def test_run_job_outputs_result_json(monkeypatch, capsys, tmp_path):
    mod = types.ModuleType("clipgen.jobs")

    def fake_run_daily_job(date, out_dir, **kwargs):
        return {
            "date": date,
            "out_dir": str(out_dir),
            "dry_run": kwargs["dry_run"],
            "source": kwargs["source"],
            "include_blocked": kwargs["include_blocked"],
            "aggressiveness": kwargs["aggressiveness"],
            "polished": kwargs["polish_provider"] is not None,
            "errors": [],
        }

    mod.run_daily_job = fake_run_daily_job
    monkeypatch.setitem(sys.modules, "clipgen.jobs", mod)

    rc = cli.main(
        [
            "run-job",
            "--date",
            "2026-05-13",
            "--out-root",
            str(tmp_path),
            "--source",
            "mock",
            "--include-blocked",
            "--aggressiveness",
            "2",
            "--dry-run",
        ]
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["date"] == "2026-05-13"
    assert payload["out_dir"] == str(tmp_path)
    assert payload["dry_run"] is True
    assert payload["source"] == "mock"
    assert payload["include_blocked"] is True
    assert payload["aggressiveness"] == 2


def test_run_job_returns_1_when_errors_present(monkeypatch, capsys, tmp_path):
    mod = types.ModuleType("clipgen.jobs")
    mod.run_daily_job = lambda *args, **kwargs: {"errors": [{"message": "failed"}]}
    monkeypatch.setitem(sys.modules, "clipgen.jobs", mod)

    rc = cli.main(["run-job", "--date", "2026-05-13", "--out-root", str(tmp_path)])

    assert rc == 1
    assert json.loads(capsys.readouterr().out)["errors"] == [{"message": "failed"}]


def test_review_writes_json_and_tsv(tmp_path):
    input_path = tmp_path / "plans.json"
    out_json = tmp_path / "review.json"
    out_tsv = tmp_path / "review.tsv"
    input_path.write_text(
        json.dumps(
            {
                "plans": [
                    {
                        "video_id": "v1",
                        "title": "High score",
                        "channel_title": "Channel",
                        "score": 0.75,
                        "usage_status": "cleared",
                        "target_format": "short",
                    },
                    {
                        "video_id": "v2",
                        "title": "Low score",
                        "channel_title": "Channel",
                        "score": 40,
                        "usage_status": "review",
                        "target_format": "long",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    rc = cli.main(
        [
            "review",
            "--input",
            str(input_path),
            "--out-json",
            str(out_json),
            "--out-tsv",
            str(out_tsv),
            "--score-threshold",
            "60",
        ]
    )

    assert rc == 0
    payload = json.loads(out_json.read_text(encoding="utf-8"))
    assert payload["total"] == 2
    assert payload["passed"] == 1
    assert payload["items"][0]["score"] == 75.0
    tsv = out_tsv.read_text(encoding="utf-8")
    assert "video_id\ttitle\tchannel_title\tscore\tusage_status\ttarget_format\tpassed" in tsv
    assert "v1\tHigh score\tChannel\t75.0\tcleared\tshort\tTrue" in tsv


def test_review_prints_json_when_no_outputs(tmp_path, capsys):
    input_path = tmp_path / "candidates.json"
    input_path.write_text(
        json.dumps({"candidates": [{"video_id": "v1", "title": "Candidate", "score": 80}]}),
        encoding="utf-8",
    )

    rc = cli.main(["review", "--input", str(input_path)])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total"] == 1
    assert payload["items"][0]["passed"] is True


def test_digest_dry_run_reads_plans_and_prints_stdout(tmp_path, capsys):
    plans_path = tmp_path / "plan.json"
    plans_path.write_text(
        json.dumps(
            {
                "plans": [
                    {"video_id": "v1", "title": "First", "channel_title": "A", "score": 0.9},
                    {"video_id": "v2", "title": "Second", "channel_title": "B", "score": 50},
                ]
            }
        ),
        encoding="utf-8",
    )

    rc = cli.main(
        [
            "digest",
            "--plans",
            str(plans_path),
            "--date",
            "2026-05-13",
            "--top-n",
            "1",
            "--dry-run",
        ]
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["date"] == "2026-05-13"
    assert len(payload["items"]) == 1
    assert payload["items"][0]["video_id"] == "v1"


@dataclass
class DummyCandidate:
    video_id: str = "v1"
    title: str = "Title"
    channel_title: str = "Channel"
    url: str = "https://example.com"
    score: float = 0.7
    view_count: int = 1000
    usage_status: str = "cleared"
    permission_scope: str = ""
    risk_flags: list[str] | None = None

    def __post_init__(self):
        if self.risk_flags is None:
            self.risk_flags = []


def test_discover_audit_log_creates_file(monkeypatch, tmp_path):
    mock_path = tmp_path / "mock.json"
    mock_path.write_text("[]", encoding="utf-8")
    audit_path = tmp_path / "audit.jsonl"

    monkeypatch.setattr(cli, "run_pipeline_mock", lambda *args, **kwargs: [DummyCandidate()])
    monkeypatch.setattr(cli, "candidates_to_dict", lambda cands: [{"video_id": c.video_id} for c in cands])

    class FakeAuditLogger:
        def __init__(self, path):
            self.path = Path(path) if path else None

        def log_discover(self, payload):
            if self.path is not None:
                self.path.write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setattr(cli, "_make_audit_logger", lambda path: FakeAuditLogger(path))

    rc = cli.main(
        [
            "discover",
            "--source",
            "mock",
            "--mock",
            str(mock_path),
            "--audit-log",
            str(audit_path),
            "--quiet",
        ]
    )

    assert rc == 0
    payload = json.loads(audit_path.read_text(encoding="utf-8"))
    assert payload["phase"] == "discover"
    assert payload["count"] == 1


def test_discover_audit_log_writes_jsonl_with_schema_version(monkeypatch, tmp_path):
    """Real AuditLogger should append a JSONL record with schema_version=1."""
    mock_path = tmp_path / "mock.json"
    mock_path.write_text("[]", encoding="utf-8")
    audit_path = tmp_path / "audit.jsonl"

    monkeypatch.setattr(cli, "run_pipeline_mock", lambda *args, **kwargs: [DummyCandidate()])
    monkeypatch.setattr(cli, "candidates_to_dict", lambda cands: [{"video_id": c.video_id} for c in cands])

    rc = cli.main(
        [
            "discover",
            "--source",
            "mock",
            "--mock",
            str(mock_path),
            "--audit-log",
            str(audit_path),
            "--quiet",
        ]
    )

    assert rc == 0
    lines = audit_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["schema_version"] == 1
    assert record["event"] == "discover"
    assert record["payload"]["count"] == 1


def test_digest_without_webhook_prints_stdout(tmp_path, capsys):
    plans_path = tmp_path / "plan.json"
    plans_path.write_text(
        json.dumps({"plans": [{"video_id": "v1", "title": "First", "score": 90}]}),
        encoding="utf-8",
    )

    rc = cli.main(
        [
            "digest",
            "--plans",
            str(plans_path),
            "--date",
            "2026-05-13",
        ]
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert "ClipGen Daily Digest - 2026-05-13" in payload["text"]


def test_run_job_polish_without_api_key_warns(monkeypatch, capsys, tmp_path):
    mod = types.ModuleType("clipgen.jobs")
    mod.run_daily_job = lambda *args, **kwargs: {
        "errors": [],
        "polish_provider": kwargs.get("polish_provider"),
    }
    monkeypatch.setitem(sys.modules, "clipgen.jobs", mod)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    rc = cli.main(
        [
            "run-job",
            "--date",
            "2026-05-13",
            "--out-root",
            str(tmp_path),
            "--polish",
            "--dry-run",
        ]
    )

    captured = capsys.readouterr()
    assert rc == 0
    assert "ANTHROPIC_API_KEY is not set" in captured.err


def test_review_accepts_fractional_threshold(tmp_path):
    input_path = tmp_path / "plans.json"
    out_json = tmp_path / "review.json"
    input_path.write_text(
        json.dumps(
            {
                "plans": [
                    {"video_id": "v1", "score": 0.75},
                    {"video_id": "v2", "score": 0.40},
                ]
            }
        ),
        encoding="utf-8",
    )

    rc = cli.main(
        [
            "review",
            "--input",
            str(input_path),
            "--out-json",
            str(out_json),
            "--score-threshold",
            "0.6",
        ]
    )

    assert rc == 0
    payload = json.loads(out_json.read_text(encoding="utf-8"))
    assert payload["score_threshold"] == 60.0
    assert payload["passed"] == 1


def test_argparse_choices_and_required_are_enforced():
    parser = cli.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["run-job", "--aggressiveness", "4"])

    with pytest.raises(SystemExit):
        parser.parse_args(["run-job"])

    with pytest.raises(SystemExit):
        parser.parse_args(["review"])

    with pytest.raises(SystemExit):
        parser.parse_args(["digest", "--date", "2026-05-13"])
