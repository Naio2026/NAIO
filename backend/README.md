# Backend (Ops Services)

This directory contains Python services used to operate the NAIO system:

- **Deposit listener**: watches USDT transfers / controller events and submits on-chain actions when needed (`listen_deposits.py`)
- **Poke caller**: periodically calls `Controller.poke()` around the scheduled window (`call_poke.py`)
- **Price recorder**: records controller price into a local SQLite DB (`price_recorder.py`)
- **Telegram bot (optional)**: user-facing bot with charts and on-chain queries (`telegram_bot.py`)
- **Keeper validator (recommended)**: independent anomaly detector + optional veto pause (`keeper_validator.py`)
- **Witness signing**:
  - headless signer service for 3/3 distributed witness mode (`witness_signer/witness_signer_service.py`)
  - optional local GUI signer runner (`witness_signer_gui.py`) (legacy; the actively maintained GUI client lives in `Keeper-GUI/`)

Most services are designed to run under `supervisor` via the repo root `deploy.sh`.

## Install (manual)

```bash
cd backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Telegram bot extras:

```bash
pip install -r requirements.telegram.txt
```

## Configuration

Services read configuration from `.env` (typically `backend/.env`).

Templates:

- `docs/backend/env.example` (full reference)
- `docs/backend/env.testnet.example` (testnet-focused example)

Common required variables:

- RPC:
  - `USE_QUICKNODE=true` + `QUICKNODE_HTTP_URL` (recommended)
  - or `BSC_RPC_URL`
- Contracts:
  - `CONTROLLER_ADDRESS`
  - `USDT_ADDRESS`
- Keeper key:
  - `KEEPER_PRIVATE_KEY`
- Optional governance / council:
  - `KEEPER_COUNCIL_ADDRESS`
- Witness mode (if enabled):
  - `WITNESS_SIGNER_1..3` / `WITNESS_SIGNER_ADDRESSES`
  - `WITNESS_SIGNATURE_DEADLINE_SECONDS`
  - Hub: `WITNESS_HUB_*`
- Validator:
  - `VALIDATOR_PRIVATE_KEY` (only if you want automatic veto tx)
  - `VALIDATOR_*` Telegram settings
- Telegram bot:
  - `TELEGRAM_BOT_TOKEN`

## Run services (manual examples)

From repo root (so `.env` discovery works as expected), or ensure `.env` is loaded.

```bash
python backend/listen_deposits.py
python backend/call_poke.py
python backend/price_recorder.py
python backend/keeper_validator.py
```

Telegram bot:

```bash
python backend/telegram_bot.py
```

## Witness signer (headless)

See `backend/witness_signer/README.md`.

## Data files

Some services create runtime files in `backend/`:

- SQLite DB: `price_history.db` (configurable via `PRICE_DB_PATH`)
- State files (examples):
  - `telegram_bot_state.json`
  - `keeper_validator_state.json`

To safely clean local state (with backups), use the repo root script:

```bash
./clean_local_data.sh --dry-run
./clean_local_data.sh --yes
```

## Security notes

- Never store production private keys on developer laptops.
- Run keeper/validator/signer keys on isolated hosts with minimal balances.
- Do not commit `.env` or config files containing secrets.

