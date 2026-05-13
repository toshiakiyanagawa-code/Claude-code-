#!/bin/bash
# Initialize podedit dev environment after devcontainer creation.
#
# Phases:
#   0. Drop Yarn apt source (its GPG key is expired in this base image).
#      Any apt-get update otherwise fails — NOT just feature installers.
#   1. apt deps (strict; failure aborts creation).
#   2. uv install + symlink into /usr/local/bin (strict).
#   3. uv sync (best effort; transient wheel-download failure does not
#      bounce the container into recovery mode — a marker file + an
#      /etc/profile.d notice surface the warning to the user's next shell).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "=== podedit init: drop expired Yarn apt source ==="
# mcr.microsoft.com/devcontainers/python:1-3.12-bookworm pre-registers
# https://dl.yarnpkg.com/debian whose signing key (NO_PUBKEY 62D54FD4003F6525)
# is expired. With signed-by enforcement this turns apt-get update into a hard
# failure under set -e. We don't use Yarn, so remove the source entirely.
sudo rm -f /etc/apt/sources.list.d/yarn.list \
           /etc/apt/sources.list.d/yarn*.list 2>/dev/null || true
sudo sed -i '/dl\.yarnpkg\.com/d' /etc/apt/sources.list 2>/dev/null || true

echo "=== podedit init: apt deps ==="
sudo apt-get update
sudo apt-get install -y --no-install-recommends ffmpeg git-lfs curl ca-certificates

echo "=== podedit init: install uv ==="
if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi

UV_BIN_DIR="$HOME/.local/bin"
export PATH="$UV_BIN_DIR:$PATH"

# Symlink uv into /usr/local/bin so it resolves in EVERY shell (login,
# non-login, interactive, non-interactive) without depending on shell-rc
# files. VS Code's integrated terminal launches non-login interactive bash
# which sources ~/.bashrc but NOT /etc/profile.d — symlinking onto a path
# that's already in the default PATH sidesteps that entirely.
if [ -x "$UV_BIN_DIR/uv" ]; then
    sudo ln -sf "$UV_BIN_DIR/uv" /usr/local/bin/uv
fi
if [ -x "$UV_BIN_DIR/uvx" ]; then
    sudo ln -sf "$UV_BIN_DIR/uvx" /usr/local/bin/uvx
fi

# Clear any stale incomplete-deps marker from a prior failed boot.
sudo rm -f /etc/profile.d/99-podedit-warning.sh
rm -f "$HOME/.podedit-deps-incomplete"

echo "=== podedit init: uv sync (best effort; heavy wheels may take a few min) ==="
if uv sync; then
    echo "uv sync: OK"
    DEPS_OK=1
else
    sync_exit=$?
    DEPS_OK=0
    echo ""
    echo "WARNING: uv sync exited with code ${sync_exit}."
    echo "The container is up but Python deps are not fully installed."
    echo "Re-run inside the container:   uv sync"
    echo ""

    # Marker file + visible warning on every interactive shell, so the user
    # cannot miss it even if they skipped the postCreate log.
    touch "$HOME/.podedit-deps-incomplete"
    sudo tee /etc/profile.d/99-podedit-warning.sh >/dev/null <<'EOF'
if [ -f "$HOME/.podedit-deps-incomplete" ]; then
    echo ""
    echo "WARNING: podedit Python deps are NOT fully installed."
    echo "         Run 'uv sync' from the project root to complete setup."
    echo ""
fi
EOF
    sudo chmod 0644 /etc/profile.d/99-podedit-warning.sh
fi

echo "=== podedit init: done ==="
if [ "$DEPS_OK" = "1" ]; then
    echo "Start the UI server with:"
    echo "  uv run podedit serve"
    echo "Then open the forwarded port 8765 in your browser."
else
    echo "Container is up but deps need 'uv sync' to complete (see warning above)."
fi
