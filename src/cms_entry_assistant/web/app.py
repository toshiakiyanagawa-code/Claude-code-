"""Merged CMS入稿アシスト Web UI.

A single FastAPI app that combines:
  - Manuscript (.docx) upload
  - Auto conversion (Manuscript → SubmissionInstruction → CMSDraft)
  - 4 tabs: 入稿指示書 / CMS HTML / 写真選定 / 確認事項
  - Photo selection with checkboxes → updates 入稿指示書 in real time
  - Downloads for canonical text and rendered HTML

Run via the CLI:
    uv run cms-assist serve --port 8767

This is intentionally an in-memory single-user demo (no SQLite, no auth).
The B0 shared-DB design (see docs/photo_precision_plan.md §3) is future work.
"""

from __future__ import annotations

import html
import io
import json
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

from cms_entry_assistant.conversion_engine import ConversionConfig, convert
from cms_entry_assistant.docx_parser import parse_docx, parse_text
from cms_entry_assistant.instruction_canonical import format_canonical
from cms_entry_assistant.instruction_parser import derive_from_manuscript
from cms_entry_assistant.istock_crawler import IstockSearchHit, crawl_search, is_available
from cms_entry_assistant.models import (
    CMSDraft,
    IstockSearchSuggestion,
    Manuscript,
    PhotoInstruction,
    SubmissionInstruction,
)
from cms_entry_assistant.photo_preferences import PreferencesStore, UsageHistory, rank_hits
from cms_entry_assistant.renderer import render_full_html, render_unresolved_report


# ---------------------------------------------------------------------------
# Case storage (in-memory; B0 SQLite layer is future work)
# ---------------------------------------------------------------------------


@dataclass
class CaseState:
    case_id: str
    created_at: str
    manuscript: Manuscript
    submission: SubmissionInstruction
    draft: CMSDraft
    selections: dict[str, str] = field(default_factory=dict)  # slot_key -> asset_id
    photo_candidates: dict[str, list[IstockSearchHit]] = field(default_factory=dict)


_cases: dict[str, CaseState] = {}


def _new_case_id() -> str:
    return secrets.token_hex(6)


def _get_case(case_id: str) -> CaseState:
    case = _cases.get(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="case not found")
    return case


# ---------------------------------------------------------------------------
# Manuscript ingest helpers
# ---------------------------------------------------------------------------


def _parse_manuscript_bytes(filename: str, blob: bytes) -> Manuscript:
    name = (filename or "").lower()
    suffix = ".docx" if name.endswith(".docx") else (".md" if name.endswith(".md") else ".txt")
    tmp_path = Path("/tmp") / f"_upload_{secrets.token_hex(4)}{suffix}"
    tmp_path.write_bytes(blob)
    try:
        if suffix == ".docx":
            return parse_docx(tmp_path)
        return parse_text(tmp_path)
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Photo candidate fetching
# ---------------------------------------------------------------------------


def _fetch_candidates(
    suggestions: list[IstockSearchSuggestion], *, hits_per_slot: int = 5
) -> dict[str, list[IstockSearchHit]]:
    """Return {slot_key: ranked_hits}. Uses the istock_crawler cache.

    Per-slot try/except: 1 つのスロットで iStock 取得が失敗しても、案件全体は失敗させない。
    """
    out: dict[str, list[IstockSearchHit]] = {}
    if not is_available():
        for s in suggestions:
            out[s.slot_key] = []
        return out
    prefs = PreferencesStore()
    history = UsageHistory()
    for s in suggestions:
        if not s.query_ja:
            out[s.slot_key] = []
            continue
        try:
            hits = _crawl_search_safe(s.query_ja, limit=8)
            out[s.slot_key] = rank_hits(
                hits, preferences=prefs, history=history, limit=hits_per_slot
            )
        except Exception:
            # crawl が失敗しても他のスロットは独立して処理 (空候補で続行)
            out[s.slot_key] = []
    return out


def _crawl_search_safe(query: str, *, limit: int = 8) -> list[IstockSearchHit]:
    """Call crawl_search() in a way that works both inside and outside an event loop.

    FastAPI のリクエスト処理は内部で event loop が走っており、その中で `crawl_search()` を
    直接呼ぶと、`crawl_search` が内部で使う `asyncio.run()` が
    "cannot be called from a running event loop" で例外を投げる。

    対策: 現在 event loop が走っているかを判定し、走っていれば別スレッドで `crawl_search`
    を実行する (スレッド側には event loop が無いので `asyncio.run` が動く)。
    """
    import asyncio
    import concurrent.futures

    try:
        asyncio.get_running_loop()
        in_loop = True
    except RuntimeError:
        in_loop = False

    if not in_loop:
        return crawl_search(query, limit=limit)

    # event loop 内 → 別スレッドで実行
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(crawl_search, query, limit=limit)
        return future.result()


# ---------------------------------------------------------------------------
# Apply selections → rebuild canonical
# ---------------------------------------------------------------------------


def _rebuild_canonical(case: CaseState) -> str:
    """Inject the selected asset_ids into submission.photo_instructions and re-render.

    Behavior per slot:
      1. 編集者が候補をチェック → そのスロットは iStock {asset_id} で上書き
      2. 未選択 (selections に無い) で、原 submission に同じ page_label の PhotoInstruction が
         あった場合 → 原指定をそのまま残す (編集者が手入力した指示を壊さない)
      3. それも無ければ "(未指定)" として空の iStock 行を追加
    """
    submission = case.submission
    # 原 submission の photo_instructions を page_label でインデックス
    original_by_label: dict[str, PhotoInstruction] = {}
    for original in case.submission.photo_instructions:
        original_by_label.setdefault(original.page_label, original)

    photo_instructions: list[PhotoInstruction] = []
    suggestion_labels: set[str] = set()
    for suggestion in case.draft.photo_suggestions:
        slot_key = suggestion.slot_key
        label = suggestion.slot_label
        suggestion_labels.add(label)
        asset_id = case.selections.get(slot_key, "").strip()
        if asset_id:
            photo_instructions.append(
                PhotoInstruction(
                    page_label=label,
                    source_kind="istock",
                    asset_id=asset_id,
                    raw_label="",
                )
            )
        elif label in original_by_label:
            # 元指示を保持 (編集者の手入力指示は壊さない)
            photo_instructions.append(original_by_label[label])
        else:
            # 未指定として空欄で残す (canonical 上は "(未指定)" 表示)
            photo_instructions.append(
                PhotoInstruction(page_label=label, source_kind="istock", asset_id="")
            )

    # スロットでカバーされない既存指示は末尾に保持
    for original in case.submission.photo_instructions:
        if original.page_label not in suggestion_labels:
            photo_instructions.append(original)

    submission_copy = _replace_photo_instructions(submission, photo_instructions)
    return format_canonical(
        submission_copy,
        recipient=submission_copy.recipient,
        author_profile=submission_copy.author_profile_instruction,
    )


def _replace_photo_instructions(
    submission: SubmissionInstruction, new_photos: list[PhotoInstruction]
) -> SubmissionInstruction:
    """Return a shallow copy with photo_instructions replaced."""
    from dataclasses import replace

    return replace(submission, photo_instructions=new_photos)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------


app = FastAPI(title="CMS 入稿アシスト")


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return _page_shell(_upload_form_html())


MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20MB


@app.post("/case", response_class=HTMLResponse)
async def create_case(manuscript: UploadFile = File(...)) -> Any:
    # Stream-read with hard cap so a huge upload doesn't blow up memory.
    blob = bytearray()
    while True:
        chunk = await manuscript.read(1024 * 1024)  # 1MB chunks
        if not chunk:
            break
        blob.extend(chunk)
        if len(blob) > MAX_UPLOAD_BYTES:
            return _page_shell(_upload_form_html(
                error=f"ファイルが大きすぎます。{MAX_UPLOAD_BYTES // 1024 // 1024} MB 以下でアップロードしてください。"
            ))
    blob = bytes(blob)
    if not blob:
        return _page_shell(_upload_form_html(error="ファイルが空です。"))
    try:
        parsed = _parse_manuscript_bytes(manuscript.filename or "", blob)
    except Exception as exc:
        return _page_shell(_upload_form_html(error=f"原稿の解析に失敗: {exc}"))
    submission = derive_from_manuscript(parsed)
    draft = convert(parsed, submission, config=ConversionConfig(allow_network=False))
    candidates = _fetch_candidates(draft.photo_suggestions)
    case = CaseState(
        case_id=_new_case_id(),
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        manuscript=parsed,
        submission=submission,
        draft=draft,
        photo_candidates=candidates,
    )
    _cases[case.case_id] = case
    return RedirectResponse(url=f"/case/{case.case_id}", status_code=303)


@app.get("/case/{case_id}", response_class=HTMLResponse)
def view_case(case_id: str) -> str:
    case = _get_case(case_id)
    return _page_shell(_case_html(case))


@app.post("/case/{case_id}/pick")
async def pick_photo(case_id: str, request: Request) -> JSONResponse:
    """Set or clear the chosen asset_id for one slot. Returns updated canonical."""
    case = _get_case(case_id)
    payload = await request.json()
    slot_key = (payload.get("slot_key") or "").strip()
    asset_id = (payload.get("asset_id") or "").strip()
    valid_slots = {s.slot_key for s in case.draft.photo_suggestions}
    if slot_key not in valid_slots:
        raise HTTPException(status_code=400, detail="unknown slot")
    if asset_id:
        # 候補一覧に存在する asset_id のみ許可 (任意の文字列を canonical に流し込ませない)
        allowed_ids = {h.asset_id for h in case.photo_candidates.get(slot_key, [])}
        if asset_id not in allowed_ids:
            raise HTTPException(status_code=400, detail="asset_id is not in this slot's candidates")
        case.selections[slot_key] = asset_id
    else:
        case.selections.pop(slot_key, None)
    canonical = _rebuild_canonical(case)
    return JSONResponse({
        "slot_key": slot_key,
        "asset_id": asset_id,
        "selections": case.selections,
        "canonical": canonical,
    })


@app.get("/case/{case_id}/canonical.txt", response_class=PlainTextResponse)
def download_canonical(case_id: str) -> str:
    case = _get_case(case_id)
    return _rebuild_canonical(case)


@app.get("/case/{case_id}/cms.html")
def download_cms_html(case_id: str):
    """Download the CMS HTML as a file (never rendered inline by browser).

    Content-Disposition: attachment にして同一オリジンでの直接スクリプト実行を防ぐ。
    """
    from fastapi.responses import Response

    case = _get_case(case_id)
    body = render_full_html(case.draft)
    return Response(
        content=body,
        media_type="text/html; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="cms-{case_id}.html"',
            "X-Content-Type-Options": "nosniff",
        },
    )


# ---------------------------------------------------------------------------
# HTML rendering (intentionally inline for a single-file UI)
# ---------------------------------------------------------------------------


def _esc(s: Any) -> str:
    return html.escape(str(s or ""))


def _page_shell(body_html: str) -> str:
    return (
        "<!doctype html><html lang='ja'><head>"
        "<meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>CMS 入稿アシスト</title>"
        f"<style>{_CSS}</style>"
        "</head><body><main>"
        f"<h1>CMS 入稿アシスト</h1>{body_html}"
        "</main></body></html>"
    )


def _upload_form_html(*, error: str = "") -> str:
    err = f'<p class="error">{_esc(error)}</p>' if error else ""
    # ファイルを選択した瞬間に submit する。JS が無効なブラウザでも動くよう、
    # noscript で「案件を作成」ボタンを併設する (progressive enhancement)。
    return (
        '<form id="upload-form" method="post" action="/case" enctype="multipart/form-data" class="uform">'
        '  <label for="manuscript">原稿(.docx)をアップロードすると、案件が自動で作成されます</label>'
        '  <span class="hint">.docx / .txt / .md 対応 / 上限 20MB</span>'
        '  <div class="row">'
        '    <input id="manuscript" name="manuscript" type="file" '
        '           accept=".docx,.txt,.md" required>'
        '    <span id="upload-status" class="pick-status"></span>'
        '  </div>'
        '  <noscript>'
        '    <div class="row"><button class="primary" type="submit">案件を作成</button></div>'
        '  </noscript>'
        f"  {err}"
        "</form>"
        "<script>"
        "(function(){"
        "  var form = document.getElementById('upload-form');"
        "  var input = document.getElementById('manuscript');"
        "  var status = document.getElementById('upload-status');"
        "  if (!form || !input) return;"
        "  var submitted = false;"
        "  var MAX_BYTES = 20 * 1024 * 1024;"  # 20MB (サーバー側 MAX_UPLOAD_BYTES と一致)
        "  input.addEventListener('change', function(){"
        "    if (submitted) return;"
        "    if (!input.files || !input.files.length) return;"
        "    var f = input.files[0];"
        # クライアント側サイズチェック: 上限超過時は送信せずエラー表示し、再選択可能に戻す。
        "    if (f.size > MAX_BYTES){"
        "      if (status){"
        "        status.textContent = 'ファイルが大きすぎます (' + Math.round(f.size/1024/1024) + 'MB)。20MB 以下を選択してください。';"
        "        status.className = 'pick-status error';"
        "      }"
        "      input.value = '';"
        "      return;"
        "    }"
        "    submitted = true;"
        "    if (status){"
        "      status.textContent = '案件を作成中... (' + f.name + ')';"
        "      status.className = 'pick-status pending';"
        "    }"
        # NOTE: input.disabled = true を form.submit() の前に行うと、disabled な
        # input は multipart 送信ペイロードから除外され、FastAPI 側で
        # 'Field required' エラーになる (回帰した経緯あり)。disabled は使わず、
        # JS の submitted フラグで二重送信を防ぎ、視覚的な非活性化は CSS
        # (pointer-events + opacity) で表現する。
        "    input.style.pointerEvents = 'none';"
        "    input.style.opacity = '0.6';"
        "    form.submit();"
        "  });"
        "})();"
        "</script>"
    )


def _case_html(case: CaseState) -> str:
    canonical_text = _rebuild_canonical(case)
    cms_html_text = render_full_html(case.draft)
    unresolved_md = render_unresolved_report(case.draft)
    photo_html = _photos_tab_html(case)
    confirm_html = _confirm_tab_html(case, unresolved_md)
    return (
        f'<div class="case-header">'
        f'  <h2>案件: {_esc(case.draft.selected_title or case.manuscript.source_file)}</h2>'
        f'  <div class="case-meta">'
        f'    案件ID <code>{_esc(case.case_id)}</code> ・ 開始 {_esc(case.created_at)}'
        f"  </div>"
        f'  <div class="downloads">'
        f'    <a href="/case/{case.case_id}/canonical.txt" download>入稿指示書 (.txt)</a> | '
        f'    <a href="/case/{case.case_id}/cms.html" download>CMS HTML</a>'
        f"  </div>"
        f"</div>"
        '<div class="tabs">'
        '  <button data-tab="canonical" class="tab active">入稿指示書</button>'
        '  <button data-tab="photos"    class="tab">写真選定</button>'
        '  <button data-tab="cmshtml"   class="tab">CMS HTML</button>'
        '  <button data-tab="confirm"   class="tab">確認事項</button>'
        "</div>"
        f'<section id="tab-canonical" class="tab-pane active">'
        f'  <h3>入稿指示書(自動生成)</h3>'
        f'  <pre id="canonical-text" class="canonical">{_esc(canonical_text)}</pre>'
        f"</section>"
        f'<section id="tab-photos" class="tab-pane">'
        f'  <h3>写真選定</h3>'
        f'  <p class="hint">スロットごとに1枚チェックすると、入稿指示書の【写真指定】欄に自動で反映されます。'
        f'  <span id="pick-status" class="pick-status"></span></p>'
        f'  {photo_html}'
        f"</section>"
        f'<section id="tab-cmshtml" class="tab-pane">'
        f'  <h3>CMS HTML ソース</h3>'
        f'  <p class="hint">CMS に貼り付ける HTML ソースです。'
        f'    <button id="copy-cms-html" class="ghost" type="button">クリップボードにコピー</button>'
        f'    <span id="copy-status" class="pick-status"></span></p>'
        f'  <pre id="cms-html-text" class="canonical">{_esc(cms_html_text)}</pre>'
        f"</section>"
        f'<section id="tab-confirm" class="tab-pane">'
        f'  <h3>確認事項</h3>{confirm_html}</section>'
        f'<script>{_JS.replace("__CASE_ID__", case.case_id)}</script>'
    )


_H4_SLOT_RE = re.compile(r"^h4_(\d+)$")


def _page_label(page_num: int) -> str:
    """Return a human-friendly badge label for a CMS page number."""
    if page_num == 1:
        return "1ページ目 (カンバン)"
    if page_num > 1:
        return f"{page_num}ページ目 (P{page_num})"
    return "その他"


def _page_for_slot(slot_key: str, page_number: int = 0) -> tuple[int, str]:
    """Return (page_number, page_label) for a slot_key.

    優先順位:
      1. ``page_number`` が 1 以上 → そのページに割り当てる (原稿の (Nページ目) マーカー由来)
      2. (1) が無く ``slot_key == "hero"`` → ページ 1 (カンバン)
      3. (1) が無く ``slot_key == "h4_N"`` → 旧 fallback (h4 1 つ = 1 ページ仮定)
      4. それ以外 → 「その他」 (末尾配置)

    `page_number` は v2 以降の `IstockSearchSuggestion.page_number` を渡す前提。
    後方互換として、page_number=0 のときは旧推定にフォールバック。
    """
    if page_number and page_number >= 1:
        return page_number, _page_label(page_number)
    if slot_key == "hero":
        return 1, _page_label(1)
    m = _H4_SLOT_RE.match(slot_key or "")
    if m:
        n = int(m.group(1))
        if n < 1:
            return 0, "その他"
        return n + 1, _page_label(n + 1)
    return 0, "その他"


def _photos_tab_html(case: CaseState) -> str:
    if not case.draft.photo_suggestions:
        return '<p class="empty">写真スロットが検出されませんでした。</p>'

    # Group suggestions by CMS page number so the editor can see page boundaries.
    by_page: dict[int, list[IstockSearchSuggestion]] = {}
    page_labels: dict[int, str] = {}
    for suggestion in case.draft.photo_suggestions:
        page_num, page_label = _page_for_slot(
            suggestion.slot_key, getattr(suggestion, "page_number", 0)
        )
        by_page.setdefault(page_num, []).append(suggestion)
        page_labels[page_num] = page_label

    # ページ番号順にソート。「その他」(page_num=0) は末尾に置く
    sort_key = lambda p: (9999 if p == 0 else p)
    ordered_pages = sorted(by_page.keys(), key=sort_key)

    parts: list[str] = []
    for page_num in ordered_pages:
        suggestions = by_page[page_num]
        page_label = page_labels.get(page_num, "その他")
        is_other = page_num == 0
        slot_parts: list[str] = []
        for suggestion in suggestions:
            slot_key = suggestion.slot_key
            slot_label = suggestion.slot_label
            query_text = suggestion.query_ja or suggestion.query_en
            hits = case.photo_candidates.get(slot_key, [])
            selected = case.selections.get(slot_key, "")
            cards = _candidate_cards_html(slot_key, hits, selected)
            rationale = _esc(suggestion.rationale) if suggestion.rationale else ""
            note_html = ""
            if getattr(suggestion, "note", ""):
                note_html = (
                    '<div class="press-hint">'
                    '<span class="press-hint-title">📰 報道写真の参考</span>'
                    f'<span class="press-hint-body">{_esc(suggestion.note)}</span>'
                    "</div>"
                )
            slot_parts.append(
                f'<section class="slot" data-slot-key="{_esc(slot_key)}">'
                f'  <header class="slot-head">'
                f'    <span class="label">{_esc(slot_label)}</span>'
                f'    <span class="type">[{_esc(suggestion.type_code)}]</span>'
                f'    <span class="query">クエリ: {_esc(query_text)}</span>'
                f"  </header>"
                f'  <div class="rationale">{rationale}</div>'
                f"{note_html}"
                f'  <div class="grid">{cards}</div>'
                f"</section>"
            )
        group_cls = "page-group" + (" page-group-other" if is_other else "")
        badge_cls = "page-badge" + (" page-badge-muted" if is_other else "")
        parts.append(
            f'<section class="{group_cls}" data-page="{page_num}" aria-label="{_esc(page_label)}">'
            f'  <header class="page-header">'
            f'    <span class="{badge_cls}">{_esc(page_label)}</span>'
            f'    <span class="page-meta">写真スロット {len(suggestions)} 枠</span>'
            f"  </header>"
            f'  <div class="page-body">{"".join(slot_parts)}</div>'
            f"</section>"
        )
    return "".join(parts)


def _candidate_cards_html(slot_key: str, hits: list[IstockSearchHit], selected: str) -> str:
    if not hits:
        return (
            '<p class="empty">候補が見つかりませんでした。'
            '<code>uv run playwright install chromium --with-deps</code> 済みかご確認ください。</p>'
        )
    out: list[str] = []
    for hit in hits:
        aid = hit.asset_id
        checked = " checked" if selected == aid else ""
        thumb = _esc(hit.thumbnail_url or "")
        alt = _esc(hit.alt or aid)
        photog = _esc(hit.photographer_username or "(撮影者不明)")
        detail = _esc(hit.detail_url or f"https://www.istockphoto.com/jp/photo/-gm{aid}")
        out.append(
            f'<label class="card{ " selected" if checked else ""}">'
            f'  <input type="checkbox" class="pick" name="pick-{_esc(slot_key)}" '
            f'data-slot-key="{_esc(slot_key)}" data-asset-id="{_esc(aid)}"{checked}>'
            f'  <img src="{thumb}" alt="{alt}" loading="lazy" referrerpolicy="no-referrer">'
            f'  <div class="meta">'
            f'    <div class="aid">{_esc(aid)}</div>'
            f'    <div class="photog">{photog}</div>'
            f'    <a class="open" href="{detail}" target="_blank" rel="noreferrer noopener">iStockで開く</a>'
            f"  </div>"
            f"</label>"
        )
    return "".join(out)


def _confirm_tab_html(case: CaseState, unresolved_md: str) -> str:
    parts: list[str] = []
    book = case.draft.book_attribution_html
    if book:
        parts.append('<h4>書籍抜粋・出典</h4>')
        # book_attribution_html はツール生成だが、原稿由来文字列が混入する可能性があるので
        # iframe sandbox に閉じ込めて安全側に倒す。
        parts.append(f'<iframe class="book-attr" sandbox srcdoc="{_esc(book)}"></iframe>')
    if case.draft.author_profile_confirmation:
        parts.append('<h4>著者プロフィール</h4>')
        parts.append(f'<pre class="canonical">{_esc(case.draft.author_profile_confirmation)}</pre>')
    if case.draft.warnings:
        parts.append('<h4>警告</h4><ul>')
        for w in case.draft.warnings:
            parts.append(f"<li>{_esc(w)}</li>")
        parts.append("</ul>")
    parts.append('<h4>未解決事項 (チェックリスト)</h4>')
    parts.append(f'<pre class="canonical">{_esc(unresolved_md)}</pre>')
    return "".join(parts)


# ---------------------------------------------------------------------------
# CSS / JS
# ---------------------------------------------------------------------------


_CSS = """
* { box-sizing: border-box; }
body { background: #11141a; color: #e6e8ec;
  font-family: -apple-system, BlinkMacSystemFont, "Hiragino Kaku Gothic ProN", "Yu Gothic", sans-serif;
  margin: 0; padding: 24px; }
main { max-width: 1180px; margin: 0 auto; }
h1 { font-size: 18px; margin: 0 0 18px 0; color: #cfd3d8; }
h2 { font-size: 16px; margin: 0 0 4px 0; color: #f0b95b; }
h3 { font-size: 14px; margin: 18px 0 8px 0; color: #cfd3d8; }
h4 { font-size: 13px; margin: 14px 0 6px 0; color: #cfd3d8; }
a { color: #6ab0ff; text-decoration: none; }
a:hover { text-decoration: underline; }

.uform { background: #181c24; border: 1px solid #2a2f38; border-radius: 8px; padding: 16px; }
.uform label { display: block; font-size: 13px; color: #aab0ba; margin-bottom: 6px; }
.uform .hint { display: block; font-size: 11px; color: #6b727d; margin-bottom: 8px; }
.row { display: flex; gap: 12px; align-items: center; }
input[type=file] { background: #0d1016; color: #e6e8ec; border: 1px solid #2a2f38;
  border-radius: 6px; padding: 6px; font-size: 13px; }
button.primary { background: #d75a3b; color: white; border: 0; padding: 8px 16px;
  border-radius: 6px; cursor: pointer; font-size: 13px; }
button.primary:hover { background: #e26a4d; }
.error { color: #ff7766; font-size: 13px; margin-top: 10px; }

.case-header { background: #181c24; border: 1px solid #2a2f38; border-radius: 8px;
  padding: 12px 16px; margin-bottom: 12px; }
.case-meta { font-size: 12px; color: #aab0ba; margin-top: 2px; }
.downloads { font-size: 12px; color: #aab0ba; margin-top: 6px; }
.downloads a { color: #6ab0ff; }

.tabs { display: flex; gap: 4px; border-bottom: 1px solid #2a2f38; margin-bottom: 16px; }
.tab { background: transparent; color: #aab0ba; border: 0;
  padding: 10px 16px; cursor: pointer; font-size: 13px; border-bottom: 2px solid transparent; }
.tab.active { color: #f0b95b; border-bottom-color: #d75a3b; }
.tab-pane { display: none; }
.tab-pane.active { display: block; }

pre.canonical { background: #0d1016; border: 1px solid #2a2f38; border-radius: 6px;
  padding: 14px; font-family: ui-monospace, SFMono-Regular, monospace; font-size: 13px;
  color: #e6e8ec; white-space: pre-wrap; line-height: 1.5; }

iframe.book-attr { background: #fff; width: 100%; min-height: 80px;
  border: 1px solid #2a2f38; border-radius: 6px; }
button.ghost { background: transparent; color: #e6e8ec; border: 1px solid #2a2f38;
  padding: 4px 10px; border-radius: 4px; cursor: pointer; font-size: 12px; margin-left: 8px; }
button.ghost:hover { border-color: #d75a3b; }

.page-group { margin-bottom: 28px; border-top: 3px solid #d75a3b; border-radius: 4px;
  background: linear-gradient(180deg, rgba(215,90,59,0.06) 0%, transparent 60px);
  padding-top: 0; }
.page-header { display: flex; align-items: center; gap: 12px;
  padding: 10px 14px; margin-bottom: 8px; border-bottom: 1px dashed #2a2f38; }
/* WCAG AA: 白文字 (#fff) × #a13a20 = コントラスト比 ~6:1 (4.5:1 を超える)
   font-weight 700 + 14px で「Large Text」基準もクリア。 */
.page-badge { background: #a13a20; color: white; padding: 4px 14px; border-radius: 4px;
  font-weight: 700; font-size: 14px; letter-spacing: 0.04em; }
.page-meta { color: #aab0ba; font-size: 12px; }
.page-body { padding: 0 4px; }
/* 「その他」(未知スロット) は CMS ページと混同しないよう、控えめなグレー基調にする
   注意: opacity を親に効かせると子の候補画像やボタンまで薄くなるので使わない。
   border/background/badge 側だけで muted を表現する。 */
.page-group-other { border-top-color: #4a5160; background: none; }
.page-badge-muted { background: #4a5160; color: #e6e8ec; font-weight: 600; }

.slot { background: #181c24; border: 1px solid #2a2f38; border-radius: 8px; padding: 14px;
  margin-bottom: 16px; }
.slot-head { display: flex; gap: 12px; align-items: baseline; margin-bottom: 6px; flex-wrap: wrap; }
.slot-head .label { font-weight: 600; color: #f0b95b; }
.slot-head .type { color: #6ab0ff; font-size: 12px; font-family: ui-monospace, monospace; }
.slot-head .query { color: #aab0ba; font-size: 12px; }
.rationale { color: #6b727d; font-size: 11px; margin-bottom: 8px; }
.press-hint {
    margin: 8px 0 12px;
    padding: 10px 12px;
    border: 1px solid #f0c542;
    border-left-width: 4px;
    border-radius: 6px;
    background: #3a3a18;
    color: #f7e7a1;
    font-size: 13px;
    line-height: 1.5;
}
.press-hint-title {
    display: inline-block;
    margin-right: 8px;
    color: #f0c542;
    font-weight: 700;
}
.press-hint-body {
    color: #fff3bf;
}
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 10px; }
.card { background: #0d1016; border: 2px solid #2a2f38; border-radius: 6px;
  padding: 8px; display: block; cursor: pointer; transition: border-color 0.15s; }
.card:hover { border-color: #d75a3b; }
.card.selected { border-color: #d75a3b; background: #1a1e26; }
.card input.pick { transform: scale(1.1); margin-right: 6px; }
.card img { width: 100%; height: 120px; object-fit: cover; border-radius: 4px; background: #0d1016; }
.meta { font-size: 12px; margin-top: 6px; color: #aab0ba; }
.meta .aid { color: #e6e8ec; font-family: ui-monospace, SFMono-Regular, monospace; }
.meta .open { font-size: 11px; }
.empty { color: #6b727d; font-size: 13px; }
.pick-status { display: inline-block; margin-left: 12px; font-size: 12px; padding: 2px 8px;
  border-radius: 4px; min-width: 100px; }
.pick-status.pending { color: #aab0ba; }
.pick-status.ok { color: #6fc46a; }
.pick-status.error { color: #ff7766; }
.book-attr { background: #0d1016; padding: 12px; border-radius: 6px; font-size: 13px; }
code { background: #0d1016; padding: 1px 5px; border-radius: 3px; font-size: 12px; }
details summary { cursor: pointer; color: #aab0ba; font-size: 12px; margin: 8px 0; }
"""


_JS = """
(function(){
  var caseId = "__CASE_ID__";
  // --- copy CMS HTML source to clipboard ---
  var copyBtn = document.getElementById("copy-cms-html");
  if (copyBtn){
    copyBtn.addEventListener("click", function(){
      var pre = document.getElementById("cms-html-text");
      var status = document.getElementById("copy-status");
      if (!pre){ return; }
      var text = pre.textContent || "";
      function done(ok){
        if (!status){ return; }
        status.textContent = ok
          ? "コピーしました ✓"
          : "コピーできませんでした。HTML ソースを手動で選択してください。";
        status.className = "pick-status " + (ok ? "ok" : "error");
        setTimeout(function(){
          if (status){ status.textContent = ""; status.className = "pick-status"; }
        }, 2500);
      }
      function tryExecCommandCopy(){
        try {
          var ta = document.createElement("textarea");
          ta.value = text;
          ta.setAttribute("readonly", "");
          ta.style.position = "fixed";
          ta.style.opacity = "0";
          document.body.appendChild(ta);
          ta.select();
          var ok = document.execCommand("copy");
          document.body.removeChild(ta);
          return ok;
        } catch (e){
          return false;
        }
      }
      // Modern Clipboard API (HTTPS or localhost required).
      // writeText が未定義 / 同期 throw / 非同期 reject のいずれでも fallback に落とす。
      if (
        navigator.clipboard
        && window.isSecureContext
        && typeof navigator.clipboard.writeText === "function"
      ){
        try {
          var p = navigator.clipboard.writeText(text);
          if (p && typeof p.then === "function"){
            p.then(
              function(){ done(true); },
              function(){ done(tryExecCommandCopy()); }
            );
            return;
          }
        } catch (e){
          // 同期 throw のケース → そのまま fallback へ
        }
      }
      done(tryExecCommandCopy());
    });
  }

  // --- tab switching ---
  document.querySelectorAll(".tab").forEach(function(btn){
    btn.addEventListener("click", function(){
      var name = btn.getAttribute("data-tab");
      document.querySelectorAll(".tab").forEach(function(b){ b.classList.remove("active"); });
      document.querySelectorAll(".tab-pane").forEach(function(p){ p.classList.remove("active"); });
      btn.classList.add("active");
      document.getElementById("tab-"+name).classList.add("active");
    });
  });
  // --- photo pick (one checkbox per slot at a time) ---
  // committed: 「最後にサーバーが受理した状態」を保持する。失敗時はここに戻す。
  // 初期値: 初回ページレンダ時点で checked になっている asset_id (= サーバー側 selections)
  function escSlot(k){ return (window.CSS && CSS.escape) ? CSS.escape(k) : k.replace(/["\\\\]/g, "\\\\$&"); }
  function slotBoxes(slotKey){
    return document.querySelectorAll('input.pick[data-slot-key="' + escSlot(slotKey) + '"]');
  }
  function syncCards(slotKey){
    slotBoxes(slotKey).forEach(function(c){
      var card = c.closest(".card");
      if (card){ card.classList.toggle("selected", c.checked); }
    });
  }
  function applyCommitted(slotKey){
    // committed[slotKey] = asset_id or null
    var target = committed[slotKey] || null;
    slotBoxes(slotKey).forEach(function(c){
      c.checked = (c.getAttribute("data-asset-id") === target);
    });
    syncCards(slotKey);
  }
  // 初期 committed state: ページレンダ時点の checked 状態から構築
  var committed = {};
  document.querySelectorAll("input.pick").forEach(function(cb){
    var k = cb.getAttribute("data-slot-key");
    if (cb.checked){ committed[k] = cb.getAttribute("data-asset-id"); }
    else if (!(k in committed)){ committed[k] = null; }
  });
  document.querySelectorAll("input.pick").forEach(function(cb){
    cb.addEventListener("change", function(){
      var slotKey = cb.getAttribute("data-slot-key");
      // 1 つだけ ON にする (UI 上の即時フィードバック)
      if (cb.checked){
        slotBoxes(slotKey).forEach(function(other){ if (other !== cb){ other.checked = false; } });
      }
      syncCards(slotKey);
      var assetId = cb.checked ? cb.getAttribute("data-asset-id") : "";
      var status = document.getElementById("pick-status");
      if (status){ status.textContent = "反映中..."; status.className = "pick-status pending"; }
      fetch("/case/" + caseId + "/pick", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({slot_key: slotKey, asset_id: assetId}),
      }).then(function(r){
        if (!r.ok){ throw new Error("HTTP " + r.status); }
        return r.json();
      }).then(function(data){
        // サーバー受理: committed を更新
        committed[slotKey] = assetId || null;
        var pre = document.getElementById("canonical-text");
        if (pre){ pre.textContent = data.canonical; }
        if (status){ status.textContent = "反映済 ✓"; status.className = "pick-status ok"; }
      }).catch(function(err){
        // 失敗: committed (= サーバーが最後に受理した状態) に完全復元
        applyCommitted(slotKey);
        if (status){
          status.textContent = "反映に失敗: " + err.message + " (前回の状態に戻しました)";
          status.className = "pick-status error";
        }
      });
    });
  });
})();
"""
