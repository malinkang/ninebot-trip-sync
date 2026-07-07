#!/usr/bin/env python3
"""Sync Ninebot trip CSV/GPX exports to Notion.

This script has two practical modes:

1. Sync existing exports produced by ninebot-dump/export/export_trips.py.
2. Optionally fetch Ninebot APIs through an external native signer/decrypter command.

The Ninebot app signs/encrypts requests inside native tokenRequest(...). A plain Python
HTTP request is not enough. For fully automatic cloud fetching, set
NINEBOT_TOKENREQUEST_COMMAND to a command that can run the app-compatible native request
and return decrypted plaintext JSON.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import requests

NOTION_VERSION = "2022-06-28"
NOTION_API = "https://api.notion.com/v1"


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Trip:
    date: str
    start_time: str
    end_time: str
    duration_sec: str
    mileage_km: str
    max_speed_kmh: str
    energy_percent: str
    energy_wh: str
    points: str
    raw_json: str
    gpx_wgs84: str
    gpx_gcj02: str
    points_csv: str

    @property
    def title(self) -> str:
        return f"Ninebot {self.start_time} - {self.mileage_km}km"

    @property
    def stable_key(self) -> str:
        return f"{self.start_time}|{self.end_time}|{self.mileage_km}"


class NotionClient:
    def __init__(self, token: str) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            }
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        url = f"{NOTION_API}{path}"
        response = self.session.request(method, url, timeout=60, **kwargs)
        if not response.ok:
            raise RuntimeError(f"Notion API {method} {path} failed: {response.status_code} {response.text}")
        return response.json()

    def create_page(
        self,
        *,
        trip: Trip,
        parent_page_id: str | None,
        database_id: str | None,
        title_property: str,
    ) -> str:
        if parent_page_id:
            parent = {"type": "page_id", "page_id": parent_page_id}
            properties = {"title": {"title": [{"text": {"content": trip.title}}]}}
        elif database_id:
            parent = {"type": "database_id", "database_id": database_id}
            properties = {title_property: {"title": [{"text": {"content": trip.title}}]}}
        else:
            raise ConfigError("Set either NOTION_PARENT_PAGE_ID or NOTION_DATABASE_ID")

        children = [
            paragraph(f"Start: {trip.start_time}"),
            paragraph(f"End: {trip.end_time}"),
            paragraph(f"Mileage: {trip.mileage_km} km"),
            paragraph(f"Duration: {trip.duration_sec} s"),
            paragraph(f"Max speed: {trip.max_speed_kmh} km/h"),
            paragraph(f"Energy: {trip.energy_percent}% / {trip.energy_wh} Wh"),
            paragraph(f"Points: {trip.points}"),
        ]
        payload = {"parent": parent, "properties": properties, "children": children}
        return self._request("POST", "/pages", data=json.dumps(payload, ensure_ascii=False))["id"]

    def append_blocks(self, page_id: str, blocks: list[dict[str, Any]]) -> None:
        if not blocks:
            return
        self._request("PATCH", f"/blocks/{page_id}/children", data=json.dumps({"children": blocks}, ensure_ascii=False))

    def upload_file(self, file_path: Path) -> str:
        create_payload = {"filename": file_path.name, "content_type": guess_content_type(file_path)}
        created = self._request("POST", "/file_uploads", data=json.dumps(create_payload, ensure_ascii=False))
        upload_id = created["id"]
        upload_url = f"{NOTION_API}/file_uploads/{upload_id}/send"
        headers = {
            "Authorization": self.session.headers["Authorization"],
            "Notion-Version": NOTION_VERSION,
        }
        with file_path.open("rb") as handle:
            files = {"file": (file_path.name, handle, guess_content_type(file_path))}
            response = requests.post(upload_url, headers=headers, files=files, timeout=120)
        if not response.ok:
            raise RuntimeError(f"Notion file upload failed for {file_path}: {response.status_code} {response.text}")
        return upload_id


def paragraph(text: str) -> dict[str, Any]:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": text}}]}}


def file_block(name: str, upload_id: str) -> dict[str, Any]:
    return {
        "object": "block",
        "type": "file",
        "file": {
            "caption": [{"type": "text", "text": {"content": name}}],
            "type": "file_upload",
            "file_upload": {"id": upload_id},
        },
    }


def guess_content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".gpx":
        return "application/gpx+xml"
    if suffix == ".csv":
        return "text/csv"
    if suffix == ".json":
        return "application/json"
    return "application/octet-stream"


def load_trips(export_dir: Path) -> list[Trip]:
    csv_path = export_dir / "trips.csv"
    if not csv_path.exists():
        raise FileNotFoundError(f"Missing {csv_path}. Run export_trips.py first.")
    with csv_path.open(newline="", encoding="utf-8") as handle:
        return [Trip(**row) for row in csv.DictReader(handle)]


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"synced": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sync_exports(export_dir: Path, state_path: Path, dry_run: bool = False) -> None:
    token = os.getenv("NOTION_TOKEN")
    if not token and not dry_run:
        raise ConfigError("Set NOTION_TOKEN")
    parent_page_id = os.getenv("NOTION_PARENT_PAGE_ID") or None
    database_id = os.getenv("NOTION_DATABASE_ID") or None
    title_property = os.getenv("NOTION_TITLE_PROPERTY", "Name")
    if not parent_page_id and not database_id and not dry_run:
        raise ConfigError("Set NOTION_PARENT_PAGE_ID or NOTION_DATABASE_ID")

    trips = load_trips(export_dir)
    state = load_state(state_path)
    synced = state.setdefault("synced", {})
    notion = None if dry_run else NotionClient(token or "")

    for trip in trips:
        key = trip.stable_key
        files = [trip.gpx_wgs84, trip.gpx_gcj02, trip.points_csv, trip.raw_json]
        digest_source = key + "|" + "|".join(file_digest(export_dir / name) for name in files)
        digest = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()
        if synced.get(key, {}).get("digest") == digest:
            print(f"skip already synced: {trip.title}")
            continue

        print(f"sync: {trip.title}")
        if dry_run:
            continue

        assert notion is not None
        page_id = notion.create_page(
            trip=trip,
            parent_page_id=parent_page_id,
            database_id=database_id,
            title_property=title_property,
        )
        blocks: list[dict[str, Any]] = []
        for name in files:
            upload_id = notion.upload_file(export_dir / name)
            blocks.append(file_block(name, upload_id))
            # Avoid tight Notion rate limits.
            time.sleep(0.4)
        notion.append_blocks(page_id, blocks)
        synced[key] = {"digest": digest, "page_id": page_id, "synced_at": int(time.time())}
        save_state(state_path, state)

    if dry_run:
        print(f"dry run ok: {len(trips)} trips found")


def tokenrequest_call(url: str, data: dict[str, Any], base: str = "bike") -> dict[str, Any]:
    command = os.getenv("NINEBOT_TOKENREQUEST_COMMAND")
    if not command:
        raise ConfigError(
            "Ninebot cloud fetch needs native tokenRequest signing/decryption. "
            "Set NINEBOT_TOKENREQUEST_COMMAND to an external command that accepts JSON on stdin "
            "and prints the decrypted response JSON."
        )
    payload = {"url": url, "base": base, "data": data}
    proc = subprocess.run(
        command,
        input=json.dumps(payload, ensure_ascii=False),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"tokenRequest command failed: {proc.stderr}")
    return json.loads(proc.stdout)


def fetch_ninebot_online() -> None:
    # This documents the discovered interfaces and provides an adapter once the native signer exists.
    wnumber = require_env("NINEBOT_WNUMBER")
    vehicle_type = require_env("NINEBOT_VEHICLE_TYPE")
    rn_version = os.getenv("NINEBOT_RN_VERSION", "753")
    month = require_env("NINEBOT_MONTH")
    page = int(os.getenv("NINEBOT_PAGE", "1"))
    list_payload = {"wnumber": wnumber, "rnVersion": rn_version, "vehicle_type": vehicle_type, "month": month, "page": page}
    result = tokenrequest_call("https://cn-cbu-gateway.ninebot.com/app-api/travel/v6/travel-list2", list_payload)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ConfigError(f"Set {name}")
    return value


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync Ninebot trip exports to Notion")
    parser.add_argument("--export-dir", default=os.getenv("NINEBOT_EXPORT_DIR", "ninebot-dump/export"))
    parser.add_argument("--state", default=".cache/ninebot-notion-sync-state.json")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--fetch-ninebot-online", action="store_true", help="Call discovered Ninebot list API through NINEBOT_TOKENREQUEST_COMMAND")
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        if args.fetch_ninebot_online:
            fetch_ninebot_online()
        else:
            sync_exports(Path(args.export_dir), Path(args.state), dry_run=args.dry_run)
        return 0
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
