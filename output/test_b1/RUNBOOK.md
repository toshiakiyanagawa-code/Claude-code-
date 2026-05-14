# テスト版 B1 実行手順書 (竹田恒泰チャンネル2 / AHqwNShdSGI)

**素材**: 「日本が危ない！中国の『AI世論操作』はここまで進んでいる！…」 (2026-05-08 公開)
**チャンネル**: 竹田恒泰チャンネル2 (https://www.youtube.com/@takeda-tsuneyasu)
**生成対象**: ロング (≥8 分) + ショート (≤60 秒) の両方

## ステップ 1 — ローカル PC で素材を取得

ローカル PC (Mac / Windows) で以下を実行。

### 前提

- `yt-dlp` インストール済み (`brew install yt-dlp` または `pip install -U yt-dlp`)
- Chrome に YouTube ログイン済み (cookies 経由で取得)
- このリポジトリを clone 済み、`feat/clipgen-m0-m4` ブランチをチェックアウト済み

### 実行

```bash
cd <リポジトリのパス>
bash output/test_b1/local_fetch.sh
```

成功すると `output/test_b1/extract/AHqwNShdSGI_long/` に以下が落ちます:

- `source.mp4` (720p MP4、推定 200MB 以上)
- `source.ja.srt` (自動生成字幕、SRT 形式)

### よくある失敗

- **`Sign in to confirm you're not a bot`** → Chrome に YouTube ログインしているか確認。`--cookies-from-browser firefox` に変えてもよい
- **mp4 が 100MB 未満** → ネットワーク中断の可能性。再実行(冪等)
- **`.srt` が出ない** → 元動画に自動字幕がついていない可能性。codespace_run.sh は字幕なしでも走るがハイライト 0 件になる

## ステップ 2 — Codespace へ 2 ファイルをアップロード

VS Code で当該 Codespace を開く。アップロード先ディレクトリが無ければ事前作成:

```bash
mkdir -p output/test_b1/extract/AHqwNShdSGI_long
```

その後、エクスプローラの `output/test_b1/extract/AHqwNShdSGI_long/` にドラッグ&ドロップ:

1. `source.mp4`
2. `source.ja.srt`

> 大容量ファイル (>100MB) のアップロードは数分かかります。

## ステップ 3 — Codespace で事前検証

```bash
cd /workspaces/Claude-code-
source .venv/bin/activate
PYTHONPATH=src python -m clipgen.cli config-check \
  --job-dir output/test_b1/extract/AHqwNShdSGI_long
```

期待される出力:

```
情報: ffmpeg バージョン: ffmpeg version ...
情報: yt-dlp バージョン: ...
情報: source.mp4 を確認しました: ... (XXX.XMB)
情報: source.ja.srt を解析しました: NNN 区間
```

エラーが出たらアップロードし直し。

## ステップ 4 — Codespace で本処理 (long + short 両方を生成)

```bash
bash output/test_b1/codespace_run.sh
```

中で以下が走ります:

- `[long 1/4]` plan_long.json 生成 (字幕からハイライト検出、≥8 分用)
- `[long 2/4]` extract 設定生成 (cut.sh, combine.sh, concat.txt)
- `[long 3/4]` ffmpeg でハイライト窓を切り出し → parts/*.mp4
- `[long 4/4]` 切り出し片を連結 → `combined.mp4` (ロング版)
- `[short 1/4]` plan_short.json 生成 (短尺向けハイライト検出、≤60 秒用)
- `[short 2/4-4/4]` 同様の流れでショート版 combined.mp4 を生成

冪等性: 既存の生成物が plan/SRT より新しければスキップします。再実行で壊れません。

## ステップ 5 — 完成物の確認

```
output/test_b1/extract/AHqwNShdSGI_long/combined.mp4   (ロング)
output/test_b1/extract/AHqwNShdSGI_short/combined.mp4  (ショート)
```

`scp` または VS Code 経由でローカルに落とせます。

タイトル候補は標準出力に表示されますし、`plan_long.json` / `plan_short.json` の `plans[0].title_candidates` にも残っています。

## 編集者向けチェックリスト (公開前)

- [ ] combined.mp4 (long) を再生し、誤情報・誤解を招く繋ぎになっていないか確認
- [ ] combined.mp4 (short) も同様に確認
- [ ] タイトル案から 1 つ選ぶ (`[REVIEW]` プレフィックスは内部マーカー、公開時は外す)
- [ ] サムネは別途作成 (plan.json の `thumbnail_candidates` は文言案のみ)
- [ ] 竹田恒泰チャンネル2 概要欄の切り抜き許諾ポリシーを再確認
- [ ] 投稿先チャンネル (CH-1 / CH-2 / CH-3) を決定
- [ ] 引用要件遵守 (出典明示、必要最小限、主従関係、改変なし)

## 完全に作り直したい場合

```bash
# 字幕→plan→extract→cut→combine 全部やり直す
rm -f output/test_b1/plan_long.json output/test_b1/plan_short.json
rm -rf output/test_b1/extract/AHqwNShdSGI_long/parts
rm -rf output/test_b1/extract/AHqwNShdSGI_short
rm -f output/test_b1/extract/AHqwNShdSGI_long/combined.mp4 \
      output/test_b1/extract/AHqwNShdSGI_long/cut.sh \
      output/test_b1/extract/AHqwNShdSGI_long/combine.sh \
      output/test_b1/extract/AHqwNShdSGI_long/concat.txt \
      output/test_b1/extract/AHqwNShdSGI_long/manifest.json
bash output/test_b1/codespace_run.sh
```

source.mp4 / source.ja.srt は残します(ダウンロードし直さない)。
