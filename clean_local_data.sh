#!/usr/bin/env bash
set -Eeuo pipefail

DRY_RUN=0
ASSUME_YES=0
WITH_STATE=0
DO_BACKUP=1
ENV_FILE="backend/.env"
BACKUP_BASE="backend/cleanup_backups"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$SCRIPT_DIR"
BACKEND_DIR="$ROOT_DIR/backend"

usage() {
  cat <<'EOF'
Usage:
  ./clean_local_data.sh [options]

Options:
  --dry-run            Preview only, do not delete anything
  --yes                Skip interactive confirmation
  --with-state         Also clean validator state file (VALIDATOR_STATE_FILE).
                       Telegram bot state (telegram_bot_state.json) is always cleaned.
  --no-backup          Do not backup files before deletion (dangerous)
  --env <path>         .env path (default: backend/.env)
  --backup-dir <path>  Backup directory (default: backend/cleanup_backups)
  -h, --help           Show this help

Examples:
  ./clean_local_data.sh --dry-run
  ./clean_local_data.sh --yes
  ./clean_local_data.sh --with-state --yes
EOF
}

log() {
  printf "[clean-local-data] %s\n" "$*"
}

to_abs_path() {
  local p="${1:-}"
  if [[ -z "$p" ]]; then
    return 1
  fi
  if [[ "$p" = /* ]]; then
    printf "%s\n" "$p"
  else
    printf "%s\n" "$ROOT_DIR/$p"
  fi
}

resolve_backend_path() {
  local p="${1:-}"
  if [[ -z "$p" ]]; then
    return 1
  fi
  if [[ "$p" = /* ]]; then
    printf "%s\n" "$p"
  else
    printf "%s\n" "$BACKEND_DIR/$p"
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      ;;
    --yes)
      ASSUME_YES=1
      ;;
    --with-state)
      WITH_STATE=1
      ;;
    --no-backup)
      DO_BACKUP=0
      ;;
    --env)
      shift
      [[ $# -gt 0 ]] || {
        echo "Missing value for --env"
        exit 1
      }
      ENV_FILE="$1"
      ;;
    --backup-dir)
      shift
      [[ $# -gt 0 ]] || {
        echo "Missing value for --backup-dir"
        exit 1
      }
      BACKUP_BASE="$1"
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1"
      usage
      exit 1
      ;;
  esac
  shift
done

if [[ ! -d "$BACKEND_DIR" ]]; then
  echo "backend directory not found under: $ROOT_DIR"
  echo "Please place this script in repo root and run it there."
  exit 1
fi

ENV_FILE_ABS="$(to_abs_path "$ENV_FILE")"
BACKUP_BASE_ABS="$(to_abs_path "$BACKUP_BASE")"

if [[ -f "$ENV_FILE_ABS" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE_ABS"
  set +a
  log "Loaded env file: $ENV_FILE_ABS"
else
  log "Env file not found: $ENV_FILE_ABS (using defaults)"
fi

DB_FILE="$(resolve_backend_path "${PRICE_DB_PATH:-price_history.db}")"

TG_STATE="$(resolve_backend_path "${TELEGRAM_BOT_STATE_FILE:-telegram_bot_state.json}")"
TARGETS=(
  "$DB_FILE"
  "${DB_FILE}-wal"
  "${DB_FILE}-shm"
  "${DB_FILE}-journal"
  "$TG_STATE"
  "${TG_STATE}.tmp"
)

if [[ "$WITH_STATE" -eq 1 ]]; then
  VD_STATE="$(resolve_backend_path "${VALIDATOR_STATE_FILE:-keeper_validator_state.json}")"
  TARGETS+=(
    "$VD_STATE"
    "${VD_STATE}.tmp"
  )
fi

EXISTING=()
for f in "${TARGETS[@]}"; do
  if [[ -e "$f" ]]; then
    EXISTING+=("$f")
  fi
done

if [[ ${#EXISTING[@]} -eq 0 ]]; then
  log "Nothing to clean."
  exit 0
fi

if command -v supervisorctl >/dev/null 2>&1; then
  RUNNING=()
  for svc in naio-listen-deposits naio-price-recorder naio-telegram-bot naio-keeper-validator; do
    if supervisorctl status "$svc" >/dev/null 2>&1; then
      st="$(supervisorctl status "$svc" | awk '{print $2}')"
      if [[ "$st" == "RUNNING" ]]; then
        RUNNING+=("$svc")
      fi
    fi
  done
  if [[ ${#RUNNING[@]} -gt 0 ]]; then
    log "Warning: running services detected: ${RUNNING[*]}"
    log "Recommended: stop related services before cleanup."
  fi
fi

echo "Files to clean:"
for f in "${EXISTING[@]}"; do
  echo "  - $f"
done

if [[ "$DRY_RUN" -eq 1 ]]; then
  log "Dry-run mode, no file deleted."
  exit 0
fi

if [[ "$ASSUME_YES" -ne 1 ]]; then
  read -r -p "Type CLEAN to continue: " confirm
  if [[ "$confirm" != "CLEAN" ]]; then
    log "Cancelled."
    exit 1
  fi
fi

if [[ "$DO_BACKUP" -eq 1 ]]; then
  TS="$(date +%Y%m%d_%H%M%S)"
  BACKUP_DIR="$BACKUP_BASE_ABS/$TS"
  mkdir -p "$BACKUP_DIR"
  for f in "${EXISTING[@]}"; do
    cp -a "$f" "$BACKUP_DIR/"
  done
  log "Backup completed: $BACKUP_DIR"
else
  log "Backup disabled (--no-backup)."
fi

for f in "${EXISTING[@]}"; do
  rm -f "$f"
done

log "Cleanup completed."
echo
echo "Next suggested steps:"
echo "1) Update backend/.env with new contract addresses"
echo "2) Restart services"
echo "3) If needed, run backfill tools for new deployment window"
