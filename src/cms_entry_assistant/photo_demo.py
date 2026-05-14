"""Standalone photo-selection demo (FastAPI single file).

Run:
    uv run uvicorn cms_entry_assistant.photo_demo:app --port 8767 --reload

Flow:
    1. Editor pastes one query per line, optionally prefixed with a slot label.
         カンバン | 国会議事堂
         P2(見出し: 不正経理) | 経理 書類 男性
       The separator is ``|`` (with optional surrounding spaces) so the label
       itself may contain colons (the canonical 入稿指示書 uses
       ``P2(見出し: …)`` form). Lines without a separator are auto-numbered
       (カンバン → P2 → P3 → …). Duplicate labels are auto-uniquified with
       ``#2``, ``#3`` suffixes.
    2. Submit triggers iStock crawl (Playwright) per slot, with cache + rate limit.
    3. The page renders up to N (default 5) thumbnail cards per slot with checkboxes.
    4. Editor ticks the ones to use; canonical panel shows the 【写真指定】 lines
       ready to paste into the 入稿指示書.

This file is intentionally self-contained — no other web/ code depends on it.
"""

from __future__ import annotations

import html
import io
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse

from cms_entry_assistant import istock_crawler
from cms_entry_assistant.istock_crawler import IstockSearchHit
from cms_entry_assistant.photo_preferences import (
    PreferencesStore,
    UsageHistory,
    rank_hits,
)


# ---------------------------------------------------------------------------
# Slot parsing
# ---------------------------------------------------------------------------


@dataclass
class Slot:
    """One subheading slot the editor wants candidates for."""

    index: int  # 0-based position in the submitted list; used as the form key
    label: str  # canonical label (no #2 suffix). Used verbatim in 【写真指定】 output.
    display_label: str  # what to show in the slot header (may include " #2" for dups)
    query: str  # iStock search query (Japanese)


_SEPARATOR = "|"


def parse_slots(raw: str) -> list[Slot]:
    """Turn the textarea contents into a list of Slots.

    Each non-empty line becomes one slot. If the line contains the separator
    ``|``, everything before it is the label and everything after is the query.
    Otherwise the label is auto-assigned: カンバン → P2 → P3 → …

    Duplicate labels keep their original ``label`` (used in canonical output)
    but get a ``#2``, ``#3`` … suffix in ``display_label`` so the editor can
    tell two slots apart on screen. Form-key collisions are prevented by the
    per-slot ``index`` field, not by uniquifying the label.

    We deliberately do *not* split on ``:`` because canonical labels themselves
    contain colons (e.g. ``P2(見出し: 不正経理)``).
    """
    slots: list[Slot] = []
    auto_index = 0  # 0 -> カンバン, 1 -> P2, 2 -> P3, ...
    seen_labels: dict[str, int] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if _SEPARATOR in line:
            label, _, query = line.partition(_SEPARATOR)
            label = label.strip()
            query = query.strip()
        else:
            label = "カンバン" if auto_index == 0 else f"P{auto_index + 1}"
            query = line
            auto_index += 1
        if not (label and query):
            continue
        count = seen_labels.get(label, 0) + 1
        seen_labels[label] = count
        display_label = label if count == 1 else f"{label} #{count}"
        slots.append(
            Slot(index=len(slots), label=label, display_label=display_label, query=query)
        )
    return slots


# ---------------------------------------------------------------------------
# URL hygiene — iStock-supplied strings only get rendered as http(s) URLs.
# ---------------------------------------------------------------------------


def _safe_url(url: str | None) -> str:
    if not url:
        return ""
    try:
        scheme = urlparse(url).scheme.lower()
    except Exception:
        return ""
    return url if scheme in {"http", "https"} else ""


# ---------------------------------------------------------------------------
# Manuscript ingest (.docx → slot lines for the textarea)
# ---------------------------------------------------------------------------


_H4_PATTERN = re.compile(r"^[\s　]*■[\s　]*(.+?)[\s　]*$")
# Drop only conservative interrogative / 文末 expressions. We intentionally do
# NOT strip 「の理由」「の真相」 here because they are often meaningful nouns
# in their own right ("事件の真相", "退職の理由"); removing them would harm
# search relevance more than it helps.
_QUERY_TAIL_NOISE = re.compile(
    r"(はどうなる|になっている|になった|になる|とは何か|について|とは|か[?？]?)$"
)

MAX_DOCX_BYTES = 10 * 1024 * 1024  # 10MB — President Online 原稿はせいぜい数百KB


class DocxIngestError(Exception):
    """Raised by _paragraphs_from_docx with a user-facing reason string."""


def _paragraphs_from_docx(blob: bytes) -> list[str]:
    """Pull all non-empty paragraphs out of a .docx byte stream.

    Raises DocxIngestError with a Japanese message describing why the file
    could not be parsed (oversized, not a real docx, parse failed).
    """
    if len(blob) > MAX_DOCX_BYTES:
        raise DocxIngestError(
            f"ファイルが大きすぎます({len(blob) // 1024} KB)。"
            f"{MAX_DOCX_BYTES // 1024 // 1024} MB 以下の .docx をアップロードしてください。"
        )
    # Quick structural check: a real .docx is a zip containing word/document.xml.
    import zipfile

    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            names = set(zf.namelist())
    except zipfile.BadZipFile:
        raise DocxIngestError(
            "ファイルが .docx として読めません。拡張子だけ変更したファイルでないか確認してください。"
        )
    if "word/document.xml" not in names:
        raise DocxIngestError(
            ".docx の内部構造を確認できませんでした。Word で保存し直してから再試行してください。"
        )

    try:
        from docx import Document  # python-docx, lazy import
    except Exception:
        raise DocxIngestError("docx パーサーが利用できません(python-docx 未インストール)。")
    try:
        doc = Document(io.BytesIO(blob))
    except Exception:
        raise DocxIngestError("原稿の解析中にエラーが発生しました。ファイルが壊れていないか確認してください。")

    paragraphs: list[str] = []
    for p in doc.paragraphs:
        text = (p.text or "").strip()
        if text:
            paragraphs.append(text)
    return paragraphs


def _query_for_heading(heading: str) -> str:
    """Heuristic: strip trailing question/「どうなる」noise so the heading text
    works better as an iStock search query. Editor can edit before searching.
    """
    q = heading.strip().strip("「」『』\"'")
    q = _QUERY_TAIL_NOISE.sub("", q)
    return q.strip() or heading.strip()


def slots_from_manuscript(paragraphs: list[str]) -> list[tuple[str, str]]:
    """Convert manuscript paragraphs into ``(label, query)`` pairs.

    - The first non-empty paragraph is treated as the article title and used
      to seed the ``カンバン`` (hero) slot. The whole title (with simple noise
      stripped) becomes the query — the editor will normally rewrite this.
    - Every line starting with ``■`` is treated as an h4 heading and becomes
      one ``P{n+1}(見出し: …)`` slot. Numbering starts at 2 because カンバン is P1.
    """
    slot_pairs: list[tuple[str, str]] = []
    title: str | None = None
    h4_index = 0
    for raw in paragraphs:
        m = _H4_PATTERN.match(raw)
        if m:
            heading = m.group(1).strip()
            if not heading:
                continue
            h4_index += 1
            label = f"P{h4_index + 1}(見出し: {heading})"
            slot_pairs.append((label, _query_for_heading(heading)))
            continue
        if title is None:
            title = raw.strip()

    if title:
        slot_pairs.insert(0, ("カンバン", _query_for_heading(title)))
    return slot_pairs


def slot_lines_from_pairs(pairs: list[tuple[str, str]]) -> str:
    return "\n".join(f"{label} | {query}" for label, query in pairs)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------


app = FastAPI(title="iStock 写真選定デモ")


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return _page_shell(
        body=_form_html(default_text=_DEFAULT_QUERY_TEXT, results_html=""),
    )


@app.post("/from-manuscript", response_class=HTMLResponse)
async def from_manuscript(manuscript: UploadFile = File(...)) -> str:
    """Accept a .docx upload, extract headings, pre-fill the slot textarea."""
    name = (manuscript.filename or "").lower()
    if not name.endswith(".docx"):
        return _upload_error(
            f".docx ファイルのみ対応しています(受信: {html.escape(manuscript.filename or '(無名)')})。"
        )

    # Stream-read with a hard cap so a 100MB file doesn't blow up memory.
    blob = bytearray()
    while True:
        chunk = await manuscript.read(1024 * 1024)  # 1MB chunks
        if not chunk:
            break
        blob.extend(chunk)
        if len(blob) > MAX_DOCX_BYTES:
            return _upload_error(
                f"ファイルが大きすぎます。{MAX_DOCX_BYTES // 1024 // 1024} MB 以下の .docx をアップロードしてください。"
            )

    if not blob:
        return _upload_error("空のファイルです。.docx をもう一度アップロードしてください。")

    try:
        paragraphs = _paragraphs_from_docx(bytes(blob))
    except DocxIngestError as exc:
        return _upload_error(str(exc))

    if not paragraphs:
        return _upload_error(
            "本文が空のようです。Word で開いて段落が残っているか確認してください。"
        )

    pairs = slots_from_manuscript(paragraphs)
    if not pairs:
        return _upload_error(
            "見出しもタイトルも抽出できませんでした。"
            "見出し行は <code>■見出し本文</code> の形式で記載してください。"
        )

    h4_count = sum(1 for label, _q in pairs if label.startswith("P"))
    if h4_count == 0:
        notice = (
            '<p class="notice">タイトル(カンバン)のみ抽出されました。'
            "h4 見出しは <code>■見出し本文</code> の形式で記載するとスロット化されます。</p>"
        )
    else:
        notice = (
            f'<p class="notice">原稿から {len(pairs)} スロット(うち見出し {h4_count})を抽出しました。'
            "クエリは必要に応じて編集してから「候補を検索」を押してください。</p>"
        )

    prefilled = slot_lines_from_pairs(pairs)
    return _page_shell(
        body=_form_html(default_text=prefilled, results_html=notice),
    )


def _upload_error(msg_html: str) -> str:
    return _page_shell(
        body=_form_html(
            default_text=_DEFAULT_QUERY_TEXT,
            results_html=f'<p class="empty">{msg_html}</p>',
        )
    )


@app.post("/search", response_class=HTMLResponse)
def search(queries: str = Form(...), limit: int = Form(5)) -> str:
    slots = parse_slots(queries)
    if not slots:
        return _page_shell(
            body=_form_html(
                default_text=queries,
                results_html='<p class="empty">クエリが空です。1行に1スロットを入力してください。</p>',
            )
        )

    if not istock_crawler.is_available():
        return _page_shell(
            body=_form_html(
                default_text=queries,
                results_html=(
                    '<p class="empty">Playwright / Chromium が利用できません。'
                    "<code>uv run playwright install chromium --with-deps</code> を実行してください。</p>"
                ),
            )
        )

    per_slot_limit = max(1, min(int(limit), 8))
    prefs = PreferencesStore()
    history = UsageHistory()

    results_blocks: list[str] = []
    for slot in slots:
        hits = istock_crawler.crawl_search(slot.query, limit=8)
        if not hits:
            results_blocks.append(_no_hits_block(slot))
            continue
        ranked = rank_hits(
            hits,
            preferences=prefs,
            history=history,
            limit=per_slot_limit,
            query_context=slot.query,
        )
        results_blocks.append(_slot_block(slot, ranked))

    return _page_shell(
        body=_form_html(
            default_text=queries,
            results_html=(
                '<form method="post" action="/canonical" id="pickForm">'
                + "".join(results_blocks)
                + '<button type="submit" class="primary">選択を 【写真指定】 行に変換</button>'
                "</form>"
            ),
        )
    )


@app.post("/canonical", response_class=HTMLResponse)
async def canonical(request: Request) -> str:
    """Convert checked candidates into 【写真指定】 lines.

    The /search page emits, per slot, one hidden field ``slot__<i>`` whose
    *value* is the original label, plus one checkbox ``pick__<i>__<asset_id>``
    per candidate. We group by slot index so duplicate labels don't merge and
    HTML-unsafe labels never become part of the form key.
    """
    form = await request.form()

    labels_by_index: dict[int, str] = {}
    chosen: dict[int, list[str]] = {}
    for key, value in form.multi_items():
        if key.startswith("slot__"):
            try:
                idx = int(key.split("__", 1)[1])
            except ValueError:
                continue
            labels_by_index[idx] = str(value)
        elif key.startswith("pick__"):
            parts = key.split("__", 2)
            if len(parts) != 3:
                continue
            try:
                idx = int(parts[1])
            except ValueError:
                continue
            asset_id = parts[2].strip()
            if asset_id:
                chosen.setdefault(idx, []).append(asset_id)

    lines: list[str] = []
    for idx in sorted(labels_by_index):
        label = labels_by_index[idx]
        ids = chosen.get(idx, [])
        if not ids:
            lines.append(f"{label}: (未選択)")
        else:
            for aid in ids:
                lines.append(f"{label}: iStock {aid}")

    body = (
        '<a href="/" class="back">← 入力に戻る</a>'
        "<h2>【写真指定】 出力</h2>"
        '<pre class="canonical">'
        + html.escape("\n".join(lines) or "(何も選択されていません)")
        + "</pre>"
    )
    return _page_shell(body=body)


# ---------------------------------------------------------------------------
# HTML rendering helpers (intentionally inline — single-file demo)
# ---------------------------------------------------------------------------


def _slot_hidden(slot: Slot) -> str:
    # slot__<i> carries the canonical label (no #2 suffix) as its VALUE so
    # canonical output stays clean even when the editor adds duplicate slots.
    return f'<input type="hidden" name="slot__{slot.index}" value="{html.escape(slot.label)}">'


def _slot_block(slot: Slot, hits: list[IstockSearchHit]) -> str:
    cards = "".join(_card_html(slot, h) for h in hits)
    display_html = html.escape(slot.display_label)
    query_html = html.escape(slot.query)
    return (
        f'<section class="slot">'
        f'  <header class="slot-head">'
        f'    <span class="label">{display_html}</span>'
        f'    <span class="query">クエリ: {query_html}</span>'
        f"  </header>"
        f"  {_slot_hidden(slot)}"
        f'  <div class="grid">{cards}</div>'
        f"</section>"
    )


def _no_hits_block(slot: Slot) -> str:
    display_html = html.escape(slot.display_label)
    query_html = html.escape(slot.query)
    return (
        f'<section class="slot empty">'
        f'  <header class="slot-head">'
        f'    <span class="label">{display_html}</span>'
        f'    <span class="query">クエリ: {query_html}</span>'
        f"  </header>"
        f"  {_slot_hidden(slot)}"
        f'  <p class="empty">'
        f"    候補が見つかりませんでした。検索語を変えて再試行してください"
        f"    (より一般的な単語にする・別の言い回しを試す)。"
        f"    iStock側のアクセス制限に止められている可能性もあります。"
        f"  </p>"
        f"</section>"
    )


def _card_html(slot: Slot, hit: IstockSearchHit) -> str:
    aid = html.escape(hit.asset_id)
    thumb = html.escape(_safe_url(hit.thumbnail_url))
    detail = html.escape(_safe_url(hit.detail_url))
    alt = html.escape(hit.alt or hit.asset_id)
    photographer = html.escape(hit.photographer_username or "(撮影者不明)")
    open_link = (
        f'<a class="open" href="{detail}" target="_blank" rel="noreferrer noopener">iStockで開く</a>'
        if detail
        else ""
    )
    img_tag = (
        f'<img src="{thumb}" alt="{alt}" loading="lazy" referrerpolicy="no-referrer">'
        if thumb
        else '<div class="noimg">(サムネなし)</div>'
    )
    return (
        f'<label class="card">'
        f'  <input type="checkbox" name="pick__{slot.index}__{aid}" value="1">'
        f"  {img_tag}"
        f'  <div class="meta">'
        f'    <div class="aid">{aid}</div>'
        f'    <div class="photog">{photographer}</div>'
        f"    {open_link}"
        f"  </div>"
        f"</label>"
    )


def _form_html(*, default_text: str, results_html: str) -> str:
    return (
        '<form method="post" action="/from-manuscript" enctype="multipart/form-data" class="uform">'
        '  <label for="manuscript">原稿(.docx)を読み込んでスロットを自動抽出'
        f'    <span class="hint">対応形式: .docx / 上限 {MAX_DOCX_BYTES // 1024 // 1024} MB / 見出し記号: <code>■</code></span>'
        "  </label>"
        '  <div class="row">'
        '    <input id="manuscript" name="manuscript" type="file" accept=".docx">'
        '    <button class="ghost" type="submit">読み込み</button>'
        "  </div>"
        "</form>"
        '<form method="post" action="/search" class="qform">'
        '  <label for="queries">スロット(1行に1スロット。例: <code>カンバン | 国会議事堂</code>)</label>'
        f'  <textarea id="queries" name="queries" rows="8">{html.escape(default_text)}</textarea>'
        '  <div class="row">'
        '    <label>1スロットの最大候補数 <input name="limit" type="number" min="1" max="8" value="5"></label>'
        '    <button class="primary" type="submit">候補を検索</button>'
        "  </div>"
        "</form>"
        f'<div class="results">{results_html}</div>'
    )


_DEFAULT_QUERY_TEXT = "カンバン | 国会議事堂\nP2(見出し: 検察の動き) | 検察官"


_CSS = """
* { box-sizing: border-box; }
body {
  background: #11141a;
  color: #e6e8ec;
  font-family: -apple-system, BlinkMacSystemFont, "Hiragino Kaku Gothic ProN", "Yu Gothic", sans-serif;
  margin: 0;
  padding: 24px;
}
main { max-width: 1080px; margin: 0 auto; }
h1 { font-size: 18px; margin: 0 0 18px 0; letter-spacing: 0.02em; color: #cfd3d8; }
h2 { font-size: 15px; margin: 18px 0 8px 0; color: #cfd3d8; }
.qform, .uform { background: #181c24; border: 1px solid #2a2f38; border-radius: 8px; padding: 16px; }
.uform { margin-bottom: 12px; }
.qform label, .uform label { display: block; font-size: 13px; color: #aab0ba; margin-bottom: 6px; }
.uform .hint { display: block; font-size: 11px; color: #6b727d; margin-top: 2px; font-weight: normal; }
.uform input[type=file] {
  background: #0d1016; color: #e6e8ec; border: 1px solid #2a2f38;
  border-radius: 6px; padding: 6px; font-size: 13px;
}
button.ghost {
  background: transparent; color: #e6e8ec; border: 1px solid #2a2f38;
  padding: 7px 14px; border-radius: 6px; cursor: pointer; font-size: 13px;
}
button.ghost:hover { border-color: #d75a3b; }
.notice {
  background: #0d1016; border: 1px solid #2a2f38; border-radius: 6px;
  padding: 10px 12px; font-size: 13px; color: #cfd3d8; margin-top: 0;
}
textarea {
  width: 100%; background: #0d1016; color: #e6e8ec;
  border: 1px solid #2a2f38; border-radius: 6px; padding: 10px;
  font-family: ui-monospace, SFMono-Regular, monospace; font-size: 13px;
  resize: vertical;
}
.row { display: flex; gap: 12px; align-items: center; margin-top: 10px; }
.row input[type=number] { width: 70px; background: #0d1016; color: #e6e8ec; border: 1px solid #2a2f38; border-radius: 4px; padding: 4px 6px; }
button.primary {
  background: #d75a3b; color: white; border: 0; padding: 8px 16px;
  border-radius: 6px; cursor: pointer; font-size: 13px;
}
button.primary:hover { background: #e26a4d; }
.results { margin-top: 24px; }
.slot { background: #181c24; border: 1px solid #2a2f38; border-radius: 8px; padding: 14px; margin-bottom: 16px; }
.slot.empty { opacity: 0.7; }
.slot-head { display: flex; gap: 12px; align-items: baseline; margin-bottom: 10px; }
.slot-head .label { font-weight: 600; color: #f0b95b; }
.slot-head .query { color: #aab0ba; font-size: 12px; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 10px; }
.card {
  background: #0d1016; border: 1px solid #2a2f38; border-radius: 6px;
  padding: 8px; display: block; cursor: pointer; transition: border-color 0.15s;
}
.card:hover { border-color: #d75a3b; }
.card input[type=checkbox] { transform: scale(1.1); margin-right: 6px; }
.card img { width: 100%; height: 120px; object-fit: cover; border-radius: 4px; background: #0d1016; }
.card .noimg {
  width: 100%; height: 120px; display: flex; align-items: center; justify-content: center;
  background: #0d1016; color: #6b727d; font-size: 12px; border-radius: 4px;
}
.meta { font-size: 12px; margin-top: 6px; color: #aab0ba; }
.meta .aid { color: #e6e8ec; font-family: ui-monospace, SFMono-Regular, monospace; }
.meta .open { color: #6ab0ff; text-decoration: none; font-size: 11px; }
.meta .open:hover { text-decoration: underline; }
.empty { color: #aab0ba; font-size: 13px; }
pre.canonical {
  background: #0d1016; border: 1px solid #2a2f38; border-radius: 6px;
  padding: 14px; font-family: ui-monospace, SFMono-Regular, monospace;
  font-size: 13px; color: #e6e8ec; white-space: pre-wrap;
}
.back { color: #6ab0ff; text-decoration: none; font-size: 12px; }
code { background: #0d1016; padding: 1px 4px; border-radius: 3px; font-size: 12px; }
"""


def _page_shell(*, body: str) -> str:
    return (
        "<!doctype html><html lang='ja'><head>"
        "<meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>iStock 写真選定デモ</title>"
        f"<style>{_CSS}</style>"
        "</head><body><main>"
        f"<h1>iStock 写真選定デモ</h1>{body}"
        "</main></body></html>"
    )
