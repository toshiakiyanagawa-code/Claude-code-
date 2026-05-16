#!/usr/bin/env bash
set -euo pipefail

MODE="${1:-dev}"

if [[ -f ".env" ]]; then
  # shellcheck disable=SC1091
  source .env
fi

if ! command -v cms-assist >/dev/null 2>&1; then
  echo "[ERROR] 'cms-assist' command was not found."
  echo "        Install the internal CLI and make sure it is available on PATH."
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
    echo "[INFO] Starting CMS assist in development mode."
    exec cms-assist run \
      --base-url "$CMS_BASE_URL" \
      --token "$CMS_API_TOKEN" \
      --space "$CMS_SPACE_ID" \
      --timeout "$CMS_TIMEOUT_MS" \
      --watch
    ;;
  prod)
    echo "[INFO] Starting CMS assist in production-like mode."
    exec cms-assist run \
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
