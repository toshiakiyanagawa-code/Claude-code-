"""Persistent glossary of project-specific proper nouns (P0-H).

編集者が UI から登録した固有名詞リスト。文字起こし時に
faster-whisper の ``initial_prompt`` / ``hotwords`` に自動結合し、
「クロード」「Anthropic」のような案件特有の語の誤認識を予防する。

設計 (codex 相談):
  - 保存先: ``<work_dir>/glossary.txt`` (1行1語、計画書通り)
  - API 返却は JSON、編集 UI は textarea (改行区切り) でシンプルに
  - 切り詰め: ~1000 字を上限に先頭から積む。空行/重複は除外
  - サーバ側で initial_prompt 未指定時のみ DEFAULT prompt と結合
"""
from __future__ import annotations

from pathlib import Path

GLOSSARY_VERSION = 1
# faster-whisper は ``initial_prompt`` の先頭 ~200 トークンしか見ない。
# 日本語で 1000 字あれば 500 トークン程度に収まる目安。
MAX_PROMPT_CHARS = 1000
# 1 語が長すぎる行はスキップする閾値。固有名詞で 50 字超は誤入力を疑う。
MAX_TERM_LEN = 50


def load_terms(path: Path) -> list[str]:
    """Load glossary terms from a plain text file. Missing file => empty list.

    1 line = 1 term. Strips whitespace; drops blanks, duplicates, and
    overly-long terms.
    """
    if not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return _clean_terms(raw.splitlines())


def save_terms(path: Path, terms: list[str]) -> list[str]:
    """Write terms to disk. Returns the cleaned list actually written."""
    cleaned = _clean_terms(terms)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "\n".join(cleaned)
    if cleaned:
        payload += "\n"
    path.write_text(payload, encoding="utf-8")
    return cleaned


def _clean_terms(lines: list[str]) -> list[str]:
    """Strip / dedupe / drop blanks and overlong terms. Order preserved."""
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        term = line.strip()
        if not term:
            continue
        if len(term) > MAX_TERM_LEN:
            continue
        if term in seen:
            continue
        seen.add(term)
        out.append(term)
    return out


def truncate_for_prompt(terms: list[str], max_chars: int = MAX_PROMPT_CHARS) -> list[str]:
    """Take terms from the front until the joined ", " form exceeds max_chars.

    Returns the longest prefix whose ``", ".join()`` representation fits in
    ``max_chars``. Empty input → empty output.
    """
    if not terms:
        return []
    kept: list[str] = []
    used = 0
    for term in terms:
        # ", ".join growth: first item costs len(term), subsequent cost len(term)+2.
        cost = len(term) if not kept else len(term) + 2
        if used + cost > max_chars:
            break
        kept.append(term)
        used += cost
    return kept


def render_prompt_suffix(terms: list[str]) -> str:
    """Build a Japanese-friendly suffix to append to the default ASR prompt.

    Empty terms => empty string (caller can detect and skip concat).
    """
    truncated = truncate_for_prompt(terms)
    if not truncated:
        return ""
    return "固有名詞: " + "、".join(truncated)


def render_hotwords(terms: list[str]) -> str | None:
    """Build the faster-whisper ``hotwords`` parameter from terms.

    Returns None for empty input. Truncates by character budget so a
    well-meaning editor pasting hundreds of terms doesn't blow the model
    context.
    """
    truncated = truncate_for_prompt(terms)
    if not truncated:
        return None
    return ", ".join(truncated)
