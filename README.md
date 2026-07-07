# Ninebot Trip Sync

Private automation repo for Ninebot trip exports.

Current scope:

- Print and validate exported trip data in GitHub Actions.
- Keep a clean sample export under `data/export/`.
- Reserve Notion sync and live Ninebot fetching for the next step.

Discovered Ninebot interfaces are documented in `data/export/README.md`.

## Run Locally

```bash
pip install -r requirements.txt
python scripts/print_trips.py --export-dir data/export
```

## GitHub Actions

Workflow: `.github/workflows/print-ninebot-data.yml`

- `workflow_dispatch`: manual run
- `schedule`: daily at 10:30 Asia/Shanghai

## Secrets Configured

- `NINEBOT_WNUMBER`
- `NINEBOT_VEHICLE_TYPE`

Notion secrets are intentionally not configured yet.
