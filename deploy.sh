#!/bin/bash
# One-shot deploy (run on Ubuntu 22.04 server)
# Usage: ./deploy.sh [options]
# Options:
#   --skip-deps    Skip dependency install (code update only)
#   --skip-deploy  Skip contract deployment
#   --force        Force reinstall (overwrite existing config)
#   --dapp-only    Deploy governance DApp service only (leave other services unchanged)

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR"
BACKEND_DIR="$PROJECT_DIR/backend"
CONTRACTS_DIR="$PROJECT_DIR/contracts"
DAPP_DIR="$PROJECT_DIR/dapp"

# Load .env (prefer backend/.env; override with DEPLOY_ENV_FILE)
ENV_FILE="${DEPLOY_ENV_FILE:-$BACKEND_DIR/.env}"
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

# Parse arguments
SKIP_DEPS=false
SKIP_DEPLOY=false
FORCE=false
DAPP_ONLY=false
for arg in "$@"; do
    case $arg in
        --skip-deps)
            SKIP_DEPS=true
            ;;
        --skip-deploy)
            SKIP_DEPLOY=true
            ;;
        --force)
            FORCE=true
            ;;
        --dapp-only)
            DAPP_ONLY=true
            ;;
        *)
            ;;
    esac
done

echo "=========================================="
if [ "$DAPP_ONLY" = true ]; then
    echo "NAIO Governance DApp - deploy this service only"
else
    echo "NAIO project deploy"
fi
echo "=========================================="
echo "Project dir: $PROJECT_DIR"
echo ""

# ============================================
# --dapp-only: deploy DApp service only
# ============================================
if [ "$DAPP_ONLY" = true ]; then
    echo "Deploying governance DApp only..."
    load_env_file "$ENV_FILE" || true

    if [ ! -f "$BACKEND_DIR/venv/bin/python" ]; then
        echo "❌ No Python venv found. Run full deploy first: ./deploy.sh --skip-deploy"
        exit 1
    fi

    DAPP_PORT="${DAPP_PORT:-8765}"
    mkdir -p /etc/supervisor/conf.d

    cat > /etc/supervisor/conf.d/naio-dapp.conf <<EOF
[program:naio-dapp]
command=$BACKEND_DIR/venv/bin/python $DAPP_DIR/server.py
directory=$DAPP_DIR
user=root
autostart=true
autorestart=true
startretries=3
stderr_logfile=/var/log/naio-dapp.err.log
stdout_logfile=/var/log/naio-dapp.out.log
environment=PATH="$BACKEND_DIR/venv/bin:%(ENV_PATH)s",DAPP_PORT="$DAPP_PORT"
EOF

    echo "   Wrote /etc/supervisor/conf.d/naio-dapp.conf"
    supervisorctl reread
    supervisorctl update
    supervisorctl start naio-dapp 2>/dev/null || supervisorctl restart naio-dapp
    echo ""
    echo "✅ DApp service started"
    echo "   URL: http://localhost:$DAPP_PORT/governance.html"
    echo "   Log: tail -f /var/log/naio-dapp.out.log"
    echo "   Manage: supervisorctl status naio-dapp"
    exit 0
fi

# Check root
if [ "$EUID" -ne 0 ]; then 
    echo "⚠️  Recommend running as root (some steps need sudo)"
    echo "   Continuing..."
fi

# ============================================
# Environment check
# ============================================
echo ""
echo "Environment check..."

# Check Python version
if command -v python3 &> /dev/null; then
    PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
    PYTHON_MAJOR=$(echo $PYTHON_VERSION | cut -d. -f1)
    PYTHON_MINOR=$(echo $PYTHON_VERSION | cut -d. -f2)
    echo "✅ Python version: $PYTHON_VERSION"
    
    # Require Python >= 3.8
    if [ "$PYTHON_MAJOR" -lt 3 ] || ([ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 8 ]); then
        echo "⚠️  Python too old (need >= 3.8), current: $PYTHON_VERSION"
        echo "   Will try to install Python 3.10..."
    fi
else
    echo "⚠️  Python 3 not installed, will install"
fi

# Check existing services
echo ""
echo "Checking existing services..."
EXISTING_SERVICES=()
if supervisorctl status naio-listen-deposits &> /dev/null; then
    EXISTING_SERVICES+=("naio-listen-deposits")
    SERVICE_STATUS=$(supervisorctl status naio-listen-deposits | awk '{print $2}')
    echo "  - naio-listen-deposits: $SERVICE_STATUS"
fi
if supervisorctl status naio-call-poke &> /dev/null; then
    EXISTING_SERVICES+=("naio-call-poke")
    SERVICE_STATUS=$(supervisorctl status naio-call-poke | awk '{print $2}')
    echo "  - naio-call-poke: $SERVICE_STATUS"
fi
if supervisorctl status naio-price-recorder &> /dev/null; then
    EXISTING_SERVICES+=("naio-price-recorder")
    SERVICE_STATUS=$(supervisorctl status naio-price-recorder | awk '{print $2}')
    echo "  - naio-price-recorder: $SERVICE_STATUS"
fi
if supervisorctl status naio-telegram-bot &> /dev/null; then
    EXISTING_SERVICES+=("naio-telegram-bot")
    SERVICE_STATUS=$(supervisorctl status naio-telegram-bot | awk '{print $2}')
    echo "  - naio-telegram-bot: $SERVICE_STATUS"
fi
if supervisorctl status naio-keeper-validator &> /dev/null; then
    EXISTING_SERVICES+=("naio-keeper-validator")
    SERVICE_STATUS=$(supervisorctl status naio-keeper-validator | awk '{print $2}')
    echo "  - naio-keeper-validator: $SERVICE_STATUS"
fi

if [ ${#EXISTING_SERVICES[@]} -gt 0 ]; then
    echo "⚠️  Existing services detected"
    if [ "$FORCE" != "true" ]; then
        echo "   Will restart after update (existing services not stopped)"
    else
        echo "   Force mode: will stop and reconfigure services"
        for service in "${EXISTING_SERVICES[@]}"; do
            supervisorctl stop "$service" > /dev/null 2>&1 || true
        done
    fi
else
    echo "✅ No existing services"
fi

# Check port usage (optional)
echo ""
echo "Checking ports..."
if command -v netstat &> /dev/null; then
    echo "  (this project does not bind fixed ports)"
elif command -v ss &> /dev/null; then
    echo "  (this project does not bind fixed ports)"
fi

# Check existing venv
if [ -d "$BACKEND_DIR/venv" ]; then
    echo ""
    echo "✅ Existing Python venv found"
    VENV_PYTHON="$BACKEND_DIR/venv/bin/python"
    if [ -f "$VENV_PYTHON" ]; then
        VENV_VERSION=$($VENV_PYTHON --version 2>&1 | awk '{print $2}')
        echo "   Venv Python: $VENV_VERSION"
    fi
fi

echo ""

# ============================================
# Step 1: Install system deps
# ============================================
if [ "$SKIP_DEPS" != "true" ]; then
    echo ""
    echo "Step 1: Installing system deps..."
    
    if ! command -v python3 &> /dev/null || [ "$FORCE" = "true" ]; then
        apt-get update -qq
        apt-get install -y -qq \
            python3 \
            python3-pip \
            python3-venv \
            > /dev/null 2>&1
        echo "✅ Python installed/updated"
    else
        echo "✅ Python already installed, skipping"
    fi
    
    PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}' | cut -d. -f1,2)
    PYTHON_MAJOR=$(echo $PYTHON_VERSION | cut -d. -f1)
    PYTHON_MINOR=$(echo $PYTHON_VERSION | cut -d. -f2)
    PYTHON_VENV_PKG="python${PYTHON_MAJOR}.${PYTHON_MINOR}-venv"
    
    if ! dpkg -l | grep -q "^ii.*${PYTHON_VENV_PKG}"; then
        echo "   Installing ${PYTHON_VENV_PKG} (required for venv)..."
        apt-get update -qq
        apt-get install -y -qq "$PYTHON_VENV_PKG" > /dev/null 2>&1
        echo "✅ ${PYTHON_VENV_PKG} installed"
    else
        echo "✅ ${PYTHON_VENV_PKG} already installed"
    fi
    
    MISSING_DEPS=()
    for dep in curl git build-essential jq supervisor; do
        if ! command -v $dep &> /dev/null; then
            MISSING_DEPS+=($dep)
        fi
    done
    
    if [ ${#MISSING_DEPS[@]} -gt 0 ]; then
        echo "   Installing missing deps: ${MISSING_DEPS[*]}"
        apt-get update -qq
        apt-get install -y -qq "${MISSING_DEPS[@]}" > /dev/null 2>&1
    fi
    
    echo "✅ System deps done"
else
    echo ""
    echo "Step 1: Skipping deps (--skip-deps)"
fi

# ============================================
# Step 2: Install Foundry (for contract deploy)
# ============================================
if [ "$SKIP_DEPS" != "true" ]; then
    echo ""
    echo "Step 2: Installing Foundry..."
    if command -v forge &> /dev/null; then
        FORGE_VERSION=$(forge --version | head -n1)
        echo "✅ Foundry installed: $FORGE_VERSION"
        
        if [ "$FORCE" = "true" ]; then
            echo "   Force: updating Foundry..."
            export PATH="$HOME/.foundry/bin:$PATH"
            foundryup
            echo "✅ Foundry updated"
        fi
    else
        echo "   Installing Foundry..."
        curl -L https://foundry.paradigm.xyz | bash
        export PATH="$HOME/.foundry/bin:$PATH"
        foundryup
        echo "✅ Foundry installed"
    fi
    
    if command -v forge &> /dev/null; then
        FORGE_VERSION=$(forge --version | head -n1)
        echo "   Foundry version: $FORGE_VERSION"
    else
        echo "❌ Foundry install failed, install manually"
        exit 1
    fi
else
    echo ""
    echo "Step 2: Skipping Foundry (--skip-deps)"
fi

# ============================================
# Step 2.1: Foundry contract deps (forge-std)
# ============================================
echo ""
echo "Step 2.1: Checking Foundry contract deps..."
if [ ! -f "$CONTRACTS_DIR/lib/forge-std/src/Test.sol" ]; then
    echo "   forge-std not found, installing..."
    cd "$CONTRACTS_DIR"
    if forge install foundry-rs/forge-std > /dev/null 2>&1; then
        echo "✅ forge-std installed"
    elif forge install foundry-rs/forge-std --commit > /dev/null 2>&1; then
        echo "✅ forge-std installed (--commit)"
    elif command -v git > /dev/null 2>&1; then
        echo "   forge install failed, trying git clone..."
        mkdir -p "$CONTRACTS_DIR/lib"
        rm -rf "$CONTRACTS_DIR/lib/forge-std"
        if git clone --depth 1 https://github.com/foundry-rs/forge-std.git "$CONTRACTS_DIR/lib/forge-std" > /dev/null 2>&1; then
            echo "✅ forge-std installed (git clone)"
        else
            echo "❌ forge-std install failed (git clone failed)"
            echo "   Check network and run manually:"
            echo "   mkdir -p $CONTRACTS_DIR/lib && git clone https://github.com/foundry-rs/forge-std.git $CONTRACTS_DIR/lib/forge-std"
            exit 1
        fi
    else
        echo "❌ forge-std install failed, run manually:"
        echo "   mkdir -p $CONTRACTS_DIR/lib && git clone https://github.com/foundry-rs/forge-std.git $CONTRACTS_DIR/lib/forge-std"
        exit 1
    fi
else
    echo "✅ forge-std already installed"
fi

# ============================================
# Step 3: Python environment
# ============================================
echo ""
echo "Step 3: Configuring Python environment..."
cd "$BACKEND_DIR"

PYTHON_CMD=python3
if command -v python3.10 &> /dev/null; then
    PYTHON_CMD=python3.10
    PYTHON_VERSION="3.10"
    echo "   Using Python 3.10"
elif command -v python3.9 &> /dev/null; then
    PYTHON_CMD=python3.9
    PYTHON_VERSION="3.9"
    echo "   Using Python 3.9"
else
    PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}' | cut -d. -f1,2)
    echo "   Using Python $PYTHON_VERSION"
fi

PYTHON_MAJOR=$(echo $PYTHON_VERSION | cut -d. -f1)
PYTHON_MINOR=$(echo $PYTHON_VERSION | cut -d. -f2)
PYTHON_VENV_PKG="python${PYTHON_MAJOR}.${PYTHON_MINOR}-venv"

if ! dpkg -l | grep -q "^ii.*${PYTHON_VENV_PKG}"; then
    echo "   Installing ${PYTHON_VENV_PKG} (required for venv)..."
    apt-get update -qq
    apt-get install -y -qq "$PYTHON_VENV_PKG" > /dev/null 2>&1
    if [ $? -eq 0 ]; then
        echo "✅ ${PYTHON_VENV_PKG} installed"
    else
        echo "⚠️  ${PYTHON_VENV_PKG} failed, trying python3-venv"
        apt-get install -y -qq python3-venv > /dev/null 2>&1
    fi
fi

if [ ! -d "venv" ]; then
    echo "   Creating Python venv..."
    if $PYTHON_CMD -m venv venv 2>&1; then
        echo "✅ Python venv created"
    else
        echo "❌ Venv creation failed, trying python3 -m venv"
        python3 -m venv venv
        if [ $? -eq 0 ]; then
            echo "✅ Python venv created (python3)"
        else
            echo "❌ Venv failed. Install python3-venv or python${PYTHON_MAJOR}.${PYTHON_MINOR}-venv"
            exit 1
        fi
    fi
elif [ "$FORCE" = "true" ]; then
    echo "   Force: recreating venv..."
    rm -rf venv
    if $PYTHON_CMD -m venv venv 2>&1; then
        echo "✅ Python venv recreated"
    else
        echo "❌ Venv failed, trying python3 -m venv"
        python3 -m venv venv
        if [ $? -eq 0 ]; then
            echo "✅ Python venv recreated (python3)"
        else
            echo "❌ Venv failed"
            exit 1
        fi
    fi
else
    if [ ! -f "venv/bin/activate" ]; then
        echo "⚠️  Venv dir exists but incomplete, recreating..."
        rm -rf venv
        if $PYTHON_CMD -m venv venv 2>&1; then
            echo "✅ Python venv recreated"
        else
            echo "❌ Venv failed, trying python3 -m venv"
            python3 -m venv venv
            if [ $? -eq 0 ]; then
                echo "✅ Python venv recreated (python3)"
            else
                echo "❌ Venv failed"
                exit 1
            fi
        fi
    else
        echo "✅ Python venv exists and complete"
    fi
fi

if [ ! -f "venv/bin/activate" ]; then
    echo "❌ Venv activate missing, check venv creation"
    exit 1
fi

source venv/bin/activate

VENV_PYTHON_VERSION=$(python --version 2>&1 | awk '{print $2}')
echo "   Venv Python: $VENV_PYTHON_VERSION"

echo "   Updating pip..."
pip install --upgrade pip -q

if [ -f "requirements.txt" ]; then
    echo "   Installing/updating Python deps..."
    pip install -r requirements.txt -q
    echo "✅ Python deps installed"
else
    echo "⚠️  requirements.txt not found"
fi

# ============================================
# Step 4: Environment variables
# ============================================
echo ""
echo "Step 4: Configuring env..."
if [ ! -f "$BACKEND_DIR/.env" ]; then
    if [ -f "$BACKEND_DIR/env.example" ]; then
        cp "$BACKEND_DIR/env.example" "$BACKEND_DIR/.env"
        echo "✅ .env created (from template)"
        echo ""
        echo "⚠️  Edit .env and set:"
        echo "   - DEPLOYER_PRIVATE_KEY"
        echo "   - KEEPER_PRIVATE_KEY (deploy script adds keeper whitelist)"
        echo "   - KEEPER_GOVERNOR_ADDRESS (keeper governance whitelist, suggest multisig)"
        echo "   - USDT_ADDRESS"
        echo "   - TRANSFER_TAX_RECEIVER_C / ECO_A / INDEPENDENT_B / MARKET_E / MARKET_F"
        echo "   - REFERRAL_BOOTSTRAP_ADDRESS"
        echo "   - CONTROLLER_ADDRESS (fill after deploy)"
        echo ""
        echo "   Edit: nano $BACKEND_DIR/.env"
        echo ""
        if [ -t 0 ]; then
            read -p "Press Enter to continue (fill CONTROLLER_ADDRESS after deploy)..."
        else
            echo "(non-interactive: skipping prompt)"
        fi
    else
        echo "⚠️  env.example not found, skipping .env creation"
    fi
else
    echo "✅ .env exists (not overwritten)"
    
    if grep -q "CONTROLLER_ADDRESS=$" "$BACKEND_DIR/.env" 2>/dev/null || ! grep -q "CONTROLLER_ADDRESS=" "$BACKEND_DIR/.env" 2>/dev/null; then
        echo "⚠️  CONTROLLER_ADDRESS not set"
    else
        CONTROLLER_ADDR=$(grep "^CONTROLLER_ADDRESS=" "$BACKEND_DIR/.env" | cut -d'=' -f2 | tr -d '"' | tr -d "'")
        if [ -n "$CONTROLLER_ADDR" ] && [ "$CONTROLLER_ADDR" != "" ]; then
            echo "   CONTROLLER_ADDRESS: $CONTROLLER_ADDR"
        fi
    fi
fi

if ! load_env_file "$BACKEND_DIR/.env"; then
    echo "⚠️  $BACKEND_DIR/.env not found, deploy params may be empty"
fi

if [ -z "$KEEPER_GOVERNOR_ADDRESS" ] && [ -n "$KEEPER_COUNCIL_ADDRESS" ]; then
    KEEPER_GOVERNOR_ADDRESS="$KEEPER_COUNCIL_ADDRESS"
fi

# ============================================
# Step 5: Deploy contracts to BSC testnet
# ============================================
if [ "$SKIP_DEPLOY" != "true" ]; then
    echo ""
    echo "Step 5: Deploying contracts to BSC testnet..."
    cd "$CONTRACTS_DIR"

if [ -z "$DEPLOYER_PRIVATE_KEY" ] || [ -z "$KEEPER_PRIVATE_KEY" ] || [ -z "$KEEPER_GOVERNOR_ADDRESS" ] || [ -z "$USDT_ADDRESS" ] || [ -z "$TRANSFER_TAX_RECEIVER_C" ] || [ -z "$ECO_A" ] || [ -z "$INDEPENDENT_B" ] || [ -z "$MARKET_E" ] || [ -z "$MARKET_F" ] || [ -z "$REFERRAL_BOOTSTRAP_ADDRESS" ] || [ -z "$WITNESS_SIGNER_1" ] || [ -z "$WITNESS_SIGNER_2" ] || [ -z "$WITNESS_SIGNER_3" ]; then
    echo ""
    echo "❌ Missing deploy params (edit $BACKEND_DIR/.env):"
    echo "  DEPLOYER_PRIVATE_KEY"
    echo "  KEEPER_PRIVATE_KEY"
    echo "  KEEPER_GOVERNOR_ADDRESS"
    echo "  USDT_ADDRESS"
    echo "  TRANSFER_TAX_RECEIVER_C / ECO_A / INDEPENDENT_B / MARKET_E / MARKET_F"
    echo "  REFERRAL_BOOTSTRAP_ADDRESS"
    echo "  WITNESS_SIGNER_1~3"
    echo ""
    echo "Then run: ./deploy.sh"
    exit 1
fi

if [ "$SKIP_DEPLOY" != "true" ] && [ -n "$DEPLOYER_PRIVATE_KEY" ]; then
    echo ""
    echo "Deploying contracts..."
    
    DEPLOY_RPC_URL="${QUICKNODE_HTTP_URL:-${BSC_TESTNET_RPC:-https://data-seed-prebsc-1-s1.binance.org:8545/}}"
    CHAIN_ID=97
    
    echo "   Building contracts..."
    forge build --force > /dev/null 2>&1
    echo "✅ Contracts built"
    
    echo "   Deploying (params from backend/.env)..."
    forge script script/DeployTestnet.s.sol:DeployTestnet \
        --rpc-url "$DEPLOY_RPC_URL" \
        --private-key "$DEPLOYER_PRIVATE_KEY" \
        --broadcast \
        --legacy \
        --slow
    
    echo ""
    echo "✅ Contract deploy done"
    echo ""
    echo "⚠️  Record deployed addresses and set CONTROLLER_ADDRESS in .env"
    else
        echo "Skipping contract deploy"
    fi
else
    echo ""
    echo "Step 5: Skipping contract deploy (--skip-deploy)"
fi

# ============================================
# Step 6: Supervisor (Python services)
# ============================================
echo ""
echo "Step 6: Configuring Supervisor..."

mkdir -p /etc/supervisor/conf.d

NEED_UPDATE=false
if [ "$FORCE" = "true" ] \
    || [ ! -f "/etc/supervisor/conf.d/naio-listen-deposits.conf" ] \
    || [ ! -f "/etc/supervisor/conf.d/naio-call-poke.conf" ] \
    || [ ! -f "/etc/supervisor/conf.d/naio-price-recorder.conf" ] \
    || [ ! -f "/etc/supervisor/conf.d/naio-telegram-bot.conf" ] \
    || [ ! -f "/etc/supervisor/conf.d/naio-keeper-validator.conf" ] \
    || [ ! -f "/etc/supervisor/conf.d/naio-dapp.conf" ]; then
    NEED_UPDATE=true
fi

# Deposit listener
cat > /etc/supervisor/conf.d/naio-listen-deposits.conf <<EOF
[program:naio-listen-deposits]
command=$BACKEND_DIR/venv/bin/python $BACKEND_DIR/listen_deposits.py
directory=$BACKEND_DIR
user=root
autostart=true
autorestart=true
startretries=3
stderr_logfile=/var/log/naio-listen-deposits.err.log
stdout_logfile=/var/log/naio-listen-deposits.out.log
environment=PATH="$BACKEND_DIR/venv/bin:%(ENV_PATH)s"
EOF

# Poke (deflation) cron
cat > /etc/supervisor/conf.d/naio-call-poke.conf <<EOF
[program:naio-call-poke]
command=$BACKEND_DIR/venv/bin/python $BACKEND_DIR/call_poke.py
directory=$BACKEND_DIR
user=root
autostart=true
autorestart=true
startretries=3
stderr_logfile=/var/log/naio-call-poke.err.log
stdout_logfile=/var/log/naio-call-poke.out.log
environment=PATH="$BACKEND_DIR/venv/bin:%(ENV_PATH)s"
EOF

# Price recorder
cat > /etc/supervisor/conf.d/naio-price-recorder.conf <<EOF
[program:naio-price-recorder]
command=$BACKEND_DIR/venv/bin/python $BACKEND_DIR/price_recorder.py
directory=$BACKEND_DIR
user=root
autostart=true
autorestart=true
startretries=3
stderr_logfile=/var/log/naio-price-recorder.err.log
stdout_logfile=/var/log/naio-price-recorder.out.log
environment=PATH="$BACKEND_DIR/venv/bin:%(ENV_PATH)s"
EOF
# Telegram Bot (optional)
cat > /etc/supervisor/conf.d/naio-telegram-bot.conf <<EOF
[program:naio-telegram-bot]
command=$BACKEND_DIR/venv/bin/python $BACKEND_DIR/telegram_bot.py
directory=$BACKEND_DIR
user=root
autostart=true
autorestart=true
startretries=3
stderr_logfile=/var/log/naio-telegram-bot.err.log
stdout_logfile=/var/log/naio-telegram-bot.out.log
environment=PATH="$BACKEND_DIR/venv/bin:%(ENV_PATH)s"
EOF

# Keeper validator (separate from main bot)
cat > /etc/supervisor/conf.d/naio-keeper-validator.conf <<EOF
[program:naio-keeper-validator]
command=$BACKEND_DIR/venv/bin/python $BACKEND_DIR/keeper_validator.py
directory=$BACKEND_DIR
user=root
autostart=false
autorestart=true
startretries=3
stderr_logfile=/var/log/naio-keeper-validator.err.log
stdout_logfile=/var/log/naio-keeper-validator.out.log
environment=PATH="$BACKEND_DIR/venv/bin:%(ENV_PATH)s"
EOF

# Governance DApp (config from .env)
DAPP_PORT="${DAPP_PORT:-8765}"
cat > /etc/supervisor/conf.d/naio-dapp.conf <<EOF
[program:naio-dapp]
command=$BACKEND_DIR/venv/bin/python $DAPP_DIR/server.py
directory=$DAPP_DIR
user=root
autostart=true
autorestart=true
startretries=3
stderr_logfile=/var/log/naio-dapp.err.log
stdout_logfile=/var/log/naio-dapp.out.log
environment=PATH="$BACKEND_DIR/venv/bin:%(ENV_PATH)s",DAPP_PORT="$DAPP_PORT"
EOF

# Reload Supervisor
if [ "$NEED_UPDATE" = true ] || [ "$FORCE" = "true" ]; then
    echo "   Updating Supervisor config..."
    if ! supervisorctl reread; then
        echo "❌ supervisorctl reread failed, check /etc/supervisor/conf.d/*.conf"
        exit 1
    fi
    if ! supervisorctl update; then
        echo "❌ supervisorctl update failed, fix config and retry"
        exit 1
    fi
    echo "✅ Supervisor config updated"
else
    echo "✅ Supervisor config exists (includes naio-keeper-validator)"
fi

# ============================================
# Step 7: Start services
# ============================================
echo ""
echo "Step 7: Starting services..."

# Check CONTROLLER_ADDRESS
HAS_CONTROLLER=false
if [ -f "$BACKEND_DIR/.env" ]; then
    CONTROLLER_ADDR=$(grep "^CONTROLLER_ADDRESS=" "$BACKEND_DIR/.env" 2>/dev/null | cut -d'=' -f2 | tr -d '"' | tr -d "'" | tr -d ' ')
    if [ -n "$CONTROLLER_ADDR" ] && [ "$CONTROLLER_ADDR" != "" ]; then
        HAS_CONTROLLER=true
    fi
fi

# Check TELEGRAM_BOT_TOKEN
HAS_TELEGRAM=false
if [ -f "$BACKEND_DIR/.env" ]; then
    TELEGRAM_TOKEN=$(grep "^TELEGRAM_BOT_TOKEN=" "$BACKEND_DIR/.env" 2>/dev/null | cut -d'=' -f2 | tr -d '"' | tr -d "'" | tr -d ' ')
    if [ -n "$TELEGRAM_TOKEN" ] && [ "$TELEGRAM_TOKEN" != "" ]; then
        HAS_TELEGRAM=true
    fi
fi

# Check validator config
HAS_VALIDATOR=false
if [ -f "$BACKEND_DIR/.env" ]; then
    VALIDATOR_PK=$(grep "^VALIDATOR_PRIVATE_KEY=" "$BACKEND_DIR/.env" 2>/dev/null | cut -d'=' -f2 | tr -d '"' | tr -d "'" | tr -d ' ')
    VALIDATOR_BOT_TOKEN=$(grep "^VALIDATOR_TELEGRAM_BOT_TOKEN=" "$BACKEND_DIR/.env" 2>/dev/null | cut -d'=' -f2 | tr -d '"' | tr -d "'" | tr -d ' ')
    VALIDATOR_CHAT_IDS=$(grep "^VALIDATOR_TELEGRAM_CHAT_IDS=" "$BACKEND_DIR/.env" 2>/dev/null | cut -d'=' -f2- | tr -d '"' | tr -d "'" | tr -d ' ')
    VALIDATOR_CHAT_ID=$(grep "^VALIDATOR_TELEGRAM_CHAT_ID=" "$BACKEND_DIR/.env" 2>/dev/null | cut -d'=' -f2 | tr -d '"' | tr -d "'" | tr -d ' ')
    if [ -n "$VALIDATOR_PK" ] && [ -n "$VALIDATOR_BOT_TOKEN" ] && { [ -n "$VALIDATOR_CHAT_IDS" ] || [ -n "$VALIDATOR_CHAT_ID" ]; }; then
        HAS_VALIDATOR=true
    fi
fi

# Start or restart services
if [ "$HAS_CONTROLLER" = true ]; then
    echo "   Starting/restarting services..."
    for service in naio-listen-deposits naio-call-poke naio-price-recorder; do
        if supervisorctl status "$service" &> /dev/null; then
            STATUS=$(supervisorctl status "$service" | awk '{print $2}')
            if [ "$STATUS" = "RUNNING" ]; then
                echo "   Restarting $service..."
                supervisorctl restart "$service" > /dev/null 2>&1 || true
            else
                echo "   Starting $service..."
                supervisorctl start "$service" > /dev/null 2>&1 || true
            fi
        else
            echo "   Starting $service..."
            supervisorctl start "$service" > /dev/null 2>&1 || true
        fi
    done
    echo "✅ Services started/restarted"
else
    echo "⚠️  CONTROLLER_ADDRESS not set, services will not start"
    echo "   Edit .env, set CONTROLLER_ADDRESS, then run:"
    echo "   supervisorctl start naio-listen-deposits"
    echo "   supervisorctl start naio-call-poke"
    echo "   supervisorctl start naio-price-recorder"
fi

# Telegram Bot (does not depend on CONTROLLER_ADDRESS)
if [ "$HAS_TELEGRAM" = true ]; then
    echo "   Starting/restarting naio-telegram-bot..."
    if supervisorctl status "naio-telegram-bot" &> /dev/null; then
        STATUS=$(supervisorctl status "naio-telegram-bot" | awk '{print $2}')
        if [ "$STATUS" = "RUNNING" ]; then
            supervisorctl restart "naio-telegram-bot" > /dev/null 2>&1 || true
        else
            supervisorctl start "naio-telegram-bot" > /dev/null 2>&1 || true
        fi
    else
        supervisorctl start "naio-telegram-bot" > /dev/null 2>&1 || true
    fi
fi

# Keeper validator (independent of main bot)
if [ "$HAS_VALIDATOR" = true ]; then
    echo "   Starting/restarting naio-keeper-validator..."
    if supervisorctl status "naio-keeper-validator" &> /dev/null; then
        STATUS=$(supervisorctl status "naio-keeper-validator" | awk '{print $2}')
        if [ "$STATUS" = "RUNNING" ]; then
            supervisorctl restart "naio-keeper-validator" > /dev/null 2>&1 || true
        else
            supervisorctl start "naio-keeper-validator" > /dev/null 2>&1 || true
        fi
    else
        echo "   Registering and starting naio-keeper-validator..."
        supervisorctl reread || true
        supervisorctl update || true
        if ! supervisorctl start "naio-keeper-validator"; then
            echo "⚠️  naio-keeper-validator start failed:"
            echo "   1) Check /etc/supervisor/conf.d/naio-keeper-validator.conf exists"
            echo "   2) Logs: tail -n 120 /var/log/naio-keeper-validator.err.log"
        fi
    fi
else
    echo "   Skipping naio-keeper-validator (missing VALIDATOR_PRIVATE_KEY / VALIDATOR_TELEGRAM_BOT_TOKEN / VALIDATOR_TELEGRAM_CHAT_IDS)"
fi

# Governance DApp (requires KEEPER_COUNCIL_ADDRESS)
HAS_COUNCIL=false
if [ -f "$BACKEND_DIR/.env" ]; then
    COUNCIL_ADDR=$(grep "^KEEPER_COUNCIL_ADDRESS=" "$BACKEND_DIR/.env" 2>/dev/null | cut -d'=' -f2 | tr -d '"' | tr -d "'" | tr -d ' ')
    if [ -n "$COUNCIL_ADDR" ] && [ "$COUNCIL_ADDR" != "" ]; then
        HAS_COUNCIL=true
    fi
fi
if [ "$HAS_COUNCIL" = true ]; then
    echo "   Starting/restarting naio-dapp..."
    if supervisorctl status "naio-dapp" &> /dev/null; then
        STATUS=$(supervisorctl status "naio-dapp" | awk '{print $2}')
        if [ "$STATUS" = "RUNNING" ]; then
            supervisorctl restart "naio-dapp" > /dev/null 2>&1 || true
        else
            supervisorctl start "naio-dapp" > /dev/null 2>&1 || true
        fi
    else
        supervisorctl reread || true
        supervisorctl update || true
        supervisorctl start "naio-dapp" > /dev/null 2>&1 || true
    fi
else
    echo "   Skipping naio-dapp (missing KEEPER_COUNCIL_ADDRESS)"
fi

# ============================================
# Done
# ============================================
echo ""
echo "=========================================="
echo "Deploy complete!"
echo "=========================================="
echo ""
echo "Project: $PROJECT_DIR"
echo "Backend: $BACKEND_DIR"
echo "Contracts: $CONTRACTS_DIR"
echo ""
echo "Service status:"
for service in naio-listen-deposits naio-call-poke naio-price-recorder naio-telegram-bot naio-keeper-validator naio-dapp; do
    if supervisorctl status "$service" &> /dev/null; then
        supervisorctl status "$service"
    else
        echo "  $service: not configured"
    fi
done
echo ""
echo "Logs:"
echo "  tail -f /var/log/naio-listen-deposits.out.log"
echo "  tail -f /var/log/naio-call-poke.out.log"
echo "  tail -f /var/log/naio-price-recorder.out.log"
echo "  tail -f /var/log/naio-telegram-bot.out.log"
echo "  tail -f /var/log/naio-keeper-validator.out.log"
echo "  tail -f /var/log/naio-dapp.out.log"
echo ""
echo "Manage:"
echo "  supervisorctl status           # status"
echo "  supervisorctl start <name>     # start"
echo "  supervisorctl stop <name>      # stop"
echo "  supervisorctl restart <name>   # restart"
echo ""
