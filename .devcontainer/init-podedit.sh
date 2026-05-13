#!/bin/bash
# Initialize podedit dev environment after devcontainer creation.
#
# Phases:
#   1. OS deps (apt) — strict; failure aborts creation.
#   2. uv install   — strict.
#   3. uv sync      — best effort; failure logs a warning but does NOT abort,
#                     so the container still comes up usable and the user can
#                     re-run sync interactively. Heavy wheels (faster-whisper,
#                     ctranslate2) may transient-fail during postCreate.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "=== podedit init: apt deps ==="
sudo apt-get update
sudo apt-get install -y --no-install-recommends ffmpeg git-lfs curl ca-certificates

echo "=== podedit init: install uv ==="
if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi

UV_BIN_DIR="$HOME/.local/bin"
export PATH="$UV_BIN_DIR:$PATH"

# Expose uv to all future shells via /etc/profile.d. More reliable than
# appending to ~/.bashrc, which is skipped by non-interactive shells and may
# already contain a different PATH= line that grep would match against.
sudo tee /etc/profile.d/99-uv.sh >/dev/null <<EOF
export PATH="$UV_BIN_DIR:\$PATH"
EOF
sudo chmod 0644 /etc/profile.d/99-uv.sh

echo "=== podedit init: uv sync (best effort; heavy wheels may take a few min) ==="
if uv sync; then
    echo "uv sync: OK"
else
    sync_exit=$?
    echo ""
    echo "WARNING: uv sync exited with code ${sync_exit}."
    echo "The container is up but Python deps are not fully installed."
    echo "Re-run inside the container:   uv sync"
    echo ""
fi

echo "=== podedit ready ==="
echo "Start the UI server with:"
echo "  uv run podedit serve"
echo "Then open the forwarded port 8765 in your browser."
