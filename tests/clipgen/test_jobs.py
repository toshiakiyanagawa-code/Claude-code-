from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from clipgen import jobs  # noqa: E402


def test_run_daily_job_writes_outputs(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "run_pipeline_mock", lambda *a, **k: [{"video_id": "v1", "title": "One", "score": 90}])
    monkeypatch.setattr(jobs, "_extract_one", lambda plan, extract_dir, dry_run: {"ok": True, "title": plan["title"]})

    result = jobs.run_daily_job("2026-05-13", tmp_path, source="fixture.json")

    assert result == {"date": "2026-05-13", "candidates": 1, "plans": 1, "extracts": 1, "errors": []}
    assert (tmp_path / "2026-05-13" / "candidates.json").exists()
    assert (tmp_path / "2026-05-13" / "plan.json").exists()
    assert (tmp_path / "2026-05-13" / "extract").is_dir()


def test_run_daily_job_dry_run_writes_no_files(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "run_pipeline_mock", lambda *a, **k: [{"video_id": "v1", "title": "One"}])
    monkeypatch.setattr(jobs, "_extract_one", lambda *a, **k: {"ok": True})

    result = jobs.run_daily_job("2026-05-13", tmp_path, dry_run=True, source="fixture.json")

    assert result["candidates"] == 1
    assert result["extracts"] == 1
    assert not (tmp_path / "2026-05-13").exists()


def test_run_daily_job_collects_pipeline_error(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(jobs, "run_pipeline_mock", fail)

    result = jobs.run_daily_job("2026-05-13", tmp_path, dry_run=True)

    assert result["candidates"] == 0
    assert result["plans"] == 0
    assert result["extracts"] == 0
    assert result["errors"] == [{"error": "pipeline", "message": "boom"}]


def test_run_daily_job_collects_plan_error_and_continues(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "run_pipeline_mock", lambda *a, **k: [{"title": "x"}])

    def bad_plan(*a, **k):
        raise ValueError("bad plan")

    monkeypatch.setattr(jobs, "_candidate_plan", bad_plan)

    result = jobs.run_daily_job("2026-05-13", tmp_path, dry_run=True)

    assert result["candidates"] == 1
    assert result["plans"] == 0
    assert result["extracts"] == 0
    assert result["errors"] == [{"error": "plan", "message": "bad plan"}]


def test_run_daily_job_collects_extract_error(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "run_pipeline_mock", lambda *a, **k: [{"title": "x"}])

    def bad_extract(*a, **k):
        raise RuntimeError("bad extract")

    monkeypatch.setattr(jobs, "_extract_one", bad_extract)

    result = jobs.run_daily_job("2026-05-13", tmp_path, dry_run=True)

    assert result["candidates"] == 1
    assert result["plans"] == 1
    assert result["extracts"] == 0
    assert result["errors"] == [{"error": "extract", "message": "bad extract"}]


def test_run_daily_job_serializes_candidates_and_plan(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "run_pipeline_mock", lambda *a, **k: [{"video_id": "v1", "title": "One"}])
    monkeypatch.setattr(jobs, "_extract_one", lambda *a, **k: {"ok": True})

    jobs.run_daily_job("2026-05-13", tmp_path, source="fixture.json")

    candidates = json.loads((tmp_path / "2026-05-13" / "candidates.json").read_text(encoding="utf-8"))
    plans = json.loads((tmp_path / "2026-05-13" / "plan.json").read_text(encoding="utf-8"))
    assert candidates[0]["video_id"] == "v1"
    assert plans[0]["title"] == "One"
