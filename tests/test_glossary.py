"""Tests for src/podedit/glossary.py."""
from __future__ import annotations

from pathlib import Path

from podedit.glossary import (
    MAX_PROMPT_CHARS,
    MAX_TERM_LEN,
    load_terms,
    render_hotwords,
    render_prompt_suffix,
    save_terms,
    truncate_for_prompt,
)


def test_load_missing_file_returns_empty():
    assert load_terms(Path("/nonexistent/glossary.txt")) == []


def test_save_then_load_round_trip(tmp_path: Path):
    p = tmp_path / "glossary.txt"
    written = save_terms(p, ["クロード", "Anthropic", "  認知バイアス  "])
    assert written == ["クロード", "Anthropic", "認知バイアス"]
    assert load_terms(p) == ["クロード", "Anthropic", "認知バイアス"]


def test_save_dedupes_and_drops_blank(tmp_path: Path):
    p = tmp_path / "glossary.txt"
    written = save_terms(p, ["クロード", "", "  ", "クロード", "Anthropic"])
    assert written == ["クロード", "Anthropic"]


def test_save_drops_overlong_terms(tmp_path: Path):
    p = tmp_path / "glossary.txt"
    overlong = "あ" * (MAX_TERM_LEN + 1)
    written = save_terms(p, ["短い", overlong, "OK"])
    assert "短い" in written
    assert "OK" in written
    assert overlong not in written


def test_load_handles_crlf(tmp_path: Path):
    p = tmp_path / "glossary.txt"
    p.write_text("クロード\r\nAnthropic\r\n", encoding="utf-8")
    assert load_terms(p) == ["クロード", "Anthropic"]


def test_truncate_for_prompt_fits_budget():
    terms = ["abc", "defg", "hi"]  # 3 + 2 + 4 + 2 + 2 = 13 chars in ", ".join
    kept = truncate_for_prompt(terms, max_chars=10)
    # "abc, defg" = 9 chars, adding "hi" would be 13 — but +2+2 = 4 more = 13 > 10
    assert kept == ["abc", "defg"]


def test_truncate_for_prompt_empty():
    assert truncate_for_prompt([]) == []


def test_truncate_for_prompt_default_cap():
    # 200 terms × ~10 chars = 2000+ chars total. Truncate should cap.
    terms = [f"用語{i:03d}" for i in range(200)]
    kept = truncate_for_prompt(terms)
    joined = ", ".join(kept)
    assert len(joined) <= MAX_PROMPT_CHARS
    # Adding one more should exceed.
    if len(kept) < len(terms):
        next_one = terms[len(kept)]
        assert len(joined) + 2 + len(next_one) > MAX_PROMPT_CHARS


def test_render_prompt_suffix_empty():
    assert render_prompt_suffix([]) == ""


def test_render_prompt_suffix_japanese_join():
    suffix = render_prompt_suffix(["クロード", "Anthropic"])
    assert suffix == "固有名詞: クロード、Anthropic"


def test_render_hotwords_empty_returns_none():
    assert render_hotwords([]) is None


def test_render_hotwords_comma_join():
    assert render_hotwords(["クロード", "Anthropic"]) == "クロード, Anthropic"


def test_load_corrupt_file_returns_empty(tmp_path: Path):
    """If the file exists but can't be read (e.g. directory), return []."""
    p = tmp_path / "glossary_dir"
    p.mkdir()
    # `read_text` on a directory raises OSError → load_terms catches it.
    assert load_terms(p) == []
