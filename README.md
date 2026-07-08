# Ninebot Trip Sync

Private automation repo for Ninebot trip exports.

Current scope:

- Fetch Ninebot cloud trip lists and every `travel-info` detail for one month or discovered historical months.
- Export per-trip JSON, GCJ-02 CSV, WGS-84 GPX, GCJ-02 GPX, and a monthly `trips.csv`.
- Print and validate fetched data in GitHub Actions.
- Try a pure-Python Passport token refresh before export when the access token is near expiry.
- Sync exported trips to a Notion database, with WGS84 GPX uploaded as a `GPX` file property.
- Infer old simplified tracks from repeated full routes before Notion sync, and
  mark inferred rows in Notion.

Discovered Ninebot interfaces are documented in `data/export/README.md`.

## References / Prior Art

This project was built after comparing several Ninebot-related tools and
libraries:

- [`ninecli`](https://pypi.org/project/ninecli/): the most useful reference for
  the cloud path. We used it to understand Passport, vehicle, travel list, and
  travel detail flows, then reimplemented the travel request/response crypto in
  pure Python for GitHub Actions.
- [`hasscc/ninebot`](https://github.com/hasscc/ninebot): Home Assistant
  integration that delegates Ninebot cloud access to `ninecli`; useful for
  understanding how a higher-level integration consumes `ninecli`.
- [`hasscc/ninebot#6`](https://github.com/hasscc/ninebot/pull/6) and
  [`hasscc/ninebot#7`](https://github.com/hasscc/ninebot/pull/7): useful context
  while checking how token/config handling and `ninecli` integration evolved.
- [`waistu/Ninebot`](https://github.com/waistu/Ninebot): useful as a historical
  Ninebot API reference, but not enough for this account's encrypted Track
  travel APIs.
- [`ownbee/ninebot-ble`](https://github.com/ownbee/ninebot-ble): useful BLE-side
  reference for direct vehicle communication; not used for cloud trip history.
- [`scooterhacking/NinebotCrypto`](https://github.com/scooterhacking/NinebotCrypto):
  useful for scooter/BLE crypto context; not the same as the cloud travel
  request crypto implemented here.
- [`r0ysue/r0capture`](https://github.com/r0ysue/r0capture): useful Android
  runtime capture option when Reqable/HTTPS proxying is blocked by app-side
  encryption or certificate pinning; not required by the final GitHub Actions
  cloud exporter.

## Run Locally

Install dependencies:

```bash
pip install -r requirements.txt
```

Fetch all cloud trips and details for the configured month:

```bash
source .env
python scripts/ninebot_cloud_export.py --month "$NINEBOT_MONTH" --export-dir data/cloud-export
python scripts/print_trips.py --export-dir data/cloud-export
```

Fetch historical months:

```bash
source .env
python scripts/ninebot_cloud_export.py --all-months --start-month 202401 --export-dir data/cloud-export
python scripts/print_trips.py --export-dir data/cloud-export
```

A successful run writes:

- `data/cloud-export/raw/travel-list-page-*.json`
- `data/cloud-export/raw/travel-info-<travel_id>.json`
- `data/cloud-export/trip_*_wgs84.gpx`
- `data/cloud-export/trip_*_gcj02.gpx`
- `data/cloud-export/trip_*_points_gcj02.csv`
- `data/cloud-export/trips.csv`
- `data/cloud-export/summary.json`

`data/cloud-export*/` is gitignored because it contains private GPS tracks.

Coordinate note:

- The Notion/Web pipeline stores WGS84 GPX as the canonical track format. This
  matches Mapbox/OpenStreetMap-style web maps and avoids source-specific frontend
  coordinate correction when Strava or other WGS84 sources are added later.
- The exporter still writes GCJ-02 artifacts for domestic-map apps that expect
  GCJ-02, but those files are not uploaded to Notion by default.

## GitHub Actions

Workflow: `.github/workflows/print-ninebot-data.yml`

- `workflow_dispatch`: manual run, optional `month` input such as `202607`; set `all_months=true` and optional `start_month` for historical export.
- `schedule`: daily at 12:00 and 22:00 Asia/Shanghai.
- Runs on `ubuntu-latest` with the pure-Python travel crypto implementation.
- Syncs fetched trips to Notion when `NOTION_TOKEN` and `NOTION_DATABASE_ID` secrets are configured.
- Repairs simplified old tracks from repeated full routes before syncing to Notion.
- Uploads `data/cloud-export` as the `ninebot-cloud-export` artifact.

Configured secrets:

- `NINEBOT_ACCESS_TOKEN`
- `NINEBOT_REFRESH_TOKEN`
- `NINEBOT_BUSINESS_UID`
- `NINEBOT_WNUMBER`
- `NINEBOT_VEHICLE_TYPE`
- `NINEBOT_RN_VERSION`
- `NINEBOT_DEVICE_ID`
- `NINEBOT_BUSINESS_TYPE`


Notion secrets:

- `NOTION_TOKEN`
- `NOTION_PARENT_PAGE_ID`
- `NOTION_DATABASE_ID`
- `NOTION_TITLE_PROPERTY`

The Notion sync writes trip metadata as database properties and uploads only the
WGS84 GPX as a JSON-wrapped file in the `GPX` property. It does not attach files
to the page body.

Note: `NINEBOT_REFRESH_TOKEN` is used by `scripts/ninebot_passport.py` for a
best-effort pure-Python Passport refresh. The reconstructed Passport signature
is accepted by the server, but this account currently returns `90002 args
missing`; the exporter logs the refresh failure and continues while the current
access token is still valid. If a refresh ever succeeds, the new tokens are used
for that run. Persisting refreshed tokens back to GitHub Secrets still requires
an additional GitHub token with permission to update repository secrets.

## Fetch From Phone Locally

Use this when you want to fetch from the logged-in Android app instead of the
cloud path. It drives the Ninebot Android app via ADB, lets the app call the
encrypted `tokenRequest` API, then extracts decrypted `travel-info` data from
logcat/heap and regenerates CSV/GPX/JSON.

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

## Raw Travel API Probe

`scripts/ninebot_raw_travel_info.py` is a Python port of the `ninecli` travel
encryption wrapper. It lets us send the e-bike `vehicle_type`, `rnVersion`,
`startTime`, `endTime`, and `businessType` fields needed by this account.

List trips for a month:

```bash
source .env
python scripts/ninebot_raw_travel_info.py list --month "$NINEBOT_MONTH"
```

Fetch a detail/trajectory payload after copying `travel_id`, `startTime`, and
`endTime` from the list output:

```bash
python scripts/ninebot_raw_travel_info.py detail \
  --travel-id "<travel_id>" \
  --start-time 1783389230 \
  --end-time 1783390364
```

Required env/secrets:

- `NINEBOT_ACCESS_TOKEN`
- `NINEBOT_BUSINESS_UID`
- `NINEBOT_WNUMBER`
- `NINEBOT_DEVICE_ID`
- `NINEBOT_VEHICLE_TYPE` (observed e-bike value: `14356`)
- `NINEBOT_RN_VERSION` (observed Track bundle value: `753`)

## Optional `ninecli` Probe

`hasscc/ninebot` delegates all Ninebot cloud calls to `ninecli==0.1.7`. This is
useful locally on macOS for reverse engineering and bootstrapping tokens, but it
is not required by the GitHub Actions export path.

Bootstrap local `ninecli` tokens from the logged-in rooted Android app and probe
travel APIs:

```bash
source .env
python scripts/ninebot_cloud_probe.py \
  --bootstrap-from-android \
  --wnumber "$NINEBOT_WNUMBER" \
  --month "$NINEBOT_MONTH" \
  --business-line ebike \
  --detail-first
```
