# clipgen 開発計画書

最終更新: 2026-05-13
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

## 3. マイルストーン

### M0（完了）— トレンド分析 + 候補抽出 MVP
- [`docs/clipgen/trend_analysis.md`](trend_analysis.md)
- 許諾チェック（allow/blocklist、channel_id 優先）、スコアリング、CLI、mock データで E2E、tests 15 件 PASS、codex 6/7 ○

### M1（着手） — Live モードのドライラン
**ゴール:** YouTube Data API キーがない開発環境でも `--source live` 経路を fixture で疑似実行できる。
- `--dry-run` フラグで HTTP 呼び出しをスタブ化（fixture JSON を返す）
- `tests/clipgen/test_live_dryrun.py`: live 経路の主要関数が fixture で動くことを保証
- 既存 `run_pipeline_live` を分割し、HTTP 層を差し替え可能にする

### M2 — allowlist/blocklist の channel_id 自動解決
**ゴール:** `scripts/refresh_channel_ids.py` を実行すると、handles → 実 channel_id が API 経由で resolve され、JSON が in-place で更新される。
- `YOUTUBE_API_KEY` 必須
- 差分は `--diff` で確認、`--write` で書き戻し
- 既存の channel_id が plausible に見えない（`UC_` で始まらない or 24 文字でない）行を検出してログ

### M3 — ハイライト検出 + フォーマット振り分け（ショート/ロング両対応）
**ゴール:** 動画 URL（または字幕 SRT）から「切り抜きに使うべき区間」を出す。さらに **ショート / ロング** の 2 フォーマットそれぞれに合わせた window 提案を返す。

実装:
- `src/clipgen/highlights.py`
  - 入力: SRT テキスト or `WebVTT`
  - スコア: 感嘆符密度、否定語、笑い表現、「絶句/論破/失言」等のホットワード、話者交代、語彙変化
  - 出力: `Highlight(start_sec, end_sec, score, rationale, keywords)` のリスト
- **ショートフォーマット**: 単一 60 秒以下、最高スコアの 1 窓を取り、両側に 5 秒ずつパディング、最大 60 秒
- **ロングフォーマット**: 上位 N 窓を結合し、合計 8〜12 分。窓間のつなぎ説明文（解説テロップ）スロットを空欄で出力
- `--format short|long|both` を CLI と pipeline で受け取り、各 Candidate に推奨ハイライトを乗せる

### M4 — タイトル / サムネ文言の自動生成
**ゴール:** 各 Candidate × フォーマット × ハイライトに対して、**タイトル案 3〜5 / サムネ文言案 3〜5** を返す。

実装:
- `src/clipgen/titles.py`
  - テンプレ集（ショート / ロング 別、フォーマット種別 別: laugh / shock / debate / reveal）
  - 変数: 人物名、ハイライトのキーワード、対象（誰に向かって）
  - 例（ショート / shock）: `【{state}】{person}、{action}の瞬間` → `【絶句】高市早苗、記者の質問に一瞬黙る瞬間`
  - 例（ロング / debate）: `{person}の{topic}論破まとめ｜なぜ{opponent}は反論できなかったのか` 等
- 出力 JSON に `title_candidates: [{text, format, risk}]`、`thumbnail_candidates: [{line1, line2, style}]` を追加
- `defamation_review_required` フラグがある候補は **タイトル全部に `[REVIEW] ` プレフィックス**を付ける

### M5（将来） — 動画切り出しコマンド出力
- `yt-dlp` + `ffmpeg` のコマンド文字列を出力
- ライセンス再チェック CLI

### M6（将来） — LLM 連携によるタイトル品質改善
- Claude API or OpenAI で煽り強度をユーザー指定の上限で制御

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
