# Ninebot -> Notion Sync

This folder contains automation for syncing exported Ninebot trips and GPX files to Notion.

## What Works Now

- Reads `ninebot-dump/export/trips.csv`.
- Uploads each trip's WGS84 GPX, GCJ-02 GPX, points CSV, and raw JSON to Notion.
- Creates one Notion page per trip.
- Keeps a local state file to avoid duplicate uploads.

## Important Limitation

The discovered Ninebot endpoints are:

- `POST https://cn-cbu-gateway.ninebot.com/app-api/travel/v6/travel-list2`
- `POST https://cn-cbu-gateway.ninebot.com/app-api/travel/v6/travel-info`

The React Native bundle calls `NBRequest`, which delegates to native `tokenRequest(...)` for signing/encryption. Plain Python `requests.post(...)` cannot directly replay the API. To make cloud-side automatic fetching work, one of these must be added:

1. A Python/native implementation of the Ninebot signing/encryption routine.
2. A self-hosted runner connected to the rooted phone, where a command can call the app/native bridge and print decrypted JSON.
3. A Frida/r0capture-based local extractor that writes the same `trips.csv` + GPX files before this Notion sync runs.

The script exposes `NINEBOT_TOKENREQUEST_COMMAND` for option 1 or 2. The command receives JSON on stdin:

```json
{
  "url": "https://cn-cbu-gateway.ninebot.com/app-api/travel/v6/travel-list2",
  "base": "bike",
  "data": {
    "wnumber": "11DFG2532J1219",
    "rnVersion": "753",
    "vehicle_type": "14356",
    "month": "2026-07",
    "page": 1
  }
}
```

It must print the decrypted plaintext response JSON to stdout.

## Local Usage

```bash
cd /Users/malinkang/Documents/Codex/2026-05-20/mtbi/note
python3 -m venv .venv-ninebot-notion
. .venv-ninebot-notion/bin/activate
pip install -r automation/ninebot_notion/requirements.txt

export NOTION_TOKEN='secret_xxx'
export NOTION_PARENT_PAGE_ID='xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'
python automation/ninebot_notion/ninebot_notion_sync.py --export-dir ninebot-dump/export
```

Dry run:

```bash
python automation/ninebot_notion/ninebot_notion_sync.py --export-dir ninebot-dump/export --dry-run
```

## GitHub Secrets

Add these repository secrets:

- `NOTION_TOKEN`: Notion integration secret.
- `NOTION_PARENT_PAGE_ID`: target Notion page ID, or use `NOTION_DATABASE_ID` instead.
- `NOTION_DATABASE_ID`: optional target database ID.
- `NOTION_TITLE_PROPERTY`: optional database title property, default `Name`.
- `NINEBOT_TOKENREQUEST_COMMAND`: optional command for native signing/decryption.

For a GitHub-hosted runner, commit or upload the generated export artifacts first. For live fetching, use a self-hosted runner with access to the rooted phone or provide a working native signer command.
