#!/bin/bash
# Initialize podedit dev environment after devcontainer creation.
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
export PATH="$HOME/.local/bin:$PATH"
if ! grep -q '.local/bin' "$HOME/.bashrc" 2>/dev/null; then
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
fi

echo "=== podedit init: uv sync (this may take a few minutes) ==="
uv sync

echo ""
echo "=== podedit ready ==="
echo "Start the UI server with:"
echo "  uv run podedit serve"
echo "Then open the forwarded port 8765 in your browser."
