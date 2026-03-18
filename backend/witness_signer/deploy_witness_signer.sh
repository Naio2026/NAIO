#!/bin/bash
# Witness signer one-shot deploy script (standalone server)
#
# Usage:
#   ./deploy_witness_signer.sh [options]
#
# Options:
#   --env-file <path>       Path to .env (default: backend/.env)
#   --service-name <name>   Supervisor service name (default: naio-witness-signer)
#   --skip-deps             Skip system dependency installation
#   --force                 Force recreate venv and reinstall dependencies
#   --no-supervisor         Do not write/restart supervisor; only prepare runtime

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_DIR="$(cd "$BACKEND_DIR/.." && pwd)"

ENV_FILE="${WITNESS_SIGNER_ENV_FILE:-$BACKEND_DIR/.env}"
SERVICE_NAME="${WITNESS_SIGNER_SERVICE_NAME:-naio-witness-signer}"
VENV_DIR="$SCRIPT_DIR/venv"

SKIP_DEPS=false
FORCE=false
NO_SUPERVISOR=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --env-file)
            ENV_FILE="$2"
            shift 2
            ;;
        --service-name)
            SERVICE_NAME="$2"
            shift 2
            ;;
        --skip-deps)
            SKIP_DEPS=true
            shift
            ;;
        --force)
            FORCE=true
            shift
            ;;
        --no-supervisor)
            NO_SUPERVISOR=true
            shift
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

if [ "$EUID" -ne 0 ]; then
    if command -v sudo >/dev/null 2>&1; then
        SUDO="sudo"
    else
        SUDO=""
    fi
else
    SUDO=""
fi

echo "=========================================="
echo "NAIO Witness Signer deploy"
echo "=========================================="
echo "Project dir: $PROJECT_DIR"
echo "Backend dir: $BACKEND_DIR"
echo "Signer dir: $SCRIPT_DIR"
echo "ENV file: $ENV_FILE"
echo "Service name: $SERVICE_NAME"
echo ""

if [ ! -f "$ENV_FILE" ]; then
    echo "ENV file not found: $ENV_FILE"
    echo "Prepare .env first, then run again."
    exit 1
fi

# shellcheck disable=SC1090
set -a
source "$ENV_FILE"
set +a

MISSING=()
for v in WITNESS_HUB_SERVER_URL WITNESS_SIGNER_PRIVATE_KEY; do
    if [ -z "${!v:-}" ]; then
        MISSING+=("$v")
    fi
done
if [ ${#MISSING[@]} -gt 0 ]; then
    echo "Missing required vars in .env:"
    for v in "${MISSING[@]}"; do
        echo "   - $v"
    done
    exit 1
fi

if [ -z "${WITNESS_SIGNER_API_KEY:-}" ]; then
    echo "WARNING: WITNESS_SIGNER_API_KEY not set; Hub auth may reject this signer."
fi
if [ -z "${WITNESS_SIGNER_EXPECTED_ADDRESS:-}" ]; then
    echo "WARNING: WITNESS_SIGNER_EXPECTED_ADDRESS not set; recommended to avoid wrong key."
fi

if [ "$SKIP_DEPS" != "true" ]; then
    echo ""
    echo "Step 1/4: Check and install system dependencies..."
    PKGS=()
    command -v python3 >/dev/null 2>&1 || PKGS+=("python3")
    command -v pip3 >/dev/null 2>&1 || PKGS+=("python3-pip")
    # venv package check (Debian/Ubuntu)
    if ! python3 -m venv --help >/dev/null 2>&1; then
        PKGS+=("python3-venv")
    fi
    if [ "$NO_SUPERVISOR" != "true" ] && ! command -v supervisorctl >/dev/null 2>&1; then
        PKGS+=("supervisor")
    fi
    if [ ${#PKGS[@]} -gt 0 ]; then
        if [ -z "$SUDO" ] && [ "$EUID" -ne 0 ]; then
            echo "Need root/sudo to install: ${PKGS[*]}"
            exit 1
        fi
        $SUDO apt-get update -qq
        $SUDO apt-get install -y -qq "${PKGS[@]}"
        echo "System dependencies installed."
    else
        echo "System dependencies OK."
    fi
else
    echo ""
    echo "Step 1/4: Skipping system deps (--skip-deps)"
fi

echo ""
echo "Step 2/4: Prepare Python venv..."
if [ "$FORCE" = "true" ] && [ -d "$VENV_DIR" ]; then
    rm -rf "$VENV_DIR"
fi
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
fi
source "$VENV_DIR/bin/activate"
pip install --upgrade pip >/dev/null
pip install -r "$BACKEND_DIR/requirements.txt" >/dev/null
deactivate
echo "Venv ready: $VENV_DIR"

RUN_CMD="$VENV_DIR/bin/python $SCRIPT_DIR/witness_signer_service.py"

echo ""
echo "Step 3/4: Pre-run check..."
WITNESS_SIGNER_DOTENV_PATH="$ENV_FILE" "$VENV_DIR/bin/python" - <<'PY'
import os
from eth_account import Account

pk = os.getenv("WITNESS_SIGNER_PRIVATE_KEY", "").strip()
if pk and not pk.startswith("0x"):
    pk = "0x" + pk
addr = Account.from_key(pk).address
print("signer_address =", addr)
expected = os.getenv("WITNESS_SIGNER_EXPECTED_ADDRESS", "").strip()
if expected and expected.lower() != addr.lower():
    raise SystemExit("WITNESS_SIGNER_EXPECTED_ADDRESS mismatch")
print("hub_url =", os.getenv("WITNESS_HUB_SERVER_URL", ""))
PY
echo "Pre-run check OK."

if [ "$NO_SUPERVISOR" = "true" ]; then
    echo ""
    echo "Step 4/4: Skipping supervisor (--no-supervisor)"
    echo "Run manually:"
    echo "  WITNESS_SIGNER_DOTENV_PATH=\"$ENV_FILE\" $RUN_CMD"
    exit 0
fi

echo ""
echo "Step 4/4: Configure and start supervisor..."
if ! command -v supervisorctl >/dev/null 2>&1; then
    echo "supervisorctl not found; install supervisor first."
    exit 1
fi
if [ -z "$SUDO" ] && [ "$EUID" -ne 0 ]; then
    echo "Need root/sudo to write /etc/supervisor/conf.d"
    echo "Or use --no-supervisor to run manually."
    exit 1
fi

SUPERVISOR_CONF="/etc/supervisor/conf.d/${SERVICE_NAME}.conf"
cat <<EOF | $SUDO tee "$SUPERVISOR_CONF" >/dev/null
[program:${SERVICE_NAME}]
directory=${BACKEND_DIR}
command=${RUN_CMD}
autostart=true
autorestart=true
startsecs=3
startretries=999
stopsignal=TERM
stopwaitsecs=30
stdout_logfile=/var/log/${SERVICE_NAME}.log
stdout_logfile_maxbytes=20MB
stdout_logfile_backups=5
stderr_logfile=/var/log/${SERVICE_NAME}.err.log
stderr_logfile_maxbytes=20MB
stderr_logfile_backups=5
environment=PYTHONUNBUFFERED="1",WITNESS_SIGNER_DOTENV_PATH="${ENV_FILE}"
EOF

$SUDO supervisorctl reread >/dev/null
$SUDO supervisorctl update >/dev/null
$SUDO supervisorctl restart "$SERVICE_NAME" >/dev/null || $SUDO supervisorctl start "$SERVICE_NAME" >/dev/null

echo "Deploy done."
$SUDO supervisorctl status "$SERVICE_NAME" || true
echo ""
echo "View logs:"
echo "  tail -f /var/log/${SERVICE_NAME}.log"
echo "  tail -f /var/log/${SERVICE_NAME}.err.log"
