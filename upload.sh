#!/bin/bash
# One-shot upload (local to server, incremental update supported)
# Usage: ./upload.sh [options]
# Options:
#   --full     Full upload (ignore existing files)
#   --dry-run  Show files to upload only, do not upload

set -e

LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"

# Load .env (override with UPLOAD_ENV_FILE; allow exported env vars to win)
ENV_FILE="${UPLOAD_ENV_FILE:-$LOCAL_DIR/.env}"
load_env_file() {
    local file="$1"
    if [ -f "$file" ]; then
        # shellcheck disable=SC1090
        set -a
        source "$file"
        set +a
        return 0
    fi
    return 1
}
load_env_file "$ENV_FILE" || true

: "${SERVER:=}"
: "${REMOTE_DIR:=}"
if [ -z "$SERVER" ] || [ -z "$REMOTE_DIR" ]; then
    echo "❌ Missing config. Please set SERVER and REMOTE_DIR in $ENV_FILE (or export them)."
    echo "   Example:"
    echo "     SERVER=root@your.server"
    echo "     REMOTE_DIR=/opt/naio"
    exit 1
fi

# Parse arguments
DRY_RUN=false
FULL_UPLOAD=false
for arg in "$@"; do
    case $arg in
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --full)
            FULL_UPLOAD=true
            shift
            ;;
        *)
            echo "Unknown argument: $arg"
            echo "Usage: $0 [--dry-run] [--full]"
            exit 1
            ;;
    esac
done

echo "=========================================="
echo "NAIO project upload to server"
echo "=========================================="
echo "Server: $SERVER"
echo "Remote dir: $REMOTE_DIR"
echo "Local dir: $LOCAL_DIR"
if [ "$DRY_RUN" = true ]; then
    echo "Mode: preview (no actual upload)"
elif [ "$FULL_UPLOAD" = true ]; then
    echo "Mode: full upload"
else
    echo "Mode: incremental (modified files only)"
fi
echo ""

# Check SSH connection
echo "Step 1: Checking SSH connection..."
if ssh -o ConnectTimeout=5 -o BatchMode=yes $SERVER "echo 'SSH OK'" 2>/dev/null; then
    echo "✅ SSH OK"
else
    echo "❌ SSH failed. Check:"
    echo "   1. Server address: $SERVER"
    echo "   2. SSH key configured"
    echo "   3. Server reachable"
    exit 1
fi

# Check remote directory
echo ""
echo "Step 2: Checking remote directory..."
if ssh $SERVER "[ -d '$REMOTE_DIR' ]" 2>/dev/null; then
    echo "✅ Remote dir exists: $REMOTE_DIR"
    IS_FIRST_UPLOAD=false
else
    echo "📁 Creating remote dir: $REMOTE_DIR"
    ssh $SERVER "mkdir -p $REMOTE_DIR"
    IS_FIRST_UPLOAD=true
fi

# Check remote .env (preserve existing config)
HAS_REMOTE_ENV=false
if ssh $SERVER "[ -f '$REMOTE_DIR/backend/.env' ]" 2>/dev/null; then
    HAS_REMOTE_ENV=true
    echo "✅ Remote .env found (will not be overwritten)"
fi

# Build rsync options
RSYNC_OPTS="-avz --progress"
if [ "$DRY_RUN" = true ]; then
    RSYNC_OPTS="$RSYNC_OPTS --dry-run"
fi

# Exclude list (protect server config and cache)
EXCLUDE_LIST=(
    --exclude='.git'
    --exclude='.github'
    --exclude='.gitignore'
    --exclude='.gitattributes'
    --exclude='.cursor'
    --exclude='.cursorignore'
    --exclude='.idea'
    --exclude='.vscode'
    --exclude='__pycache__'
    --exclude='*.pyc'
    --exclude='*.pyo'
    --exclude='*.pyd'
    --exclude='.env'              # protect server .env
    --exclude='node_modules'
    --exclude='.foundry'
    --exclude='out'
    --exclude='cache'
    --exclude='.pytest_cache'
    --exclude='.mypy_cache'
    --exclude='.ruff_cache'
    --exclude='.coverage'
    --exclude='htmlcov'
    --exclude='build'
    --exclude='dist'
    --exclude='extract'
    --exclude='docs/0x1095DC37aB3b09C4D303F16E20D0ad4a50f39819'
    # not required for runtime: docs and local helpers
    --exclude='docs'
    --exclude='Keeper-GUI'
    --exclude='tools'
    # not required: root docs
    --exclude='*.md'
    # not required: backend test/analysis scripts
    --exclude='backend/test_*.py'
    --exclude='backend/analyze_*.py'
    --exclude='backend/backfill_*.py'
    --exclude='backend/query_stats.py'
    --exclude='backend/witness_signer_gui.py'
    --exclude='venv'              # server venv
    --exclude='.venv'
    --exclude='env'
    --exclude='ENV'
    --exclude='*.log'            # log files
    --exclude='*.tmp'
    --exclude='*.swp'
    --exclude='*.swo'
    # protect runtime DB (do not overwrite production)
    --exclude='backend/*.db'
    --exclude='backend/*.db-wal'
    --exclude='backend/*.db-shm'
    --exclude='price_history.db'
    --exclude='*.db'
    --exclude='*.db-wal'
    --exclude='*.db-shm'
    --exclude='*.sqlite'
    --exclude='*.sqlite3'
    # protect runtime state (do not overwrite progress)
    --exclude='backend/telegram_bot_state.json'
    --exclude='backend/keeper_validator_state.json'
    --exclude='backend/*.state.json'
    --exclude='backend/*.pid'
    --exclude='backend/*.sock'
    # local sensitive test data (do not upload)
    --exclude='nodes_keys.json'
    --exclude='nodes_keys.csv'
    --exclude='nodes_list.txt'
    --exclude='.DS_Store'        # macOS
    --exclude='Thumbs.db'        # Windows
)

# Upload
echo ""
if [ "$DRY_RUN" = true ]; then
    echo "Step 3: Preview files to upload..."
else
    echo "Step 3: Uploading (incremental)..."
fi

rsync $RSYNC_OPTS \
    "${EXCLUDE_LIST[@]}" \
    "$LOCAL_DIR/" "$SERVER:$REMOTE_DIR/"

if [ "$DRY_RUN" = true ]; then
    echo ""
    echo "=========================================="
    echo "Preview done (no upload)"
    echo "=========================================="
    echo ""
    echo "To upload, run: $0"
    exit 0
fi

echo "✅ Upload complete"

# Set permissions
echo ""
echo "Step 4: Setting permissions..."
ssh $SERVER "chmod +x $REMOTE_DIR/deploy.sh $REMOTE_DIR/upload.sh 2>/dev/null || true"
echo "✅ Permissions set"

# Show update summary
echo ""
echo "Step 5: Checking versions..."
REMOTE_VERSION=$(ssh $SERVER "cd $REMOTE_DIR && git log -1 --format='%h %s' 2>/dev/null || echo 'unknown'" 2>/dev/null || echo "unknown")
LOCAL_VERSION=$(cd "$LOCAL_DIR" && git log -1 --format='%h %s' 2>/dev/null || echo "unknown")

echo ""
echo "=========================================="
echo "Upload complete!"
echo "=========================================="
echo ""
if [ "$IS_FIRST_UPLOAD" = true ]; then
    echo "📦 First upload done"
    echo ""
    echo "Next: SSH to server and run deploy"
    echo "  ssh $SERVER"
    echo "  cd $REMOTE_DIR"
    echo "  ./deploy.sh"
else
    echo "🔄 Incremental update done"
    echo ""
    echo "Local:  $LOCAL_VERSION"
    echo "Remote: $REMOTE_VERSION"
    echo ""
    echo "Tips:"
    echo "  - If Python deps changed: cd $REMOTE_DIR/backend && source venv/bin/activate && pip install -r requirements.txt"
    echo "  - If contracts changed: cd $REMOTE_DIR/contracts && forge build"
    echo "  - If service config changed: supervisorctl reread && supervisorctl update"
    echo "  - Restart: supervisorctl restart naio-listen-deposits naio-call-poke naio-price-recorder naio-telegram-bot"
fi
echo ""
