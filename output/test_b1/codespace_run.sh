#!/usr/bin/env bash
set -euo pipefail
# Codespace 内で全部走らせるスクリプト。yt-dlp で素材取得から combined.mp4 まで一気通貫。
# Codespace IP は YouTube に bot 判定されるので、cookies.txt が必須。
#
# cookies.txt の取得方法:
# 1) ローカル Chrome に "Get cookies.txt LOCALLY" 拡張を入れ、youtube.com で Export
# 2) または: yt-dlp --cookies-from-browser chrome --cookies cookies.txt --get-id
# 3) VS Code 経由で Codespace の output/test_b1/cookies.txt にドラッグ&ドロップ

SOURCE_MOCK="output/test_b1/source_mock.json"
OUT_ROOT="output/test_b1/extract"
VIDEO_ID="AHqwNShdSGI"
VIDEO_URL="https://www.youtube.com/watch?v=${VIDEO_ID}"
COOKIES="output/test_b1/cookies.txt"
LONG_DIR="${OUT_ROOT}/${VIDEO_ID}_long"
SHORT_DIR="${OUT_ROOT}/${VIDEO_ID}_short"
SOURCE_MP4="${LONG_DIR}/source.mp4"
SOURCE_SRT="${LONG_DIR}/source.ja.srt"
MIN_MP4_BYTES=$((10 * 1024 * 1024))
# 解説調・冷静な動画はクリックベイトキーワード少ない → 閾値 0、煽り素材は 0.3
MIN_HIGHLIGHT_SCORE="${MIN_HIGHLIGHT_SCORE:-0.0}"

mp4_bytes() {
  [ -f "$1" ] && wc -c < "$1" || echo 0
}

newer_than() {
  [ -f "$1" ] || return 1
  [ -f "$2" ] || return 0
  [ "$1" -nt "$2" ]
}

parts_all_present() {
  local extract_dir="$1"
  local concat="${extract_dir}/concat.txt"
  [ -s "${concat}" ] || return 1
  while IFS= read -r line; do
    if [[ "${line}" =~ ^file[[:space:]]+\'(.+)\'$ ]]; then
      local p="${BASH_REMATCH[1]}"
    elif [[ "${line}" =~ ^file[[:space:]]+(.+)$ ]]; then
      local p="${BASH_REMATCH[1]}"
    else
      continue
    fi
    [[ "${p}" = /* ]] || p="${extract_dir}/${p}"
    [ -s "${p}" ] || return 1
  done < "${concat}"
  return 0
}

# ── [0/5] cookies + yt-dlp で source.mp4 + source.ja.srt を取得 ──────────
mkdir -p "${LONG_DIR}"

if [ -f "${SOURCE_MP4}" ] && [ "$(mp4_bytes "${SOURCE_MP4}")" -ge "${MIN_MP4_BYTES}" ] && [ -s "${SOURCE_SRT}" ]; then
  echo "[0/5] SKIP: 既に source.mp4 ($(mp4_bytes "${SOURCE_MP4}") bytes) と source.ja.srt があります。"
else
  if [ ! -s "${COOKIES}" ]; then
    echo "ERROR: ${COOKIES} が見つかりません。" >&2
    echo "  Chrome に \"Get cookies.txt LOCALLY\" 拡張を入れて youtube.com で Export し、" >&2
    echo "  VS Code 経由で ${COOKIES} にドラッグ&ドロップしてください。" >&2
    exit 1
  fi
  chmod 600 "${COOKIES}" || true
  echo "[0/5] yt-dlp で素材取得 (cookies 使用)..."
  yt-dlp \
    --cookies "${COOKIES}" \
    -f "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[height<=720]" \
    --merge-output-format mp4 \
    --write-auto-subs --sub-lang ja --convert-subs srt \
    -o "${LONG_DIR}/source.%(ext)s" \
    "${VIDEO_URL}"

  if [ "$(mp4_bytes "${SOURCE_MP4}")" -lt "${MIN_MP4_BYTES}" ]; then
    echo "ERROR: ${SOURCE_MP4} が小さすぎます (要 100MB 以上)。" >&2
    exit 1
  fi
  if [ ! -s "${SOURCE_SRT}" ]; then
    echo "WARN: ${SOURCE_SRT} が見つかりません。自動字幕がついていない可能性があります。" >&2
  fi
fi

if [ ! -f ".venv/bin/activate" ]; then
  echo "ERROR: .venv/bin/activate が見つかりません。" >&2
  exit 1
fi
source .venv/bin/activate

# ── 1 フォーマット分のパイプラインを走らせる ────────────────────────────
run_format() {
  local fmt="$1"
  local plan_json="output/test_b1/plan_${fmt}.json"
  local extract_dir="${OUT_ROOT}/${VIDEO_ID}_${fmt}"
  local combined_mp4="${extract_dir}/combined.mp4"

  echo
  echo "============================================================"
  echo "[${fmt}] 開始"
  echo "============================================================"

  if [ "${fmt}" = "short" ]; then
    mkdir -p "${extract_dir}"
    ln -sfn "../${VIDEO_ID}_long/source.mp4" "${extract_dir}/source.mp4"
    if [ -s "${SOURCE_SRT}" ]; then
      ln -sfn "../${VIDEO_ID}_long/source.ja.srt" "${extract_dir}/source.ja.srt"
    fi
  fi

  local SRT_ARG=()
  if [ -s "${SOURCE_SRT}" ]; then
    SRT_ARG=(--srt "${SOURCE_SRT}")
  fi

  # [1/4] plan
  local plan_needs_regen=1
  if [ -s "${plan_json}" ]; then
    plan_needs_regen=0
    newer_than "${SOURCE_SRT}" "${plan_json}" && plan_needs_regen=1
    newer_than "${SOURCE_MOCK}" "${plan_json}" && plan_needs_regen=1
  fi

  if [ "${plan_needs_regen}" = "1" ]; then
    echo "[${fmt} 1/4] plan_${fmt}.json を生成します..."
    PYTHONPATH=src python -m clipgen.cli plan \
      --source mock --mock "${SOURCE_MOCK}" \
      --format "${fmt}" --top 1 --include-blocked \
      --aggressiveness 2 \
      --min-score "${MIN_HIGHLIGHT_SCORE}" \
      "${SRT_ARG[@]}" \
      --out "${plan_json}" \
      --now 2026-05-14T12:00:00+00:00
    rm -f "${extract_dir}/manifest.json" "${extract_dir}/cut.sh" "${extract_dir}/combine.sh" "${extract_dir}/concat.txt" "${combined_mp4}"
    rm -rf "${extract_dir}/parts"
  else
    echo "[${fmt} 1/4] SKIP: 既存 plan を使います: ${plan_json}"
  fi

  # [2/4] extract
  local extract_needs_regen=1
  local manifest="${extract_dir}/manifest.json"
  if [ -s "${manifest}" ] && [ -s "${extract_dir}/cut.sh" ] && [ -s "${extract_dir}/combine.sh" ] && [ -s "${extract_dir}/concat.txt" ]; then
    extract_needs_regen=0
    newer_than "${plan_json}" "${manifest}" && extract_needs_regen=1
  fi

  if [ "${extract_needs_regen}" = "1" ]; then
    echo "[${fmt} 2/4] extract 設定を生成します..."
    PYTHONPATH=src python -m clipgen.cli extract \
      --plan "${plan_json}" --out-root "${OUT_ROOT}" --top 1
    rm -rf "${extract_dir}/parts" "${combined_mp4}"
  else
    echo "[${fmt} 2/4] SKIP: 既存 extract 設定を使います: ${manifest}"
  fi

  # [3/4] cut
  if [ -s "${extract_dir}/cut.sh" ]; then
    local cut_needs_rerun=1
    if parts_all_present "${extract_dir}"; then
      cut_needs_rerun=0
      local sample_part="$(find "${extract_dir}/parts" -type f -name '*.mp4' 2>/dev/null | head -1)"
      if [ -n "${sample_part}" ]; then
        newer_than "${SOURCE_MP4}" "${sample_part}" && cut_needs_rerun=1
        newer_than "${extract_dir}/cut.sh" "${sample_part}" && cut_needs_rerun=1
        newer_than "${extract_dir}/concat.txt" "${sample_part}" && cut_needs_rerun=1
      fi
    fi
    if [ "${cut_needs_rerun}" = "1" ]; then
      echo "[${fmt} 3/4] ffmpeg で切り出します..."
      rm -rf "${extract_dir}/parts"
      mkdir -p "${extract_dir}/parts"
      bash "${extract_dir}/cut.sh"
      rm -f "${combined_mp4}"
    else
      echo "[${fmt} 3/4] SKIP: parts は source/cut/concat より新しいので再利用します。"
    fi
  else
    echo "[${fmt} 3/4] cut.sh が空または未生成です。ハイライト検出 0 件の可能性があります。" >&2
  fi

  # [4/4] combine
  if [ -s "${extract_dir}/combine.sh" ]; then
    local combined_needs_regen=1
    if [ -s "${combined_mp4}" ] && parts_all_present "${extract_dir}"; then
      combined_needs_regen=0
      newer_than "${extract_dir}/concat.txt" "${combined_mp4}" && combined_needs_regen=1
      newer_than "${extract_dir}/cut.sh" "${combined_mp4}" && combined_needs_regen=1
    fi
    if [ "${combined_needs_regen}" = "1" ]; then
      echo "[${fmt} 4/4] 連結します..."
      bash "${extract_dir}/combine.sh"
      echo "完成: ${combined_mp4}"
    else
      echo "[${fmt} 4/4] SKIP: 連結済み動画は parts/concat より新しいので再利用します: ${combined_mp4}"
    fi
  else
    echo "[${fmt} 4/4] combine.sh がありません。連結対象がない可能性があります。" >&2
  fi
}

run_format long
run_format short

# 最終的に combined.mp4 が両方できているか検証
LONG_COMBINED="${LONG_DIR}/combined.mp4"
SHORT_COMBINED="${SHORT_DIR}/combined.mp4"
missing=()
[ -s "${LONG_COMBINED}" ] || missing+=("ロング: ${LONG_COMBINED}")
[ -s "${SHORT_COMBINED}" ] || missing+=("ショート: ${SHORT_COMBINED}")

if [ "${#missing[@]}" -gt 0 ]; then
  echo
  echo "ERROR: 以下の最終成果物が生成されていません:" >&2
  for m in "${missing[@]}"; do
    echo "  - ${m}" >&2
  done
  echo "ハイライト検出 0 件、SRT 取得失敗、cut/combine 失敗のいずれかが原因です。" >&2
  exit 1
fi

echo
echo "============================================================"
echo "完成"
echo "============================================================"
echo "ロング: ${LONG_COMBINED}"
echo "ショート: ${SHORT_COMBINED}"

echo
echo "--- タイトル候補 (long) ---"
python - "output/test_b1/plan_long.json" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as f:
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

echo
echo "--- タイトル候補 (short) ---"
python - "output/test_b1/plan_short.json" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as f:
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
