"""Parse President Online editorial manuscripts.

The parser accepts both real .docx files and plain text fixtures. It extracts
CMS-facing metadata from editorial labels, and normalizes the article body into
typed blocks that the conversion engine can render.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from cms_entry_assistant.models import BodyBlock, Manuscript

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W_NS}


def _q(name: str) -> str:
    return f"{{{W_NS}}}{name}"


def _run_text(run: ET.Element) -> str:
    parts: list[str] = []
    for child in run:
        if child.tag == _q("t"):
            parts.append(child.text or "")
        elif child.tag == _q("tab"):
            parts.append("\t")
        elif child.tag in {_q("br"), _q("cr")}:
            parts.append("\n")
    return "".join(parts)


def _paragraph_text_and_runs(p: ET.Element) -> tuple[str, list[tuple[int, int, str]]]:
    text_parts: list[str] = []
    runs: list[tuple[int, int, str]] = []
    pos = 0
    for run in p.iter(_q("r")):
        chunk = _run_text(run)
        if not chunk:
            continue
        start = pos
        text_parts.append(chunk)
        pos += len(chunk)
        rpr = run.find(_q("rPr"))
        if rpr is not None:
            if rpr.find(_q("b")) is not None:
                runs.append((start, pos, "bold"))
            if rpr.find(_q("i")) is not None:
                runs.append((start, pos, "italic"))
    return "".join(text_parts), runs


def _read_docx_paragraphs(path: Path) -> list[tuple[str, list[tuple[int, int, str]], int]]:
    with zipfile.ZipFile(path) as zf:
        with zf.open("word/document.xml") as fp:
            root = ET.parse(fp).getroot()

    body = root.find("w:body", NS)
    if body is None:
        return []

    paragraphs: list[tuple[str, list[tuple[int, int, str]], int]] = []
    line_no = 0
    for p in body.iter(_q("p")):
        line_no += 1
        text, runs = _paragraph_text_and_runs(p)
        text = text.strip()
        if text:
            paragraphs.append((text, runs, line_no))
    return paragraphs


META_LABELS: dict[str, str] = {
    "・タイトル": "title",
    "・メイン": "title",
    "・サブタイトル": "subtitle",
    "・サブタイ": "subtitle",
    "・キャッチ": "subtitle",
    "・ショルダー": "shoulder",
    "・肩": "shoulder",
    "・抜粋箇所": "excerpt",
    "・抜粋": "excerpt",
    "・概要": "lead",
}

LEAD_LABEL_RE = re.compile(r"^・リード[①②③④⑤⑥⑦⑧⑨⑩\d]*\s*$")
HEADING_RE = re.compile(r"^■\s*(.+?)\s*$")
PAGE_BREAK_RE = re.compile(
    r"^[（(]\s*(?:[0-9０-９一二三四五六七八九十]+ページ目|[pP]\.?\s*[0-9０-９]+)\s*[）)]\s*$"
)
KAKOMI_START_RE = re.compile(r"^[（(]\s*(?:カコミ|囲み)\s*[）)]\s*$")
KAKOMI_END_RE = re.compile(r"^[（(]\s*(?:ココマデ|ここまで)\s*[）)]\s*$")
EDITOR_NOTE_RE = re.compile(r"^[（(]\s*(?:見出し|トル|登録済み)\s*[）)]\s*$")
CREDIT_RE = re.compile(r"^[（(]\s*クレジット\s*[：:]\s*(.+?)\s*[）)]\s*$")
CAPTION_RE = re.compile(r"^[（(]\s*キャプション\s*[：:]\s*(.+?)\s*[）)]\s*$")
SERIAL_NAME_RE = re.compile(r"^連載名\s*[：:]")
ATTRIBUTION_RE = re.compile(r"^※")

_PROFILE_NAME_KANJI_FURIGANA = re.compile(r"^[一-龥々]{2,6}\s*[（(][ぁ-んァ-ヶー\s]+[）)]")
_PROFILE_NAME_KANA_BIRTH = re.compile(r"^[一-龥々]{2,8}\s*$")
_PROFILE_TITLE_HINTS = ("プロフィール", "著者略歴", "著者プロフィール", "略歴")


def parse_docx(path: Path | str) -> Manuscript:
    path = Path(path)
    paragraphs = _read_docx_paragraphs(path)
    return _parse_paragraphs(path.name, paragraphs)


def parse_text(path: Path | str) -> Manuscript:
    path = Path(path)
    raw = path.read_text(encoding="utf-8")
    paragraphs = [
        (line.strip(), [], i)
        for i, line in enumerate(raw.splitlines(), start=1)
        if line.strip()
    ]
    manuscript = _parse_paragraphs(path.name, paragraphs)
    manuscript.raw_text = raw.strip()
    return manuscript


def _parse_paragraphs(
    source_file: str, paragraphs: list[tuple[str, list[tuple[int, int, str]], int]]
) -> Manuscript:
    manuscript = Manuscript(source_file=source_file)
    manuscript.raw_text = "\n".join(text for text, _runs, _line in paragraphs)

    pending_meta: str | None = None
    body_started = False
    in_author_profile = False

    for text, runs, src_line in paragraphs:
        clean = text.strip()
        if not clean:
            continue

        label_key = _canonical_meta_label(clean)
        if label_key:
            pending_meta = label_key
            body_started = False if label_key != "lead" and not manuscript.body_blocks else body_started
            continue

        if SERIAL_NAME_RE.match(clean):
            series = SERIAL_NAME_RE.sub("", clean).strip()
            if series:
                manuscript.shoulder_candidates.append(series)
            continue

        if ATTRIBUTION_RE.match(clean) and not body_started:
            manuscript.caution_notes.append(clean)
            pending_meta = None
            continue

        if pending_meta and not _is_structural(clean):
            _append_meta(manuscript, pending_meta, clean)
            if pending_meta != "lead":
                pending_meta = None
            continue

        if any(hint in clean for hint in _PROFILE_TITLE_HINTS):
            in_author_profile = True
            manuscript.body_blocks.append(BodyBlock("heading_h5_candidate", clean, runs, src_line))
            body_started = True
            pending_meta = None
            continue

        if in_author_profile and clean:
            existing = [manuscript.author_profile] if manuscript.author_profile else []
            existing.append(clean)
            manuscript.author_profile = "\n".join(existing)
            manuscript.body_blocks.append(BodyBlock("paragraph", clean, runs, src_line))
            body_started = True
            continue

        m = HEADING_RE.match(clean)
        if m:
            manuscript.body_blocks.append(
                BodyBlock("heading_h4", m.group(1).strip(), runs, src_line)
            )
            body_started = True
            pending_meta = None
            continue

        if PAGE_BREAK_RE.match(clean):
            # 元マーカー文字列を text に保持 (例: "（2ページ目）") して、下流の処理が
            # 「N ページ目」の N を読めるようにする。
            manuscript.body_blocks.append(BodyBlock("page_break", clean, [], src_line))
            body_started = True
            pending_meta = None
            continue

        if KAKOMI_START_RE.match(clean):
            manuscript.body_blocks.append(BodyBlock("kakomi_start", "", [], src_line))
            body_started = True
            pending_meta = None
            continue

        if KAKOMI_END_RE.match(clean):
            manuscript.body_blocks.append(BodyBlock("kakomi_end", "", [], src_line))
            body_started = True
            pending_meta = None
            continue

        m_credit = CREDIT_RE.match(clean)
        if m_credit:
            manuscript.body_blocks.append(
                BodyBlock("credit", m_credit.group(1).strip(), runs, src_line)
            )
            body_started = True
            pending_meta = None
            continue

        m_caption = CAPTION_RE.match(clean)
        if m_caption:
            manuscript.body_blocks.append(
                BodyBlock("caption", m_caption.group(1).strip(), runs, src_line)
            )
            body_started = True
            pending_meta = None
            continue

        if EDITOR_NOTE_RE.match(clean):
            manuscript.body_blocks.append(BodyBlock("editor_note", clean, runs, src_line))
            body_started = True
            pending_meta = None
            continue

        if not body_started and not manuscript.title_candidates:
            manuscript.title_candidates.append(clean)
            continue

        if not body_started and not manuscript.lead_candidates and not manuscript.body_blocks:
            manuscript.lead_candidates.append(clean)
            continue

        manuscript.body_blocks.append(BodyBlock("paragraph", clean, runs, src_line))
        body_started = True
        pending_meta = None

    _detect_author_profile(manuscript)
    return manuscript


def _canonical_meta_label(text: str) -> str | None:
    label = re.sub(r"\s+", "", text)
    if label in META_LABELS:
        return META_LABELS[label]
    if LEAD_LABEL_RE.match(label):
        return "lead"
    return None


def _append_meta(manuscript: Manuscript, label: str, text: str) -> None:
    if label == "title":
        manuscript.title_candidates.append(text)
    elif label == "subtitle":
        manuscript.subtitle_candidates.append(text)
    elif label == "shoulder":
        manuscript.shoulder_candidates.append(text)
    elif label == "lead":
        manuscript.lead_candidates.append(text)
    elif label == "excerpt":
        manuscript.excerpt_range = text


def _is_structural(text: str) -> bool:
    return bool(
        HEADING_RE.match(text)
        or PAGE_BREAK_RE.match(text)
        or KAKOMI_START_RE.match(text)
        or KAKOMI_END_RE.match(text)
        or CREDIT_RE.match(text)
        or CAPTION_RE.match(text)
    )


def _detect_author_profile(manuscript: Manuscript) -> None:
    """Best-effort extraction of author profile text from body blocks."""

    if manuscript.author_profile:
        return

    blocks = manuscript.body_blocks
    for i, block in enumerate(blocks):
        if block.kind not in {"heading_h4", "heading_h5_candidate", "paragraph"}:
            continue
        if not any(hint in block.text for hint in _PROFILE_TITLE_HINTS):
            continue

        profile_lines: list[str] = []
        for follower in blocks[i + 1 :]:
            if follower.kind in {"heading_h4", "page_break"} and profile_lines:
                break
            if follower.kind == "paragraph" and follower.text:
                profile_lines.append(follower.text)
        if profile_lines:
            manuscript.author_profile = "\n".join(profile_lines)
            return

    # Some manuscripts only put the profile as a name line near the end.
    tail = [b for b in blocks[-8:] if b.kind == "paragraph" and b.text]
    for i, block in enumerate(tail):
        if _PROFILE_NAME_KANJI_FURIGANA.match(block.text):
            profile = "\n".join(b.text for b in tail[i:])
            manuscript.author_profile = profile
            manuscript.parse_warnings.append("著者プロフィールらしき末尾テキストを検出しました。")
            return
        if _PROFILE_NAME_KANA_BIRTH.match(block.text) and i + 1 < len(tail):
            next_text = tail[i + 1].text
            if any(token in next_text for token in ("大学", "教授", "作家", "医師", "評論家")):
                profile = "\n".join(b.text for b in tail[i:])
                manuscript.author_profile = profile
                manuscript.parse_warnings.append("著者プロフィールらしき末尾テキストを検出しました。")
                return
