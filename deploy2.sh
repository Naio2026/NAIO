#!/bin/bash
# =============================================================================
# deploy2.sh — Fresh Ubuntu 20.04: Python stack, Foundry + forge-std, Supervisor
#
# Intended layout (recommended for ops docs):
#   cd /opt
#   git clone <your-repo-url> naio
#   cd /opt/naio
#   sudo ./deploy2.sh
#
# Compared to deploy.sh:
#   - Does NOT broadcast contract deployments (you run forge script manually).
#   - Writes all supervisor programs with autostart=false; you start with supervisorctl.
#   - Enforces Python / pip / Foundry version bands to reduce syntax or toolchain drift.
#
# Usage:
#   sudo ./deploy2.sh
#   sudo ./deploy2.sh --force              # Recreate venv, reinstall Foundry if needed, rewrite supervisor
#   sudo ./deploy2.sh --skip-telegram     # Skip Telegram bot pip extras (Python must still be in range)
#   sudo ./deploy2.sh --skip-foundry       # Skip Foundry / forge-std / forge build (Python + supervisor only)
#   sudo ./deploy2.sh --skip-forge-build   # Skip forge build smoke test after deps install
#
# Optional environment:
#   FOUNDRY_VERSION   Tag passed to foundryup -i (e.g. v1.0.0). Default: DEFAULT_FOUNDRY_VERSION below.
#   FORGE_STD_TAG     Git tag for forge-std (default v1.9.4).
#   DAPP_PORT         Governance DApp HTTP port (default 8765).
#
# After this script, typical manual steps:
#   1) Fill backend/.env (and contracts/nodes_list.txt for deploy scripts — see contracts/README.md).
#   2) Run forge script to deploy contracts; write deployed addresses into backend/.env.
#   3) supervisorctl start <programs> as needed.
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR"
BACKEND_DIR="$PROJECT_DIR/backend"
CONTRACTS_DIR="$PROJECT_DIR/contracts"
DAPP_DIR="$PROJECT_DIR/dapp"

# Recommended clone path (informational only)
EXPECTED_ROOT="/opt/naio"

# Version policy (aligned with backend/requirements*.txt and contracts/foundry.toml)
# Python: python-telegram-bot 21.x needs >=3.9; 3.13+ may lag wheels — cap at 3.12
PY_MIN_MINOR=9
PY_MAX_MINOR=12

# pip: avoid ancient pip; avoid pip 25+ edge cases with some stacks
PIP_SPEC='pip>=24.0,<25'

# Foundry: too old may mishandle solc 0.8.24 / IR; pin a reproducible binary tag (override if missing on foundryup -l)
DEFAULT_FOUNDRY_VERSION="${DEFAULT_FOUNDRY_VERSION:-v1.0.0}"
FOUNDRY_VERSION="${FOUNDRY_VERSION:-$DEFAULT_FOUNDRY_VERSION}"

# Pinned forge-std tag so installs do not float on main
FORGE_STD_TAG="${FORGE_STD_TAG:-v1.9.4}"

DAPP_PORT="${DAPP_PORT:-8765}"

FORCE=false
SKIP_TELEGRAM=false
SKIP_FOUNDRY=false
SKIP_FORGE_BUILD=false
for arg in "$@"; do
    case "$arg" in
        --force) FORCE=true ;;
        --skip-telegram) SKIP_TELEGRAM=true ;;
        --skip-foundry) SKIP_FOUNDRY=true ;;
        --skip-forge-build) SKIP_FORGE_BUILD=true ;;
        *)
            echo "Unknown option: $arg"
            echo "Usage: sudo $0 [--force] [--skip-telegram] [--skip-foundry] [--skip-forge-build]"
            exit 1
            ;;
    esac
done

# --- Helpers: semver compare (numeric a.b.c only) ---
_ver_to_cmp() {
    echo "$1" | awk -F. '{ printf "%05d%05d%05d\n", $1+0, $2+0, $3+0 }'
}

_version_ge() {
    [ "$(printf '%s\n' "$(_ver_to_cmp "$2")" "$(_ver_to_cmp "$1")" | sort -V | head -n1)" = "$(_ver_to_cmp "$2")" ]
}

_extract_forge_semver() {
    forge --version 2>/dev/null | head -n1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -n1
}

# Pick Python 3.9–3.12; on Ubuntu 20.04 (python3 = 3.8) install via deadsnakes (see try_install_deadsnakes_python)
ensure_python_for_venv() {
    local cand ver major minor
    for cand in python3.12 python3.11 python3.10 python3.9; do
        if command -v "$cand" &>/dev/null; then
            ver=$($cand -c 'import sys; print(sys.version_info[0], sys.version_info[1])' 2>/dev/null)
            major=$(echo "$ver" | awk '{print $1}')
            minor=$(echo "$ver" | awk '{print $2}')
            if [ "$major" -eq 3 ] && [ "$minor" -ge "$PY_MIN_MINOR" ] && [ "$minor" -le "$PY_MAX_MINOR" ]; then
                echo "$cand"
                return 0
            fi
        fi
    done

    if command -v python3 &>/dev/null; then
        ver=$(python3 -c 'import sys; print(sys.version_info[0], sys.version_info[1])' 2>/dev/null)
        major=$(echo "$ver" | awk '{print $1}')
        minor=$(echo "$ver" | awk '{print $2}')
        if [ "$major" -eq 3 ] && [ "$minor" -ge "$PY_MIN_MINOR" ] && [ "$minor" -le "$PY_MAX_MINOR" ]; then
            echo "python3"
            return 0
        fi
    fi

    echo ""
    return 1
}

# Add deadsnakes PPA and try python3.10, then 3.9, 3.11, 3.12 (some mirrors lack python3.10-venv; 3.9 often works).
try_install_deadsnakes_python() {
    echo ""
    echo "   [INFO] Ubuntu 20.04 ships Python 3.8; we need 3.9+. Adding deadsnakes PPA and trying 3.10, 3.9, 3.11, 3.12 (in that order)."
    echo "   [INFO] If apt fails, check network access to Launchpad (PPA) or use a proxy, then re-run this script."
    apt-get install -y -qq software-properties-common ca-certificates
    if ! grep -rq "deadsnakes/ppa" /etc/apt/sources.list.d/ 2>/dev/null; then
        add-apt-repository -y ppa:deadsnakes/ppa
    else
        echo "   (deadsnakes PPA already present)"
    fi
    apt-get update -qq

    local pyver
    set +e
    for pyver in 10 9 11 12; do
        echo "   Trying: apt install python3.${pyver} python3.${pyver}-venv python3.${pyver}-dev ..."
        if apt-get install -y python3.${pyver} python3.${pyver}-venv python3.${pyver}-dev; then
            echo "✅ Installed python3.${pyver} (deadsnakes)"
            set -e
            return 0
        fi
        echo "   (python3.${pyver} not available from apt, trying next version...)"
    done
    set -e
    echo ""
    echo "❌ Could not install Python 3.9–3.12 from deadsnakes. Try:"
    echo "   1) Fix network/DNS or use a proxy so apt can reach the PPA (Launchpad)."
    echo "   2) Manual: sudo apt update && sudo apt install -y python3.9 python3.9-venv python3.9-dev"
    echo "   3) Or use Ubuntu 22.04 (Python 3.10 in default repos)."
    return 1
}

echo "=========================================="
echo "NAIO deploy2 — env / Foundry / Supervisor (Ubuntu 20.04)"
echo "=========================================="
echo "[Hint] Typical path: cd /opt/naio && sudo ./deploy2.sh"
echo "Project root: $PROJECT_DIR"
if [ "$PROJECT_DIR" != "$EXPECTED_ROOT" ]; then
    echo "Note: docs often use clone path ${EXPECTED_ROOT}; current root is fine if this repo is complete."
fi
echo "Python allowed: 3.${PY_MIN_MINOR} – 3.${PY_MAX_MINOR} (matches requirements)"
echo "pip constraint: ${PIP_SPEC}"
echo ""

if [ "${EUID:-0}" -ne 0 ]; then
    echo "❌ Run as root or with sudo: sudo ./deploy2.sh"
    exit 1
fi

if [ ! -d "$BACKEND_DIR" ] || [ ! -d "$CONTRACTS_DIR" ]; then
    echo "❌ Missing backend/ or contracts/. Run this script from the repository root (e.g. cd ${EXPECTED_ROOT})."
    exit 1
fi

if [ -f /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    if [ "${ID:-}" = "ubuntu" ] && [ "${VERSION_ID:-}" != "20.04" ]; then
        echo "⚠️  OS: ${PRETTY_NAME:-unknown} (primary target is Ubuntu 20.04)"
        echo ""
    fi
fi

export DEBIAN_FRONTEND=noninteractive

# =============================================================================
# Step 1 — APT: supervisor, toolchain, libs for Python wheels / matplotlib
# =============================================================================
echo "Step 1: System packages (apt)..."
apt-get update -qq

MISSING_APT=()
for pkg in python3 python3-pip python3-venv python3-dev build-essential pkg-config \
    libssl-dev libffi-dev libfreetype6-dev libpng-dev curl git jq ca-certificates \
    software-properties-common supervisor; do
    if ! dpkg -s "$pkg" &>/dev/null; then
        MISSING_APT+=("$pkg")
    fi
done

if [ ${#MISSING_APT[@]} -gt 0 ]; then
    echo "   Installing: ${MISSING_APT[*]}"
    apt-get install -y -qq "${MISSING_APT[@]}" >/dev/null
else
    echo "   Required apt packages already present"
fi

systemctl enable supervisor --now 2>/dev/null || true
if ! supervisorctl status >/dev/null 2>&1; then
    service supervisor start 2>/dev/null || true
fi
echo "✅ Step 1 done (supervisor installed / running)"

# =============================================================================
# Step 2 — venv + pinned pip + requirements
# =============================================================================
echo ""
echo "Step 2: Python venv and pip dependencies..."

PYTHON_CMD=$(ensure_python_for_venv || true)
if [ -z "$PYTHON_CMD" ]; then
    try_install_deadsnakes_python || exit 1
    PYTHON_CMD=$(ensure_python_for_venv || true)
fi
if [ -z "$PYTHON_CMD" ]; then
    echo "❌ Still no Python 3.${PY_MIN_MINOR}–3.${PY_MAX_MINOR} after deadsnakes. Install python3.9–3.12 manually, then re-run."
    exit 1
fi

echo "   Using interpreter: $PYTHON_CMD ($($PYTHON_CMD --version 2>&1))"

if ! "$PYTHON_CMD" -c "import sys; assert (3,${PY_MIN_MINOR}) <= sys.version_info[:2] <= (3,${PY_MAX_MINOR})" 2>/dev/null; then
    echo "❌ Python version outside allowed range 3.${PY_MIN_MINOR}–3.${PY_MAX_MINOR}"
    exit 1
fi

cd "$BACKEND_DIR"

if [ "$FORCE" = true ] && [ -d "venv" ]; then
    echo "   --force: removing old venv..."
    rm -rf venv
fi

if [ ! -d "venv" ]; then
    echo "   Creating venv..."
    "$PYTHON_CMD" -m venv venv
fi

if [ ! -f "venv/bin/activate" ]; then
    echo "❌ venv incomplete"
    exit 1
fi

# shellcheck disable=SC1091
source venv/bin/activate

VENV_PY=$(python -c 'import sys; print("%d.%d.%d"%sys.version_info[:3])')
echo "   venv Python: $VENV_PY"

if ! python -c "import sys; assert (3,${PY_MIN_MINOR}) <= sys.version_info[:2] <= (3,${PY_MAX_MINOR})" 2>/dev/null; then
    echo "❌ venv Python invalid; retry with --force"
    deactivate 2>/dev/null || true
    exit 1
fi

echo "   Upgrading pip (${PIP_SPEC})..."
python -m pip install --upgrade "${PIP_SPEC}" -q

if [ ! -f "requirements.txt" ]; then
    echo "❌ Missing backend/requirements.txt"
    deactivate 2>/dev/null || true
    exit 1
fi

echo "   pip install -r requirements.txt ..."
pip install -r requirements.txt -q

if [ "$SKIP_TELEGRAM" != true ]; then
    if [ -f "requirements.telegram.txt" ]; then
        echo "   pip install -r requirements.telegram.txt ..."
        pip install -r requirements.telegram.txt -q
    else
        echo "⚠️  requirements.telegram.txt not found; skipping Telegram extras"
    fi
else
    echo "   Skipped Telegram deps (--skip-telegram)"
fi

echo "✅ Step 2 done"
deactivate 2>/dev/null || true

# =============================================================================
# Step 3 — backend/.env template
# =============================================================================
echo ""
echo "Step 3: backend/.env ..."
if [ ! -f "$BACKEND_DIR/.env" ]; then
    if [ -f "$BACKEND_DIR/env.example" ]; then
        cp "$BACKEND_DIR/env.example" "$BACKEND_DIR/.env"
        echo "✅ Created backend/.env from env.example — edit before deploy / starting services"
    else
        echo "⚠️  env.example missing; create backend/.env manually"
    fi
else
    echo "✅ backend/.env already exists (not overwritten)"
fi

# =============================================================================
# Step 4 — Foundry (forge/cast) + PATH
# =============================================================================
echo ""
if [ "$SKIP_FOUNDRY" = true ]; then
    echo "Step 4: Skipping Foundry (--skip-foundry)"
else
    echo "Step 4: Foundry (target tag: ${FOUNDRY_VERSION})..."
    export PATH="${HOME}/.foundry/bin:${PATH}"

    NEED_FOUNDRY_INSTALL=true
    if command -v forge &>/dev/null; then
        FVER=$(_extract_forge_semver)
        if [ -n "$FVER" ] && _version_ge "$FVER" "0.2.0"; then
            echo "   forge already present: $(forge --version | head -n1)"
            if [ "$FORCE" != true ]; then
                NEED_FOUNDRY_INSTALL=false
            else
                echo "   --force: reinstalling Foundry..."
            fi
        else
            echo "   forge missing or too old / unreadable; will install or replace"
        fi
    fi

    if [ "$NEED_FOUNDRY_INSTALL" = true ]; then
        if ! command -v foundryup &>/dev/null; then
            echo "   Downloading Foundry installer..."
            curl -fsSL https://foundry.paradigm.xyz | bash
        fi
        export PATH="${HOME}/.foundry/bin:${PATH}"
        if ! command -v foundryup &>/dev/null; then
            echo "❌ foundryup not available; check network or install Foundry manually"
            exit 1
        fi
        echo "   Running foundryup -i ${FOUNDRY_VERSION} (prebuilt binaries)..."
        if ! foundryup -i "${FOUNDRY_VERSION}"; then
            echo "❌ foundryup -i ${FOUNDRY_VERSION} failed."
            echo "   Check tags at https://github.com/foundry-rs/foundry/releases and run:"
            echo "   export FOUNDRY_VERSION=<tag> && sudo -E ./deploy2.sh --force"
            exit 1
        fi
    fi

    export PATH="${HOME}/.foundry/bin:${PATH}"
    if ! command -v forge &>/dev/null; then
        echo "❌ forge not on PATH; ensure ${HOME}/.foundry/bin is available"
        exit 1
    fi

    FVER=$(_extract_forge_semver)
    echo "   forge line: $(forge --version | head -n1)"
    if [ -z "$FVER" ]; then
        echo "⚠️  Could not parse forge semver; verify manually"
    else
        if ! _version_ge "$FVER" "0.2.0"; then
            echo "❌ forge ${FVER} is too old (need >= 0.2.0 for solc 0.8.24 / modern layouts)"
            exit 1
        fi
        if _version_ge "$FVER" "2.0.0"; then
            echo "⚠️  forge ${FVER} >= 2.0.0 — if forge build fails, lower FOUNDRY_VERSION and re-run with --force"
        fi
    fi

    PROFILE_D="/etc/profile.d/naio-foundry.sh"
    echo "export PATH=\"\$HOME/.foundry/bin:\$PATH\"" > "$PROFILE_D"
    chmod 0644 "$PROFILE_D"
    echo "✅ Step 4 done (wrote $PROFILE_D for new shells)"
fi

# =============================================================================
# Step 5 — forge-std + forge build (no broadcast)
# =============================================================================
echo ""
if [ "$SKIP_FOUNDRY" = true ]; then
    echo "Step 5: Skipping contract toolchain (--skip-foundry)"
else
    echo "Step 5: forge-std + compile smoke test..."
    export PATH="${HOME}/.foundry/bin:${PATH}"

    if [ ! -f "$CONTRACTS_DIR/lib/forge-std/src/Test.sol" ] || [ "$FORCE" = true ]; then
        echo "   Installing forge-std @ ${FORGE_STD_TAG} ..."
        cd "$CONTRACTS_DIR"
        rm -rf lib/forge-std 2>/dev/null || true
        if forge install "foundry-rs/forge-std@${FORGE_STD_TAG}" --no-commit 2>/dev/null; then
            echo "✅ forge-std installed (${FORGE_STD_TAG})"
        elif forge install "foundry-rs/forge-std@${FORGE_STD_TAG}" 2>/dev/null; then
            echo "✅ forge-std installed (${FORGE_STD_TAG})"
        elif git clone --depth 1 --branch "$FORGE_STD_TAG" https://github.com/foundry-rs/forge-std.git "$CONTRACTS_DIR/lib/forge-std" 2>/dev/null; then
            echo "✅ forge-std installed via git clone (${FORGE_STD_TAG})"
        else
            echo "❌ forge-std install failed; try manually:"
            echo "   cd $CONTRACTS_DIR && forge install foundry-rs/forge-std@${FORGE_STD_TAG} --no-commit"
            exit 1
        fi
    else
        echo "✅ forge-std already present (use --force to reinstall)"
    fi

    if [ "$SKIP_FORGE_BUILD" = true ]; then
        echo "   Skipped forge build (--skip-forge-build)"
    else
        echo "   forge build (solc 0.8.24 from foundry.toml)..."
        cd "$CONTRACTS_DIR"
        forge build
        echo "✅ forge build succeeded"
    fi
fi

# =============================================================================
# Step 6 — Supervisor: all programs, autostart=false (manual supervisorctl start)
# =============================================================================
echo ""
echo "Step 6: Writing supervisor configs (autostart=false)..."

mkdir -p /etc/supervisor/conf.d

write_sup() {
    local name="$1"
    local cmd="$2"
    local dir="$3"
    local f="/etc/supervisor/conf.d/${name}.conf"
    {
        echo "[program:${name}]"
        echo "command=${cmd}"
        echo "directory=${dir}"
        echo "user=root"
        echo "autostart=false"
        echo "autorestart=true"
        echo "startretries=3"
        echo "stderr_logfile=/var/log/${name}.err.log"
        echo "stdout_logfile=/var/log/${name}.out.log"
        echo "environment=PATH=\"${BACKEND_DIR}/venv/bin:%(ENV_PATH)s\""
    } > "$f"
    echo "   Wrote $f"
}

write_sup_dapp() {
    local f="/etc/supervisor/conf.d/naio-dapp.conf"
    {
        echo "[program:naio-dapp]"
        echo "command=${BACKEND_DIR}/venv/bin/python ${DAPP_DIR}/server.py"
        echo "directory=${DAPP_DIR}"
        echo "user=root"
        echo "autostart=false"
        echo "autorestart=true"
        echo "startretries=3"
        echo "stderr_logfile=/var/log/naio-dapp.err.log"
        echo "stdout_logfile=/var/log/naio-dapp.out.log"
        echo "environment=PATH=\"${BACKEND_DIR}/venv/bin:%(ENV_PATH)s\",DAPP_PORT=\"${DAPP_PORT}\""
    } > "$f"
    echo "   Wrote $f"
}

write_sup "naio-listen-deposits" \
    "$BACKEND_DIR/venv/bin/python $BACKEND_DIR/listen_deposits.py" \
    "$BACKEND_DIR"

write_sup "naio-call-poke" \
    "$BACKEND_DIR/venv/bin/python $BACKEND_DIR/call_poke.py" \
    "$BACKEND_DIR"

write_sup "naio-price-recorder" \
    "$BACKEND_DIR/venv/bin/python $BACKEND_DIR/price_recorder.py" \
    "$BACKEND_DIR"

write_sup "naio-telegram-bot" \
    "$BACKEND_DIR/venv/bin/python $BACKEND_DIR/telegram_bot.py" \
    "$BACKEND_DIR"

write_sup "naio-keeper-validator" \
    "$BACKEND_DIR/venv/bin/python $BACKEND_DIR/keeper_validator.py" \
    "$BACKEND_DIR"

write_sup_dapp

if supervisorctl reread && supervisorctl update; then
    echo "✅ Supervisor loaded (programs STOPPED, autostart=false)"
else
    echo "❌ supervisorctl failed; check /etc/supervisor/supervisord.conf"
    exit 1
fi

# --- Optional: remind about nodes_list.txt (deploy scripts often require 1000 lines) ---
NODES_FILE="$CONTRACTS_DIR/nodes_list.txt"
if [ -f "$NODES_FILE" ]; then
    NL=$(wc -l < "$NODES_FILE" | tr -d ' ')
    if [ "$NL" != "1000" ]; then
        echo ""
        echo "⚠️  contracts/nodes_list.txt has $NL lines (many deploy flows expect exactly 1000). See contracts/README.md."
    fi
else
    echo ""
    echo "⚠️  contracts/nodes_list.txt is missing — create it before running deploy scripts (see contracts/README.md)."
fi

# =============================================================================
# Done
# =============================================================================
echo ""
echo "=========================================="
echo "deploy2 finished"
echo "=========================================="
echo ""
FINAL_PY_VER=$("$BACKEND_DIR/venv/bin/python" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null || echo "?")
echo "Versions:"
echo "  Python venv: $BACKEND_DIR/venv/bin/python ($FINAL_PY_VER)"
if [ "$SKIP_FOUNDRY" != true ]; then
    export PATH="${HOME}/.foundry/bin:${PATH}"
    echo "  forge: $(command -v forge 2>/dev/null || echo not installed) — $(forge --version 2>/dev/null | head -n1 || true)"
fi
echo ""
echo "Next — configure (before manual contract deploy):"
echo "  • Edit backend/.env (RPC, keys, addresses required by forge script)."
echo "  • Prepare contracts/nodes_list.txt if your deploy script needs it (often 1000 lines)."
echo ""
echo "Contract deploy (manual; this script does not broadcast):"
echo "  cd $CONTRACTS_DIR"
echo "  # Example: set -a && source $BACKEND_DIR/.env && set +a"
echo "  forge script script/DeployTestnet.s.sol:DeployTestnet --rpc-url \"\$QUICKNODE_HTTP_URL\" ..."
echo "  # Then write CONTROLLER_ADDRESS and other addresses into backend/.env"
echo ""
echo "Start services (manual examples):"
echo "  supervisorctl start naio-listen-deposits"
echo "  supervisorctl start naio-call-poke"
echo "  supervisorctl start naio-price-recorder"
echo "  supervisorctl start naio-telegram-bot"
echo "  supervisorctl start naio-keeper-validator"
echo "  supervisorctl start naio-dapp"
echo "  supervisorctl status"
echo ""
echo "Logs: tail -f /var/log/naio-listen-deposits.out.log"
echo ""
