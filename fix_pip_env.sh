#!/bin/bash
# =============================================================================
# fix_pip_env.sh — Repair pip / PyPI index issues on Ubuntu 20.04 after deploy2.sh
#
# Symptom: pip install fails with "No matching distribution found for python-dotenv==1.2.1"
# Cause:   Stale third-party PyPI mirror, or old pip, while requirements pin newer packages.
#
# This script does NOT change application code. It only:
#   - Uses the existing backend/venv from deploy2.sh
#   - Upgrades pip / setuptools / wheel using the official PyPI index
#   - Reinstalls backend/requirements.txt from PyPI.org
#
# Usage (from repo root, e.g. /opt/naio):
#   sudo ./fix_pip_env.sh
#
# Then generate node addresses:
#   cd /opt/naio && source backend/venv/bin/activate
#   python tools/generate_nodes.py --count 1000
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR"
BACKEND_DIR="$PROJECT_DIR/backend"
VENV_PY="$BACKEND_DIR/venv/bin/python"

echo "=========================================="
echo "NAIO fix_pip_env — official PyPI + reinstall backend deps"
echo "=========================================="
echo "Project: $PROJECT_DIR"
echo ""

if [ ! -x "$VENV_PY" ]; then
    echo "❌ Missing $VENV_PY — run deploy2.sh first to create backend/venv."
    exit 1
fi

if [ ! -f "$BACKEND_DIR/requirements.txt" ]; then
    echo "❌ Missing $BACKEND_DIR/requirements.txt"
    exit 1
fi

# Official index (avoids mirrors that lag behind PyPI for pinned versions)
PYPI_URL="https://pypi.org/simple"
TRUSTED="pypi.org files.pythonhosted.org"

echo "Step 1: Upgrade pip / setuptools / wheel (index: $PYPI_URL) ..."
"$VENV_PY" -m pip install --upgrade pip setuptools wheel \
    -i "$PYPI_URL" \
    --trusted-host pypi.org --trusted-host files.pythonhosted.org

echo ""
echo "Step 2: Install backend/requirements.txt from official PyPI ..."
"$VENV_PY" -m pip install -r "$BACKEND_DIR/requirements.txt" \
    -i "$PYPI_URL" \
    --trusted-host pypi.org --trusted-host files.pythonhosted.org

echo ""
echo "=========================================="
echo "Done."
echo "=========================================="
echo "Try:"
echo "  cd $PROJECT_DIR && source backend/venv/bin/activate"
echo "  python tools/generate_nodes.py --count 1000"
echo ""
echo "If it still fails, check /etc/pip.conf or ~/.pip/pip.conf for a mirror; temporarily"
echo "comment it out or ensure the machine can reach $PYPI_URL"
echo ""
