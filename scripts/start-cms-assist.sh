#!/usr/bin/env bash
set -euo pipefail

MODE="${1:-dev}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -f ".env" ]]; then
  # shellcheck disable=SC1091
  source .env
fi

LOCAL_CLI="$ROOT_DIR/bin/cms-assist.js"
if [[ -f "$LOCAL_CLI" ]]; then
  if ! command -v node >/dev/null 2>&1; then
    echo "[ERROR] Node.js was not found. Install Node.js 20+ or add it to PATH."
    exit 1
  fi
  CMS_ASSIST_CMD=(node "$LOCAL_CLI")
elif command -v cms-assist >/dev/null 2>&1; then
  CMS_ASSIST_CMD=(cms-assist)
else
  echo "[ERROR] 'cms-assist' command was not found."
  echo "        Install the internal CLI/binary and add it to PATH."
  exit 1
fi

: "${CMS_BASE_URL:=}"
: "${CMS_API_TOKEN:=}"
: "${CMS_SPACE_ID:=}"
: "${CMS_TIMEOUT_MS:=15000}"

if [[ -z "$CMS_BASE_URL" || -z "$CMS_API_TOKEN" || -z "$CMS_SPACE_ID" ]]; then
  echo "[ERROR] Required environment variables are missing. Check .env."
  echo "        Required: CMS_BASE_URL / CMS_API_TOKEN / CMS_SPACE_ID"
  exit 1
fi

case "$MODE" in
  dev)
    echo "[INFO] Starting CMS assist in development mode"
    exec "${CMS_ASSIST_CMD[@]}" run \
      --base-url "$CMS_BASE_URL" \
      --token "$CMS_API_TOKEN" \
      --space "$CMS_SPACE_ID" \
      --timeout "$CMS_TIMEOUT_MS" \
      --watch
    ;;
  prod)
    echo "[INFO] Starting CMS assist in production-like mode"
    exec "${CMS_ASSIST_CMD[@]}" run \
      --base-url "$CMS_BASE_URL" \
      --token "$CMS_API_TOKEN" \
      --space "$CMS_SPACE_ID" \
      --timeout "$CMS_TIMEOUT_MS"
    ;;
  *)
    echo "Usage: $0 [dev|prod]"
    exit 2
    ;;
esac
