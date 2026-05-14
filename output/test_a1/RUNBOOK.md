# test_a1 RUNBOOK

この手順は、YouTube 取得だけをローカル PC で行い、切り出しと連結を Codespace で行うためのものです。Codespace から YouTube に直接取りに行くと IP block されることがあるため、`source.mp4` と `source.ja.srt` はローカルで作ってアップロードします。

## 1. ローカル PC で素材を取得する

ローカル PC のリポジトリ直下で実行します。Chrome の YouTube ログイン Cookie を使うため、Chrome にログイン済みの状態で実行してください。

```bash
bash output/test_a1/local_fetch.sh
```

成功すると次の 2 ファイルができます。

```text
output/test_a1/extract/HYUe6mRpwVs_long/source.mp4
output/test_a1/extract/HYUe6mRpwVs_long/source.ja.srt
```

既に 2 ファイルがある場合、スクリプトは取得をスキップします。

## 2. Codespace にアップロードする

VS Code のファイルツリーで、Codespace 側の次のディレクトリを開きます。

```text
output/test_a1/extract/HYUe6mRpwVs_long/
```

ローカル PC で作った `source.mp4` と `source.ja.srt` を、このディレクトリへドラッグ&ドロップします。

## 3. Codespace で事前チェックする

Codespace のリポジトリ直下で実行します。

```bash
source .venv/bin/activate
PYTHONPATH=src python -m clipgen.cli config-check --job-dir output/test_a1/extract/HYUe6mRpwVs_long
```

`python`、`ffmpeg`、`yt-dlp` のバージョン、`source.mp4` のサイズ、`source.ja.srt` の SRT 解析結果が表示されます。`エラー:` が出た場合は、その内容を直してから次へ進んでください。

## 4. Codespace で plan / extract / cut / combine を実行する

```bash
bash output/test_a1/codespace_run.sh
```

スクリプトは既存の中間ファイルを見て、済んでいる工程をスキップします。

主な生成物は次の通りです。

```text
output/test_a1/plan.json
output/test_a1/extract/HYUe6mRpwVs_long/manifest.json
output/test_a1/extract/HYUe6mRpwVs_long/parts/
output/test_a1/extract/HYUe6mRpwVs_long/combined.mp4
```

## 5. 再実行したい場合

そのまま再実行すると、存在する中間ファイルはスキップされます。

plan から作り直す場合:

```bash
rm -f output/test_a1/plan.json
bash output/test_a1/codespace_run.sh
```

extract 設定から作り直す場合:

```bash
rm -f output/test_a1/extract/HYUe6mRpwVs_long/manifest.json
rm -f output/test_a1/extract/HYUe6mRpwVs_long/cut.sh
rm -f output/test_a1/extract/HYUe6mRpwVs_long/combine.sh
rm -f output/test_a1/extract/HYUe6mRpwVs_long/concat.txt
bash output/test_a1/codespace_run.sh
```

切り出しと連結を作り直す場合:

```bash
rm -rf output/test_a1/extract/HYUe6mRpwVs_long/parts
rm -f output/test_a1/extract/HYUe6mRpwVs_long/combined.mp4
bash output/test_a1/codespace_run.sh
```
