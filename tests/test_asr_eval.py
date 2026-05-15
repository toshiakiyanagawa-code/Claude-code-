from __future__ import annotations

import pytest

from podedit.asr_eval import compute_cer, compute_glossary_recall, transcript_to_text


def test_compute_cer_exact_match() -> None:
    assert compute_cer("文系AI部 123", "文系AI部 123") == 0.0


def test_compute_cer_full_substitution() -> None:
    assert compute_cer("abc", "xyz") == 1.0


def test_compute_cer_nfkc_normalises_widths() -> None:
    assert compute_cer("ＡＩ１２３", "AI123") == 0.0


def test_glossary_recall_empty() -> None:
    recall, details = compute_glossary_recall("クロードの話", [])

    assert recall == 0.0
    assert details == []


def test_glossary_recall_partial() -> None:
    recall, details = compute_glossary_recall(
        "今日はクロードとanthropicについて話します。",
        ["クロード", "Anthropic", "スタンフォード"],
    )

    assert recall == pytest.approx(2 / 3)
    assert details == [
        {"term": "クロード", "found": True, "occurrences": 1},
        {"term": "Anthropic", "found": True, "occurrences": 1},
        {"term": "スタンフォード", "found": False, "occurrences": 0},
    ]


def test_transcript_to_text_handles_missing_words() -> None:
    transcript = {
        "segments": [
            {"id": "s0", "text": "ignored because words is missing"},
            {"id": "s1", "words": [{"text": "文系"}, {"text": "AI"}, {"text": "部"}]},
            {"id": "s2", "words": None},
        ]
    }

    assert transcript_to_text(transcript) == "文系AI部"
