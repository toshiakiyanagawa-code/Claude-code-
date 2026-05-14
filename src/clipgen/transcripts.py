"""YouTube transcript helpers."""
from __future__ import annotations

from collections.abc import Iterable
from importlib import import_module
from typing import Any

from .highlights import Cue

DEFAULT_LANGUAGES: tuple[str, ...] = ("ja", "ja-JP", "en")

_TRANSCRIPT_ERROR_NAMES = (
    "NoTranscriptFound",
    "TranscriptsDisabled",
    "VideoUnavailable",
    "CouldNotRetrieveTranscript",
    "YouTubeRequestFailed",
    "IpBlocked",
    "RequestBlocked",
    "AgeRestricted",
    "InvalidVideoId",
    "VideoUnplayable",
    "TooManyRequests",
    "NotTranslatable",
    "TranslationLanguageNotAvailable",
)


def _load_transcript_error_types() -> tuple[type[BaseException], ...]:
    try:
        errors_mod = import_module("youtube_transcript_api._errors")
    except Exception:
        return ()

    error_types: list[type[BaseException]] = []
    for name in _TRANSCRIPT_ERROR_NAMES:
        exc = getattr(errors_mod, name, None)
        if isinstance(exc, type) and issubclass(exc, BaseException):
            error_types.append(exc)
    return tuple(error_types)


def _snippet_value(snippet: Any, name: str) -> Any:
    if isinstance(snippet, dict):
        return snippet.get(name)
    return getattr(snippet, name)


def fetch_youtube_transcript(
    video_id: str,
    languages: Iterable[str] | None = DEFAULT_LANGUAGES,
) -> list[Cue]:
    """Fetch a YouTube transcript and convert it to highlight cues.

    youtube-transcript-api v1 returns a FetchedTranscript object from
    YouTubeTranscriptApi().fetch(...). Its caption rows live in
    FetchedTranscript.snippets and expose .start, .duration and .text.
    Network/IP/transcript availability failures are treated as "no transcript".
    """
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except Exception:
        return []

    language_list = list(languages) if languages is not None else list(DEFAULT_LANGUAGES)
    error_types = _load_transcript_error_types()

    try:
        fetched = YouTubeTranscriptApi().fetch(video_id, languages=language_list)
    except Exception as exc:
        if not error_types or isinstance(exc, error_types):
            return []
        raise

    snippets = getattr(fetched, "snippets", None)
    if snippets is None:
        return []

    cues: list[Cue] = []
    for snippet in snippets:
        try:
            start = float(_snippet_value(snippet, "start"))
            duration = float(_snippet_value(snippet, "duration"))
            text_value = _snippet_value(snippet, "text")
        except (AttributeError, TypeError, ValueError):
            continue

        if duration < 0 or text_value is None:
            continue

        text = str(text_value).replace("\r\n", "\n").replace("\r", "\n").strip()
        if not text:
            continue

        cues.append(Cue(start_sec=start, end_sec=start + duration, text=text))

    return cues


def _format_srt_timestamp(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"


def transcript_to_srt(cues: list[Cue]) -> str:
    blocks: list[str] = []
    for i, cue in enumerate(cues, start=1):
        blocks.append(
            "\n".join(
                [
                    str(i),
                    f"{_format_srt_timestamp(cue.start_sec)} --> {_format_srt_timestamp(cue.end_sec)}",
                    str(cue.text),
                ]
            )
        )
    return "\n\n".join(blocks) + ("\n" if blocks else "")


__all__ = ["fetch_youtube_transcript", "transcript_to_srt"]
