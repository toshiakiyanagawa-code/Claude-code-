#!/usr/bin/env bash
set -euo pipefail
# Codespace 内で実行するスクリプト。
# 先にローカル PC で local_fetch.sh を実行し、source.mp4 / source.ja.srt を
# 同じ場所 (output/test_a1/extract/HYUe6mRpwVs_long/) へアップロードしてください。

PLAN_JSON="output/test_a1/plan.json"
SOURCE_MOCK="output/test_a1/source_mock.json"
OUT_ROOT="output/test_a1/extract"
EXTRACT_DIR="${OUT_ROOT}/HYUe6mRpwVs_long"
SRT_PATH="${EXTRACT_DIR}/source.ja.srt"
SOURCE_MP4="${EXTRACT_DIR}/source.mp4"
COMBINED_MP4="${EXTRACT_DIR}/combined.mp4"
MIN_MP4_BYTES=$((100 * 1024 * 1024))

# ── helpers ──────────────────────────────────────────────────────────────
mp4_bytes() {
  [ -f "$1" ] && wc -c < "$1" || echo 0
}

newer_than() {
  # newer_than A B : A が B より新しければ 0、それ以外 1
  [ -f "$1" ] || return 1
  [ -f "$2" ] || return 0
  [ "$1" -nt "$2" ]
}

parts_all_present() {
  # concat.txt の全行 (file '...') を読み、全部 size > 0 で存在するか
  local concat="${EXTRACT_DIR}/concat.txt"
  [ -s "${concat}" ] || return 1
  while IFS= read -r line; do
    [[ "${line}" =~ ^file[[:space:]]+\'(.+)\'$ ]] || continue
    local p="${BASH_REMATCH[1]}"
    [[ "${p}" = /* ]] || p="${EXTRACT_DIR}/${p}"
    [ -s "${p}" ] || return 1
  done < "${concat}"
  return 0
}

# ── pre-check ────────────────────────────────────────────────────────────
if [ ! -f "${SOURCE_MP4}" ] || [ "$(mp4_bytes "${SOURCE_MP4}")" -lt "${MIN_MP4_BYTES}" ]; then
  echo "ERROR: ${SOURCE_MP4} が見つからないかサイズが小さすぎます (要 100MB 以上)。" >&2
  echo "ローカルで local_fetch.sh 実行後、source.mp4 / source.ja.srt を" >&2
  echo "VS Code にドラッグ&ドロップで Codespace の同じパスに置いてください。" >&2
  exit 1
fi

SRT_ARG=()
if [ -s "${SRT_PATH}" ]; then
  SRT_ARG=(--srt "${SRT_PATH}")
else
  echo "WARN: ${SRT_PATH} なし。字幕なしで進めますがハイライト 0 件になります。" >&2
fi

if [ ! -f ".venv/bin/activate" ]; then
  echo "ERROR: .venv/bin/activate が見つかりません。先に依存関係をセットアップしてください。" >&2
  exit 1
fi

source .venv/bin/activate

# ── [1/4] plan ───────────────────────────────────────────────────────────
plan_needs_regen=1
if [ -s "${PLAN_JSON}" ]; then
  plan_needs_regen=0
  newer_than "${SRT_PATH}" "${PLAN_JSON}" && plan_needs_regen=1
  newer_than "${SOURCE_MOCK}" "${PLAN_JSON}" && plan_needs_regen=1
fi

if [ "${plan_needs_regen}" = "1" ]; then
  echo "[1/4] plan.json を生成します..."
  PYTHONPATH=src python -m clipgen.cli plan \
    --source mock --mock "${SOURCE_MOCK}" \
    --format long --top 1 --include-blocked \
    --aggressiveness 2 \
    "${SRT_ARG[@]}" \
    --out "${PLAN_JSON}" \
    --now 2026-05-13T12:00:00+00:00
  # plan 更新後は後続成果物も無効化
  rm -f "${EXTRACT_DIR}/manifest.json" "${EXTRACT_DIR}/cut.sh" "${EXTRACT_DIR}/combine.sh" "${EXTRACT_DIR}/concat.txt" "${COMBINED_MP4}"
  rm -rf "${EXTRACT_DIR}/parts"
else
  echo "[1/4] SKIP: 既存の plan.json を使います (SRT/mock より新しい): ${PLAN_JSON}"
fi

# ── [2/4] extract 設定 ───────────────────────────────────────────────────
extract_needs_regen=1
manifest="${EXTRACT_DIR}/manifest.json"
if [ -s "${manifest}" ] && [ -s "${EXTRACT_DIR}/cut.sh" ] && [ -s "${EXTRACT_DIR}/combine.sh" ] && [ -s "${EXTRACT_DIR}/concat.txt" ]; then
  extract_needs_regen=0
  newer_than "${PLAN_JSON}" "${manifest}" && extract_needs_regen=1
fi

if [ "${extract_needs_regen}" = "1" ]; then
  echo "[2/4] extract 設定を生成します..."
  PYTHONPATH=src python -m clipgen.cli extract \
    --plan "${PLAN_JSON}" --out-root "${OUT_ROOT}" --top 1
  rm -rf "${EXTRACT_DIR}/parts" "${COMBINED_MP4}"
else
  echo "[2/4] SKIP: 既存の extract 設定を使います (plan より新しい): ${manifest}"
fi

# ── [3/4] cut (parts/) ───────────────────────────────────────────────────
if [ -s "${EXTRACT_DIR}/cut.sh" ]; then
  if parts_all_present; then
    echo "[3/4] SKIP: parts は concat.txt の全ファイルが揃っています。"
  else
    echo "[3/4] ffmpeg で切り出します..."
    rm -rf "${EXTRACT_DIR}/parts"
    mkdir -p "${EXTRACT_DIR}/parts"
    (cd "${EXTRACT_DIR}" && bash cut.sh)
    rm -f "${COMBINED_MP4}"
  fi
else
  echo "[3/4] cut.sh が空または未生成です。ハイライト検出 0 件の可能性があります。" >&2
fi

# ── [4/4] combine ────────────────────────────────────────────────────────
if [ -s "${EXTRACT_DIR}/combine.sh" ]; then
  combined_needs_regen=1
  if [ -s "${COMBINED_MP4}" ] && parts_all_present; then
    combined_needs_regen=0
    # combined.mp4 が concat.txt または cut.sh より古ければ再生成
    newer_than "${EXTRACT_DIR}/concat.txt" "${COMBINED_MP4}" && combined_needs_regen=1
    newer_than "${EXTRACT_DIR}/cut.sh" "${COMBINED_MP4}" && combined_needs_regen=1
  fi
  if [ "${combined_needs_regen}" = "1" ]; then
    echo "[4/4] 連結します..."
    (cd "${EXTRACT_DIR}" && bash combine.sh)
    echo "完成: ${COMBINED_MP4}"
  else
    echo "[4/4] SKIP: 連結済み動画は parts/concat より新しいので再利用します: ${COMBINED_MP4}"
  fi
else
  echo "[4/4] combine.sh がありません。連結対象がない可能性があります。" >&2
fi

echo
echo "--- タイトル候補 ---"
python - "${PLAN_JSON}" <<'PY'
import json
import sys

path = sys.argv[1]
with open(path, encoding="utf-8") as f:
    payload = json.load(f)

plans = payload.get("plans", [])
if not plans:
    print("  plan が空です。")
    raise SystemExit(0)

p = plans[0]
print(f"  highlight_status: {p['highlight_status']}")
print(f"  highlights: {len(p['highlights'])} 区間")
for t in p["title_candidates"]:
    print(f"  - {t['text']}")
PY
