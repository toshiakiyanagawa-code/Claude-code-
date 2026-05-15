**Must-fix**
- 診断は概ね正しいです。`playwright` / `playwright_stealth` 未導入なら `is_available()` が `False` になり、空候補になります。
- ただし `pyproject.toml` だけでは不十分です。Chromium 本体は別管理なので、Codespaces が標準環境なら `postCreateCommand` かイメージビルドで `uv run playwright install --with-deps chromium` を自動化すべきです。
- `is_available()` は現状 import しか見ていません。「Chromium が起動できる」は検査していないため、missing browser / OS deps / launch failure を「候補なし」と混同します。setup failure は candidates 空ではなく、明示的なエラー種別で UI に返すべきです。

**Should**
- iStock 取得が CMS ツールで事実上必須なら、optional ではなく通常 dependencies で妥当です。optional にするなら `uv sync --extra istock` を強制する仕組みが必要で、今回の再発リスクが上がります。
- `playwright>=1.59.0` は妥当です。PyPI 上でも 1.59.0 は 2026-04-29 時点の最新です。`playwright-stealth>=2.0.0` も `Stealth` 前提なら妥当です。安定性重視なら `uv.lock` を必ず commit、さらに不意の major 更新を避けて `<2` / `<3` を付ける判断はありです。
- UI 文言は編集者向けに分けるべきです。例: 「素材候補の取得機能を起動できませんでした。担当者に連絡してください。」詳細ログには `playwright install --with-deps chromium` を出す。通常のゼロ件は「条件に合う素材候補が見つかりませんでした。検索語を変えて再実行してください。」に分離。

**Nice**
- `is_available()` は `is_importable()` に改名するか、`check_environment()` にして import / browser missing / launch failed を enum 的に返すとよいです。
- 起動時 health check か管理者用 diagnostics で `chromium.launch(headless=True)` まで確認すると、アップロード後に初めて壊れるのを避けられます。
- `_fetch_candidates` が broad `except` で空配列に潰している場合、network / CAPTCHA / selector 変更 / iStock 側ブロックも同じ UI になるので、ログとエラー分類を追加した方がよいです。

参照: [Playwright browser install docs](https://playwright.dev/python/docs/browsers), [playwright PyPI](https://pypi.org/project/playwright/), [playwright-stealth PyPI](https://pypi.org/project/playwright-stealth/)
