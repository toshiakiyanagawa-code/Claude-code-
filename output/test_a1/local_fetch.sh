#!/usr/bin/env bash
# ローカル PC で 1 回だけ実行するスクリプト。
# 動画 (720p MP4) と 自動字幕 (ja → SRT) を同時取得します。
# Codespace IP は YouTube に block されるため、Codespace では動きません。
set -euo pipefail
OUT_DIR="output/test_a1/extract/HYUe6mRpwVs_long"
mkdir -p "${OUT_DIR}"
yt-dlp \
  --cookies-from-browser chrome \
  -f "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[height<=720]" \
  --merge-output-format mp4 \
  --write-auto-subs --sub-lang ja --convert-subs srt \
  -o "${OUT_DIR}/source.%(ext)s" \
  "https://www.youtube.com/watch?v=HYUe6mRpwVs"
echo
echo "完了: ${OUT_DIR}/source.mp4 / source.ja.srt"
echo "VS Code の Codespace にドラッグ&ドロップしてアップロードしてください。"
