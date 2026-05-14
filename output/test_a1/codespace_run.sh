#!/usr/bin/env bash
# Codespace 内で実行するスクリプト (ローカルで local_fetch.sh 実行 + アップロード後)
set -euo pipefail
PLAN_JSON="output/test_a1/plan.json"
SOURCE_MOCK="output/test_a1/source_mock.json"
OUT_ROOT="output/test_a1/extract"
EXTRACT_DIR="${OUT_ROOT}/HYUe6mRpwVs_long"
SRT_PATH="${EXTRACT_DIR}/source.ja.srt"
SOURCE_MP4="${EXTRACT_DIR}/source.mp4"

if [ ! -f "${SOURCE_MP4}" ]; then
  echo "ERROR: ${SOURCE_MP4} が見つかりません。ローカルで local_fetch.sh 実行後、" >&2
  echo "source.mp4 / source.ja.srt を VS Code にドラッグ&ドロップしてください。" >&2
  exit 1
fi

SRT_ARG=""
if [ -f "${SRT_PATH}" ]; then
  SRT_ARG="--srt ${SRT_PATH}"
else
  echo "WARN: ${SRT_PATH} なし。字幕なしで進めますがハイライト 0 件になります。" >&2
fi

source .venv/bin/activate
PYTHONPATH=src python -m clipgen.cli plan \
  --source mock --mock "${SOURCE_MOCK}" \
  --format long --top 1 --include-blocked \
  --aggressiveness 2 \
  ${SRT_ARG} \
  --out "${PLAN_JSON}" \
  --now 2026-05-13T12:00:00+00:00

# 既存の extract 生成物のみ削除 (source.mp4/srt はアップロード済みなので残す)
rm -f "${EXTRACT_DIR}/cut.sh" "${EXTRACT_DIR}/combine.sh" "${EXTRACT_DIR}/concat.txt" "${EXTRACT_DIR}/download.sh" "${EXTRACT_DIR}/manifest.json"
rm -rf "${EXTRACT_DIR}/parts"
PYTHONPATH=src python -m clipgen.cli extract \
  --plan "${PLAN_JSON}" --out-root "${OUT_ROOT}" --top 1

if [ -f "${EXTRACT_DIR}/cut.sh" ] && [ -s "${EXTRACT_DIR}/cut.sh" ]; then
  echo "[3/4] ffmpeg で切り出し..."
  (cd "${EXTRACT_DIR}" && bash cut.sh)
else
  echo "[3/4] cut.sh が空。ハイライト検出 0 件です。" >&2
fi

if [ -f "${EXTRACT_DIR}/combine.sh" ]; then
  echo "[4/4] 連結..."
  (cd "${EXTRACT_DIR}" && bash combine.sh)
  echo "完成: ${EXTRACT_DIR}/combined.mp4"
fi

echo
echo "--- タイトル候補 ---"
python -c "
import json
p = json.load(open('${PLAN_JSON}'))['plans'][0]
print(f\"  highlight_status: {p['highlight_status']}\")
print(f\"  highlights: {len(p['highlights'])} 区間\")
for t in p['title_candidates']:
    print(f\"  - {t['text']}\")
"
