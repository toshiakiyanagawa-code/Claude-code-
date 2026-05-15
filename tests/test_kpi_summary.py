from __future__ import annotations

import json
from pathlib import Path

import pytest

from podedit.asr_eval import summarize_kpi_jsonl


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_correction_clicks_count(tmp_path: Path) -> None:
    path = tmp_path / "episode.kpi.jsonl"
    _write_jsonl(
        path,
        [
            {"event": "ui.loaded", "ts": 10.0, "audio_duration_sec": 1800.0},
            {"event": "ui.op.delete", "ts": 11.0},
            {"event": "ui.op.delete", "ts": 12.0},
            {"event": "ui.op.move", "ts": 13.0},
            {"event": "ui.annotation.fillers.added", "ts": 14.0, "count": 3},
            {"event": "ui.click.word", "ts": 15.0},
            {"event": "ui.dblclick.word", "ts": 16.0},
            {"event": "ui.drag.select", "ts": 17.0},
        ],
    )

    summary = summarize_kpi_jsonl(path)

    assert summary["counts"]["ops.delete"] == 2
    assert summary["counts"]["ops.move"] == 1
    assert summary["counts"]["ops.fillers.added"] == 3
    assert summary["correction_clicks"] == 6
    assert summary["counts"]["word_clicks"] == 2
    assert summary["counts"]["drag_selections"] == 1
    assert summary["session_wall_sec"] == pytest.approx(7.0)


def test_per_hour_normalisation(tmp_path: Path) -> None:
    path = tmp_path / "episode.kpi.jsonl"
    _write_jsonl(
        path,
        [
            {"event": "ui.loaded", "ts": 0.0},
            {"event": "ui.op.delete", "ts": 1.0},
            {"event": "ui.op.delete", "ts": 2.0},
        ],
    )

    summary = summarize_kpi_jsonl(path, audio_duration_sec=1800.0)

    assert summary["correction_clicks"] == 2
    assert summary["correction_clicks_per_audio_hour"] == pytest.approx(4.0)


def test_corrupted_lines_skipped(tmp_path: Path) -> None:
    path = tmp_path / "episode.kpi.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps({"event": "ui.loaded", "ts": 0.0, "audio_duration_sec": 3600.0}),
                "{broken-json",
                json.dumps({"event": "ui.op.move", "ts": 3.0}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    summary = summarize_kpi_jsonl(path)

    assert summary["events_read"] == 2
    assert summary["events_skipped"] == 1
    assert summary["correction_clicks"] == 1
    assert summary["correction_clicks_per_audio_hour"] == pytest.approx(1.0)
