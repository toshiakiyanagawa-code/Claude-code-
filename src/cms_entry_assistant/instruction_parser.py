"""Parse President Online submission instructions.

The incoming instruction text is semi-structured and edited by humans, so the
parser favors conservative extraction with warnings over strict validation.
"""

from __future__ import annotations

import re
import unicodedata

from cms_entry_assistant.models import (
    PhotoInstruction,
    SubmissionInstruction,
    YahooRelatedImage,
)

FIELD_LABELS: dict[str, str] = {
    "タイトル": "title",
    "ショルダー": "shoulder",
    "記事アドレス": "article_url",
    "記事URL": "article_url",
    "URL": "article_url",
    "テストページ希望日時": "test_page_due",
    "公開予定日時": "publish_schedule",
    "写真指定": "photo_spec",
    "写真": "photo_spec",
    "図表・図版": "chart_count",
    "図表・図版数": "chart_count",
    "図表": "chart_count",
    "カテゴリ": "category",
    "外部配信": "external_distribution",
    "備考": "remarks",
    "著者プロフィール": "author_profile_instruction",
    "著者略歴": "author_profile_instruction",
    "書籍情報": "book_info",
    "出典書籍": "book_info",
    "本稿": "book_info",
    "連載名": "series_name",
    "スポット": "_marker_spot",
    "書籍抜粋": "_marker_book",
    "連載": "_marker_series",
}

MARKER_PAYLOAD_FIELDS: dict[str, str] = {
    "_marker_book": "book_info",
    "_marker_series": "series_name",
}

HEADER_RE = re.compile(r"【([^】]+)】")
SERIES_BEST_RE = re.compile(r"2026年[0-9０-９]+月BEST|20[2-9][0-9]年[0-9０-９]+月BEST")
NUM_RE = re.compile(r"[0-9０-９]+")
RECIPIENT_RE = re.compile(r"(?<![\w一-龥ぁ-んァ-ヶ])([^\s【】、。,.]{2,10}さん)")

ARTICLE_TYPE_RE: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"【\s*書籍抜粋\s*】"), "book_excerpt"),
    (re.compile(r"【\s*連載\s*】.*?BEST"), "best_republish"),
    (re.compile(r"2026年.*BEST|20[2-9][0-9]年.*BEST"), "best_republish"),
    (re.compile(r"【\s*連載\s*】"), "series"),
    (re.compile(r"【\s*スポット\s*】"), "spot"),
]

PAGE_LABEL_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^(?:カンバン|看板|看板写真)$"), "hero"),
    (re.compile(r"^[Pp]([0-9０-９]+)$"), "p{n}"),
    (re.compile(r"^([0-9０-９]+)[Pp]$"), "p{n}"),
    (re.compile(r"^([0-9０-９]+)\s*ページ目$"), "p{n}"),
    (re.compile(r"^P([0-9０-９]+)目$"), "p{n}"),
    (re.compile(r"^([0-9０-９]+)P目$"), "p{n}"),
]

ISTOCK_ID_RE = re.compile(r"(?:iStock|istock|istockphoto)\s*[-‐_]?\s*([0-9０-９]{6,12})")
PLAIN_ID_RE = re.compile(r"\b([0-9０-９]{6,12})\b")
WIKIMEDIA_URL_RE = re.compile(
    r"https?://(?:commons\.wikimedia\.org|en\.wikipedia\.org)/[^\s)）]+"
)
URL_RE = re.compile(r"https?://[^\s)）]+")
KYODO_RE = re.compile(r"共同(?:通信社?|フォト)?\s*([0-9０-９]{6,15})?")
JIJI_RE = re.compile(r"時事(?:通信社?|フォト)?\s*([0-9０-９]{6,15})?")
ANCHOR_RE = re.compile(r"[（(]([^（()）]+?)の下(?:にお願い(?:します|いたします))?[）)]")
ITEM_IMAGE_DEFAULT = "写真はイメージ"

YAHOO_PATTERNS: list[re.Pattern[str]] = [
    re.compile(
        r"※?\s*(?P<ref>[^\sを]+?)\s*を\s*Yahoo!?(?:関連)?(?:写真|画像)?\s*で\s*"
        r"[「『](?P<title>[^」』]+)[」』]\s*で?\s*設定",
        re.MULTILINE,
    ),
    re.compile(
        r"(?P<ref>[A-Za-zＡ-Ｚａ-ｚ][0-9０-９]+|カンバン|看板)\s*画像(?:について)?、?"
        r"\s*リンクタイトル\s*=\s*[「『](?P<title>[^」』]+)[」』]",
        re.MULTILINE,
    ),
    re.compile(
        r"ヤフー関連(?:設定|画像)?\s*[「『]?"
        r"(?P<title>【(?:写真|図表|画像)を(?:見る|みる)】[^」』\n]+)[」』]?"
    ),
]

EXPANSION_RE = re.compile(
    r"横展開\s*(?:＝|=)?\s*(?:なし|無し|✕|×)?\s*"
    r"(woman|p-?books|family|family〇|woman〇|p-?books〇)?",
    re.IGNORECASE,
)
CLOSEUP_RE = re.compile(r"(?:Close[- ]?Up|CU|クローズアップ).*?[「『]([^」』]+)[」』]")
_BOOK_ATTRIB_RE = re.compile(
    r"※本稿は[、,]?\s*(?P<author>[^『]+)『(?P<title>[^』]+)』\s*[（(](?P<publisher>[^）)]+)[）)]"
)


def _normalize_label(label: str) -> str:
    label = unicodedata.normalize("NFKC", label)
    return re.sub(r"\s+", "", label.strip())


def _normalize_for_match(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def _split_fields(text: str) -> tuple[dict[str, list[str]], str]:
    fields: dict[str, list[str]] = {}
    preamble: list[str] = []
    current: str | None = None
    seen_header = False

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        match = HEADER_RE.match(line.lstrip())
        if match:
            seen_header = True
            label = _normalize_label(match.group(1))
            key = FIELD_LABELS.get(label, label)
            rest = line.lstrip()[match.end() :].strip()
            if key.startswith("_marker_"):
                current = MARKER_PAYLOAD_FIELDS.get(key)
                if current:
                    fields.setdefault(current, [])
                    if rest:
                        fields[current].append(rest)
                continue
            current = key
            fields.setdefault(key, [])
            if rest:
                fields[key].append(rest)
            continue

        if not seen_header:
            preamble.append(line)
        elif current:
            fields.setdefault(current, []).append(line)

    return fields, "\n".join(preamble)


def _field_text(fields: dict[str, list[str]], key: str) -> str:
    return "\n".join(line.strip() for line in fields.get(key, []) if line.strip()).strip()


def _extract_recipient(text: str, preamble: str) -> str:
    candidates = [preamble, text[:400]]
    for chunk in candidates:
        m = RECIPIENT_RE.search(chunk)
        if m:
            return m.group(1).strip()
    return ""


def detect_article_type(text: str) -> str:
    normalized = _normalize_for_match(text)
    for pattern, article_type in ARTICLE_TYPE_RE:
        if pattern.search(normalized):
            return article_type
    return "unknown"


def _norm_digits(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def _normalize_page_label(label: str) -> str:
    label = _norm_digits(label).strip().strip("：:、")
    for pattern, template in PAGE_LABEL_PATTERNS:
        match = pattern.match(label)
        if not match:
            continue
        if "{n}" in template:
            return template.format(n=match.group(1))
        return template
    return label.lower()


def _classify_source(raw: str) -> str:
    normalized = _normalize_for_match(raw)
    if WIKIMEDIA_URL_RE.search(normalized) or "Wikimedia" in normalized:
        return "wikimedia"
    if ISTOCK_ID_RE.search(normalized) or "iStock" in raw or "istock" in raw.lower():
        return "istock"
    if KYODO_RE.search(normalized):
        return "kyodo"
    if JIJI_RE.search(normalized):
        return "jiji"
    if "提供" in raw:
        return "provided"
    if "Getty" in raw or "AFP" in raw or "ロイター" in raw:
        return "press"
    return "unknown"


def _extract_asset_id(raw: str, source_kind: str) -> str:
    normalized = _norm_digits(raw)
    if source_kind == "istock":
        m = ISTOCK_ID_RE.search(normalized) or PLAIN_ID_RE.search(normalized)
    elif source_kind == "kyodo":
        m = KYODO_RE.search(normalized) or PLAIN_ID_RE.search(normalized)
    elif source_kind == "jiji":
        m = JIJI_RE.search(normalized) or PLAIN_ID_RE.search(normalized)
    else:
        m = PLAIN_ID_RE.search(normalized)
    return m.group(1) if m and m.group(1) else ""


def _extract_anchor(raw: str) -> str:
    m = ANCHOR_RE.search(raw)
    return m.group(1).strip() if m else ""


def _extract_yahoo_related(text: str) -> list[YahooRelatedImage]:
    out: list[YahooRelatedImage] = []
    for pattern in YAHOO_PATTERNS:
        for match in pattern.finditer(text):
            group = match.groupdict()
            ref = (group.get("ref") or "カンバン").strip()
            title = (group.get("title") or "").strip()
            if not title:
                continue
            target_page = _normalize_page_label(ref)
            target_asset_id = ""
            if re.fullmatch(r"[0-9０-９]{6,12}", _norm_digits(ref)):
                target_asset_id = _norm_digits(ref)
            item = YahooRelatedImage(
                target_page=target_page,
                target_asset_id=target_asset_id,
                link_title=title,
            )
            if item not in out:
                out.append(item)
    return out


def _extract_expansion_flags(text: str) -> list[str]:
    flags: list[str] = []
    for match in EXPANSION_RE.finditer(text):
        raw = (match.group(1) or "").lower().replace("〇", "")
        if raw:
            normalized = raw.replace("-", "")
            if normalized == "pbooks":
                normalized = "p-books"
            if normalized not in flags:
                flags.append(normalized)
        elif "なし" not in match.group(0) and "無し" not in match.group(0):
            flags.append("要確認")
    return flags


def _extract_closeup(text: str) -> str:
    m = CLOSEUP_RE.search(text)
    return m.group(1).strip() if m else ""


def _split_photo_line(line: str) -> tuple[str, str] | None:
    line = line.strip()
    if not line:
        return None
    # Prefer explicit separators because labels can contain parentheses.
    m = re.match(r"^(?P<label>カンバン|看板|[Pp]?[0-9０-９]+[Pp]?|[0-9０-９]+ページ目)\s*[：:]\s*(?P<body>.+)$", line)
    if m:
        return m.group("label"), m.group("body").strip()
    m = re.match(r"^(?P<label>カンバン|看板|[Pp][0-9０-９]+|[0-9０-９]+[Pp]?)\s+(?P<body>.+)$", line)
    if m:
        return m.group("label"), m.group("body").strip()
    return None


def _parse_photo_spec(raw: str) -> list[PhotoInstruction]:
    instructions: list[PhotoInstruction] = []
    for line in raw.splitlines():
        parsed = _split_photo_line(line)
        if not parsed:
            continue
        page_label, body = parsed
        source_kind = _classify_source(body)
        asset_url = ""
        if source_kind == "wikimedia":
            m_url = WIKIMEDIA_URL_RE.search(body)
            asset_url = m_url.group(0) if m_url else ""
        elif source_kind in {"provided", "press", "unknown"}:
            m_url = URL_RE.search(body)
            asset_url = m_url.group(0) if m_url else ""
        instructions.append(
            PhotoInstruction(
                page_label=page_label,
                page_normalized=_normalize_page_label(page_label),
                source_kind=source_kind,
                asset_id=_extract_asset_id(body, source_kind),
                asset_url=asset_url,
                anchor_text=_extract_anchor(body),
                raw_label=body.strip(),
                is_image_default_caption=ITEM_IMAGE_DEFAULT in body,
            )
        )
    return instructions


def parse_instruction(text: str) -> SubmissionInstruction:
    article_type = detect_article_type(text)
    fields, preamble = _split_fields(text)

    photo_spec_raw = _field_text(fields, "photo_spec")
    remarks = _field_text(fields, "remarks")
    all_free_text = "\n".join([photo_spec_raw, remarks])

    submission = SubmissionInstruction(
        recipient=_extract_recipient(text, preamble),
        article_type=article_type,
        series_name=_field_text(fields, "series_name"),
        book_info=_field_text(fields, "book_info"),
        title=_field_text(fields, "title"),
        shoulder=_field_text(fields, "shoulder"),
        article_url=_field_text(fields, "article_url"),
        test_page_due=_field_text(fields, "test_page_due"),
        publish_schedule=_field_text(fields, "publish_schedule"),
        photo_instructions=_parse_photo_spec(photo_spec_raw),
        photo_spec_raw=photo_spec_raw,
        chart_count=_field_text(fields, "chart_count"),
        category=_field_text(fields, "category"),
        external_distribution=_field_text(fields, "external_distribution"),
        yahoo_related_images=_extract_yahoo_related(all_free_text),
        remarks=remarks,
        expansion_flags=_extract_expansion_flags(remarks),
        closeup_tag=_extract_closeup(remarks),
        author_profile_instruction=_field_text(fields, "author_profile_instruction"),
    )
    return submission


def derive_from_manuscript(manuscript) -> SubmissionInstruction:
    """Create a minimal instruction object when no explicit instruction exists."""

    submission = SubmissionInstruction()
    submission.title = next(iter(getattr(manuscript, "title_candidates", []) or []), "")
    submission.shoulder = next(iter(getattr(manuscript, "shoulder_candidates", []) or []), "")
    for note in getattr(manuscript, "caution_notes", []) or []:
        m = _BOOK_ATTRIB_RE.search(note)
        if m:
            submission.book_info = f"{m.group('author').strip()}『{m.group('title')}』（{m.group('publisher')}）"
            submission.article_type = "book_excerpt"
            break
    if getattr(manuscript, "author_profile", ""):
        submission.author_profile_instruction = manuscript.author_profile
    try:
        from cms_entry_assistant.category_predictor import predict_category

        submission.category = predict_category(manuscript).category
    except Exception:
        pass
    return submission
