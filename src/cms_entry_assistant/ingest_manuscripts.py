"""Batch-ingest President Online .docx manuscripts.

The President Online editorial format uses bullet section markers:

    ・タイトル
    <title text>
    <optional subtitle>
    ・リード
    <lead paragraphs...>
    ※本稿は『<book>』(<publisher>)の一部を再編集したものです。
    ■<h4 heading 1>
    <body>
    （2ページ目）
    ■<h4 heading 2>
    ...

Variations seen in the corpus:
  - "・タイトル" / "・メイン" / "・抜粋箇所"
  - Some docs already start with the title (no marker)
  - Page break shown as "（2ページ目）" / "(N ページ目)" / "（Nページ目）"

Usage:
    uv run python -m cms_entry_assistant.ingest_manuscripts [path]
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from cms_entry_assistant.photo_demo import DocxIngestError, _paragraphs_from_docx

DEFAULT_DIR = Path("data/manuscripts")
INDEX_PATH = Path("data/manuscripts_index.json")

# Section markers in editorial drafts. Lines starting with "・" + one of these
# words are *labels*, not content. The next non-empty line is the value.
_SECTION_LABEL_RE = re.compile(
    r"^[・・][\s　]*(タイトル|サブタイトル|サブタイ|メイン|リード|抜粋箇所|抜粋|本稿|キャッチ|見出し|概要)[\s　]*$"
)
_H4_RE = re.compile(r"^[\s　]*■[\s　]*(.+?)[\s　]*$")
_PAGE_BREAK_RE = re.compile(r"^[（\(][\s　]*(\d+)[\s　]*ページ目[\s　]*[）\)]$")
_HONGOU_RE = re.compile(r"^[※\*]?本稿は")


def _is_section_label(text: str) -> str | None:
    m = _SECTION_LABEL_RE.match(text)
    return m.group(1) if m else None


def extract_record(docx_path: Path) -> dict:
    blob = docx_path.read_bytes()
    try:
        paragraphs = _paragraphs_from_docx(blob)
    except DocxIngestError as exc:
        return {"file": docx_path.name, "error": str(exc)}

    title = ""
    subtitle = ""
    lead: list[str] = []
    hongou_marker = ""
    h4_entries: list[dict] = []  # [{label, page, body_chars}]
    body_chars_in_current_h4 = 0
    current_page = 1  # カンバン = page 1, first h4 = page 2 by convention
    current_h4_label = ""
    pending_section: str | None = None

    for line in paragraphs:
        # Page break marker — update page counter but otherwise drop
        m_page = _PAGE_BREAK_RE.match(line)
        if m_page:
            current_page = int(m_page.group(1))
            continue

        # H4 heading
        m_h4 = _H4_RE.match(line)
        if m_h4:
            if current_h4_label:
                h4_entries[-1]["body_chars"] = body_chars_in_current_h4
            current_h4_label = m_h4.group(1).strip()
            h4_entries.append(
                {
                    "label": current_h4_label,
                    "page": max(current_page, len(h4_entries) + 2),
                    "body_chars": 0,
                }
            )
            body_chars_in_current_h4 = 0
            pending_section = None
            continue

        # Section label ("・タイトル", "・リード", ...)
        section = _is_section_label(line)
        if section:
            pending_section = section
            continue

        # Hongou marker — capture wherever it appears (usually right after lead)
        if _HONGOU_RE.match(line) and not hongou_marker:
            hongou_marker = line

        # Assign content to the pending section
        if pending_section == "タイトル" or pending_section == "メイン":
            if not title:
                title = line
            elif not subtitle and not _H4_RE.match(line):
                subtitle = line
                pending_section = None
            continue
        if pending_section == "サブタイトル" or pending_section == "サブタイ":
            if not subtitle:
                subtitle = line
            pending_section = None
            continue
        if pending_section == "リード":
            if len(lead) < 5:
                lead.append(line)
            continue
        if pending_section in {"抜粋箇所", "抜粋", "本稿", "概要"}:
            # These are editorial notes — capture into hongou_marker if empty
            if not hongou_marker:
                hongou_marker = line
            pending_section = None
            continue

        # Fallback: if title is still empty, treat the first plain paragraph as title.
        if not title and not pending_section:
            title = line
            continue

        # Body of the current h4 → just count chars (for later "lead-heavy" detection)
        if current_h4_label:
            body_chars_in_current_h4 += len(line)

    if h4_entries:
        h4_entries[-1]["body_chars"] = body_chars_in_current_h4

    return {
        "file": docx_path.name,
        "title": title,
        "subtitle": subtitle,
        "lead": lead,
        "hongou_marker": hongou_marker,
        "is_excerpt": bool(hongou_marker),
        "h4_entries": h4_entries,
        "h4_count": len(h4_entries),
        "paragraph_count": len(paragraphs),
    }


def main(folder: Path = DEFAULT_DIR) -> None:
    if not folder.exists():
        print(f"[!] folder not found: {folder}", file=sys.stderr)
        sys.exit(2)

    docx_files = sorted(folder.glob("*.docx"))
    if not docx_files:
        print(f"[!] no .docx files in {folder}", file=sys.stderr)
        sys.exit(1)

    records = []
    for path in docx_files:
        rec = extract_record(path)
        records.append(rec)
        if "error" in rec:
            print(f"  [x] {path.name}: {rec['error']}")
        else:
            marker = " 抜粋" if rec["is_excerpt"] else ""
            title_preview = rec["title"][:50] if rec["title"] else "(no title)"
            print(
                f"  [o] {path.name[:45]:45s} h4={rec['h4_count']:2d}{marker} | {title_preview}"
            )

    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(
        json.dumps(records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\nwrote index: {INDEX_PATH} ({len(records)} records)")


if __name__ == "__main__":
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DIR
    main(folder)
