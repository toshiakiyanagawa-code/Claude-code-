# clipgen 開発計画書

最終更新: 2026-05-17
オーナー: プレジデントオンライン編集部 / 柳川 利明
レビュー方針: 各マイルストーン完了ごとに codex レビューを実施し、○ になるまで反復（[[feedback-codex-review]]）

---

## 0. 目的

「政治系切り抜き動画を量産する」ための **半自動パイプライン** を構築する。完成形は以下のワークフローを 1 コマンドで回せる状態:

1. 政治系トレンド話題から **候補動画** を抽出（→ M0 / マイルストーン 1）
2. 候補動画ごとに **切り抜きハイライト窓** を提示（→ M3）
3. ハイライト窓に沿った **動画フォーマット案**（ショート 60 秒 / ロング 8 分以上）を作る（→ M3）
4. 動画ごとに **タイトル案 / サムネ文言案** を 3〜5 種類提案（→ M4）
5. 編集者が手動で 1 案選択 → 動画編集 → アップロード

**著作権・名誉毀損リスクは編集者の最終チェックに委ねる**前提だが、ツール側で「不可素材」「要編集レビュー素材」を自動振り分け、選択を絞り込む。

---

## 1. ユーザーストーリー

> 編集者として、毎朝 clipgen を 1 回叩くと、その日伸びそうな政治系切り抜き候補が
> ショート向け / ロング向けの 2 種類で、タイトル案・ハイライト窓付きで JSON 出力されている
> 状態にしたい。

---

## 2. スコープ

| 範囲 | やる | やらない（v0.1） |
|---|---|---|
| 候補抽出 | YouTube Data API ベース、許諾チェック付き | TikTok / X 等の他プラットフォーム |
| ハイライト | 字幕（SRT）ベースの感情・話題ピーク検出 | 音声波形解析、表情解析 |
| タイトル生成 | テンプレ + ホット人物名 + フォーマット語 | 大規模 LLM 呼び出し（あとで追加可） |
| サムネ | 文言案のみ | 画像合成 |
| 編集 | ハイライト窓と書き出しコマンド提示まで | 実際の ffmpeg 結合 |
| 投稿 | しない | YouTube 自動アップロード |

---

## 3. 現在の到達点

2026-05-17 時点で、clipgen は「素材発見 → 企画案 → 切り出しコマンド → レビュー補助」までの半自動 MVP として動作する。
ローカル開発環境では `uv run clipgen ...` で CLI を起動でき、`uv run pytest tests/clipgen` は 130 件 PASS。

### M0（完了）— トレンド分析 + 候補抽出 MVP
- [`docs/clipgen/trend_analysis.md`](trend_analysis.md) に政治系切り抜き市場・許諾リスク・候補ソースを整理済み。
- `src/clipgen/pipeline.py` / `scoring.py` / `sources.py` で YouTube 候補の許諾チェック、allow/blocklist、スコアリング、mock E2E を実装済み。

### M1（完了）— Live モードのドライラン
- `--source live --dry-run` で YouTube API キーなしでも fixture 経由の疑似 live 経路を実行可能。
- `tests/clipgen/test_live_dryrun.py` で回帰テスト済み。

### M2（実装済み・live再検証待ち）— allowlist/blocklist の channel_id 自動解決
- `scripts/refresh_channel_ids.py` で handles → channel_id の解決、`--diff` / `--write` 運用を想定。
- 本番 API キーを使った最新 allow/blocklist の再解決は、次回 live E2E の一部として確認する。

### M3（完了）— ハイライト検出 + ショート/ロング振り分け
- `src/clipgen/highlights.py` で SRT/VTT を解析し、感嘆符、否定語、笑い表現、話題語、語彙変化などから `Highlight` を生成。
- `src/clipgen/political_scoring.py` で日本政治語彙ベースの多軸スコアリングを追加。
- `src/clipgen/llm_rerank.py` で `ANTHROPIC_API_KEY` がある場合の LLM rerank を実装。失敗時は deterministic score に fallback。

### M4（完了）— タイトル / サムネ文言の自動生成
- `src/clipgen/titles.py` で short/long 別のタイトル案とサムネ文言案を生成。
- `CLIPGEN_AGGRESSIVENESS=0..3` と `--aggressiveness` で煽り強度を制御。
- `defamation_review_required` や `manual_review` の候補には `[REVIEW]` を付与し、公開前チェックを促す。

### M5（完了）— 動画切り出しコマンド出力
- `src/clipgen/clip_extract.py` で `yt-dlp` / `ffmpeg` の download / cut / concat コマンドを生成。
- `clipgen extract --plan ...` で `download.sh`、`cut.sh`、`combine.sh`、`manifest.json` を出力できる。
- Windows でも生成コマンドの shell quoting が安定するよう、Path は POSIX 形式で quote する。

### M6（完了）— LLM 連携によるタイトル品質改善
- `src/clipgen/llm.py` で Claude API によるタイトル polish を実装。
- `--polish` 指定時に `ANTHROPIC_API_KEY` がない場合は警告を出して deterministic タイトルで継続する。

### M7-M12（初期実装済み）— 運用補助
- `clipgen config-check`: 環境変数、allow/blocklist、seed、出力先、job dir を検証。
- `clipgen compliance-check`: takedown list による再フィルタ。
- `clipgen review`: candidates/plans を JSON/TSV に変換し、review_required を明示。
- `clipgen digest`: plans から日次 digest を生成し、Slack webhook 投稿に対応。
- `clipgen run-job`: candidates、plans、extract manifests、review reports、digest を short/long 両方で生成する日次ジョブへ拡張済み。live 本番実行には `YOUTUBE_API_KEY` が必要。

### 実動画ワークフロー試験
- `output/test_a1/RUNBOOK.md`: ローカルで動画+字幕を取得し、Codespace で切り出し・連結する方式。
- `output/test_b1/RUNBOOK.md`: `cookies.txt` を Codespace に渡して、ロング/ショート両方の `combined.mp4` を生成する方式。
- `docs/clipgen/b1_validation_2026-05-17.md`: B1 preflight 結果。現 workspace では `cookies.txt` / `source.mp4` / `source.ja.srt` が未配置のため mp4 生成は未実行。

---

## 4. 短尺 / 長尺の両対応設計

| 観点 | ショート（≤60s） | ロング（≥8min） |
|---|---|---|
| ハイライト窓 | 1 区間 / 30〜60 秒 | 3〜5 区間 / 各 60〜180 秒、合計 8〜12 分 |
| タイトル長 | 25 字以内推奨、煽り重め | 35〜45 字、解説寄り |
| サムネ文言 | 2 行 / 各 12 字以内 | 2 行 / 各 18 字以内 |
| スコアの duration_fit | `duration_sec ≤ 60` で +0.1 | `60 < duration_sec ≤ 600` で +0.05、`>600` は 0、極端な長尺は減点 |
| 候補化条件 | 短尺ソースを優先 | 公式中継・記者会見・討論会のような長尺ソースを優先 |
| 出力ファイル | `output/candidates_short.json` | `output/candidates_long.json` |

ロング対応のため、現行 `scoring.py` の `duration_fit` を `target_format` 引数で切り替える形に拡張する。

---

## 5. 成功指標

- M3 完了時点で、自民党公式・国会公式の上位 10 動画から **手動編集 30 分以内でショート 1 本** が作れる状態
- M4 完了時点で、タイトル/サムネ案を見て編集者が **3 分以内で 1 案決定** できる
- v0.1 を 2026-05-31 までに完成、ベータ運用開始

---

## 6. 進め方ルール

1. 各マイルストーン完了で `pytest tests/clipgen/` 全 PASS
2. 完了ごとに **codex レビュー**（[[feedback-codex-review]]）、○ になるまで反復
3. 大きな設計判断（外部 API 追加、データスキーマ破壊変更）はユーザーに確認
4. 自明な前進はオートモードで進める（[[feedback-auto-mode]]）

---

## 7. リスクと対応

| リスク | 対応 |
|---|---|
| YouTube Data API quota（10,000 unit/日） | search.list は 100 unit/req、videos.list 1 unit/req。1 日 1 回 30 クエリ程度に絞る |
| 字幕がない動画 | M3 は SRT 必須、ない場合は外部 ASR（Whisper）を将来 M5 で導入。M3 では「字幕なし」を `manual_review` フラグで返す |
| 政治的偏向の自動増幅 | M4 で煽り強度上限を環境変数 `CLIPGEN_AGGRESSIVENESS=0..3` で制御 |
| 名誉毀損訴訟リスク | `defamation_review_required` の候補は出力時に強調表示、最終公開判断は編集者 |

Related: [[trend-analysis]] [[feedback-codex-review]] [[feedback-auto-mode]]
