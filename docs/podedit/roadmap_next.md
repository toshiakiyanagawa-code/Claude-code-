# podedit 次フェーズ設計: W14 / W14' / Tier4

## 前提

podedit は faster-whisper の word-level timestamps を持つ transcript JSON を編集単位の中核に置き、日本語会話品質を差別化要素として強化する。次フェーズでは W14 の相槌/フィラー分類、W14' の自動ハイライト抽出、Tier4 の経済系 YouTube 転用を、既存の編集 UI と export パイプラインに最小追加で接続する。

## W14: 相槌 / フィラー分類

### 目的

日本語 podcast に多い「えー」「あの」「うん」などを transcript 上で自動マークし、編集者が 1 ボタンで削除候補へ追加できる状態にする。削除の自動確定ではなく、会話の自然さを壊さない候補提示を MVP とする。

### 技術スタック案

- MVP は deterministic heuristics + 小さな辞書で開始し、LLM は曖昧ケースの batch 再分類に限定する。
- 入力は faster-whisper の word-level timestamps、出力は word/span に `tag: filler|aizuchi` と `confidence` を付ける。
- 辞書は日本語表記ゆれを正規化し、「えー」「えっと」「あの」「まあ」「うん」「はい」などをカテゴリ別に管理する。
- 軽量モデル化は、編集者の accept/reject ログが溜まってから検討する。

### データフロー

transcript JSON を読み込み、word 正規化、辞書一致、前後文脈による除外判定、span 結合、候補 annotation 生成の順に処理する。UI は annotation layer を transcript view に重ね、`Add all fillers to delete candidates` が delete list に span を一括追加する。

### MVP範囲

- word-level timestamp 上の filler/aizuchi 候補検出。
- transcript 上の視覚マークとフィルタ表示。
- 全フィラーを削除候補に追加する 1 ボタン。
- 誤検出を戻せる accept/reject 操作とログ保存。

### 将来拡張

LLM による「相槌だが残すべき同意」「意味を持つ返答」の判定、話者別の癖学習、番組ごとの辞書チューニング、編集ログからの軽量分類モデル学習を追加する。

### リスク

「うん」「はい」は意味のある応答にもなるため、単語一致だけで削除すると会話の流れを破壊する。MVP では confidence が低い候補を一括削除対象から外し、必ず undo 可能にする。

## W14': 自動ハイライト抽出

### 目的

30 分 podcast から 30-90 秒の「面白そうな 3-5 か所」を自動抽出し、編集 UI 上に marker として表示する。編集者が聴く場所を絞れることを主目的にし、自動切り抜き確定は MVP に含めない。

### 技術スタック案

- clipgen の `highlights.py` の窓検出ロジックを再利用し、podedit transcript JSON 用 adapter を追加する。
- SRT/WebVTT ではなく word/segment timestamp から疑似 cue 列を生成して既存 scoring に渡す。
- スコア要素は発話密度、キーワード、感情語、固有名詞、章境界、無音長を最小セットにする。
- LLM 要約は候補上位の title/reason 生成に限定し、抽出本体はローカル処理を優先する。

### データフロー

podedit transcript JSON から segment/cue 配列を生成し、`highlights.py` 相当の scoring で short/long window を抽出する。上位候補を `{start,end,score,reason,type:"highlight"}` として annotation layer に保存し、timeline と transcript に marker 表示する。

### MVP範囲

- 30-90 秒 window の上位 3-5 件抽出。
- timeline marker、transcript marker、候補リスト表示。
- marker の採用、却下、範囲調整。
- export JSON に highlight annotation を含める。

### 将来拡張

番組ジャンル別 scoring、SNS 用 short window と YouTube 用 long window の同時生成、LLM による hook 文生成、再生維持率データを使った scoring 改善を追加する。

### リスク

clipgen 側の SRT/WebVTT 前提が強い場合、word timestamp からの cue 変換で意味単位が崩れる。adapter では pause と punctuation を使って segment を再構成し、元 transcript への参照 ID を保持する。

## Tier4: 経済系AI解説 YouTube 転用

### 目的

podedit を、公的 IR、決算会見、政策会見などの素材音声から経済系 AI 解説 YouTube を量産する前処理ツールへ拡張する。音声編集後の成果物に、AI ナレーション差し込みポイント、サムネ案、Markdown スクリプト、YouTube 説明文を追加する。

### 技術スタック案

- podedit 本体には domain preset と export template を追加し、重い生成処理は別 job として分離する。
- transcript annotation に `important`, `cut`, `move`, `narration_insert`, `source_quote` を追加する。
- LLM は編集済み transcript と highlight annotation から script/description/thumbnail ideas を生成する。
- 経済用語辞書、企業名、政策キーワードは scoring と script 生成の補助データとして扱う。

### データフロー

素材音声をロードし、transcript 生成、重要箇所 marker、削除/移動編集、ナレーション差し込み点 annotation の順に編集する。export 時に edited timeline と annotation を生成 job に渡し、Markdown スクリプト、YouTube 説明文、サムネ案、引用元メモを bundle 出力する。

### MVP範囲

- `economy-youtube` preset の追加。
- 重要箇所、引用、ナレーション差し込み点の annotation type 追加。
- 編集後 transcript から Markdown script と YouTube description を生成する export。
- サムネ案はテキスト案 3 件までに限定する。

### 将来拡張

3ch 量産向けに channel persona、語尾、尺、禁止表現、サムネ文体を preset 化する。IR PDF、決算短信、政策資料を transcript と一緒に RAG 参照し、ファクトチェックと引用リンク生成を追加する。

### リスク

経済解説は誤情報、投資助言、著作権、引用範囲のリスクが高い。MVP では出典 annotation と引用元メモを必須化し、生成物には断定表現を抑える review step を入れる。

## 実装順序

1. annotation schema を拡張し、filler/highlight/narration_insert を同じ layer で扱えるようにする。
2. W14 の heuristics classifier と UI 一括追加ボタンを実装する。
3. W14' の transcript adapter と highlight marker 表示を実装する。
4. `economy-youtube` preset と Markdown/description export を追加する。
5. accept/reject ログを保存し、辞書と scoring の改善に使う。

## 成功条件

編集者が 30 分音声を開いた直後に、フィラー削除候補と 3-5 個のハイライト候補を確認できる。経済系素材では、編集後に YouTube 制作用の Markdown script、説明文、サムネ案まで 1 回の export で得られる。

## codex 全体コードレビュー (2026-05-13) からの追加課題

| 優先 | 課題 |
|---|---|
| 1 | `server/app.py` が 1483 行に肥大、routes / services / upload / export / static に分割推奨 |
| 2 | chunk upload + filesystem picker のパストラバーサル / 上書き / 拡張子偽装 / 巨大ファイル / 同名競合 防御の重点確認 |
| 3 | ASR background job のキャンセル/失敗/再実行/同時実行/モデルキャッシュ破棄/進捗不整合テスト |
| 4 | render/export の ffmpeg 依存、LUFS two-pass、mp3/flac、crossfade/zero-cross の失敗契約化 |
| 5 | autosave/undo/redo/move/delete/selection clamp のシナリオテスト |

### テスト薄い領域 (追加)

- `server/app.py` HTTP API、静的キャッシュヘッダ、アップロード 3 段階、Export、Transcribe status の E2E
- ブラウザ UI の drag-to-move、preview-skip、Shift+Arrow、copy transcript、episode switch (Playwright 相当)
- `library.py` の root 外参照、相対パス、Unicode ファイル名、重複名、権限エラー、破損音声
- `asr.py` の tiny/small/quality preset、JA prompt bias、tri-state API、CPU fallback の回帰検知

### ドキュメント不足

- ffmpeg 必須バージョン、対応 OS、ASR モデル DL、初回起動時間、Codespaces 制約、保存場所
- 編集セッション形式、再現可能な export 条件、失敗時の復旧、アップロード上書き仕様
