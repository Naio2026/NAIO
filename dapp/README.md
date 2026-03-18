# Governance DApp (KeeperCouncil)

This directory contains a static governance UI (`governance.html`) and a tiny local server (`server.py`) that serves the page and exposes `config.json` from your `.env`.

The UI is designed to be opened in a mobile wallet “DApp browser” for voting and executing KeeperCouncil proposals.

## Run locally (config auto-loaded)

```bash
cd /path/to/bsc-naio
python dapp/server.py
```

Then open:

- `http://localhost:8765/governance.html`
- Mobile (same LAN): `http://<host-ip>:8765/governance.html`

## Configuration source

No manual editing is required in most cases. `dapp/server.py` reads from (in order):

1. `backend/.env`
2. `.env` (repo root)

Relevant env vars:

- `KEEPER_COUNCIL_ADDRESS`
- `CONTROLLER_ADDRESS`
- `BSC_RPC_URL` or `QUICKNODE_HTTP_URL`
- `CHAIN_ID` (or `BSC_TESTNET=true`)
- `DAPP_PORT` (optional, default `8765`)

## Member forwarding flow (1→2→3→4→5)

1. Member A creates a proposal, then copies the generated link (with `?id=<proposalId>`).
2. Send the link to member B (Telegram/Signal/etc).
3. Member B opens it in a wallet DApp browser, connects wallet, taps **Approve**, then forwards to the next member.
4. When approvals reach the threshold (3/5 or 5/5), any member can execute on-chain.

## Thresholds

- **3/5**: operational actions (keeper enable/disable, pause/resume accounting, set validator, etc.)
- **5/5**: high-impact actions (replace witness, replace council member, change governance target/controller, etc.)

## Static preview (without config endpoint)

If you only want to preview the UI without the `.env`-backed config:

```bash
cd dapp
python3 -m http.server 8080
```

Open `http://localhost:8080/governance.html`.

Note: wallet connections generally require HTTPS or `localhost`. For production, serve over HTTPS.

