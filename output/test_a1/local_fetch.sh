#!/usr/bin/env bash
set -euo pipefail

mkdir -p output/test_a1/extract/HYUe6mRpwVs_long

yt-dlp \
  --cookies-from-browser chrome \
  --no-write-subs \
  --no-write-auto-subs \
  -f "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[height<=720]" \
  --merge-output-format mp4 \
  -o "output/test_a1/extract/HYUe6mRpwVs_long/source.%(ext)s" \
  "https://www.youtube.com/watch?v=HYUe6mRpwVs"
