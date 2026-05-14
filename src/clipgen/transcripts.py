"""YouTube transcript helpers."""
from __future__ import annotations

from collections.abc import Iterable

from .highlights import Cue


def fetch_youtube_transcript(
    video_id: str,
    *,
    languages: tuple[str, ...] = ("ja", "ja-JP", "en"),
) -> list[Cue]:
    """Fetch a YouTube transcript and normalize it to Cue objects.

    Returns an empty list when transcripts are unavailable so callers can
    fall back to non-transcript behavior.
    """
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        from youtube_transcript_api._errors import (
            NoTranscriptFound,
            TranscriptsDisabled,
            VideoUnavailable,
        )
    except ImportError:
        return []

    try:
        rows = YouTubeTranscriptApi.get_transcript(video_id, languages=list(languages))
    except (NoTranscriptFound, TranscriptsDisabled, VideoUnavailable):
        return []
    except Exception:
        return []

    cues: list[Cue] = []
    for row in rows:
        try:
            start = float(row["start"])
            duration = float(row.get("duration", 0.0))
            text = str(row.get("text", "")).strip()
        except (TypeError, ValueError, KeyError):
            continue
        if not text:
            continue
        cues.append(Cue(start_sec=start, end_sec=start + max(duration, 0.0), text=text))
    return cues


def _format_timestamp(seconds: float) -> str:
    total_ms = max(int(round(seconds * 1000)), 0)
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"


def transcript_to_srt(cues: Iterable[Cue]) -> str:
    """Convert cues to standard numbered SRT text."""
    blocks: list[str] = []
    for i, cue in enumerate(cues, start=1):
        text = cue.text.replace("\r\n", "\n").replace("\r", "\n").strip()
        blocks.append(
            "\n".join(
                [
                    str(i),
                    f"{_format_timestamp(cue.start_sec)} --> {_format_timestamp(cue.end_sec)}",
                    text,
                ]
            )
        )
    return "\n\n".join(blocks) + ("\n" if blocks else "")
