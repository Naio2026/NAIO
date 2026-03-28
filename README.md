# NAIO (BSC) – Contracts + Ops Tooling

This repository contains:

- **Solidity contracts** (Foundry) for the NAIO system.
- **Backend ops services** (Python) for deposit listening, periodic `poke`, price recording, Telegram bot, and an independent validator.
- **Governance DApp** (static HTML + tiny Python server) for KeeperCouncil proposals/votes/execution.
- **Witness signer tooling** for distributed **3/3 witness** mode (headless service + GUI client).

## Repo layout

- `contracts/`: Foundry project (build/test/deploy scripts).
- `backend/`: Python services and optional Telegram bot.
- `dapp/`: Governance committee web UI (`governance.html`) + local server (`server.py`).
- `Keeper-GUI/`: Cross-platform GUI witness signer client + packaging scripts.
- `tools/`: Utility scripts (e.g. testnet faucet/deposit helpers).
- `docs/`: Operational docs and configuration templates.

## Quick start (server deployment)

### 1) Upload code to a server

`upload.sh` uploads this repo to a remote host (default `/opt/naio`) via `rsync`, with safe excludes to avoid overwriting runtime state.

```bash
./upload.sh
# or preview:
./upload.sh --dry-run
```

### 2) Deploy and start services

On the server, prepare `backend/.env` (see `docs/backend/env.example` and `docs/backend/env.testnet.example`), then run:

```bash
sudo ./deploy.sh
```

Useful options:

- `--skip-deps`: skip system dependency install
- `--skip-deploy`: do not deploy contracts (ops-only)
- `--force`: recreate venv / reinstall deps / rewrite supervisor config
- `--dapp-only`: deploy only the governance DApp service

After deployment, services are managed via `supervisorctl`.

## Local development (typical)

### Contracts

```bash
cd contracts
forge build
forge test -vvv
```

See `contracts/README.md` for deployment prerequisites and post-deploy checks.

### Backend services (manual)

```bash
cd backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Then run a service directly (examples):

```bash
python listen_deposits.py
python call_poke.py
python price_recorder.py
python keeper_validator.py
```

For the Telegram bot (same venv; deps are in `requirements.txt`):

```bash
python telegram_bot.py
```

See `backend/README.md` for details and required env vars.

### Governance DApp (local)

```bash
python dapp/server.py
```

Open `http://localhost:8765/governance.html` (mobile: use `http://<host-ip>:8765/governance.html` on the same network).

See `dapp/README.md`.

## Configuration

Primary configuration is done via `backend/.env`.

- Templates:
  - `docs/backend/env.example` (full reference)
  - `docs/backend/env.testnet.example` (testnet-focused example)
- Contract deployment scripts read from `backend/.env` and require addresses/keys such as:
  - `DEPLOYER_PRIVATE_KEY`, `KEEPER_PRIVATE_KEY`
  - `WITNESS_SIGNER_1..3`, `KEEPER_GOVERNOR_ADDRESS`
  - fixed receiver addresses (`TRANSFER_TAX_RECEIVER_C`, `ECO_A`, `INDEPENDENT_B`, `MARKET_E`, `MARKET_F`)
  - and post-deploy backfills like `CONTROLLER_ADDRESS`

## Security notes

- **Never commit secrets**: private keys, API keys, `.env` files, or signer config.
- Prefer using **dedicated hot wallets** with minimal balances for keeper/validator/signer roles.
- Treat witness signer private keys as production secrets and run them on isolated machines.

## Directory READMEs

- `contracts/README.md`: Foundry usage and deploy checklist
- `backend/README.md`: ops services, env vars, supervisor layout
- `dapp/README.md`: governance DApp usage and config
- `Keeper-GUI/README.md`: GUI witness signer usage and packaging
