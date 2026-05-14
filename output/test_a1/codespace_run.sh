#!/usr/bin/env bash
set -euo pipefail

PLAN_JSON="output/test_a1/plan.json"
OUT_ROOT="output/test_a1/extract"
EXTRACT_DIR="${OUT_ROOT}/HYUe6mRpwVs_long"

uv run python -m clipgen.cli plan \
  --from-youtube HYUe6mRpwVs \
  --format long \
  --top 1 \
  --out "${PLAN_JSON}"

uv run python -m clipgen.cli extract \
  --plan "${PLAN_JSON}" \
  --out-root "${OUT_ROOT}" \
  --top 1

(
  cd "${EXTRACT_DIR}"
  bash cut.sh
  bash combine.sh
)

printf '%s\n' "${EXTRACT_DIR}/combined.mp4"
