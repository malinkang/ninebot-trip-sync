# AGENTS.md

## Project Goal

This repo automates Ninebot trip export and syncs trip metadata plus GPX data to Notion through GitHub Actions.

Current expected path:

1. Fetch Ninebot cloud trip list and `travel-info` details.
2. Export private local artifacts under `data/cloud-export/`.
3. Sync each trip to the Notion child database `Ninebot Trips`.
4. Upload only the WGS84 GPX as a JSON-wrapped file in the Notion `GPX` database property.
5. Keep GitHub Actions scheduled sync working.

## Sensitive Data Rules

- Never print real values for Ninebot tokens, Notion tokens, cookies, phone numbers, device ids, or raw GPS tracks.
- Never commit `.env`, `.cache/`, `data/cloud-export*/`, HPROF dumps, Reqable captures, or other private artifacts.
- Keep local secret files chmod `0600` where possible.
- When updating GitHub Secrets, always pipe values through stdin, for example:

```bash
printf '%s' "$NINEBOT_ACCESS_TOKEN" | gh secret set NINEBOT_ACCESS_TOKEN
printf '%s' "$NOTION_TOKEN" | gh secret set NOTION_TOKEN
```

- In status messages, say a secret is "configured" or "updated"; do not reveal the value.

## Core Files

- `.github/workflows/print-ninebot-data.yml`: scheduled/manual cloud export and Notion sync workflow.
- `scripts/ninebot_cloud_export.py`: cloud trip list/detail export entrypoint.
- `scripts/notion_existing_keys.py`: reads Notion sync state and existing `Stable Key` values for incremental sync.
- `scripts/ninebot_raw_travel_info.py`: encrypted Ninebot travel API client.
- `scripts/ninebot_passport.py`: best-effort Passport refresh support.
- `scripts/ninebot_phone_fetch.py`: local Android app fallback export helper.
- `scripts/ninebot_notion_sync.py`: Notion database sync.
- `scripts/print_trips.py`: export validation/summary.
- `README.md` and `README_NOTION.md`: user-facing runbook.

## Notion Sync Rules

- Use the official Python `notion-client` SDK.
- Target the configured Notion database, normally from `NOTION_DATABASE_ID`.
- Preserve/update the Notion schema through `data_sources.update`, not legacy-only database APIs.
- Use `data_sources.query` for querying rows.
- Create pages with parent `{"data_source_id": "..."}`.
- The database must include a `GPX` files property.
- Upload only `*_wgs84.gpx` after wrapping it as JSON named `*_wgs84.gpx.json`.
- Do not attach GPX/CSV/JSON files to the page body.
- On forced resync, clear old page body blocks left by older sync versions.
- A successful Notion sync should have every trip row with:
  - title in `Name`
  - unique `Stable Key`
  - content hash in `Digest`
  - WGS84 JSON-wrapped GPX in `GPX`
  - no sync-created page body attachments

## Coordinate Rules

- The Notion/Web pipeline should store WGS84 GPX as the canonical track format.
- The Notion `GPX` property should store the `*_wgs84.gpx.json` wrapper only.
- Keep generating both WGS-84 and GCJ-02 artifacts when export scripts already do so, but do not upload both to Notion. GCJ-02 remains useful for domestic-map apps.
- Do not infer, repair, or fabricate GPS points for old Ninebot records. Ninebot
  cloud only returns complete track points for roughly the latest 180 days; older
  records may contain only simplified start/end points.

## Token Update Workflow

When the user asks to refresh, capture, or update Ninebot tokens:

1. Inspect current repo state:

```bash
git status --short
```

2. Load `.env` locally if present, but do not print secret values.
3. Prefer pure-Python/cloud refresh first if `NINEBOT_REFRESH_TOKEN` is available.
4. If phone capture is required, verify the Android device first:

```bash
adb devices
```

5. Prefer low-impact token sources before runtime capture:
   - existing `.env` / `.cache` values
   - app-accessible cache/shared prefs when appropriate
   - ninecli cache when available
   - logcat/app export helpers
6. Use Reqable, r0capture, Frida, heap extraction, or root-only Android workflows only when needed and only for the minimum data required.
7. Update local `.env` without printing token values.
8. Update GitHub Secrets through stdin.
9. Trigger the workflow and watch it to completion.
10. Verify Notion rows and `GPX` file properties after sync.

## GitHub Actions Workflow

Workflow file: `.github/workflows/print-ninebot-data.yml`.

- Manual trigger: `workflow_dispatch`.
- Scheduled trigger: daily at 12:00 and 22:00 Asia/Shanghai.
- Scheduled/default incremental runs query the latest Notion `Start Time`, use
  that month as the export start month, then fetch forward to the current month.
- If Notion has no rows, scheduled/default incremental runs start from
  `today - 180 days`, because Ninebot only keeps complete trajectories for
  roughly 180 days.
- Existing Notion `Stable Key` values are loaded before export so already synced
  rows skip Ninebot detail/GPX fetches. Manual `incremental=false` forces the
  legacy current-month behavior. Historical `all_months=true` remains
  full-history.
- Export/sync order should remain old-to-new: process months ascending and keep
  each `trips.csv` sorted by `start_time`.
- GitHub cron is UTC, so the expected cron entries are:

```yaml
- cron: '0 4 * * *'
- cron: '0 14 * * *'
```

Required Ninebot secrets:

- `NINEBOT_ACCESS_TOKEN`
- `NINEBOT_REFRESH_TOKEN`
- `NINEBOT_BUSINESS_UID`
- `NINEBOT_WNUMBER`
- `NINEBOT_VEHICLE_TYPE`
- `NINEBOT_RN_VERSION`
- `NINEBOT_DEVICE_ID`
- `NINEBOT_BUSINESS_TYPE`

Required Notion secrets:

- `NOTION_TOKEN`
- `NOTION_PARENT_PAGE_ID`
- `NOTION_DATABASE_ID`
- `NOTION_TITLE_PROPERTY`

## Common Commands

Install dependencies:

```bash
pip install -r requirements.txt
```

Compile-check scripts:

```bash
python3 -m py_compile \
  scripts/ninebot_cloud_export.py \
  scripts/ninebot_raw_travel_info.py \
  scripts/ninebot_passport.py \
  scripts/ninebot_notion_sync.py \
  scripts/print_trips.py
```

Fetch current month locally:

```bash
source .env
python scripts/ninebot_cloud_export.py --export-dir data/cloud-export
python scripts/print_trips.py --export-dir data/cloud-export
```

Fetch historical months locally:

```bash
source .env
python scripts/ninebot_cloud_export.py --all-months --start-month 202401 --export-dir data/cloud-export
python scripts/print_trips.py --export-dir data/cloud-export
```

Sync to Notion locally:

```bash
source .env
python scripts/ninebot_notion_sync.py --sync --export-dir data/cloud-export
```

Force re-upload Notion `GPX` properties and clear old page-body attachments:

```bash
source .env
python scripts/ninebot_notion_sync.py --sync --force --export-dir data/cloud-export
```

Trigger GitHub Actions manually:

```bash
gh workflow run print-ninebot-data.yml -f month= -f all_months=false -f start_month=
gh run list --workflow print-ninebot-data.yml --limit 1
gh run watch <run-id> --exit-status
```

## Completion Checks

Before claiming the automation is fixed or complete, verify the relevant scope:

- `git diff --check` passes.
- Python compile checks pass for edited scripts.
- GitHub Actions workflow succeeds after push when workflow behavior changed.
- Notion database `Ninebot Trips` exists under the configured parent page.
- All expected trip rows have a `GPX` file property.
- All `GPX` filenames end with `_wgs84.gpx.json`.
- No old sync-created body attachments remain when a forced cleanup was requested.
- No secrets or private GPS artifacts are staged or committed.

## Git Hygiene

- Do not revert user changes unless explicitly requested.
- Do not commit `.env`, `.cache/`, or `data/cloud-export*/`.
- Commit only files relevant to the requested change.
- Push to `main` only when the user asks to push or the task explicitly requires deployment.
