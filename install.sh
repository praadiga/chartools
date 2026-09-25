#!/usr/bin/env bash
# install.sh — set up chartools from scratch
# Usage: bash install.sh
set -e

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-python3}"

echo "=== chartools installer ==="
echo "Repo: $REPO_DIR"
echo ""

# 1. Check Python
if ! command -v "$PYTHON" &>/dev/null; then
    echo "ERROR: python3 not found. Install Python 3.9+ first."
    exit 1
fi
PY_VER=$("$PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "[1/5] Python $PY_VER found at $("$PYTHON" -c "import sys; print(sys.executable)")"

# 2. Install Python dependencies
echo "[2/5] Installing Python dependencies..."
"$PYTHON" -m pip install -q -e "$REPO_DIR"
echo "      Done."

# 3. Confirm chartools CLI is available
if ! command -v chartools &>/dev/null; then
    echo ""
    echo "      NOTE: 'chartools' not on PATH yet."
    echo "      Add this to ~/.bashrc or ~/.zshrc:"
    echo ""
    echo "        export PATH=\"\$HOME/.local/bin:\$PATH\""
    echo ""
    echo "      Then restart your shell and re-run: bash install.sh"
    echo ""
    # Fallback: use python -m
    CHARTOOLS="$PYTHON $REPO_DIR/chartools.py"
else
    CHARTOOLS="chartools"
    echo "[3/5] chartools CLI available at $(command -v chartools)"
fi

# 4. amgctl — check if already configured
echo "[4/5] Checking amgctl..."
AMGCTL="${CHARTOOLS_AMGCTL:-$HOME/bin/amgctl}"
if [ -x "$AMGCTL" ]; then
    echo "      amgctl found at $AMGCTL"
else
    echo "      amgctl not found — it will be downloaded on first 'chartools daemon start'."
    echo "      After install, run: amgctl configure"
fi

# 5. Install systemd service
echo "[5/5] Installing systemd service..."
$CHARTOOLS daemon install
echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. chartools daemon start        — start the daemon"
echo "  2. chartools daemon status       — confirm it's running"
echo "  3. chartools daemon logs -f      — follow logs"
echo ""
echo "Add a testsuite:"
echo "  chartools testsuite add <path/to/your/testsuite>"
echo "  chartools status <path/to/your/testsuite>"
