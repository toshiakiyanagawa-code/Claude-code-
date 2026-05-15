"""Format parsed instructions back into a canonical 入稿指示 form."""

from __future__ import annotations

import re

from cms_entry_assistant.models import PhotoInstruction, SubmissionInstruction

URL_RE = re.compile(r"https?://\S+")
DRIVE_URL_RE = re.compile(r"https?://(?:drive|docs)\.google\.com/\S+")
BOOK_INFO_RE = re.compile(r"(?P<author>[^『]+)?『(?P<title>[^』]+)』[（(](?P<publisher>[^）)]+)[）)]")
_DIGIT_TRANS = str.maketrans("０１２３４５６７８９", "0123456789")


def _clean(text: str) -> str:
    return (text or "").strip()


def _norm_digits(text: str) -> str:
    return (text or "").translate(_DIGIT_TRANS)


def _strip_urls(text: str) -> tuple[str, list[str]]:
    urls = URL_RE.findall(text or "")
    body = text or ""
    for url in urls:
        body = body.replace(url, "")
    return body.strip(), urls


def _normalize_book_info(text: str) -> str:
    text = _clean(text)
    if not text:
        return ""
    m = BOOK_INFO_RE.search(text)
    if not m:
        return text
    author = _clean(m.group("author") or "")
    title = _clean(m.group("title"))
    publisher = _clean(m.group("publisher"))
    prefix = f"{author}" if author else ""
    return f"{prefix}『{title}』（{publisher}）"


def _article_line(submission: SubmissionInstruction) -> str:
    if submission.article_type == "book_excerpt":
        book = _normalize_book_info(submission.book_info)
        return f"【書籍抜粋】{book}" if book else "【書籍抜粋】"
    if submission.article_type == "series":
        series = _clean(submission.series_name)
        return f"【連載】{series}" if series else "【連載】"
    if submission.article_type == "best_republish":
        return "【連載】BEST再掲"
    if submission.article_type == "spot":
        return "【スポット】"
    return "【記事種別】要確認"


def _photo_line(photo: PhotoInstruction) -> str:
    page = _clean(photo.page_label) or _clean(photo.page_normalized) or "P?"
    raw = _clean(photo.raw_label)
    if raw:
        return f"{page}：{raw}"
    if photo.source_kind == "istock" and photo.asset_id:
        return f"{page}：iStock {photo.asset_id}"
    if photo.source_kind == "wikimedia" and photo.asset_url:
        return f"{page}：Wikimedia {photo.asset_url}"
    if photo.source_kind == "kyodo":
        return f"{page}：共同通信 {photo.asset_id}".rstrip()
    if photo.source_kind == "jiji":
        return f"{page}：時事通信 {photo.asset_id}".rstrip()
    if photo.asset_url:
        return f"{page}：{photo.asset_url}"
    return f"{page}：(未指定)"


def _fallback_photo_lines(raw: str) -> list[str]:
    lines: list[str] = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if line:
            lines.append(line)
    return lines


def _photo_lines(submission: SubmissionInstruction) -> list[str]:
    if submission.photo_instructions:
        return [_photo_line(photo) for photo in submission.photo_instructions]
    return _fallback_photo_lines(submission.photo_spec_raw)


def _chart_count_and_urls(submission: SubmissionInstruction) -> tuple[str, list[str]]:
    body, urls = _strip_urls(submission.chart_count)
    drive_urls = DRIVE_URL_RE.findall("\n".join([submission.chart_count, submission.remarks]))
    for url in drive_urls:
        if url not in urls:
            urls.append(url)
    return _norm_digits(body), urls


def _expansion_text(submission: SubmissionInstruction) -> str:
    if submission.expansion_flags:
        return "・横展開：" + " / ".join(submission.expansion_flags)
    if re.search(r"横展開", submission.remarks or ""):
        return "・横展開：要確認"
    return ""


def _yahoo_image_lines(submission: SubmissionInstruction) -> list[str]:
    out: list[str] = []
    for item in submission.yahoo_related_images:
        target = item.target_page
        if item.target_asset_id:
            target = f"{target} / asset_id={item.target_asset_id}"
        title = _clean(item.link_title)
        out.append(f"・Yahoo!転載用画像：{target}、クレジットは「{title}」でお願いします")
    return out


def _remark_kind(line: str) -> str:
    line = line.strip()
    if not line:
        return "blank"
    if re.search(r"横展開|Close[- ]?Up|クローズアップ|Yahoo", line, re.IGNORECASE):
        return "derived"
    return "raw"


def _raw_remark_sections(remarks: str) -> list[str]:
    out: list[str] = []
    for line in (remarks or "").splitlines():
        line = line.strip()
        if _remark_kind(line) == "raw":
            out.append(line)
    return out


def _has_new_author(submission: SubmissionInstruction, author_profile: str) -> bool:
    return bool(_clean(author_profile) or _clean(submission.author_profile_instruction))


def format_canonical(
    submission: SubmissionInstruction, recipient: str = "", author_profile: str = ""
) -> str:
    """Return a clean, pasteable 入稿指示 text block."""

    lines: list[str] = []
    to_name = _clean(recipient) or _clean(submission.recipient)
    if to_name:
        lines.append(to_name)
    lines.append(_article_line(submission))

    if _clean(submission.title):
        lines.append(f"【タイトル】{_clean(submission.title)}")
    if _clean(submission.shoulder):
        lines.append(f"【ショルダー】{_clean(submission.shoulder)}")
    if _clean(submission.article_url):
        lines.append(f"【記事アドレス】{_clean(submission.article_url)}")
    if _clean(submission.test_page_due):
        lines.append(f"【テストページ希望日時】{_clean(submission.test_page_due)}")
    if _clean(submission.publish_schedule):
        lines.append(f"【　　公開予定日時　　】{_clean(submission.publish_schedule)}")

    photo_lines = _photo_lines(submission)
    if photo_lines:
        lines.append("【写真指定】")
        lines.extend(photo_lines)

    if _clean(submission.chart_count):
        chart_body, urls = _chart_count_and_urls(submission)
        lines.append(f"【図表・図版（0点含め点数を明記）】{chart_body}")
        lines.extend(urls)

    if _clean(submission.category):
        lines.append(f"【カテゴリ】　{_clean(submission.category)}")
    if _clean(submission.external_distribution):
        lines.append(f"【外部配信】{_clean(submission.external_distribution)}")

    remark_lines: list[str] = []
    remark_lines.extend(_raw_remark_sections(submission.remarks))
    expansion = _expansion_text(submission)
    if expansion:
        remark_lines.append(expansion)
    remark_lines.extend(_yahoo_image_lines(submission))
    if _clean(submission.closeup_tag):
        remark_lines.append(f"・Close-Up：{_clean(submission.closeup_tag)}")
    if _has_new_author(submission, author_profile):
        remark_lines.append("・新規著者〇")
        profile = _clean(author_profile) or _clean(submission.author_profile_instruction)
        if profile:
            remark_lines.append(profile)

    if remark_lines:
        lines.append("【備考】")
        lines.extend(remark_lines)

    return "\n".join(lines).rstrip()


def _amazon_search_url(title: str) -> str:
    import urllib.parse

    query = re.sub(r"\s+", " ", title or "").strip()
    return "https://www.amazon.co.jp/s?" + urllib.parse.urlencode({"k": query[:80]})
