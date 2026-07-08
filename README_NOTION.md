# Ninebot -> Notion Sync

Sync exported Ninebot trips into a Notion database with the official
`notion-client` SDK.

## Current Behavior

- Reads `data/cloud-export/trips.csv` by default.
- Creates or updates one Notion database row per trip.
- Writes trip metadata into database properties.
- Uploads only the WGS84 GPX file as a JSON-wrapped file named
  `*_wgs84.gpx.json` into the `GPX` database property.
- Clears old page-body blocks during forced updates so files are not duplicated
  in the page article body.

Notion does not accept `.gpx` as a native upload extension, so the script wraps
the GPX XML into a JSON file before uploading.

## Local Usage

```bash
cd /Users/malinkang/Documents/Codex/2026-05-20/mtbi/ninebot-trip-sync
source .env
python scripts/ninebot_notion_sync.py --sync --export-dir data/cloud-export
```

Force re-upload and clear previous page-body attachments:

```bash
source .env
python scripts/ninebot_notion_sync.py --sync --force --export-dir data/cloud-export
```

Create or update the child database schema under the configured parent page:

```bash
source .env
python scripts/ninebot_notion_sync.py --ensure-database
```

## Required Environment

- `NOTION_TOKEN`: Notion integration secret.
- `NOTION_PARENT_PAGE_ID`: parent page for `--ensure-database`.
- `NOTION_DATABASE_ID`: target database id for sync.
- `NOTION_TITLE_PROPERTY`: optional, defaults to `Name`.

The database contains a `GPX` files property plus trip metadata properties such
as date, mileage, duration, points, and sync digest.
