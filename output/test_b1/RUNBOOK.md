# テスト版 B1 実行手順書 (竹田恒泰チャンネル2 / AHqwNShdSGI)

**素材**: 「日本が危ない！中国の『AI世論操作』はここまで進んでいる！…」 (2026-05-08 公開)
**チャンネル**: 竹田恒泰チャンネル2 (https://www.youtube.com/channel/UCTxDz8sXbnpYAfulQMRFNEQ)
**生成対象**: ロング (≥8 分) + ショート (≤60 秒) の両方
**推奨ルート**: cookies.txt を Codespace にアップロード → Codespace 側で全部生成

## 推奨ルート: cookies.txt 方式 (ローカル作業は cookies 取得のみ)

Codespace の IP は YouTube から bot 判定されるため、Codespace で yt-dlp を走らせるには
ローカル Chrome の cookies が必要です。cookies.txt (数KB) だけアップロードすれば、
200MB の mp4 をローカルで落とす必要がなくなります。

### ステップ 1 — ローカル PC で cookies.txt を取得

Chrome に拡張機能 **「Get cookies.txt LOCALLY」** をインストール (Chrome ウェブストア):
https://chromewebstore.google.com/search/Get%20cookies.txt%20LOCALLY

1. Chrome で https://www.youtube.com を開く (ログイン状態を確認)
2. 拡張機能アイコンをクリック → 「Export」ボタン → `cookies.txt` がダウンロードされる

> 代替: `yt-dlp --cookies-from-browser chrome --cookies /tmp/cookies.txt --skip-download --print '' "https://www.youtube.com"` でも同等のファイルが取得できます。

### ステップ 2 — VS Code から cookies.txt を Codespace にアップロード

VS Code の左サイドバー (エクスプローラ) で `output/test_b1/` ディレクトリを開き、
`cookies.txt` を**そこにドラッグ&ドロップ**。

- アップロード先: `output/test_b1/cookies.txt`
- 通常は数 KB なのですぐ終わる
- `.gitignore` で commit されないようにしてあります(誤って public 化されない)

### ステップ 3 — Codespace で一括実行

```bash
cd /workspaces/Claude-code-
bash output/test_b1/codespace_run.sh
```

中で以下が走ります:

- `[0/5]` yt-dlp で `source.mp4` + `source.ja.srt` をダウンロード (cookies 使用、数分)
- `[long 1/4]` plan_long.json 生成 (字幕からハイライト検出、≥8 分用)
- `[long 2/4]` extract 設定生成 (cut.sh, combine.sh, concat.txt)
- `[long 3/4]` ffmpeg でハイライト窓を切り出し → parts/*.mp4
- `[long 4/4]` 切り出し片を連結 → `combined.mp4` (ロング版)
- `[short 1/4-4/4]` ショート版を同じ流れで生成

完成物:

```
output/test_b1/extract/AHqwNShdSGI_long/combined.mp4   (ロング)
output/test_b1/extract/AHqwNShdSGI_short/combined.mp4  (ショート)
```

冪等性: 既存の生成物が plan/SRT より新しければスキップ。再実行で壊れません。

### ステップ 4 — 完成物のダウンロード

VS Code エクスプローラで `combined.mp4` を右クリック → Download。

タイトル候補は標準出力に表示され、`plan_long.json` / `plan_short.json` の
`plans[0].title_candidates` にも残っています。

## よくある失敗と対処

- **`ERROR: output/test_b1/cookies.txt が見つかりません`** → ステップ 2 を完了
- **`Sign in to confirm you're not a bot`** → cookies.txt が古い・ログアウト状態で取った可能性。Chrome に YouTube ログインしているか確認して再取得
- **`source.mp4 が小さすぎます (要 100MB 以上)`** → ネットワーク中断。再実行(冪等)
- **`source.ja.srt` がない** → 元動画に自動字幕がついていない。長さ 0 件になるので別動画推奨

## 代替ルート: 完全ローカル方式 (cookies アップロードしたくない場合)

ローカル PC で動画+字幕を取得し、両ファイルを Codespace にアップロードする方式:

```bash
# ローカル PC で実行
bash output/test_b1/local_fetch.sh
# → source.mp4 (200MB+) と source.ja.srt が落ちる
```

その後、VS Code で `output/test_b1/extract/AHqwNShdSGI_long/` に source.mp4 と
source.ja.srt をドラッグ&ドロップしてから、Codespace で `codespace_run.sh` を実行。
cookies.txt が無くても source.mp4 が既にあれば yt-dlp ステップはスキップされます。

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
rm -rf output/test_b1/extract output/test_b1/plan_*.json
bash output/test_b1/codespace_run.sh
```

cookies.txt は残ります(ダウンロードし直すだけ)。
