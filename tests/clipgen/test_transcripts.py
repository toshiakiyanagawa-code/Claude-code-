from __future__ import annotations

import sys
import types

import pytest

from clipgen.highlights import Cue, parse_srt
from clipgen.transcripts import fetch_youtube_transcript, transcript_to_srt


def _snippet(start, duration, text):
    return types.SimpleNamespace(start=start, duration=duration, text=text)


def _install_fake_youtube_api(monkeypatch, *, snippets=None, exc_name=None, on_fetch=None):
    api_mod = types.ModuleType("youtube_transcript_api")
    errors_mod = types.ModuleType("youtube_transcript_api._errors")

    class NoTranscriptFound(Exception):
        pass

    class TranscriptsDisabled(Exception):
        pass

    class VideoUnavailable(Exception):
        pass

    class YouTubeRequestFailed(Exception):
        pass

    class IpBlocked(Exception):
        pass

    class RequestBlocked(Exception):
        pass

    error_classes = {
        "NoTranscriptFound": NoTranscriptFound,
        "TranscriptsDisabled": TranscriptsDisabled,
        "VideoUnavailable": VideoUnavailable,
        "YouTubeRequestFailed": YouTubeRequestFailed,
        "IpBlocked": IpBlocked,
        "RequestBlocked": RequestBlocked,
    }

    for name, cls in error_classes.items():
        setattr(errors_mod, name, cls)

    class FetchedTranscript:
        def __init__(self, fetched_snippets):
            self.snippets = list(fetched_snippets or [])

    class YouTubeTranscriptApi:
        def fetch(self, video_id, languages):
            if on_fetch is not None:
                on_fetch(video_id, languages)
            if exc_name is not None:
                raise error_classes[exc_name]()
            return FetchedTranscript(snippets)

    api_mod.YouTubeTranscriptApi = YouTubeTranscriptApi

    monkeypatch.setitem(sys.modules, "youtube_transcript_api", api_mod)
    monkeypatch.setitem(sys.modules, "youtube_transcript_api._errors", errors_mod)
    return errors_mod


def test_fetch_youtube_transcript_returns_cues(monkeypatch):
    _install_fake_youtube_api(
        monkeypatch,
        snippets=[
            _snippet(1.2, 2.3, "こんにちは"),
            _snippet(4.0, 1.0, "次の字幕"),
        ],
    )

    cues = fetch_youtube_transcript("video123", languages=("ja",))

    assert cues == [
        Cue(start_sec=1.2, end_sec=3.5, text="こんにちは"),
        Cue(start_sec=4.0, end_sec=5.0, text="次の字幕"),
    ]


def test_fetch_youtube_transcript_uses_requested_languages(monkeypatch):
    seen = {}

    def on_fetch(video_id, languages):
        seen["video_id"] = video_id
        seen["languages"] = languages

    _install_fake_youtube_api(
        monkeypatch,
        snippets=[_snippet(0, 1, "hello")],
        on_fetch=on_fetch,
    )

    fetch_youtube_transcript("abc", languages=("en", "ja"))

    assert seen == {"video_id": "abc", "languages": ["en", "ja"]}


@pytest.mark.parametrize(
    "exc_name",
    [
        "TranscriptsDisabled",
        "NoTranscriptFound",
        "VideoUnavailable",
        "YouTubeRequestFailed",
        "IpBlocked",
        "RequestBlocked",
    ],
)
def test_fetch_youtube_transcript_returns_empty_for_youtube_failures(monkeypatch, exc_name):
    _install_fake_youtube_api(monkeypatch, exc_name=exc_name)

    assert fetch_youtube_transcript("video123") == []


def test_fetch_youtube_transcript_skips_invalid_or_empty_snippets(monkeypatch):
    _install_fake_youtube_api(
        monkeypatch,
        snippets=[
            _snippet(0, 1, "ok"),
            _snippet("bad", 1, "skip"),
            _snippet(2, 1, "   "),
            types.SimpleNamespace(duration=1, text="skip"),
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
