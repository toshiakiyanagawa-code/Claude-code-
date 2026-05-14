"""Tests for src/podedit/asr_dictionary.py."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from podedit.asr_dictionary import (
    DictEntry,
    Dictionary,
    apply_dictionary,
    dict_ops_path_for,
    load_dictionary,
    save_dictionary,
    write_dict_ops,
)


def _seg(seg_id: str, words: list[dict]) -> dict:
    return {
        "id": seg_id,
        "start": words[0]["start"] if words else 0.0,
        "end": words[-1]["end"] if words else 0.0,
        "text": "".join(w["text"] for w in words),
        "words": words,
    }


def _word(wid: str, start: float, end: float, text: str, conf: float | None = 0.9) -> dict:
    w = {"id": wid, "start": start, "end": end, "text": text}
    if conf is not None:
        w["confidence"] = conf
    return w


def _make_tx(segments: list[dict]) -> dict:
    return {"schema_version": 1, "segments": segments}


def test_empty_dictionary_returns_input_unchanged():
    tx = _make_tx([_seg("s0", [_word("s0-w0", 0.0, 0.5, "黒"), _word("s0-w1", 0.5, 1.0, "だ")])])
    new_tx, ops = apply_dictionary(tx, Dictionary(entries=()))
    assert ops == []
    assert new_tx["segments"][0]["words"][0]["text"] == "黒"
    assert new_tx["segments"][0]["words"][1]["text"] == "だ"


def test_single_word_match():
    tx = _make_tx([_seg("s0", [_word("s0-w0", 0.0, 0.6, "クロウド")])])
    d = Dictionary(entries=(DictEntry(id="e0", from_="クロウド", to="クロード"),))
    new_tx, ops = apply_dictionary(tx, d)
    assert len(ops) == 1
    assert new_tx["segments"][0]["words"][0]["text"] == "クロード"
    assert new_tx["segments"][0]["text"] == "クロード"


def test_two_word_match_merges_span():
    w0 = _word("s0-w0", 1.0, 1.3, "黒", conf=0.5)
    w1 = _word("s0-w1", 1.3, 1.6, "だ", conf=0.6)
    tx = _make_tx([_seg("s0", [w0, w1])])
    d = Dictionary(entries=(DictEntry(id="e0", from_="黒だ", to="クロード"),))
    new_tx, ops = apply_dictionary(tx, d)
    words = new_tx["segments"][0]["words"]
    assert len(words) == 1
    assert words[0]["text"] == "クロード"
    assert words[0]["start"] == 1.0
    assert words[0]["end"] == 1.6
    assert words[0]["confidence"] == pytest.approx(0.5)  # min of span
    assert ops[0]["before_words"][0]["text"] == "黒"
    assert ops[0]["before_words"][1]["text"] == "だ"
    assert ops[0]["after_words"][0]["text"] == "クロード"
    assert ops[0]["note"] == "dict:黒だ=>クロード"


def test_longest_match_wins():
    """If both '黒' and '黒だ' are in the dict, '黒だ' wins at the position."""
    w0 = _word("s0-w0", 0.0, 0.3, "黒")
    w1 = _word("s0-w1", 0.3, 0.6, "だ")
    tx = _make_tx([_seg("s0", [w0, w1])])
    d = Dictionary(
        entries=(
            DictEntry(id="short", from_="黒", to="クロ"),
            DictEntry(id="long", from_="黒だ", to="クロード"),
        )
    )
    new_tx, ops = apply_dictionary(tx, d)
    assert len(new_tx["segments"][0]["words"]) == 1
    assert new_tx["segments"][0]["words"][0]["text"] == "クロード"
    assert ops[0]["entry_id"] == "long"


def test_segment_boundary_not_crossed():
    tx = _make_tx(
        [
            _seg("s0", [_word("s0-w0", 0.0, 0.3, "黒")]),
            _seg("s1", [_word("s1-w0", 0.3, 0.6, "だ")]),
        ]
    )
    d = Dictionary(entries=(DictEntry(id="e0", from_="黒だ", to="クロード"),))
    new_tx, ops = apply_dictionary(tx, d)
    assert ops == []
    assert new_tx["segments"][0]["words"][0]["text"] == "黒"
    assert new_tx["segments"][1]["words"][0]["text"] == "だ"


def test_nfkc_normalization():
    """Full-width ASR output should match ASCII-input dictionary entry."""
    w0 = _word("s0-w0", 0.0, 0.5, "ＡＰＩ")  # full-width
    tx = _make_tx([_seg("s0", [w0])])
    d = Dictionary(entries=(DictEntry(id="e0", from_="API", to="API"),))
    new_tx, ops = apply_dictionary(tx, d)
    assert len(ops) == 1
    assert new_tx["segments"][0]["words"][0]["text"] == "API"


def test_disabled_entry_is_skipped():
    tx = _make_tx([_seg("s0", [_word("s0-w0", 0.0, 0.5, "クロウド")])])
    d = Dictionary(entries=(DictEntry(id="e0", from_="クロウド", to="クロード", enabled=False),))
    new_tx, ops = apply_dictionary(tx, d)
    assert ops == []
    assert new_tx["segments"][0]["words"][0]["text"] == "クロウド"


def test_max_conf_suppresses_high_confidence_match():
    """Entry with max_conf set should refuse to replace high-confidence spans."""
    w0 = _word("s0-w0", 0.0, 0.5, "クロウド", conf=0.95)
    tx = _make_tx([_seg("s0", [w0])])
    d = Dictionary(
        entries=(DictEntry(id="e0", from_="クロウド", to="クロード", max_conf=0.5),)
    )
    new_tx, ops = apply_dictionary(tx, d)
    assert ops == []
    assert new_tx["segments"][0]["words"][0]["text"] == "クロウド"


def test_max_conf_allows_low_confidence_match():
    w0 = _word("s0-w0", 0.0, 0.5, "クロウド", conf=0.3)
    tx = _make_tx([_seg("s0", [w0])])
    d = Dictionary(
        entries=(DictEntry(id="e0", from_="クロウド", to="クロード", max_conf=0.5),)
    )
    new_tx, ops = apply_dictionary(tx, d)
    assert len(ops) == 1
    assert new_tx["segments"][0]["words"][0]["text"] == "クロード"


def test_no_match_leaves_words_intact():
    w0 = _word("s0-w0", 0.0, 0.3, "おはよう")
    w1 = _word("s0-w1", 0.3, 0.6, "ござい")
    w2 = _word("s0-w2", 0.6, 0.9, "ます")
    tx = _make_tx([_seg("s0", [w0, w1, w2])])
    d = Dictionary(entries=(DictEntry(id="e0", from_="こんにちは", to="HI"),))
    new_tx, ops = apply_dictionary(tx, d)
    assert ops == []
    assert [w["text"] for w in new_tx["segments"][0]["words"]] == ["おはよう", "ござい", "ます"]


def test_input_dict_is_not_mutated():
    original = _make_tx([_seg("s0", [_word("s0-w0", 0.0, 0.5, "クロウド")])])
    d = Dictionary(entries=(DictEntry(id="e0", from_="クロウド", to="クロード"),))
    new_tx, _ops = apply_dictionary(original, d)
    assert new_tx["segments"][0]["words"][0]["text"] == "クロード"
    # Original untouched.
    assert original["segments"][0]["words"][0]["text"] == "クロウド"


def test_load_save_round_trip(tmp_path: Path):
    src = Dictionary(
        entries=(
            DictEntry(id="e0", from_="黒だ", to="クロード"),
            DictEntry(id="e1", from_="アンソロピック", to="Anthropic", enabled=False),
        )
    )
    p = tmp_path / "dictionary.json"
    save_dictionary(p, src)
    loaded = load_dictionary(p)
    assert [e.from_ for e in loaded.entries] == ["黒だ", "アンソロピック"]
    assert [e.to for e in loaded.entries] == ["クロード", "Anthropic"]
    assert loaded.entries[1].enabled is False


def test_load_missing_file_returns_empty():
    d = load_dictionary(Path("/nonexistent/path/dictionary.json"))
    assert d.entries == ()


def test_load_malformed_file_returns_empty(tmp_path: Path):
    p = tmp_path / "dictionary.json"
    p.write_text("not json{", encoding="utf-8")
    d = load_dictionary(p)
    assert d.entries == ()


def test_load_skips_invalid_entries(tmp_path: Path):
    p = tmp_path / "dictionary.json"
    p.write_text(
        json.dumps(
            {
                "version": 1,
                "entries": [
                    {"from": "黒だ", "to": "クロード"},  # ok, id auto-assigned
                    {"from": "", "to": "X"},               # empty from, skip
                    {"to": "Y"},                           # missing from, skip
                    {"from": "Z"},                         # missing to, skip
                ],
            }
        ),
        encoding="utf-8",
    )
    d = load_dictionary(p)
    assert len(d.entries) == 1
    assert d.entries[0].from_ == "黒だ"


def test_dict_ops_path_for_default_layout():
    tp = Path("/tmp/work/ep01.transcript.json")
    assert dict_ops_path_for(tp).name == "ep01.transcript.dict-ops.jsonl"


def test_write_dict_ops_appends(tmp_path: Path):
    p = tmp_path / "ep.dict-ops.jsonl"
    write_dict_ops(p, [{"op": "dict_replace", "entry_id": "e0"}])
    write_dict_ops(p, [{"op": "dict_replace", "entry_id": "e1"}])
    lines = p.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["entry_id"] == "e0"
    assert json.loads(lines[1])["entry_id"] == "e1"


def test_write_dict_ops_noop_for_empty(tmp_path: Path):
    p = tmp_path / "ep.dict-ops.jsonl"
    write_dict_ops(p, [])
    assert not p.exists()


def test_nonempty_dict_but_no_match_is_byte_identical_dict():
    """When a dictionary is configured but nothing matches, transcript dict stays equal."""
    tx = _make_tx(
        [
            _seg(
                "s0",
                [
                    _word("s0-w0", 0.0, 0.3, "おはよう"),
                    _word("s0-w1", 0.3, 0.6, "ござい"),
                    _word("s0-w2", 0.6, 0.9, "ます"),
                ],
            )
        ]
    )
    d = Dictionary(entries=(DictEntry(id="e0", from_="存在しない語", to="X"),))
    import copy

    snapshot = copy.deepcopy(tx)
    new_tx, ops = apply_dictionary(tx, d)
    assert ops == []
    assert new_tx == snapshot


def test_space_joined_segment_text_preserved():
    """English-style segment text uses space join — replacement must keep that."""
    w0 = _word("s0-w0", 0.0, 0.5, "hello")
    w1 = _word("s0-w1", 0.5, 1.0, "world")
    seg = {
        "id": "s0",
        "start": 0.0,
        "end": 1.0,
        "text": "hello world",
        "words": [w0, w1],
    }
    tx = _make_tx([seg])
    d = Dictionary(entries=(DictEntry(id="e0", from_="world", to="universe"),))
    new_tx, ops = apply_dictionary(tx, d)
    assert len(ops) == 1
    # Words list reflects replacement.
    assert [w["text"] for w in new_tx["segments"][0]["words"]] == ["hello", "universe"]
    # Segment text preserves the original join style (space-separated).
    assert new_tx["segments"][0]["text"] == "hello universe"


def test_concat_joined_segment_text_preserved():
    """Japanese-style segment text uses concat join — replacement must keep that."""
    w0 = _word("s0-w0", 0.0, 0.3, "クロウド")
    seg = {
        "id": "s0",
        "start": 0.0,
        "end": 0.3,
        "text": "クロウド",
        "words": [w0],
    }
    tx = _make_tx([seg])
    d = Dictionary(entries=(DictEntry(id="e0", from_="クロウド", to="クロード"),))
    new_tx, _ops = apply_dictionary(tx, d)
    assert new_tx["segments"][0]["text"] == "クロード"


def test_overlapping_matches_left_wins():
    """For `A B C` with entries 'A B' and 'B C', greedy left-to-right picks 'A B'."""
    w0 = _word("s0-w0", 0.0, 0.3, "A")
    w1 = _word("s0-w1", 0.3, 0.6, "B")
    w2 = _word("s0-w2", 0.6, 0.9, "C")
    tx = _make_tx([_seg("s0", [w0, w1, w2])])
    d = Dictionary(
        entries=(
            DictEntry(id="ab", from_="AB", to="ab"),
            DictEntry(id="bc", from_="BC", to="bc"),
        )
    )
    new_tx, ops = apply_dictionary(tx, d)
    assert len(ops) == 1
    assert ops[0]["entry_id"] == "ab"
    assert [w["text"] for w in new_tx["segments"][0]["words"]] == ["ab", "C"]
