#!/usr/bin/env bash
# Verify & publish a deployed contract on BscScan via Foundry.
#
# Usage:
#   cd contracts
#   ./verify.sh --address 0x... --contract src/Foo.sol:Foo --chain bsc
#
# Notes:
# - Requires Foundry (forge) installed.
# - Requires BSCSCAN_API_KEY (recommended: export it in your shell).
# - If you prefer using a local file, you can put BSCSCAN_API_KEY in ./contracts/.env
#   and this script will load it automatically (the file should NOT be committed).
#
# Examples:
#   export BSCSCAN_API_KEY="xxxx"
#   ./verify.sh --address 0x1234... --contract src/NAIOController.sol:NAIOController --chain bsc
#
#   # Testnet
#   ./verify.sh --address 0x1234... --contract src/NAIOToken.sol:NAIOToken --chain bsc-testnet
#

set -euo pipefail

CONTRACTS_DIR="$(cd "$(dirname "$0")" && pwd)"

load_env_file() {
  local file="$1"
  if [[ -f "$file" ]]; then
    # shellcheck disable=SC1090
    set -a
    source "$file"
    set +a
  fi
}

# Optional: load ./contracts/.env for local-only secrets
load_env_file "$CONTRACTS_DIR/.env"

ADDRESS=""
CONTRACT_ID=""
CHAIN="bsc"
RPC_URL=""
CONSTRUCTOR_ARGS=""

print_help() {
  cat <<'EOF'
verify.sh - Verify & publish a contract on (Bsc)Scan using Foundry.

Required:
  --address <0x...>                 Deployed contract address
  --contract <path:Name>            Fully qualified contract name, e.g. src/Foo.sol:Foo

Optional:
  --chain <bsc|bsc-testnet|97|56>   Target chain (default: bsc)
  --rpc-url <url>                   RPC URL for fetching constructor args (optional but recommended)
  --constructor-args <hex>          ABI-encoded constructor args (0x...) if needed

Env:
  BSCSCAN_API_KEY                   API key (export in shell, or put in ./contracts/.env)

Examples:
  ./verify.sh --address 0x... --contract src/NAIOController.sol:NAIOController --chain bsc
  ./verify.sh --address 0x... --contract src/NAIOToken.sol:NAIOToken --chain bsc-testnet
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --address)
      ADDRESS="${2:-}"; shift 2;;
    --contract)
      CONTRACT_ID="${2:-}"; shift 2;;
    --chain)
      CHAIN="${2:-}"; shift 2;;
    --rpc-url)
      RPC_URL="${2:-}"; shift 2;;
    --constructor-args)
      CONSTRUCTOR_ARGS="${2:-}"; shift 2;;
    -h|--help)
      print_help; exit 0;;
    *)
      echo "Unknown argument: $1" >&2
      echo "" >&2
      print_help >&2
      exit 1;;
  esac
done

if [[ -z "$ADDRESS" || -z "$CONTRACT_ID" ]]; then
  echo "ERROR: --address and --contract are required." >&2
  echo "" >&2
  print_help >&2
  exit 1
fi

if ! command -v forge >/dev/null 2>&1; then
  echo "ERROR: forge not found. Install Foundry first." >&2
  exit 1
fi

if [[ -z "${BSCSCAN_API_KEY:-}" ]]; then
  echo "ERROR: BSCSCAN_API_KEY is not set." >&2
  echo "Set it via: export BSCSCAN_API_KEY=\"...\"" >&2
  echo "Or put it in: $CONTRACTS_DIR/.env (local-only, do not commit)" >&2
  exit 1
fi

# Normalize chain option:
# - Foundry supports chain names like 'bsc' and 'bsc-testnet'
# - Some users prefer passing chainId numbers.
case "$CHAIN" in
  56) CHAIN="bsc" ;;
  97) CHAIN="bsc-testnet" ;;
esac

VERIFY_ARGS=(verify-contract "$ADDRESS" "$CONTRACT_ID" --chain "$CHAIN" --watch)

if [[ -n "$RPC_URL" ]]; then
  VERIFY_ARGS+=(--rpc-url "$RPC_URL")
fi

if [[ -n "$CONSTRUCTOR_ARGS" ]]; then
  VERIFY_ARGS+=(--constructor-args "$CONSTRUCTOR_ARGS")
fi

echo "Verifying on chain: $CHAIN"
echo "Contract address: $ADDRESS"
echo "Contract id:      $CONTRACT_ID"
if [[ -n "$RPC_URL" ]]; then
  echo "RPC URL:          (provided)"
fi
echo ""

forge "${VERIFY_ARGS[@]}"

