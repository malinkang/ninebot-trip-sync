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

## Fetch From Phone Locally

Use this when you want to fetch more than the checked-in sample data. It drives the logged-in Ninebot Android app via ADB, lets the app call the encrypted `tokenRequest` API, then extracts decrypted `travel-info` data from logcat/heap and regenerates CSV/GPX/JSON.

Prerequisites:

- Phone connected via `adb devices`.
- Ninebot app installed and logged in.
- Root is recommended for reliable `am dumpheap` extraction.
- Reqable/VPN should be stopped; the script force-stops Reqable automatically.

Load local env:

```bash
source .env
```

List visible trips for a month without exporting details:

```bash
python scripts/ninebot_phone_fetch.py --month "$NINEBOT_MONTH" --list-only --max-scrolls 8
```

Fetch one trip for a quick smoke test:

```bash
python scripts/ninebot_phone_fetch.py --month "$NINEBOT_MONTH" --export-dir data/export --max-trips 1
```

Fetch the visible month:

```bash
python scripts/ninebot_phone_fetch.py --month "$NINEBOT_MONTH" --export-dir data/export --max-scrolls 20
```

Useful options:

- `--serial <adb-serial>`: choose a specific device.
- `--month 2026-07`: target a visible month tab such as `07月`.
- `--max-trips 0`: no limit; this is the default.
- `--keep-debug`: keep pulled `.hprof` files under `.cache/`; these may contain secrets and are gitignored.

After fetching, validate/print:

```bash
python scripts/print_trips.py --export-dir data/export
```
