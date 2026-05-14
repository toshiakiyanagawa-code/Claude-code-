from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from click.testing import CliRunner

from cms_entry_assistant.cli import main
from cms_entry_assistant.conversion_engine import ConversionConfig, convert
from cms_entry_assistant.docx_parser import parse_text
from cms_entry_assistant.instruction_canonical import format_canonical
from cms_entry_assistant.instruction_parser import parse_instruction
from cms_entry_assistant.istock_search import build_suggestion
from cms_entry_assistant.photo_audit import build_photo_audit, render_photo_audit_html
from cms_entry_assistant.photo_preferences import PreferencesStore, UsageHistory, rank_hits
from cms_entry_assistant.photographer_lookup import PhotographerLookup
from cms_entry_assistant.renderer import render_full_html, render_unresolved_report


MANUSCRIPT_TEXT = """・タイトル
すごい健康法
・サブタイトル
毎日歩く理由
・ショルダー
医師が解説
・リード①
これはリードです。
※本稿は、山田太郎『健康の本』（プレジデント社）の一部を再編集したものです。
■第一の見出し
本文です。
（クレジット：写真＝iStock.com／foo）
（キャプション：写真はイメージです）
（2ページ目）
■第二の見出し
続きです。
"""

INSTRUCTION_TEXT = """山田さん
【書籍抜粋】
【タイトル】指示タイトル
【ショルダー】指示ショルダー
【写真指定】
カンバン：iStock １２３４５６７８９
P2: iStock 123456789（第一の見出しの下にお願いします）
P3: 共同通信 987654321
【カテゴリ】ライフ＞健康
【外部配信】あり
【備考】
横展開 woman〇
Close-Up「足の健康」
カンバンをYahoo関連写真で「【写真を見る】健康な足」で設定
"""


def test_parse_text_extracts_metadata_and_body_blocks(tmp_path):
    manuscript_path = tmp_path / "sample.txt"
    manuscript_path.write_text(MANUSCRIPT_TEXT, encoding="utf-8")

    manuscript = parse_text(manuscript_path)

    assert manuscript.title_candidates == ["すごい健康法"]
    assert manuscript.subtitle_candidates == ["毎日歩く理由"]
    assert manuscript.shoulder_candidates == ["医師が解説"]
    assert manuscript.lead_candidates == ["これはリードです。"]
    assert manuscript.caution_notes[0].startswith("※本稿は")
    assert [block.kind for block in manuscript.body_blocks].count("heading_h4") == 2
    assert any(block.kind == "credit" for block in manuscript.body_blocks)


def test_parse_text_accepts_common_title_variants(tmp_path):
    manuscript_path = tmp_path / "variant.txt"
    manuscript_path.write_text(
        """・メイン
不登校の記事タイトル
・概要
概要リードです。
■見出し
本文です。
""",
        encoding="utf-8",
    )

    manuscript = parse_text(manuscript_path)

    assert manuscript.title_candidates == ["不登校の記事タイトル"]
    assert manuscript.lead_candidates == ["概要リードです。"]


def test_parse_instruction_normalizes_photo_lines_and_remarks():
    instruction = parse_instruction(INSTRUCTION_TEXT)

    assert instruction.recipient == "山田さん"
    assert instruction.article_type == "book_excerpt"
    assert instruction.title == "指示タイトル"
    assert instruction.photo_instructions[0].page_normalized == "hero"
    assert instruction.photo_instructions[0].asset_id == "123456789"
    assert instruction.photo_instructions[1].anchor_text == "第一の見出し"
    assert instruction.photo_instructions[2].source_kind == "kyodo"
    assert instruction.expansion_flags == ["woman"]
    assert instruction.closeup_tag == "足の健康"
    assert instruction.yahoo_related_images[0].link_title == "【写真を見る】健康な足"


def test_parse_instruction_preserves_marker_payloads():
    book = parse_instruction(
        """佐藤さん
【書籍抜粋】佐藤一郎『仕事の本』（プレジデント社）
【タイトル】指示タイトル
"""
    )
    series = parse_instruction(
        """佐藤さん
【連載】仕事大全
【タイトル】連載タイトル
"""
    )
    multiline_book = parse_instruction(
        """佐藤さん
【書籍抜粋】
佐藤一郎『仕事の本』（プレジデント社）
【タイトル】指示タイトル
"""
    )

    assert book.article_type == "book_excerpt"
    assert book.book_info == "佐藤一郎『仕事の本』（プレジデント社）"
    assert series.article_type == "series"
    assert series.series_name == "仕事大全"
    assert multiline_book.book_info == "佐藤一郎『仕事の本』（プレジデント社）"


def test_convert_outputs_cms_html_and_checklist(tmp_path):
    manuscript_path = tmp_path / "sample.txt"
    manuscript_path.write_text(MANUSCRIPT_TEXT, encoding="utf-8")
    manuscript = parse_text(manuscript_path)
    instruction = parse_instruction(INSTRUCTION_TEXT)
    lookup = PhotographerLookup(tmp_path / "photographers.json")
    lookup.upsert("123456789", "known_user", review_status="verified")

    draft = convert(
        manuscript,
        instruction,
        photographer=lookup,
        config=ConversionConfig(allow_network=False),
    )
    full_html = render_full_html(draft)
    report = render_unresolved_report(draft)

    assert draft.selected_title == "指示タイトル"
    assert '<h4>第一の見出し</h4>' in full_html
    assert "写真＝iStock.com／known_user" in full_html
    assert "ISBN_REQUIRED" in full_html
    assert "EXTERNAL_DISTRIBUTION_CONFIRM" in [item.code for item in draft.unresolved_items]
    assert "CMS入稿 確認リスト" in report
    assert "各小見出しのiStock写真候補" in report


def test_convert_uses_amazon_id_from_instruction(tmp_path):
    manuscript_path = tmp_path / "sample.txt"
    manuscript_path.write_text(MANUSCRIPT_TEXT, encoding="utf-8")
    manuscript = parse_text(manuscript_path)
    instruction = parse_instruction(
        """山田さん
【書籍抜粋】山田太郎『健康の本』（プレジデント社） ASIN: B0ABCDEF12
【タイトル】指示タイトル
"""
    )

    draft = convert(manuscript, instruction, config=ConversionConfig(allow_network=False))
    full_html = render_full_html(draft)
    unresolved_codes = [item.code for item in draft.unresolved_items]

    assert "/ASIN/B0ABCDEF12/presidentjp-22" in full_html
    assert "ISBN_REQUIRED" not in full_html
    assert "ISBN_REQUIRED" not in unresolved_codes
    assert draft.meta_fields["amazon_id"] == "B0ABCDEF12"


def test_convert_preserves_image_asset_url_in_html_and_json(tmp_path):
    manuscript_path = tmp_path / "sample.txt"
    manuscript_path.write_text(
        """・タイトル
写真テスト
・リード①
リードです。
■第一の見出し
本文です。
（クレジット：写真＝Wikimedia Commons）
（キャプション：東京の写真）
""",
        encoding="utf-8",
    )
    manuscript = parse_text(manuscript_path)
    instruction = parse_instruction(
        """【写真指定】
P2: Wikimedia https://commons.wikimedia.org/wiki/File:Tokyo.jpg
"""
    )

    draft = convert(manuscript, instruction, config=ConversionConfig(allow_network=False))
    full_html = render_full_html(draft)
    draft_json = asdict(draft)

    assert 'src="https://commons.wikimedia.org/wiki/File:Tokyo.jpg"' in full_html
    assert draft_json["image_placements"][0]["asset_url"] == (
        "https://commons.wikimedia.org/wiki/File:Tokyo.jpg"
    )


def test_format_canonical_is_pasteable():
    instruction = parse_instruction(INSTRUCTION_TEXT)

    canonical = format_canonical(instruction, author_profile="著者プロフィール")

    assert "山田さん" in canonical
    assert "【写真指定】" in canonical
    assert "カンバン：iStock １２３４５６７８９" in canonical
    assert "・横展開：woman" in canonical
    assert "・新規著者〇" in canonical


def test_rank_hits_prioritizes_japanese_no_face_people_policy(tmp_path):
    class Hit:
        def __init__(self, asset_id: str, alt: str, photographer: str = ""):
            self.asset_id = asset_id
            self.alt = alt
            self.detail_url = ""
            self.photographer_username = photographer

    hits = [
        Hit("1", "笑顔の白人ビジネスマンのポートレート"),
        Hit("2", "日本人ビジネスパーソンの手元、書類を書く顔なし写真"),
        Hit("3", "外国人女性がカメラ目線で会議をする"),
    ]

    ranked = rank_hits(
        hits,
        preferences=PreferencesStore(tmp_path / "prefs.json"),
        history=UsageHistory(tmp_path / "history.json"),
        query_context="日本人 ビジネス 書類 手元 顔なし",
    )

    assert ranked[0].asset_id == "2"


def test_photo_audit_renders_actual_and_cached_candidate_images(tmp_path):
    manuscripts_dir = tmp_path / "manuscripts"
    manuscripts_dir.mkdir()
    manuscript_path = manuscripts_dir / "sample.txt"
    manuscript_path.write_text(
        """・タイトル
なぜインネパが日本に定着したのか
・リード①
ネパール人経営のインドカレー店について。
■第一の見出し
インドカレー店の話です。
""",
        encoding="utf-8",
    )
    published_path = tmp_path / "published.json"
    published_path.write_text(
        """
{
  "sample.txt": {
    "url": "https://president.jp/articles/-/1",
    "published_title": "公開タイトル",
    "pages": [
      {
        "page": 1,
        "images": [
          {
            "role": "hero",
            "src": "https://example.com/actual.jpg",
            "alt": "カレー",
            "caption": "写真＝iStock.com／Chihiro",
            "source": "iStock",
            "photographer": "Chihiro"
          }
        ]
      }
    ]
  }
}
""",
        encoding="utf-8",
    )
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(
        """
{
  "ネパール人経営 インドカレー店": {
    "fetched_at": "2026-05-14T00:00:00+00:00",
    "query": "ネパール人経営 インドカレー店",
    "hits": [
      {
        "asset_id": "123456789",
        "thumbnail_url": "https://example.com/candidate.jpg",
        "alt": "インドカレーとナン",
        "photographer_username": "Chihiro",
        "detail_url": "https://www.istockphoto.com/jp/photo/-gm123456789"
      }
    ],
    "error": ""
  }
}
""",
        encoding="utf-8",
    )

    report = build_photo_audit(
        manuscripts_dir,
        published_path=published_path,
        cache_path=cache_path,
        preferences_path=tmp_path / "prefs.json",
        history_path=tmp_path / "history.json",
    )
    html = render_photo_audit_html(report)

    assert report.stats.matched_articles == 1
    assert report.stats.suggestions_with_cached_hits >= 1
    assert "https://example.com/actual.jpg" in html
    assert "https://example.com/candidate.jpg" in html
    assert "ネパール人経営 インドカレー店" in html


def test_cli_convert_writes_expected_files(tmp_path):
    manuscript_path = tmp_path / "sample.txt"
    instruction_path = tmp_path / "instruction.txt"
    out_dir = tmp_path / "out"
    dict_path = tmp_path / "photographers.json"
    manuscript_path.write_text(MANUSCRIPT_TEXT, encoding="utf-8")
    instruction_path.write_text(INSTRUCTION_TEXT, encoding="utf-8")
    lookup = PhotographerLookup(dict_path)
    lookup.upsert("123456789", "known_user", review_status="verified")
    lookup.save()

    result = CliRunner().invoke(
        main,
        [
            "convert",
            "--docx",
            str(manuscript_path),
            "--instruction",
            str(instruction_path),
            "--out-dir",
            str(out_dir),
            "--dict-path",
            str(dict_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert (out_dir / "full.html").exists()
    assert (out_dir / "checklist.md").exists()
    draft_json = (out_dir / "draft.json").read_text(encoding="utf-8")
    assert "指示タイトル" in draft_json


def test_cli_photo_audit_writes_html(tmp_path):
    manuscripts_dir = tmp_path / "manuscripts"
    manuscripts_dir.mkdir()
    (manuscripts_dir / "sample.txt").write_text(
        """・タイトル
丸まった背中の死亡リスク
・リード①
高齢者の姿勢について。
■見出し
本文です。
""",
        encoding="utf-8",
    )
    published_path = tmp_path / "published.json"
    published_path.write_text(
        """
{
  "sample.txt": {
    "url": "https://president.jp/articles/-/2",
    "published_title": "公開タイトル",
    "pages": [{"page": 1, "images": [{"role": "hero", "src": "https://example.com/back.jpg", "caption": "写真＝iStock.com／kazuma", "source": "iStock"}]}]
  }
}
""",
        encoding="utf-8",
    )
    cache_path = tmp_path / "cache.json"
    cache_path.write_text("{}", encoding="utf-8")
    out_path = tmp_path / "audit.html"

    result = CliRunner().invoke(
        main,
        [
            "photo-audit",
            "--manuscripts-dir",
            str(manuscripts_dir),
            "--published-path",
            str(published_path),
            "--cache-path",
            str(cache_path),
            "--out",
            str(out_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert out_path.exists()
    assert "CMS Photo Candidate Audit" in out_path.read_text(encoding="utf-8")


def _web_test_client():
    from fastapi.testclient import TestClient

    from cms_entry_assistant.web import app as app_module

    # Reset in-memory case store between tests
    app_module._cases.clear()
    return TestClient(app_module.app), app_module


def test_web_app_renders_upload_form_at_root():
    client, _ = _web_test_client()
    response = client.get("/")
    assert response.status_code == 200
    # 自動アップロード説明文 (新仕様)
    assert "案件が自動で作成されます" in response.text
    # JS 無効環境用フォールバックの送信ボタン (noscript 内)
    assert "<noscript>" in response.text
    assert "案件を作成" in response.text
    # 自動 submit を発火させる input.change ハンドラ
    assert "input.addEventListener('change'" in response.text
    assert "form.submit()" in response.text
    assert ".docx" in response.text
    # 回帰防止: input.disabled=true を form.submit() の前に行うと、
    # multipart で送信されず FastAPI 側で Field required になる。
    # disabled は使わず、submitted フラグと pointer-events で二重送信を防ぐ。
    assert "input.disabled = true" not in response.text
    assert "submitted = true" in response.text
    assert "pointerEvents = 'none'" in response.text
    # クライアント側 20MB チェック (サーバー側 MAX_UPLOAD_BYTES と一致)
    assert "MAX_BYTES = 20 * 1024 * 1024" in response.text
    assert "ファイルが大きすぎます" in response.text


def test_web_app_creates_case_and_renders_four_tabs(tmp_path, monkeypatch):
    # Avoid hitting iStock during tests
    from cms_entry_assistant.web import app as app_module

    monkeypatch.setattr(app_module, "_fetch_candidates", lambda *_a, **_k: {})

    client, mod = _web_test_client()

    response = client.post(
        "/case",
        files={"manuscript": ("sample.txt", MANUSCRIPT_TEXT.encode("utf-8"), "text/plain")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/case/")
    case_id = location.rsplit("/", 1)[-1]
    assert case_id in mod._cases

    page = client.get(location)
    assert page.status_code == 200
    body = page.text
    for tab_label in ("入稿指示書", "写真選定", "CMS HTML", "確認事項"):
        assert tab_label in body
    assert "canonical-text" in body


def test_web_app_pick_endpoint_updates_canonical(monkeypatch):
    from cms_entry_assistant.web import app as app_module
    from cms_entry_assistant.istock_crawler import IstockSearchHit

    # Inject a single fake candidate for each slot so pick can succeed.
    def fake_candidates(suggestions, hits_per_slot=5, **_kwargs):
        return {
            s.slot_key: [
                IstockSearchHit(
                    asset_id="999000111",
                    thumbnail_url="https://example.com/t.jpg",
                    alt="dummy",
                    photographer_username="fake_user",
                    detail_url="https://example.com/d",
                )
            ]
            for s in suggestions
        }

    monkeypatch.setattr(app_module, "_fetch_candidates", fake_candidates)

    client, mod = _web_test_client()
    create = client.post(
        "/case",
        files={"manuscript": ("sample.txt", MANUSCRIPT_TEXT.encode("utf-8"), "text/plain")},
        follow_redirects=False,
    )
    case_id = create.headers["location"].rsplit("/", 1)[-1]

    case = mod._cases[case_id]
    assert case.draft.photo_suggestions, "test manuscript should produce at least one slot"
    target_slot = case.draft.photo_suggestions[0].slot_key

    # Pick a photo
    pick = client.post(
        f"/case/{case_id}/pick",
        json={"slot_key": target_slot, "asset_id": "999000111"},
    )
    assert pick.status_code == 200
    data = pick.json()
    assert data["selections"][target_slot] == "999000111"
    assert "iStock 999000111" in data["canonical"]

    # Unpicking clears the slot
    clear = client.post(
        f"/case/{case_id}/pick",
        json={"slot_key": target_slot, "asset_id": ""},
    )
    assert clear.status_code == 200
    assert target_slot not in clear.json()["selections"]


def test_web_app_pick_rejects_unknown_slot(monkeypatch):
    from cms_entry_assistant.web import app as app_module

    monkeypatch.setattr(app_module, "_fetch_candidates", lambda *_a, **_k: {})

    client, mod = _web_test_client()
    create = client.post(
        "/case",
        files={"manuscript": ("sample.txt", MANUSCRIPT_TEXT.encode("utf-8"), "text/plain")},
        follow_redirects=False,
    )
    case_id = create.headers["location"].rsplit("/", 1)[-1]

    response = client.post(
        f"/case/{case_id}/pick",
        json={"slot_key": "h99_bogus", "asset_id": "123"},
    )
    assert response.status_code == 400


def test_web_app_downloads_canonical_and_html(monkeypatch):
    from cms_entry_assistant.web import app as app_module

    monkeypatch.setattr(app_module, "_fetch_candidates", lambda *_a, **_k: {})

    client, _ = _web_test_client()
    create = client.post(
        "/case",
        files={"manuscript": ("sample.txt", MANUSCRIPT_TEXT.encode("utf-8"), "text/plain")},
        follow_redirects=False,
    )
    case_id = create.headers["location"].rsplit("/", 1)[-1]

    canonical = client.get(f"/case/{case_id}/canonical.txt")
    assert canonical.status_code == 200
    assert "【写真指定】" in canonical.text or "【タイトル】" in canonical.text

    cms_html = client.get(f"/case/{case_id}/cms.html")
    assert cms_html.status_code == 200
    assert "第一の見出し" in cms_html.text
    # 配布前のセキュリティ要件: ブラウザに直接レンダさせない
    assert "attachment" in cms_html.headers.get("content-disposition", "")
    assert cms_html.headers.get("x-content-type-options") == "nosniff"


def test_web_app_rejects_unknown_asset_id(monkeypatch):
    from cms_entry_assistant.web import app as app_module
    from cms_entry_assistant.istock_crawler import IstockSearchHit

    def fake_candidates(suggestions, hits_per_slot=5, **_kwargs):
        return {
            s.slot_key: [
                IstockSearchHit(asset_id="111", thumbnail_url="", alt="", photographer_username="", detail_url="")
            ]
            for s in suggestions
        }

    monkeypatch.setattr(app_module, "_fetch_candidates", fake_candidates)
    client, mod = _web_test_client()
    create = client.post(
        "/case",
        files={"manuscript": ("sample.txt", MANUSCRIPT_TEXT.encode("utf-8"), "text/plain")},
        follow_redirects=False,
    )
    case_id = create.headers["location"].rsplit("/", 1)[-1]
    target_slot = mod._cases[case_id].draft.photo_suggestions[0].slot_key
    # 任意の asset_id は候補にないので 400
    bad = client.post(
        f"/case/{case_id}/pick",
        json={"slot_key": target_slot, "asset_id": "999"},
    )
    assert bad.status_code == 400
    # 候補内の id は OK
    ok = client.post(
        f"/case/{case_id}/pick",
        json={"slot_key": target_slot, "asset_id": "111"},
    )
    assert ok.status_code == 200


def test_web_app_enforces_upload_size_limit(monkeypatch):
    from cms_entry_assistant.web import app as app_module

    monkeypatch.setattr(app_module, "_fetch_candidates", lambda *_a, **_k: {})
    # 上限を 1KB に絞ってテスト (本番は 20MB だがテストは小さく)
    monkeypatch.setattr(app_module, "MAX_UPLOAD_BYTES", 1024)
    client, _ = _web_test_client()
    big = b"x" * 4096  # 4KB > 1KB の上限
    response = client.post(
        "/case",
        files={"manuscript": ("big.txt", big, "text/plain")},
        follow_redirects=False,
    )
    assert response.status_code == 200  # form re-render
    assert "ファイルが大きすぎます" in response.text


def test_web_app_cms_html_tab_shows_escaped_source_with_copy_button(monkeypatch):
    """CMS HTML タブは プレビューを廃止し、エスケープ済みソース + コピーボタンを表示する。"""
    from cms_entry_assistant.web import app as app_module

    monkeypatch.setattr(app_module, "_fetch_candidates", lambda *_a, **_k: {})
    client, _ = _web_test_client()
    create = client.post(
        "/case",
        files={"manuscript": ("sample.txt", MANUSCRIPT_TEXT.encode("utf-8"), "text/plain")},
        follow_redirects=False,
    )
    case_id = create.headers["location"].rsplit("/", 1)[-1]
    page = client.get(f"/case/{case_id}").text
    # プレビュー (iframe / 生 div) は出さない
    assert "iframe class=\"cms-preview\"" not in page
    assert "<div class=\"cms-preview\">" not in page
    # ソース表示と Copy ボタンを出す
    assert 'id="cms-html-text"' in page
    assert 'id="copy-cms-html"' in page
    assert "クリップボードにコピー" in page
    # body 内 HTML タグはエスケープされている (XSS にならない)
    assert "&lt;h4&gt;" in page
    # 生の <h4>... タグは出さない (CMS-rendered タグはエスケープ済みであるべき)
    # ただし UI 自体の <h4> 見出し (例: <h4>書籍抜粋・出典</h4>) は許可されるため、原稿由来の
    # 見出しテキスト「第一の見出し」が <h4> タグの中に直接出ていないことを確認する。
    assert "<h4>第一の見出し</h4>" not in page


def test_page_for_slot_uses_suggestion_page_number_first():
    """page_number が原稿の (Nページ目) マーカー由来で渡された場合、そちらを優先する。"""
    from cms_entry_assistant.web.app import _page_for_slot

    # 原稿で h4_1 が「2ページ目」にある (2つ目のh4と同居せず先頭1つだけ) ようなパターン
    assert _page_for_slot("h4_1", page_number=2) == (2, "2ページ目 (P2)")
    # 同じ page_number=1 (= カンバンと同居する h4) も正しく page=1 にグループ化される
    assert _page_for_slot("h4_1", page_number=1) == (1, "1ページ目 (カンバン)")
    # 2ページに 2 つの h4 が同居するケース
    assert _page_for_slot("h4_2", page_number=2) == (2, "2ページ目 (P2)")
    assert _page_for_slot("h4_3", page_number=2) == (2, "2ページ目 (P2)")


def test_page_for_slot_falls_back_to_slot_key_when_page_number_missing():
    """page_number=0 (未設定) の場合は旧推定 (h4_N → page N+1) にフォールバック。"""
    from cms_entry_assistant.web.app import _page_for_slot

    assert _page_for_slot("hero") == (1, "1ページ目 (カンバン)")
    assert _page_for_slot("h4_1") == (2, "2ページ目 (P2)")
    assert _page_for_slot("h4_5") == (6, "6ページ目 (P6)")
    # h4_0 は hero と衝突しないよう「その他」へ落とす
    page, label = _page_for_slot("h4_0")
    assert page == 0 and "その他" in label
    # 未知 slot_key も「その他」扱い
    page, label = _page_for_slot("unknown_xx")
    assert page == 0 and "その他" in label


def test_parse_page_break_number_handles_marker_variants():
    """マーカー文字列から N を取り出す: 半角/全角/漢数字/括弧種別/不正値。"""
    from cms_entry_assistant.conversion_engine import _parse_page_break_number

    assert _parse_page_break_number("（2ページ目）") == 2
    assert _parse_page_break_number("(3ページ目)") == 3
    assert _parse_page_break_number("（１０ページ目）") == 10  # 全角数字
    assert _parse_page_break_number("（十ページ目）") == 10    # 漢数字
    assert _parse_page_break_number("（七ページ目）") == 7
    # 11+ の漢数字も対応
    assert _parse_page_break_number("（十一ページ目）") == 11
    assert _parse_page_break_number("（二十ページ目）") == 20
    assert _parse_page_break_number("（二十三ページ目）") == 23
    assert _parse_page_break_number("（九十九ページ目）") == 99
    assert _parse_page_break_number("（ページ目）") is None    # N なし
    assert _parse_page_break_number("") is None
    assert _parse_page_break_number(None) is None


def test_build_photo_suggestions_honors_explicit_page_break_numbers():
    """マーカーに記載の N を尊重する (連続マーカー・欠番・冒頭マーカーを含む)。"""
    from cms_entry_assistant.docx_parser import parse_text

    # シナリオ: (5ページ目) で大きく飛ぶ + (6ページ目) 連続 + 欠番
    text = """・タイトル
記事
・リード①
リード文。
■h1
本文1.
（5ページ目）
■h5_a
本文5a.
■h5_b
本文5b.
（6ページ目）
■h6
本文6.
"""
    p = Path("/tmp") / "sample_explicit_pages.txt"
    p.write_text(text, encoding="utf-8")
    manuscript = parse_text(p)
    draft = convert(manuscript, parse_instruction(""), config=ConversionConfig(allow_network=False))
    by_slot = {s.slot_key: s for s in draft.photo_suggestions}
    # 1 ページ目: hero + h1
    assert by_slot["hero"].page_number == 1
    assert by_slot["h4_1"].page_number == 1
    # マーカー (5ページ目) を尊重して 5 に飛ぶ
    assert by_slot["h4_2"].page_number == 5
    assert by_slot["h4_3"].page_number == 5
    # (6ページ目) を尊重
    assert by_slot["h4_4"].page_number == 6


def test_build_photo_suggestions_falls_back_when_marker_n_is_not_monotonic():
    """単調増加が守れない不正マーカー (戻り) は +1 に fallback。"""
    from cms_entry_assistant.docx_parser import parse_text

    # (5ページ目) → (3ページ目) という戻りは無視して +1
    text = """・タイトル
記事
・リード①
リード文。
■h1
本文1.
（5ページ目）
■h5
本文5.
（3ページ目）
■should_be_page_6
本文.
"""
    p = Path("/tmp") / "sample_bad_marker.txt"
    p.write_text(text, encoding="utf-8")
    manuscript = parse_text(p)
    draft = convert(manuscript, parse_instruction(""), config=ConversionConfig(allow_network=False))
    by_slot = {s.slot_key: s for s in draft.photo_suggestions}
    assert by_slot["h4_2"].page_number == 5
    # (3ページ目) は current=5 より小さいので +1 → 6
    assert by_slot["h4_3"].page_number == 6


def test_build_photo_suggestions_assigns_real_page_numbers_from_page_break_markers():
    """conversion_engine が (Nページ目) マーカーを読んで page_number を正しく振る。

    実際の編集部慣例:
      - 1ページ目 = カンバン + 最初のh4 (1〜2 個)
      - (2ページ目) → 2ページ目に h4 が 2 個
      - (3ページ目) → 3ページ目に h4 が 2 個
    """
    from cms_entry_assistant.docx_parser import parse_text

    text = """・タイトル
記事のタイトル
・リード①
これはリードです。
■見出し1 (1ページ目)
本文1.
（2ページ目）
■見出し2 (2ページ目)
本文2.
■見出し3 (2ページ目)
本文3.
（3ページ目）
■見出し4 (3ページ目)
本文4.
■見出し5 (3ページ目)
本文5.
"""
    p = Path("/tmp") / "sample_pagebreak.txt"
    p.write_text(text, encoding="utf-8")
    manuscript = parse_text(p)
    draft = convert(manuscript, parse_instruction(""), config=ConversionConfig(allow_network=False))
    by_slot = {s.slot_key: s for s in draft.photo_suggestions}
    assert by_slot["hero"].page_number == 1
    # 1ページ目: カンバン + 見出し1
    assert by_slot["h4_1"].page_number == 1
    # 2ページ目: 見出し2,3
    assert by_slot["h4_2"].page_number == 2
    assert by_slot["h4_3"].page_number == 2
    # 3ページ目: 見出し4,5
    assert by_slot["h4_4"].page_number == 3
    assert by_slot["h4_5"].page_number == 3


def test_photos_tab_groups_slots_by_page(monkeypatch):
    """写真選定タブが (Nページ目) マーカー由来の page_number でグルーピングする。

    MANUSCRIPT_TEXT は「(2ページ目)」マーカーを 1 つ持つので:
      - ページ 1: hero (カンバン) + h4_1 (■第一の見出し)  = 2 スロット
      - ページ 2: h4_2 (■第二の見出し)                     = 1 スロット
    """
    from cms_entry_assistant.web import app as app_module

    monkeypatch.setattr(app_module, "_fetch_candidates", lambda *_a, **_k: {})
    client, mod = _web_test_client()
    create = client.post(
        "/case",
        files={"manuscript": ("sample.txt", MANUSCRIPT_TEXT.encode("utf-8"), "text/plain")},
        follow_redirects=False,
    )
    case_id = create.headers["location"].rsplit("/", 1)[-1]
    page = client.get(f"/case/{case_id}").text

    # ページごとのコンテナ (semantic section + aria-label)
    assert 'data-page="1"' in page  # カンバン + h4_1
    assert 'data-page="2"' in page  # h4_2 ((2ページ目) マーカー後)
    assert 'data-page="3"' not in page  # マーカーに従い 3 ページ目は出ない
    assert 'aria-label="1ページ目 (カンバン)"' in page
    assert 'aria-label="2ページ目 (P2)"' in page
    # ページラベル + バッジ
    assert "1ページ目 (カンバン)" in page
    assert "2ページ目 (P2)" in page
    assert 'class="page-badge"' in page
    # ページ 1 は hero + h4_1 で 2 枠、ページ 2 は h4_2 で 1 枠
    assert "写真スロット 2 枠" in page
    assert "写真スロット 1 枠" in page
    # ページ順 (1 が 2 より先)
    assert page.index("1ページ目 (カンバン)") < page.index("2ページ目 (P2)")


def test_photos_tab_renders_unknown_slot_as_muted_other_group(monkeypatch):
    """未知 slot_key は末尾に「その他」グループとして muted スタイルで出る。"""
    from cms_entry_assistant.web import app as app_module
    from cms_entry_assistant.models import IstockSearchSuggestion

    # 通常スロット (hero) と未知スロットを 1 つずつ仕込む
    captured: dict = {}

    def fake_fetch(suggestions, hits_per_slot=5, **_kwargs):
        captured["suggestions"] = suggestions
        return {s.slot_key: [] for s in suggestions}

    monkeypatch.setattr(app_module, "_fetch_candidates", fake_fetch)
    client, mod = _web_test_client()
    create = client.post(
        "/case",
        files={"manuscript": ("sample.txt", MANUSCRIPT_TEXT.encode("utf-8"), "text/plain")},
        follow_redirects=False,
    )
    case_id = create.headers["location"].rsplit("/", 1)[-1]
    case = mod._cases[case_id]
    # 後から「未知 slot_key」を疑似的に追加
    case.draft.photo_suggestions.append(
        IstockSearchSuggestion(
            slot_key="manual_pick_1",
            slot_label="編集者手配スロット",
            query_ja="",
        )
    )
    case.photo_candidates["manual_pick_1"] = []
    page = client.get(f"/case/{case_id}").text
    # その他グループが muted スタイルで出る
    assert "page-group-other" in page
    assert "page-badge-muted" in page
    # 末尾配置: カンバンより後ろにある
    assert page.index('aria-label="1ページ目 (カンバン)"') < page.index('aria-label="その他"')


def test_web_app_cms_html_pre_escapes_critical_html_characters(monkeypatch):
    """<pre id="cms-html-text"> の中身は < > & が確実にエスケープされている。

    これが破れると </pre> や <script> がページに注入され XSS になる。
    """
    import re as _re

    from cms_entry_assistant.web import app as app_module

    monkeypatch.setattr(app_module, "_fetch_candidates", lambda *_a, **_k: {})
    client, _ = _web_test_client()
    create = client.post(
        "/case",
        files={"manuscript": ("sample.txt", MANUSCRIPT_TEXT.encode("utf-8"), "text/plain")},
        follow_redirects=False,
    )
    case_id = create.headers["location"].rsplit("/", 1)[-1]
    page = client.get(f"/case/{case_id}").text
    m = _re.search(r'<pre id="cms-html-text"[^>]*>(.*?)</pre>', page, _re.S)
    assert m, "cms-html-text <pre> not found"
    pre_inner = m.group(1)
    # <pre> 内の HTML 特殊文字はすべて entity 化されている
    assert "<h4>" not in pre_inner          # 生の <h4 が混入していない
    assert "&lt;h4&gt;" in pre_inner        # 代わりに entity 表記が出る
    # 閉じタグ早期終了攻撃に対する基本ガード
    assert "</pre>" not in pre_inner


def test_crawl_search_does_not_persist_error_entries(tmp_path, monkeypatch):
    """エラー時はディスクキャッシュに永続化しない (再試行で自己回復)。"""
    import json

    from cms_entry_assistant import istock_crawler

    cache_file = tmp_path / "cache.json"
    # is_available を True、_do_search を例外で固定
    monkeypatch.setattr(istock_crawler, "is_available", lambda: True)

    async def boom(*args, **kwargs):
        raise RuntimeError("simulated outage")

    monkeypatch.setattr(istock_crawler, "_do_search", boom)
    # asyncio.run 経由でエラーが起きる
    result = istock_crawler.crawl_search("q", cache_path=cache_file)
    assert result == []
    # キャッシュには q が永続化されていない (= 次回も再試行できる)
    data = json.loads(cache_file.read_text(encoding="utf-8")) if cache_file.exists() else {}
    assert "q" not in data


def test_crawl_search_safe_works_inside_running_event_loop(monkeypatch):
    """crawl_search を event loop 内で直接呼ぶと asyncio.run() が落ちる。
    _crawl_search_safe は別スレッドへ逃がして動くようにする。
    """
    import asyncio

    from cms_entry_assistant.istock_crawler import IstockSearchHit
    from cms_entry_assistant.web import app as app_module

    captured: dict = {"in_main_thread": False, "called": False}
    import threading

    main_thread = threading.current_thread().ident

    def fake_crawl(query, limit=8):
        captured["called"] = True
        captured["in_main_thread"] = threading.current_thread().ident == main_thread
        # event loop が走っていないか確認 (走っていればこの asyncio.run は失敗するはず)
        asyncio.run(asyncio.sleep(0))
        return [IstockSearchHit(asset_id="ok", thumbnail_url="", alt="", photographer_username="", detail_url="")]

    monkeypatch.setattr(app_module, "crawl_search", fake_crawl)

    async def driver():
        return app_module._crawl_search_safe("q", limit=3)

    result = asyncio.run(driver())
    assert captured["called"] is True
    # event loop 内から呼んだので、別スレッドで実行されるべき
    assert captured["in_main_thread"] is False
    assert len(result) == 1 and result[0].asset_id == "ok"


def test_crawl_search_safe_uses_main_thread_outside_event_loop(monkeypatch):
    """event loop が走っていない通常コンテキストでは、同じスレッドで crawl_search を呼ぶ。"""
    import threading

    from cms_entry_assistant.istock_crawler import IstockSearchHit
    from cms_entry_assistant.web import app as app_module

    captured = {"in_main_thread": False}
    main_thread = threading.current_thread().ident

    def fake_crawl(query, limit=8):
        captured["in_main_thread"] = threading.current_thread().ident == main_thread
        return [IstockSearchHit(asset_id="ok", thumbnail_url="", alt="", photographer_username="", detail_url="")]

    monkeypatch.setattr(app_module, "crawl_search", fake_crawl)
    app_module._crawl_search_safe("q")
    assert captured["in_main_thread"] is True


def test_fetch_candidates_isolates_per_slot_failure(monkeypatch):
    """1 つのスロットで iStock 取得が失敗しても、他のスロットと案件全体は壊れない。"""
    from cms_entry_assistant.web import app as app_module
    from cms_entry_assistant.istock_crawler import IstockSearchHit
    from cms_entry_assistant.models import IstockSearchSuggestion

    # is_available を True に固定して try/except 経路を通す
    monkeypatch.setattr(app_module, "is_available", lambda: True)

    calls = []
    def flaky_crawl(query, limit=8):
        calls.append(query)
        if "FAIL" in query:
            raise RuntimeError("simulated iStock outage")
        return [IstockSearchHit(asset_id="abc", thumbnail_url="", alt="", photographer_username="", detail_url="")]

    monkeypatch.setattr(app_module, "crawl_search", flaky_crawl)
    suggestions = [
        IstockSearchSuggestion(slot_key="hero", slot_label="カンバン", query_ja="FAIL me"),
        IstockSearchSuggestion(slot_key="h4_1", slot_label="P2", query_ja="working query"),
    ]
    out = app_module._fetch_candidates(suggestions)
    assert out["hero"] == []           # 失敗スロットは空
    assert len(out["h4_1"]) == 1       # 別スロットは通常通り取得
    assert calls == ["FAIL me", "working query"]


def test_fetch_candidates_llm_mode_prepends_llm_queries(monkeypatch):
    """search_mode='llm' は LLM の search_queries を先頭に積み増す。"""
    from cms_entry_assistant.web import app as app_module
    from cms_entry_assistant import llm_query_generator as gen_module
    from cms_entry_assistant.istock_crawler import IstockSearchHit
    from cms_entry_assistant.models import IstockSearchSuggestion

    monkeypatch.setattr(app_module, "is_available", lambda: True)

    queries_seen: list[str] = []
    def fake_crawl(query, limit=8):
        queries_seen.append(query)
        return [IstockSearchHit(asset_id=query, thumbnail_url="", alt=query, photographer_username="", detail_url=f"https://x/{query}")]

    monkeypatch.setattr(app_module, "crawl_search", fake_crawl)

    # LLM が "llm-fresh" を返す stub
    fake_plan = gen_module.LlmQueryPlan(
        search_queries=["llm-fresh"],
        intent="test",
        keywords=[],
        negative_keywords=[],
    )
    fake_result = gen_module.QueryPlanResult(
        slot_hash="x", plan=fake_plan, model="stub"
    )
    monkeypatch.setattr(
        app_module,
        "_maybe_generate_llm_plan",
        lambda suggestion, article_title="": fake_plan,
    )

    suggestions = [
        IstockSearchSuggestion(slot_key="hero", slot_label="hero", query_ja="legacy-primary"),
    ]
    out = app_module._fetch_candidates(suggestions, search_mode="llm")

    # 先頭は LLM の query。1 件で十分集まったらそこで止まる。
    assert queries_seen[0] == "llm-fresh"
    assert out["hero"]
    assert out["hero"][0].asset_id == "llm-fresh"


def test_fetch_candidates_llm_rerank_uses_rerank_order(monkeypatch):
    """search_mode='llm_rerank' は candidate_reranker を経由して並べ替える。"""
    from cms_entry_assistant.web import app as app_module
    from cms_entry_assistant import llm_query_generator as gen_module
    from cms_entry_assistant.istock_crawler import IstockSearchHit
    from cms_entry_assistant.models import IstockSearchSuggestion

    monkeypatch.setattr(app_module, "is_available", lambda: True)

    def fake_crawl(query, limit=8):
        # 2 件返す: 順序を逆転させたいので alt を変える
        return [
            IstockSearchHit(asset_id="off", thumbnail_url="", alt="unrelated", photographer_username="", detail_url="https://x/off"),
            IstockSearchHit(asset_id="hit", thumbnail_url="", alt="shanghai skyline", photographer_username="", detail_url="https://x/hit"),
        ]

    monkeypatch.setattr(app_module, "crawl_search", fake_crawl)

    fake_plan = gen_module.LlmQueryPlan(
        search_queries=["shanghai"],
        intent="shanghai skyline",
        keywords=["shanghai", "skyline"],
        negative_keywords=[],
    )
    monkeypatch.setattr(
        app_module,
        "_maybe_generate_llm_plan",
        lambda suggestion, article_title="": fake_plan,
    )

    suggestions = [
        IstockSearchSuggestion(slot_key="hero", slot_label="hero", query_ja="legacy"),
    ]
    out = app_module._fetch_candidates(suggestions, search_mode="llm_rerank")

    # intent_terms = "shanghai skyline" にマッチする hit が先頭に来る
    assert out["hero"][0].asset_id == "hit"


def test_fetch_candidates_legacy_mode_skips_llm_call(monkeypatch):
    """search_mode='legacy' は LLM を一切呼ばない (互換維持)。"""
    from cms_entry_assistant.web import app as app_module
    from cms_entry_assistant.istock_crawler import IstockSearchHit
    from cms_entry_assistant.models import IstockSearchSuggestion

    monkeypatch.setattr(app_module, "is_available", lambda: True)
    monkeypatch.setattr(app_module, "crawl_search", lambda q, limit=8: [
        IstockSearchHit(asset_id="x", thumbnail_url="", alt="", photographer_username="", detail_url="https://x/x")
    ])

    llm_called: list[bool] = []
    def boom(suggestion, article_title=""):
        llm_called.append(True)
        return None

    monkeypatch.setattr(app_module, "_maybe_generate_llm_plan", boom)

    suggestions = [
        IstockSearchSuggestion(slot_key="hero", slot_label="hero", query_ja="something"),
    ]
    app_module._fetch_candidates(suggestions, search_mode="legacy")
    assert llm_called == []


def test_rebuild_canonical_preserves_original_photo_instructions(monkeypatch):
    """未選択時、submission の既存 photo_instructions は破壊されない。"""
    from cms_entry_assistant.web import app as app_module
    from cms_entry_assistant.istock_crawler import IstockSearchHit
    from cms_entry_assistant.models import PhotoInstruction

    monkeypatch.setattr(
        app_module,
        "_fetch_candidates",
        lambda suggestions, hits_per_slot=5, **_kwargs: {
            s.slot_key: [IstockSearchHit(asset_id=f"orig{i}", thumbnail_url="", alt="", photographer_username="", detail_url="")]
            for i, s in enumerate(suggestions)
        },
    )
    client, mod = _web_test_client()
    create = client.post(
        "/case",
        files={"manuscript": ("sample.txt", MANUSCRIPT_TEXT.encode("utf-8"), "text/plain")},
        follow_redirects=False,
    )
    case_id = create.headers["location"].rsplit("/", 1)[-1]
    case = mod._cases[case_id]
    # 編集者が事前に手入力した想定の指示 (一致する slot_label がある場合)
    first_label = case.draft.photo_suggestions[0].slot_label
    case.submission.photo_instructions.append(
        PhotoInstruction(
            page_label=first_label,
            source_kind="kyodo",
            asset_id="MANUAL999",
        )
    )
    canonical = app_module._rebuild_canonical(case)
    # 未選択スロットでも、既存の手入力指示 "共同通信 MANUAL999" が canonical に残る
    assert "MANUAL999" in canonical

def test_build_suggestion_v8_hero_uses_lead_or_h4_text():
    """hero スロットは lead_text を context に使う (記事代表)。"""
    s = build_suggestion(
        "hero",
        "カンバン",
        h4_text="和田秀樹 シニアの腰痛対策で寝たきりを防ぐ",
        surrounding_paragraphs=[],
        lead_text="リードに病院を取り上げる。",
    )
    # lead_text の「病院」が type A (ランドマーク) ルールでヒットする
    assert "病院" in s.query_ja or "hospital" in s.query_en
    # 余分な「日本人 顔なし 手元」サフィックスは付かない (v2-era 廃止仕様の回帰防止)
    assert "顔なし" not in s.query_ja
    assert "手元" not in s.query_ja


def test_build_suggestion_v8_h4_uses_heading_plus_surrounding_paragraphs():
    """h4 は h4_text + 直近 2 段落の context を使い、lead_text には反応しない (slot-local)。

    「ストレッチ」+「毎日」が SPECIFIC_SCENE_RULES (type G) にヒットして
    「シニア ストレッチ 自宅」へ具体化される。slot-local 文脈の正しい挙動。
    """
    s = build_suggestion(
        "h4_2",
        "■毎日のストレッチが鍵",
        h4_text="毎日のストレッチが鍵",
        surrounding_paragraphs=["運動が大事です。", "毎朝の習慣にしましょう。"],
        lead_text="無関係のリード。",
    )
    # G (具体シーン) または D (運動) のどちらでも妥当 — 単独 C にはならない
    assert s.type_code in {"D", "G"}
    # ストレッチ系のクエリになっていること
    assert "ストレッチ" in s.query_ja or "運動" in s.query_ja
    assert "顔なし" not in s.query_ja  # 機械サフィックス禁止


def test_build_suggestion_v8_h4_slots_do_not_collapse_on_lead():
    """h4 スロットは lead_text には引っ張られない。別 slot は別 query になる。

    v2 期は lead_text/article_title の強語 (例: 腰痛) が全 slot に伝播して同じクエリに
    なるバグがあった。v8 は h4_text + 直近段落のみを使うため、別段落の slot は別クエリ
    になる。
    """
    s_a = build_suggestion(
        "h4_1", "■見出しA", h4_text="見出しA",
        surrounding_paragraphs=["皇居の周辺を散策。"], lead_text="腰痛と寝たきり",
    )
    s_b = build_suggestion(
        "h4_2", "■見出しB", h4_text="見出しB",
        surrounding_paragraphs=["半導体工場が拡大。"], lead_text="腰痛と寝たきり",
    )
    # 各 slot は周辺段落由来の異なるクエリになる (lead_text に引っ張られない)
    assert s_a.query_ja != s_b.query_ja
    assert "皇居" in s_a.query_ja  # type A LANDMARKS にヒット
    # s_b は周辺段落「半導体工場」由来 (半導体=E or 工場=A、どちらでも v2 期の "腰痛" にはならない)
    assert ("半導体" in s_b.query_ja) or ("工場" in s_b.query_ja)
    # 重要: どちらのスロットにも lead_text の "腰痛" が混入しない
    assert "腰" not in s_a.query_ja
    assert "腰" not in s_b.query_ja


def test_build_suggestion_v8_no_mechanical_suffix():
    """機械的な「日本人 顔なし 後ろ姿 手元」suffix を 付与しないこと (v2 廃止仕様)。"""
    s = build_suggestion(
        "hero", "カンバン",
        h4_text="高齢者の生活",
        surrounding_paragraphs=[],
        lead_text="高齢者の老後について",
    )
    for forbidden in ("顔なし", "後ろ姿", "手元", "no face"):
        assert forbidden not in s.query_ja, f"{forbidden} が混入: {s.query_ja}"
        assert forbidden not in s.query_en, f"{forbidden} が混入: {s.query_en}"


def test_build_suggestion_v8_keeps_5_types_only():
    """A〜E の 5 type のみが返ること (旧 F/G/H/I は v8 復活で廃止)。"""
    s = build_suggestion("hero", "カンバン", h4_text="病院での出来事", surrounding_paragraphs=[])
    assert s.type_code in {"A", "B", "C", "D", "E"}, f"unexpected type: {s.type_code}"
