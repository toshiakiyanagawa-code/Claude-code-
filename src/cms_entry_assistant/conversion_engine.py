"""Convert parsed manuscript + instruction data into CMS draft artifacts."""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable

from cms_entry_assistant.istock_search import build_suggestion
from cms_entry_assistant.models import (
    BodyBlock,
    CMSDraft,
    ImagePlacement,
    IstockSearchSuggestion,
    Manuscript,
    PhotoInstruction,
    SubmissionInstruction,
    UnresolvedItem,
)
from cms_entry_assistant.photographer_lookup import PhotographerLookup


def _esc(text: str) -> str:
    return html.escape(text or "", quote=True)


def _apply_runs(text: str, runs: list[tuple[int, int, str]]) -> str:
    if not runs:
        return _esc(text)
    pieces: list[str] = []
    cursor = 0
    for start, end, style in sorted(runs, key=lambda run: (run[0], run[1])):
        start = max(0, min(start, len(text)))
        end = max(start, min(end, len(text)))
        if start > cursor:
            pieces.append(_esc(text[cursor:start]))
        body = _esc(text[start:end])
        if style == "bold":
            body = f"<strong>{body}</strong>"
        elif style == "italic":
            body = f"<em>{body}</em>"
        pieces.append(body)
        cursor = end
    if cursor < len(text):
        pieces.append(_esc(text[cursor:]))
    return "".join(pieces)


def _classify_credit_to_display_mode(credit_text: str) -> str:
    if not credit_text:
        return "source_div"
    if "キャプション" in credit_text:
        return "caption_inline"
    if "なし" in credit_text or "不要" in credit_text:
        return "none"
    return "source_div"


@dataclass
class ConversionConfig:
    """Runtime toggles for conversion."""

    allow_network: bool = False
    cms_amazon_aid: str = "presidentjp-22"
    page_break_html: str = '<div style="page-break-after: always"><span style="display: none;">&nbsp;</span></div>'


def _normalize_page_label(label: str) -> str:
    label = (label or "").strip()
    if not label:
        return ""
    if re.match(r"^(?:カンバン|看板|hero)$", label, re.I):
        return "hero"
    label = label.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    m = re.match(r"^[Pp]?(\d+)(?:[Pp]|ページ目)?$", label)
    if m:
        return f"p{m.group(1)}"
    return label.lower()


def _image_html_for_placement(placement: ImagePlacement, config: ConversionConfig | None = None) -> str:
    width = placement.width_px or 670
    css_class = placement.figure_orient or "figure-center"
    alt = _esc(placement.alt)
    source_comment = ""
    if not placement.asset_url:
        source_comment = f"\n  <!-- src for {placement.key}: upload to CMS and paste asset URL -->"
    src = _esc(placement.asset_url or "")
    caption = _esc(placement.caption)
    credit = _esc(placement.credit_text)

    source_html = ""
    if placement.display_mode == "source_div" and credit:
        source_html = f'\n<div class="source">{credit}</div>'
    elif placement.display_mode == "caption_inline" and credit:
        caption = f"{caption}<br>{credit}" if caption else credit

    caption_html = f'\n\n<div class="caption">{caption}</div>' if caption else ""
    return (
        f'<div class="image-area {css_class}" style="width:{width}px;">\n'
        f'<div class="image-area-inner"><img alt="{alt}" src="{src}" width="{width}" />'
        f"{source_comment}{source_html}\n"
        f"</div>{caption_html}\n"
        f"</div>"
    )


@dataclass
class _BodyBuildResult:
    body_html: str
    image_placements: list[ImagePlacement] = field(default_factory=list)
    unresolved_items: list[UnresolvedItem] = field(default_factory=list)


def _build_body(
    manuscript: Manuscript,
    submission: SubmissionInstruction,
    *,
    photographer: PhotographerLookup | None,
    config: ConversionConfig,
) -> _BodyBuildResult:
    lines: list[str] = []
    placements: list[ImagePlacement] = []
    unresolved: list[UnresolvedItem] = []
    instruction_index = _index_instructions(submission.photo_instructions)
    used_instruction_keys: set[str] = set()
    sequential = iter(submission.photo_instructions)
    open_kakomi = False
    page_number = 2
    current_heading = ""
    i = 0

    body_blocks = manuscript.body_blocks
    while i < len(body_blocks):
        block = body_blocks[i]
        if block.kind == "heading_h4":
            current_heading = block.text
            lines.append(f"<h4>{_esc(block.text)}</h4>")
        elif block.kind == "heading_h5_candidate":
            lines.append(f"<h5>{_esc(block.text)}</h5>")
        elif block.kind == "paragraph":
            lines.append(f"<p>{_apply_runs(block.text, block.runs)}</p>")
        elif block.kind == "kakomi_start":
            lines.append('<div class="kakomi2">')
            open_kakomi = True
        elif block.kind == "kakomi_end":
            if open_kakomi:
                lines.append("</div>")
                open_kakomi = False
        elif block.kind == "page_break":
            if open_kakomi:
                lines.append("</div>")
                open_kakomi = False
            lines.append(config.page_break_html)
            page_number += 1
        elif block.kind == "credit":
            caption = ""
            if i + 1 < len(body_blocks) and body_blocks[i + 1].kind == "caption":
                caption = body_blocks[i + 1].text
                i += 1
            page_label = f"P{page_number}"
            normalized = _normalize_page_label(page_label)
            instruction = instruction_index.get(normalized)
            if instruction:
                used_instruction_keys.add(normalized)
            else:
                instruction = _next_unused_instruction(sequential, used_instruction_keys)
                if instruction:
                    used_instruction_keys.add(instruction.page_normalized or instruction.page_label)
            placement = _placement_from_credit(
                key=f"img_{len(placements) + 1}",
                role="page_image",
                page_label=page_label,
                credit_block=block,
                caption=caption,
                heading=current_heading,
                instruction=instruction,
                photographer=photographer,
                config=config,
            )
            placements.append(placement)
            lines.append(placement.html_placeholder)
            unresolved.extend(_unresolved_for_placement(placement))
        elif block.kind == "caption":
            lines.append(f'<div class="caption">{_esc(block.text)}</div>')
        elif block.kind == "editor_note":
            unresolved.append(
                UnresolvedItem(
                    code="EDITOR_NOTE",
                    severity="info",
                    message="原稿内の編集メモを確認してください。",
                    source="docx_parser",
                    target_field="body_html",
                    raw_text=block.text,
                )
            )
        i += 1

    if open_kakomi:
        lines.append("</div>")

    rendered = "\n\n".join(lines)
    for placement in placements:
        rendered = rendered.replace(
            placement.html_placeholder, _image_html_for_placement(placement, config)
        )

    unused = [
        inst
        for inst in submission.photo_instructions
        if (inst.page_normalized or inst.page_label) not in used_instruction_keys
        and inst.page_normalized != "hero"
    ]
    for inst in unused:
        unresolved.append(
            UnresolvedItem(
                code="UNUSED_PHOTO_INSTRUCTION",
                severity="warn",
                message=f"指示書の写真指定 '{inst.page_label}' が本文中の写真マーカーに対応していません。",
                source="conversion_engine",
                target_field="image_placements",
                raw_text=inst.raw_label,
            )
        )

    if submission.photo_instructions and len(placements) != len(
        [p for p in submission.photo_instructions if p.page_normalized != "hero"]
    ):
        unresolved.append(
            UnresolvedItem(
                code="PHOTO_COUNT_MISMATCH",
                severity="warn",
                message=(
                    f"本文の写真マーカー数({len(placements)})と、本文向け写真指定数"
                    f"({len([p for p in submission.photo_instructions if p.page_normalized != 'hero'])})"
                    "が一致しません。"
                ),
                source="conversion_engine",
                target_field="image_placements",
                suggested_action="原稿のクレジット/キャプションと指示書の写真指定を照合してください。",
            )
        )

    return _BodyBuildResult(body_html=rendered, image_placements=placements, unresolved_items=unresolved)


def _next_unused_instruction(
    instructions: Iterable[PhotoInstruction], used: set[str]
) -> PhotoInstruction | None:
    for instruction in instructions:
        key = instruction.page_normalized or instruction.page_label
        if key == "hero" or key in used:
            continue
        return instruction
    return None


def _format_credit_text(placement: ImagePlacement) -> str:
    if placement.source_type == "istock":
        name = placement.photographer_username or f"（iStockで選択→ID:{placement.asset_id or '未指定'}）"
        return f"写真＝iStock.com／{name}"
    if placement.source_type == "wikimedia":
        return f"写真＝Wikimedia Commons（{placement.asset_url or placement.source_label_raw}）"
    if placement.source_type == "kyodo":
        suffix = f"（{placement.asset_id}）" if placement.asset_id else ""
        return f"写真＝共同通信{suffix}"
    if placement.source_type == "jiji":
        suffix = f"（{placement.asset_id}）" if placement.asset_id else ""
        return f"写真＝時事通信{suffix}"
    if placement.source_type == "provided":
        return placement.source_label_raw or "写真＝提供"
    return placement.source_label_raw or "写真クレジット要確認"


def _derive_alt(caption: str, heading: str) -> str:
    text = caption or heading or "記事イメージ"
    text = re.sub(r"写真[＝=].*$", "", text).strip()
    if len(text) > 60:
        text = text[:57] + "..."
    return text


def _placement_from_credit(
    *,
    key: str,
    role: str,
    page_label: str,
    credit_block: BodyBlock,
    caption: str,
    heading: str,
    instruction: PhotoInstruction | None,
    photographer: PhotographerLookup | None,
    config: ConversionConfig,
) -> ImagePlacement:
    source_label = credit_block.text
    source_type = instruction.source_kind if instruction else _source_from_credit(source_label)
    asset_id = instruction.asset_id if instruction else _asset_id_from_credit(source_label)
    asset_url = instruction.asset_url if instruction else ""
    photographer_username = ""
    if source_type == "istock" and asset_id and photographer:
        entry = photographer.get(asset_id)
        if not entry and config.allow_network:
            entry = photographer.fetch_asset_meta(asset_id)
        if entry:
            photographer_username = entry.photographer_username

    placement = ImagePlacement(
        key=key,
        role=role,
        page_label=instruction.page_label if instruction else page_label,
        source_type=source_type,
        source_label_raw=source_label,
        asset_id=asset_id,
        asset_url=asset_url,
        photographer_username=photographer_username,
        caption=caption,
        alt=_derive_alt(caption, heading),
        anchor_text=instruction.anchor_text if instruction else "",
        display_mode=_classify_credit_to_display_mode(source_label),
        html_placeholder=f"<!-- IMG:{key} -->",
    )
    placement.credit_text = _format_credit_text(placement)
    return placement


def _source_from_credit(text: str) -> str:
    if "iStock" in text or "istock" in text.lower():
        return "istock"
    if "Wikimedia" in text or "wikipedia" in text.lower():
        return "wikimedia"
    if "共同" in text:
        return "kyodo"
    if "時事" in text:
        return "jiji"
    if "提供" in text:
        return "provided"
    return "unknown"


def _asset_id_from_credit(text: str) -> str:
    text = (text or "").translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    m = re.search(r"([0-9]{6,12})", text)
    return m.group(1) if m else ""


_AMAZON_URL_ID_RE = re.compile(
    r"amazon\.[^\s/）)]+/[^\s）)]*?(?:/dp/|/gp/product/|/ASIN/|obidos/ASIN/)"
    r"([A-Za-z0-9][-A-Za-z0-9]{8,20})",
    re.I,
)
_ASIN_LABEL_RE = re.compile(r"\bASIN\s*[：:]?\s*([A-Za-z0-9]{10})\b", re.I)
_ISBN_LABEL_RE = re.compile(
    r"\bISBN(?:-1[03])?\s*[：:]?\s*([0-9０-９Xx][-0-9０-９Xx\s]{8,24})",
    re.I,
)


def _normalize_amazon_id(raw: str) -> str:
    token = unicodedata.normalize("NFKC", raw or "")
    token = re.sub(r"[^0-9A-Za-zXx]", "", token).upper()
    if token == "ISBNREQUIRED":
        return ""
    if re.fullmatch(r"[A-Z0-9]{10}", token):
        return token
    if re.fullmatch(r"97[89][0-9]{10}", token):
        return token
    return ""


def _extract_amazon_id(texts: Iterable[str]) -> str:
    for text in texts:
        normalized = unicodedata.normalize("NFKC", text or "")
        for pattern in (_AMAZON_URL_ID_RE, _ASIN_LABEL_RE, _ISBN_LABEL_RE):
            match = pattern.search(normalized)
            if not match:
                continue
            amazon_id = _normalize_amazon_id(match.group(1))
            if amazon_id:
                return amazon_id
    return ""


def _suggest_page_label_for_index(index: int, page_breaks_before: int, heading_count: int) -> str:
    return "カンバン" if index == 0 else f"P{page_breaks_before + heading_count + 1}"


def _index_instructions(instructions: list[PhotoInstruction]) -> dict[str, PhotoInstruction]:
    out: dict[str, PhotoInstruction] = {}
    for instruction in instructions:
        key = instruction.page_normalized or _normalize_page_label(instruction.page_label)
        if key:
            out[key] = instruction
    return out


def convert(
    manuscript: Manuscript,
    submission: SubmissionInstruction,
    *,
    photographer: PhotographerLookup | None = None,
    config: ConversionConfig | None = None,
) -> CMSDraft:
    config = config or ConversionConfig()
    photographer = photographer or PhotographerLookup()

    draft = CMSDraft()
    draft.selected_title = submission.title or _first(manuscript.title_candidates)
    draft.selected_subtitle = _first(manuscript.subtitle_candidates)
    draft.selected_shoulder = submission.shoulder or _first(manuscript.shoulder_candidates)
    draft.selected_lead = "\n".join(manuscript.lead_candidates).strip()
    amazon_id = _extract_amazon_id(
        [submission.book_info, submission.remarks, *manuscript.caution_notes]
    )
    draft.book_attribution_html = _render_book_attribution(
        manuscript.caution_notes,
        config.cms_amazon_aid,
        amazon_id=amazon_id,
    )
    if "ISBN_REQUIRED" in draft.book_attribution_html:
        draft.unresolved_items.append(
            UnresolvedItem(
                code="ISBN_REQUIRED",
                severity="high",
                message="出典書籍のAmazon ISBN/ASINを埋めてください。",
                source="conversion_engine",
                target_field="book_attribution_html",
                suggested_action="Amazonで書籍を検索し、ISBN_REQUIREDをASINまたはISBNに置換してください。",
                raw_text="\n".join(manuscript.caution_notes),
            )
        )

    body = _build_body(manuscript, submission, photographer=photographer, config=config)
    draft.body_html = body.body_html
    draft.body_html_rendered = body.body_html
    draft.image_placements = body.image_placements
    draft.unresolved_items.extend(body.unresolved_items)

    draft.photo_suggestions = _build_photo_suggestions(manuscript, draft.selected_title)
    draft.yahoo_related_images = submission.yahoo_related_images
    draft.category = submission.category or _predict_category(manuscript)
    draft.external_distribution = submission.external_distribution
    draft.expansion_flags = submission.expansion_flags
    draft.closeup_tag = submission.closeup_tag
    draft.author_profile_confirmation = (
        submission.author_profile_instruction or manuscript.author_profile
    )
    draft.warnings.extend(manuscript.parse_warnings)
    draft.warnings.extend(submission.parse_warnings)
    draft.meta_fields = {
        "article_type": submission.article_type,
        "series_name": submission.series_name,
        "book_info": submission.book_info,
        "amazon_id": amazon_id,
        "article_url": submission.article_url,
        "test_page_due": submission.test_page_due,
        "publish_schedule": submission.publish_schedule,
        "chart_count": submission.chart_count,
    }

    if draft.external_distribution and draft.external_distribution not in {"なし", "無し", "無"}:
        draft.unresolved_items.append(
            UnresolvedItem(
                code="EXTERNAL_DISTRIBUTION_CONFIRM",
                severity="high",
                message=f"外部配信設定: {draft.external_distribution} — CMSで手動確認してください。",
                source="conversion_engine",
                target_field="external_distribution",
            )
        )

    return draft


def _first(values: list[str]) -> str:
    return next((v for v in values if v), "")


def _predict_category(manuscript: Manuscript) -> str:
    try:
        from cms_entry_assistant.category_predictor import predict_category

        return predict_category(manuscript).category
    except Exception:
        return ""


def _render_book_attribution(
    caution_notes: list[str], amazon_aid: str, *, amazon_id: str = ""
) -> str:
    for note in caution_notes:
        m = re.search(r"※本稿は[、,]?\s*(?P<author>[^『]+)『(?P<title>[^』]+)』\s*[（(](?P<publisher>[^）)]+)[）)]", note)
        if not m:
            continue
        author = m.group("author").strip()
        title = m.group("title").strip()
        publisher = m.group("publisher").strip()
        link_id = amazon_id or "ISBN_REQUIRED"
        link = f"https://www.amazon.co.jp/exec/obidos/ASIN/{link_id}/{amazon_aid}"
        return (
            '<p class="caution">※本稿は、'
            f"{_esc(author)}『<a href=\"{_esc(link)}\" target=\"_blank\">{_esc(title)}</a>』"
            f"（{_esc(publisher)}）の一部を再編集したものです。</p>"
        )
    if caution_notes:
        return "\n".join(f'<p class="caution">{_esc(note)}</p>' for note in caution_notes)
    return ""


_PAGE_BREAK_N_RE = re.compile(r"[（(]\s*([0-9０-９一二三四五六七八九十]+)\s*ページ目")
_KANJI_DIGIT_MAP = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_FW_DIGIT_TRANS = str.maketrans("０１２３４５６７８９", "0123456789")


def _kanji_to_int(s: str) -> int | None:
    """Convert a small kanji-number string (1〜99) to int.

    Examples: 一→1, 十→10, 十一→11, 二十→20, 二十三→23, 九十九→99.
    Returns None on unsupported / >=100.
    """
    if not s:
        return None
    if s == "十":
        return 10
    if len(s) == 1:
        return _KANJI_DIGIT_MAP.get(s)
    # 十X (X=1..9)
    if s.startswith("十"):
        rest = s[1:]
        if len(rest) == 1 and rest in _KANJI_DIGIT_MAP:
            return 10 + _KANJI_DIGIT_MAP[rest]
        return None
    # X十 or X十Y (X=1..9)
    if "十" in s:
        head, _, tail = s.partition("十")
        if len(head) == 1 and head in _KANJI_DIGIT_MAP:
            base = _KANJI_DIGIT_MAP[head] * 10
            if not tail:
                return base
            if len(tail) == 1 and tail in _KANJI_DIGIT_MAP:
                return base + _KANJI_DIGIT_MAP[tail]
    return None


def _parse_page_break_number(marker_text: str) -> int | None:
    """Extract N from page-break marker text like "（2ページ目）" / "(3ページ目)" / "（十ページ目）".

    Returns None if the marker text doesn't contain a parseable N.
    Supports half-width / full-width digits and kanji 1〜99.
    """
    if not marker_text:
        return None
    m = _PAGE_BREAK_N_RE.search(marker_text)
    if not m:
        return None
    raw = m.group(1)
    digits = raw.translate(_FW_DIGIT_TRANS)
    if digits.isdigit():
        return int(digits)
    return _kanji_to_int(raw)


def _build_photo_suggestions(
    manuscript: Manuscript, article_title: str
) -> list[IstockSearchSuggestion]:
    """h4 と (Nページ目) マーカーから写真スロットを生成する。

    body_blocks を順に走査し、`page_break` ブロックでページ番号を 1 増やす。
    各 h4 にはそのとき入っているページ番号を割り当てる。

    President Online 編集部の慣例:
      - 1 ページ目 = カンバン (hero) + 最初の h4 群 (多くは 1〜2 個)
      - (2ページ目) マーカー後 → 2 ページ目, 通常 h4 が 2 個
      - (3ページ目) マーカー後 → 3 ページ目, …
    """
    suggestions: list[IstockSearchSuggestion] = []
    lead_text = " ".join(manuscript.lead_candidates)
    # hero は記事代表 → lead_text を context に使う (article_title は使わない)
    hero = build_suggestion(
        "hero",
        "カンバン(冒頭)",
        h4_text=article_title or lead_text,
        surrounding_paragraphs=[],
        lead_text=lead_text,
        article_title=article_title,
    )
    hero.page_number = 1
    suggestions.append(hero)

    # body_blocks を 1 周し、(heading, surrounding_paragraphs, page_number) を収集
    headings: list[tuple[str, list[str], int]] = []
    current_heading = ""
    current_paragraphs: list[str] = []
    current_page = 1  # カンバンと同じページ (1) から開始
    current_heading_page = current_page
    for block in manuscript.body_blocks:
        if block.kind == "page_break":
            # ページ区切り → マーカー文字列に N が含まれていればそれを優先 (尊重)。
            # 含まれない / 解釈不能なら従来通り +1。後退禁止: 既に N より大きい
            # page にいる場合は +1 にフォールバック (時系列の単調増加を守る)。
            if current_heading:
                headings.append((current_heading, list(current_paragraphs), current_heading_page))
                current_heading = ""
                current_paragraphs = []
            n = _parse_page_break_number(block.text)
            if n is not None and n > current_page:
                current_page = n
            else:
                current_page += 1
        elif block.kind == "heading_h4":
            if current_heading:
                headings.append((current_heading, list(current_paragraphs), current_heading_page))
            current_heading = block.text
            current_heading_page = current_page
            current_paragraphs = []
        elif current_heading and block.kind == "paragraph":
            current_paragraphs.append(block.text)
    if current_heading:
        headings.append((current_heading, list(current_paragraphs), current_heading_page))

    # h4 ごとに: h4_text + 直近 2 段落 + lead_text を context に (v8 復活、title 非使用)
    for idx, (heading, paragraphs, page_num) in enumerate(headings, start=1):
        suggestion = build_suggestion(
            f"h4_{idx}",
            f"■{heading}",
            h4_text=heading,
            surrounding_paragraphs=paragraphs[:2],
            lead_text=lead_text,
            article_title=article_title,  # press soft-flag 検出のみに使う
        )
        suggestion.page_number = page_num
        suggestions.append(suggestion)
    return suggestions


def _derive_placements_from_submission_only(
    instructions: list[PhotoInstruction],
    *,
    photographer: PhotographerLookup | None,
    config: ConversionConfig,
) -> list[ImagePlacement]:
    placements: list[ImagePlacement] = []
    for i, instruction in enumerate(instructions, start=1):
        placement = ImagePlacement(
            key=f"img_{i}",
            role="instruction_only",
            page_label=instruction.page_label,
            source_type=instruction.source_kind,
            source_label_raw=instruction.raw_label,
            asset_id=instruction.asset_id,
            asset_url=instruction.asset_url,
            anchor_text=instruction.anchor_text,
            html_placeholder=f"<!-- IMG:img_{i} -->",
        )
        if instruction.source_kind == "istock" and instruction.asset_id and photographer:
            entry = photographer.get(instruction.asset_id)
            if not entry and config.allow_network:
                entry = photographer.fetch_asset_meta(instruction.asset_id)
            if entry:
                placement.photographer_username = entry.photographer_username
        placement.credit_text = _format_credit_text(placement)
        placement.alt = _derive_alt("", instruction.page_label)
        placements.append(placement)
    return placements


def _unresolved_for_placement(placement: ImagePlacement) -> list[UnresolvedItem]:
    out: list[UnresolvedItem] = []
    if placement.source_type == "istock" and not placement.asset_id:
        out.append(
            UnresolvedItem(
                code="MISSING_ISTOCK_ID",
                severity="high",
                message=f"{placement.page_label} のiStock IDを確認してください。",
                source="conversion_engine",
                target_field="image_placements",
                raw_text=placement.source_label_raw,
            )
        )
    if placement.source_type == "unknown":
        out.append(
            UnresolvedItem(
                code="UNKNOWN_PHOTO_SOURCE",
                severity="warn",
                message=f"{placement.page_label} の写真クレジット種別を判定できません。",
                source="conversion_engine",
                target_field="image_placements",
                raw_text=placement.source_label_raw,
            )
        )
    return out
