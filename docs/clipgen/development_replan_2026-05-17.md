# clipgen 開発計画 再整理版

作成日: 2026-05-17

## 0. 前提

この計画は、`feat/clipgen-m0-m4` の現行コード、`docs/clipgen/*`、`output/test_a1` / `output/test_b1`、および親 worktree の `docs/clipgen_related_data_inventory_2026-05-17.md` を前提に立て直したもの。

現状の到達点:

- 候補抽出、許諾チェック、スコアリング、SRT からのハイライト検出、タイトル/サムネ文言生成は実装済み。
- `clipgen run-job` は candidates、plans、extract manifests、review reports、digest を short/long 両方で生成できる。
- `clipgen review` は takedown list と `review_required` 判定に対応済み。
- `uv run pytest` は 187 件 PASS。
- 実動画 `combined.mp4` 生成は、`cookies.txt` または `source.mp4` / `source.ja.srt` が未提供のため未完了。
- `YOUTUBE_API_KEY` はローカル `.env` に設定済み。最小 live smoke は 1 クエリ / 1 件で成功済み。
- Windows の Python HTTPS 問題に対応するため、CLI は `.env` 自動読み込み、Python 3.12 固定、YouTube API の curl backend に対応済み。

## 1. ゴール

編集者が毎朝 1 回のジョブで、政治系切り抜き候補を short / long それぞれ取得し、以下を確認できる状態にする。

1. 候補動画リスト
2. 権利・禁止ソース・takedown 反映後のステータス
3. 推奨ハイライト窓
4. タイトル案 / サムネ文言案
5. `yt-dlp` / `ffmpeg` による切り出し成果物、または再現可能なコマンド
6. 公開前レビュー用 TSV / JSON
7. 日次 digest

自動投稿は当面スコープ外。公開判断は編集者が行う。

## 2. ユーザー提供が必要なもの

### 必須

| 項目 | 用途 | 提供形式 | 保存先 |
|---|---|---|---|
| `YOUTUBE_API_KEY` | YouTube Data API の live 候補抽出 | API key 文字列 | `.env` |
| `cookies.txt` または実素材 | YouTube bot 判定を避けて動画/字幕を取得 | `cookies.txt`、または `source.mp4` + `source.ja.srt` | `output/test_b1/cookies.txt` など |
| 使用許諾方針 | allowlist / blocklist の確定 | チャンネル URL、許諾メモ、NG 条件 | `src/clipgen/data/allowlist.json` / `blocklist.json` |
| 公開前レビュー担当 | `review_required` の確認責任者 | 名前またはチーム名 | 運用メモ / digest |

### 強く推奨

| 項目 | 用途 | 提供形式 |
|---|---|---|
| `ANTHROPIC_API_KEY` | `political_llm` rerank、タイトル polish | API key 文字列 |
| takedown list | 削除依頼・使用不可素材の運用反映 | JSON または TSV |
| 対象チャンネル方針 | CH-1 / CH-2 / CH-3 などの投稿先ごとの文体・禁止表現 | Markdown または JSON |
| テスト対象動画 ID | B1 以外の実動画 E2E | YouTube video ID のリスト |

### 任意

| 項目 | 用途 |
|---|---|
| `SLACK_WEBHOOK_URL` | 日次 digest 投稿 |
| 編集者が採用/不採用にした履歴 | スコアリング改善 |
| サムネ制作ルール | サムネ文言のトーン調整 |

## 3. 秘密情報の扱い

- `.env` と `.env.local` は `.gitignore` 済み。
- `cookies.txt` は commit しない。
- API key、cookie、CMS token などの値はドキュメントやテストログに載せない。
- API key の有無確認は、値ではなく `set` / `missing` のみを出す。

`.env` 例:

```text
YOUTUBE_API_KEY=...
ANTHROPIC_API_KEY=...
SLACK_WEBHOOK_URL=...
```

## 4. フェーズ別計画

### Phase 1 — 実 live smoke を通す

目的: YouTube Data API の本番通信を最小 quota で確認する。

作業:

- `.env` に `YOUTUBE_API_KEY` を設定。完了。
- seed 全体ではなく、1 クエリ / 数件だけの smoke 経路を作る。`--query-limit` / `--max-per-query` で対応済み。
- `discover --source live` の本番レスポンスを candidates JSON に保存。完了。
- allow/blocklist の handle → channel_id 解決を `--diff` で確認。

完了条件:

- 本番 API で候補が 1 件以上取得できる。完了。
- quota 消費を記録できる。
- `clipgen config-check` の `YOUTUBE_API_KEY is not set` 警告が消える。完了。

### Phase 2 — B1 実動画 E2E

目的: `combined.mp4` を long / short 両方で生成する。

作業:

- `output/test_b1/cookies.txt` を配置、または `source.mp4` / `source.ja.srt` を配置。
- `uv run clipgen config-check --job-dir output/test_b1/extract/AHqwNShdSGI_long` を通す。
- `bash output/test_b1/codespace_run.sh` を実行。
- `combined.mp4` を long / short で再生確認。
- ハイライト窓、タイトル案、サムネ文言案を編集者目線で評価。

完了条件:

- `output/test_b1/extract/AHqwNShdSGI_long/combined.mp4` が生成される。
- `output/test_b1/extract/AHqwNShdSGI_short/combined.mp4` が生成される。
- `plan_long.json` / `plan_short.json` に使える候補が残る。
- 公開前チェックリストの NG が記録される。

### Phase 3 — 日次ジョブを運用形にする

目的: 毎朝の候補抽出を `run-job` に一本化する。

作業:

- `run-job --source live --format both` を本番向けに smoke → small → daily の3段階に分ける。
- `--srt` / `--from-youtube` の扱いを、候補ごとの字幕取得に拡張するか判断。
- `candidates_short.json` / `candidates_long.json` / `plan.json` / `review.tsv` / `digest.txt` を日付ディレクトリに安定出力。
- `review_required` がある場合の終了コード・通知方針を決める。

完了条件:

- `uv run clipgen run-job --date YYYY-MM-DD --source live --format both ...` が通る。
- `review.tsv` を編集者がそのまま見られる。
- `digest.txt` が候補確認に使える。

### Phase 4 — 権利・名誉毀損レビュー運用

目的: 使ってはいけない素材、タイトルリスク、TV/新聞系素材を漏らさない。

作業:

- allowlist / blocklist を最新化。
- `scripts/refresh_channel_ids.py --diff` を運用前チェックに入れる。
- takedown list のフォーマットを確定。
- `clipgen review --fail-on-review-required` を CI または日次チェックに組み込む。
- `[REVIEW]` プレフィックスの扱いを編集者ルールに明記。

完了条件:

- `blocked` / `manual_review` / `cleared` の判断理由が TSV に残る。
- takedown list で既知 NG 動画・チャンネルが落ちる。
- TV/新聞/第三者転載系の候補が review_required になる。

### Phase 5 — LLM 品質改善

目的: ハイライトとタイトル案の品質を上げつつ、煽りすぎを抑える。

作業:

- `ANTHROPIC_API_KEY` を設定。
- `--selection-mode political_llm` を B1 / 追加動画で比較。
- `--polish` あり/なしのタイトル案を比較。
- `CLIPGEN_AGGRESSIVENESS=0..3` の編集部基準を決める。

完了条件:

- deterministic と LLM rerank の採用率比較ができる。
- LLM 失敗時も deterministic fallback でジョブが止まらない。
- 公開不可・名誉毀損リスクがある表現を LLM が増幅していない。

### Phase 6 — 量産運用

目的: 1日1回のベータ運用から、複数チャンネル向けの量産へ進める。

作業:

- CH-1 / CH-2 / CH-3 の投稿先別 persona / 禁止表現 / 尺 / タイトル文体を定義。
- candidate 採用履歴を保存し、同じ素材の重複使用を避ける。
- digest を Slack に投稿する場合、`SLACK_WEBHOOK_URL` を設定。
- 完成動画の保存場所と命名規則を決める。

完了条件:

- 1週間分の日次 job を実行できる。
- 採用/不採用理由が残る。
- 編集者が 30 分以内に short 1 本の候補を選べる。

## 5. 直近の優先順位

1. `output/test_b1/cookies.txt` または B1 の `source.mp4` / `source.ja.srt` を提供して実動画 E2E を完了する。
2. allowlist / blocklist の確認者を決める。
3. takedown list の初期フォーマットを作る。
4. `run-job --source live --format both` の small daily run を開始する。
5. `ANTHROPIC_API_KEY` を提供して LLM rerank / polish の品質確認を始める。

## 6. 現在ブロックされていること

| ブロック | 理由 | 解除に必要なもの |
|---|---|---|
| B1 実 mp4 生成 | cookies / source files 未配置 | `cookies.txt` または `source.mp4` + `source.ja.srt` |
| LLM rerank / polish | `ANTHROPIC_API_KEY` 未提供 | `ANTHROPIC_API_KEY` |
| Slack digest 投稿 | webhook 未提供 | `SLACK_WEBHOOK_URL` |
| 公開可否の最終判断 | 編集部ルール未確定 | allow/blocklist 方針、takedown list、レビュー担当 |

## 7. コマンド集

開発テスト:

```powershell
uv run pytest
```

dry-run 日次ジョブ:

```powershell
uv run clipgen run-job `
  --date 2026-05-17 `
  --out-root output/dev_run_job `
  --source mock `
  --format both `
  --top 1 `
  --srt src/clipgen/data/fixtures/sample.srt `
  --dry-run
```

B1 preflight:

```powershell
uv run clipgen config-check --job-dir output/test_b1/extract/AHqwNShdSGI_long
```

B1 実行:

```bash
bash output/test_b1/codespace_run.sh
```

review:

```powershell
uv run clipgen review `
  --input output/dev_run_job/2026-05-17/plan.json `
  --out-json output/dev_run_job/2026-05-17/review.json `
  --out-tsv output/dev_run_job/2026-05-17/review.tsv `
  --fail-on-review-required
```

## 8. 次に作るべきもの

- `docs/clipgen/takedown_list.example.tsv`
- `docs/clipgen/channel_policy.md`
- `scripts/clipgen_live_smoke.py` または `clipgen live-smoke` subcommand
- 候補ごとに YouTube 字幕を取得する `run-job` 拡張
- 採用/不採用履歴 `data/clipgen_usage_history.json`
