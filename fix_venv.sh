#!/bin/bash
# Quick fix for Python venv. Run on server: bash fix_venv.sh

set -e

cd /opt/naio/backend

echo "Fixing Python venv..."

# Remove broken venv
if [ -d "venv" ]; then
    echo "Removing broken venv..."
    rm -rf venv
fi

# Ensure python3.10-venv is installed
if ! dpkg -l | grep -q "^ii.*python3.10-venv"; then
    echo "Installing python3.10-venv..."
    apt-get update -qq
    apt-get install -y -qq python3.10-venv
fi

# Create venv
echo "Creating Python venv..."
python3.10 -m venv venv

# Verify venv
if [ ! -f "venv/bin/activate" ]; then
    echo "❌ Venv creation failed"
    exit 1
fi

# Activate and install deps
echo "Activating venv and installing deps..."
source venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q

echo "✅ Venv fixed"
echo ""
echo "Python: $(python --version)"
echo "pip: $(pip --version | awk '{print $2}')"
echo ""
echo "Next: cd /opt/naio && ./deploy.sh --skip-deps"
