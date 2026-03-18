# Contracts (Foundry)

This directory is a Foundry (`forge`) project for the NAIO contracts.

## Requirements

- Foundry installed (`forge`, `cast`)
- RPC endpoint for your target network (BSC mainnet/testnet)

## Install dependencies

From repo root:

```bash
cd contracts
forge install foundry-rs/forge-std --no-commit
```

Notes:

- Tests and scripts depend on `forge-std`.
- The repo root `deploy.sh` can also install Foundry and `forge-std` on a fresh Ubuntu server.

## Build and test

```bash
cd contracts
forge build
forge test -vvv
```

## Pre-deploy checklist

### 1) Node list (if used by your deploy flow)

Some deployment flows expect `contracts/nodes_list.txt` to exist and contain:

- exactly 1000 lines
- one `0x...` address per line
- no `0x0000000000000000000000000000000000000000`
- no duplicates

Quick check:

```bash
cd ..
wc -l contracts/nodes_list.txt
```

### 2) Env vars (`backend/.env`)

Deployment scripts commonly rely on values in `backend/.env`. At minimum you will usually need:

- `DEPLOYER_PRIVATE_KEY`
- `USDT_ADDRESS`
- `KEEPER_PRIVATE_KEY`
- `KEEPER_GOVERNOR_ADDRESS`
- `WITNESS_SIGNER_1`, `WITNESS_SIGNER_2`, `WITNESS_SIGNER_3`
- fixed receiver addresses:
  - `TRANSFER_TAX_RECEIVER_C`
  - `ECO_A`
  - `INDEPENDENT_B`
  - `MARKET_E`
  - `MARKET_F`
- `REFERRAL_BOOTSTRAP_ADDRESS`
- RPC endpoint:
  - `QUICKNODE_HTTP_URL` (recommended) or `BSC_RPC_URL`

Use `docs/backend/env.example` / `docs/backend/env.testnet.example` as templates.

### 3) Key semantics

- Burn address is `0x000000000000000000000000000000000000dEaD`.
- `REFERRAL_BOOTSTRAP_ADDRESS` is used for referral cold start flows.
- Current witness rule engine is configured as a one-time setup pattern (no mutable owner knobs for the witness rule engine after initialization).

## Deploy

### Testnet (example)

```bash
cd ..
set -a
source backend/.env
set +a

cd contracts
forge script script/DeployTestnet.s.sol:DeployTestnet \
  --rpc-url "$QUICKNODE_HTTP_URL" \
  --private-key "$DEPLOYER_PRIVATE_KEY" \
  --broadcast \
  --legacy \
  --slow
```

### Mainnet (example)

```bash
cd ..
set -a
source backend/.env
set +a

cd contracts
forge script script/Deploy.s.sol:Deploy \
  --rpc-url "$QUICKNODE_HTTP_URL" \
  --private-key "$DEPLOYER_PRIVATE_KEY" \
  --broadcast \
  --legacy \
  --slow
```

## Post-deploy: backfill `backend/.env`

After deployment, backfill addresses at least:

- `CONTROLLER_ADDRESS`
- `INITIAL_POOL_SEEDER_ADDRESS`
- `NODE_SEAT_POOL_ADDRESS`
- (recommended) `NAIO_TOKEN_ADDRESS`

## Suggested on-chain sanity checks

```bash
cast call "$CONTROLLER_ADDRESS" "depositRuleEngine()(address)" --rpc-url "$QUICKNODE_HTTP_URL"
cast call "$CONTROLLER_ADDRESS" "keeperAccountingPaused()(bool)" --rpc-url "$QUICKNODE_HTTP_URL"
cast call "$CONTROLLER_ADDRESS" "keeper()(address)" --rpc-url "$QUICKNODE_HTTP_URL"
```

If you recorded a rule engine address:

```bash
export RULE_ENGINE_ADDRESS=0x...
cast call "$RULE_ENGINE_ADDRESS" "controller()(address)" --rpc-url "$QUICKNODE_HTTP_URL"
cast call "$RULE_ENGINE_ADDRESS" "witnessThreshold()(uint16)" --rpc-url "$QUICKNODE_HTTP_URL"
cast call "$RULE_ENGINE_ADDRESS" "isWitnessSigner(address)(bool)" "$WITNESS_SIGNER_1" --rpc-url "$QUICKNODE_HTTP_URL"
cast call "$RULE_ENGINE_ADDRESS" "isWitnessSigner(address)(bool)" "$WITNESS_SIGNER_2" --rpc-url "$QUICKNODE_HTTP_URL"
cast call "$RULE_ENGINE_ADDRESS" "isWitnessSigner(address)(bool)" "$WITNESS_SIGNER_3" --rpc-url "$QUICKNODE_HTTP_URL"
```

## Optional: deploy KeeperCouncil separately

```bash
cd ..
set -a
source backend/.env
set +a

cd contracts
forge script script/DeployKeeperCouncil.s.sol:DeployKeeperCouncil \
  --rpc-url "$QUICKNODE_HTTP_URL" \
  --private-key "$DEPLOYER_PRIVATE_KEY" \
  --broadcast \
  --legacy \
  --slow
```

