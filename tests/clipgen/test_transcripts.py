from __future__ import annotations

import sys
import types

from clipgen.highlights import Cue, parse_srt
from clipgen.transcripts import fetch_youtube_transcript, transcript_to_srt


def _install_fake_youtube_api(monkeypatch, *, rows=None, exc=None):
    api_mod = types.ModuleType("youtube_transcript_api")
    errors_mod = types.ModuleType("youtube_transcript_api._errors")

    class NoTranscriptFound(Exception):
        pass

    class TranscriptsDisabled(Exception):
        pass

    class VideoUnavailable(Exception):
        pass

    class YouTubeTranscriptApi:
        @staticmethod
        def get_transcript(video_id, languages):
            if exc is not None:
                raise exc
            return rows

    errors_mod.NoTranscriptFound = NoTranscriptFound
    errors_mod.TranscriptsDisabled = TranscriptsDisabled
    errors_mod.VideoUnavailable = VideoUnavailable
    api_mod.YouTubeTranscriptApi = YouTubeTranscriptApi

    monkeypatch.setitem(sys.modules, "youtube_transcript_api", api_mod)
    monkeypatch.setitem(sys.modules, "youtube_transcript_api._errors", errors_mod)
    return errors_mod


def test_fetch_youtube_transcript_returns_cues(monkeypatch):
    _install_fake_youtube_api(
        monkeypatch,
        rows=[
            {"start": 1.2, "duration": 2.3, "text": "こんにちは"},
            {"start": 4.0, "duration": 1.0, "text": "次の字幕"},
        ],
    )

    cues = fetch_youtube_transcript("video123", languages=("ja",))

    assert cues == [
        Cue(start_sec=1.2, end_sec=3.5, text="こんにちは"),
        Cue(start_sec=4.0, end_sec=5.0, text="次の字幕"),
    ]


def test_fetch_youtube_transcript_uses_requested_languages(monkeypatch):
    seen = {}

    api_mod = types.ModuleType("youtube_transcript_api")
    errors_mod = types.ModuleType("youtube_transcript_api._errors")

    class NoTranscriptFound(Exception):
        pass

    class TranscriptsDisabled(Exception):
        pass

    class VideoUnavailable(Exception):
        pass

    class YouTubeTranscriptApi:
        @staticmethod
        def get_transcript(video_id, languages):
            seen["video_id"] = video_id
            seen["languages"] = languages
            return [{"start": 0, "duration": 1, "text": "hello"}]

    errors_mod.NoTranscriptFound = NoTranscriptFound
    errors_mod.TranscriptsDisabled = TranscriptsDisabled
    errors_mod.VideoUnavailable = VideoUnavailable
    api_mod.YouTubeTranscriptApi = YouTubeTranscriptApi
    monkeypatch.setitem(sys.modules, "youtube_transcript_api", api_mod)
    monkeypatch.setitem(sys.modules, "youtube_transcript_api._errors", errors_mod)

    fetch_youtube_transcript("abc", languages=("en", "ja"))

    assert seen == {"video_id": "abc", "languages": ["en", "ja"]}


def test_fetch_youtube_transcript_returns_empty_when_transcripts_disabled(monkeypatch):
    errors_mod = _install_fake_youtube_api(monkeypatch, rows=[])
    _install_fake_youtube_api(monkeypatch, exc=errors_mod.TranscriptsDisabled())

    assert fetch_youtube_transcript("video123") == []


def test_fetch_youtube_transcript_returns_empty_when_no_transcript(monkeypatch):
    errors_mod = _install_fake_youtube_api(monkeypatch, rows=[])
    _install_fake_youtube_api(monkeypatch, exc=errors_mod.NoTranscriptFound())

    assert fetch_youtube_transcript("video123") == []


def test_fetch_youtube_transcript_skips_invalid_or_empty_rows(monkeypatch):
    _install_fake_youtube_api(
        monkeypatch,
        rows=[
            {"start": 0, "duration": 1, "text": "ok"},
            {"start": "bad", "duration": 1, "text": "skip"},
            {"start": 2, "duration": 1, "text": "   "},
            {"duration": 1, "text": "skip"},
        ],
    )

    assert fetch_youtube_transcript("video123") == [Cue(start_sec=0.0, end_sec=1.0, text="ok")]


def test_transcript_to_srt_round_trips_with_parse_srt():
    cues = [
        Cue(start_sec=0.0, end_sec=1.25, text="一行目"),
        Cue(start_sec=61.5, end_sec=63.0, text="二行目"),
    ]

    srt = transcript_to_srt(cues)
    parsed = parse_srt(srt)

    assert parsed == cues
    assert "1\n00:00:00,000 --> 00:00:01,250\n一行目" in srt
    assert "2\n00:01:01,500 --> 00:01:03,000\n二行目" in srt


def test_transcript_to_srt_collapses_multiline_text_on_parse():
    # parse_srt joins multi-line body with a single space, so round-trip
    # collapses embedded newlines. Document the expected behavior explicitly.
    cues = [Cue(start_sec=0.0, end_sec=1.0, text="二行目\n続き")]
    parsed = parse_srt(transcript_to_srt(cues))
    assert parsed == [Cue(start_sec=0.0, end_sec=1.0, text="二行目 続き")]
