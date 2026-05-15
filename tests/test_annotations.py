from podedit.annotations import build_annotation_payload, detect_filler_annotations


def transcript(words: list[tuple[str, float, float]], seg_id: str = "s0") -> dict:
    return {
        "segments": [
            {
                "id": seg_id,
                "start": words[0][1] if words else 0.0,
                "end": words[-1][2] if words else 0.0,
                "text": "".join(w[0] for w in words),
                "words": [
                    {"id": f"{seg_id}-w{i}", "text": text, "start": start, "end": end}
                    for i, (text, start, end) in enumerate(words)
                ],
            }
        ]
    }


def test_detects_recommended_filler_span_and_merges_adjacent_words() -> None:
    # All three are in the recommended set so they should merge into a
    # single high-confidence filler span. ``あのー`` was downgraded to weak
    # after the codex review (it can also be a deictic), so the test uses
    # ``えっと`` here instead — still adjacent, still merged.
    anns = detect_filler_annotations(transcript([
        ("えー", 0.0, 0.2),
        ("えっと", 0.25, 0.55),
        ("本題です", 0.8, 1.2),
    ]))

    assert len(anns) == 1
    ann = anns[0]
    assert ann["type"] == "filler"
    assert ann["start"] == 0.0
    assert ann["end"] == 0.55
    assert ann["word_ids"] == ["s0-w0", "s0-w1"]
    assert ann["delete_recommended"] is True
    assert ann["confidence"] >= 0.9
    assert ann["id"].startswith("ann-filler-")


def test_demoted_あのー_is_marked_but_not_delete_recommended() -> None:
    # Regression test for the codex-flagged risk: ``あの``/``あのー`` should
    # still surface as filler hints but must never appear in the one-click
    # delete batch.
    anns = detect_filler_annotations(transcript([
        ("あのー", 0.0, 0.3),
        ("質問なんですけど", 0.4, 1.0),
    ]))

    fillers = [a for a in anns if a["type"] == "filler"]
    assert fillers, "expected あのー to surface as a filler annotation"
    assert all(not a["delete_recommended"] for a in fillers)


def test_marks_aizuchi_but_does_not_recommend_deletion() -> None:
    anns = detect_filler_annotations(transcript([
        ("はい", 0.0, 0.18),
        ("それは重要です", 0.4, 1.0),
    ]))

    assert len(anns) == 1
    assert anns[0]["type"] == "aizuchi"
    assert anns[0]["text"] == "はい"
    assert anns[0]["delete_recommended"] is False


def test_weak_fillers_are_marked_but_not_batch_delete_candidates() -> None:
    anns = detect_filler_annotations(transcript([
        ("まあ", 0.0, 0.2),
        ("そうですね", 0.3, 0.7),
    ]))

    assert [ann["type"] for ann in anns] == ["filler", "aizuchi"]
    assert [ann["delete_recommended"] for ann in anns] == [False, False]


def test_missing_word_ids_get_stable_fallback_ids() -> None:
    data = {
        "segments": [
            {
                "id": "seg-a",
                "words": [
                    {"text": "えっと", "start": 1.0, "end": 1.3},
                ],
            }
        ]
    }

    anns = detect_filler_annotations(data)

    assert anns[0]["word_ids"] == ["seg-a-w0"]
    assert anns[0]["delete_recommended"] is True


def test_invalid_or_empty_words_are_ignored() -> None:
    data = {
        "segments": [
            {
                "id": "s0",
                "words": [
                    {"id": "s0-w0", "text": "えっと", "start": 1.0, "end": 1.0},
                    {"id": "s0-w1", "text": "", "start": 2.0, "end": 2.2},
                ],
            }
        ]
    }

    assert detect_filler_annotations(data) == []


def test_build_annotation_payload_wraps_schema_and_source() -> None:
    payload = build_annotation_payload(transcript([("えー", 0.0, 0.2)]))

    assert payload["schema_version"] == 1
    assert payload["source"] == "ja-filler-heuristic-v1"
    assert len(payload["annotations"]) == 1
