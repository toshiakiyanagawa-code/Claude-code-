#!/usr/bin/env bash
set -euo pipefail
# ローカル PC で実行するスクリプト。
# 動画 (720p MP4) と自動字幕 (ja → SRT) を取得します。
# Codespace IP は YouTube に block されるため、Codespace では動きません。

OUT_DIR="output/test_a1/extract/HYUe6mRpwVs_long"
VIDEO_URL="https://www.youtube.com/watch?v=HYUe6mRpwVs"
SOURCE_MP4="${OUT_DIR}/source.mp4"
SOURCE_SRT="${OUT_DIR}/source.ja.srt"
MIN_MP4_BYTES=$((100 * 1024 * 1024))  # 100MB: 720p で約 66 分なら 400MB 超のはず

mkdir -p "${OUT_DIR}"

mp4_ok() {
  [ -f "${SOURCE_MP4}" ] && [ "$(wc -c < "${SOURCE_MP4}")" -gt "${MIN_MP4_BYTES}" ]
}
srt_ok() {
  [ -s "${SOURCE_SRT}" ]
}

if mp4_ok && srt_ok; then
  echo "SKIP: 既に取得済みです: ${SOURCE_MP4} ($(wc -c < "${SOURCE_MP4}") bytes) / ${SOURCE_SRT}"
  echo "VS Code の Codespace に source.mp4 / source.ja.srt をドラッグ&ドロップしてください。"
  exit 0
fi

if [ -f "${SOURCE_MP4}" ] && ! mp4_ok; then
  echo "INFO: ${SOURCE_MP4} がサイズ不足 (要 100MB 以上)。再取得します。" >&2
  rm -f "${SOURCE_MP4}"
fi

yt-dlp \
  --cookies-from-browser chrome \
  -f "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[height<=720]" \
  --merge-output-format mp4 \
  --write-auto-subs --sub-lang ja --convert-subs srt \
  -o "${OUT_DIR}/source.%(ext)s" \
  "${VIDEO_URL}"

echo

if ! mp4_ok; then
  echo "ERROR: ${SOURCE_MP4} が作成されていないかサイズ不足 (要 100MB 以上)。" >&2
  exit 1
fi

if ! srt_ok; then
  echo "WARN: ${SOURCE_SRT} が見つかりません。自動字幕が取得できていない可能性があります。" >&2
else
  echo "完了: ${SOURCE_MP4} ($(wc -c < "${SOURCE_MP4}") bytes) / ${SOURCE_SRT}"
fi

echo "VS Code の Codespace に source.mp4 / source.ja.srt をドラッグ&ドロップしてアップロードしてください。"
