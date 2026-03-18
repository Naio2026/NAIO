# Witness Signer Service

Headless signer service for distributed 3/3 witness mode.

## Run

```bash
cd /opt/naio/backend
python witness_signer/witness_signer_service.py
```

## Auto Deploy

```bash
cd /opt/naio/backend/witness_signer
chmod +x deploy_witness_signer.sh
./deploy_witness_signer.sh
```

Options:

- `--env-file <path>` custom dotenv path (default `backend/.env`)
- `--service-name <name>` custom supervisor service name (default `naio-witness-signer`)
- `--skip-deps` skip system dependency installation
- `--force` recreate venv and reinstall python dependencies
- `--no-supervisor` do not write supervisor config, only prepare runtime

## Env Variables

Required:

- `WITNESS_HUB_SERVER_URL` - Hub base URL, e.g. `http://hub-host:8787`
- `WITNESS_SIGNER_PRIVATE_KEY` - signer private key for this machine

Optional:

- `WITNESS_SIGNER_API_KEY` - API key header (`X-Api-Key`)
- `WITNESS_SIGNER_EXPECTED_ADDRESS` - sanity check against derived signer address
- `WITNESS_SIGNER_POLL_INTERVAL_SECONDS` - polling interval, default `2`
- `WITNESS_SIGNER_HTTP_TIMEOUT_SECONDS` - HTTP timeout, default `10`
- `WITNESS_SIGNER_DOTENV_PATH` - override dotenv path (default `backend/.env`)
- `LOG_LEVEL` - logger level (`INFO`, `DEBUG`, etc.)
