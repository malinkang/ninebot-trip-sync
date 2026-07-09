#!/usr/bin/env python3
"""Sync Ninebot trip exports into a Notion database.

Uses the official notion-client SDK. GPX files are uploaded as JSON files because
Notion's file upload API rejects/does not support the .gpx extension.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from notion_client import Client

from ninebot_raw_travel_info import load_env_file

DEFAULT_PARENT_PAGE_ID = "39686019c92c807aa0e0c1c4c600d302"
DEFAULT_DATABASE_TITLE = "Ninebot Trips"
DEFAULT_TITLE_PROPERTY = "Name"
DEFAULT_GPX_PROPERTY = "GPX"


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Trip:
    export_dir: Path
    row: dict[str, str]

    @property
    def date(self) -> str:
        return self.row.get("date", "")

    @property
    def start_time(self) -> str:
        return self.row.get("start_time", "")

    @property
    def end_time(self) -> str:
        return self.row.get("end_time", "")

    @property
    def mileage_km(self) -> str:
        return self.row.get("mileage_km", "")

    @property
    def title(self) -> str:
        return f"Ninebot {self.start_time} - {self.mileage_km}km"

    @property
    def stable_key(self) -> str:
        return f"{self.start_time}|{self.end_time}|{self.mileage_km}"

    def gpx_wgs84_name(self) -> str:
        name = self.row.get("gpx_wgs84", "")
        return name if name and (self.export_dir / name).exists() else ""

    def file_names(self) -> list[str]:
        name = self.gpx_wgs84_name()
        return [name] if name else []


def parse_number(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def parse_int(value: str | None) -> int | None:
    number = parse_number(value)
    return int(number) if number is not None else None


def parse_notion_date(value: str | None) -> dict[str, str] | None:
    if not value:
        return None
    text = value.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text, fmt)
            if fmt == "%Y-%m-%d":
                return {"start": dt.date().isoformat()}
            return {"start": dt.replace(tzinfo=timezone.utc).isoformat()}
        except ValueError:
            pass
    return {"start": text}


def rich_text(text: str) -> dict[str, Any]:
    return {"rich_text": [{"type": "text", "text": {"content": text[:2000]}}]}


def title_text(text: str) -> dict[str, Any]:
    return {"title": [{"type": "text", "text": {"content": text[:2000]}}]}


def number_prop(value: str | None) -> dict[str, Any] | None:
    number = parse_number(value)
    return {"number": number} if number is not None else None


def date_prop(value: str | None) -> dict[str, Any] | None:
    parsed = parse_notion_date(value)
    return {"date": parsed} if parsed else None


def database_schema() -> dict[str, Any]:
    return {
        "Name": {"title": {}},
        "Stable Key": {"rich_text": {}},
        "Date": {"date": {}},
        "Start Time": {"date": {}},
        "End Time": {"date": {}},
        "Mileage km": {"number": {"format": "number"}},
        "Duration sec": {"number": {"format": "number"}},
        "Max Speed km/h": {"number": {"format": "number"}},
        "Energy Percent": {"number": {"format": "percent"}},
        "Energy Wh": {"number": {"format": "number"}},
        "Points": {"number": {"format": "number"}},
        "Digest": {"rich_text": {}},
        "GPX": {"files": {}},
        "Synced At": {"date": {}},
    }


def normalize_id(value: str) -> str:
    return value.replace("-", "").strip()


def format_id(value: str) -> str:
    raw = normalize_id(value)
    if len(raw) != 32:
        return value
    return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"


def find_child_database(notion: Client, parent_page_id: str, title: str) -> str | None:
    cursor: str | None = None
    while True:
        kwargs: dict[str, Any] = {"block_id": parent_page_id, "page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor
        result = notion.blocks.children.list(**kwargs)
        for block in result.get("results", []):
            if block.get("type") == "child_database" and block.get("child_database", {}).get("title") == title:
                return block["id"]
        if not result.get("has_more"):
            return None
        cursor = result.get("next_cursor")


def ensure_database(notion: Client, parent_page_id: str, title: str = DEFAULT_DATABASE_TITLE) -> str:
    existing = find_child_database(notion, parent_page_id, title)
    if existing:
        notion.data_sources.update(data_source_id=resolve_data_source_id(notion, existing), properties=database_schema())
        return existing
    created = notion.databases.create(
        parent={"type": "page_id", "page_id": parent_page_id},
        title=[{"type": "text", "text": {"content": title}}],
        properties=database_schema(),
    )
    notion.data_sources.update(data_source_id=resolve_data_source_id(notion, created["id"]), properties=database_schema())
    return created["id"]


def resolve_data_source_id(notion: Client, database_id: str) -> str:
    database = notion.databases.retrieve(database_id=database_id)
    data_sources = database.get("data_sources") or []
    if not data_sources:
        raise ConfigError(f"Database {database_id} has no data sources")
    return data_sources[0]["id"]


def load_trips(export_dir: Path) -> list[Trip]:
    csv_files = [export_dir / "trips.csv"] if (export_dir / "trips.csv").exists() else sorted(export_dir.glob("*/trips.csv"))
    if not csv_files:
        raise FileNotFoundError(f"Missing trips.csv under {export_dir}")
    trips: list[Trip] = []
    for csv_path in csv_files:
        with csv_path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                trips.append(Trip(export_dir=csv_path.parent, row=dict(row)))
    return trips


def file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def trip_digest(trip: Trip) -> str:
    parts = [trip.stable_key]
    for name in trip.file_names():
        parts.append(name)
        parts.append(file_digest(trip.export_dir / name))
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def query_page_by_stable_key(notion: Client, data_source_id: str, stable_key: str) -> dict[str, Any] | None:
    result = notion.data_sources.query(
        data_source_id=data_source_id,
        filter={"property": "Stable Key", "rich_text": {"equals": stable_key}},
        page_size=1,
    )
    items = result.get("results", [])
    return items[0] if items else None


def page_digest(page: dict[str, Any]) -> str | None:
    prop = page.get("properties", {}).get("Digest", {})
    texts = prop.get("rich_text", [])
    if not texts:
        return None
    return texts[0].get("plain_text")


def file_upload_property(filename: str, file_upload_id: str) -> dict[str, Any]:
    return {"files": [{"name": filename, "type": "file_upload", "file_upload": {"id": file_upload_id}}]}


def trip_properties(trip: Trip, digest: str | None = None, gpx_upload: tuple[str, str] | None = None) -> dict[str, Any]:
    props: dict[str, Any] = {
        "Name": title_text(trip.title),
        "Stable Key": rich_text(trip.stable_key),
        "Digest": rich_text(digest) if digest else {"rich_text": []},
    }
    if gpx_upload:
        props[DEFAULT_GPX_PROPERTY] = file_upload_property(gpx_upload[0], gpx_upload[1])
    if digest:
        props["Synced At"] = {"date": {"start": datetime.now(timezone.utc).isoformat()}}
    optional = {
        "Date": date_prop(trip.date),
        "Start Time": date_prop(trip.start_time),
        "End Time": date_prop(trip.end_time),
        "Mileage km": number_prop(trip.mileage_km),
        "Duration sec": number_prop(trip.row.get("duration_sec")),
        "Max Speed km/h": number_prop(trip.row.get("max_speed_kmh")),
        "Energy Percent": number_prop(trip.row.get("energy_percent")),
        "Energy Wh": number_prop(trip.row.get("energy_wh")),
        "Points": {"number": parse_int(trip.row.get("points"))} if parse_int(trip.row.get("points")) is not None else None,
    }
    props.update({key: value for key, value in optional.items() if value is not None})
    return props


def uploadable_file(path: Path) -> tuple[Path, str, str, tempfile.TemporaryDirectory[str] | None]:
    if path.suffix.lower() != ".gpx":
        content_type = "text/csv" if path.suffix.lower() == ".csv" else "application/json" if path.suffix.lower() == ".json" else "application/octet-stream"
        return path, path.name, content_type, None
    tmp = tempfile.TemporaryDirectory(prefix="ninebot-gpx-json-")
    upload_path = Path(tmp.name) / f"{path.stem}.gpx.json"
    payload = {"original_filename": path.name, "content_type": "application/gpx+xml", "gpx": path.read_text(encoding="utf-8")}
    upload_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return upload_path, upload_path.name, "application/json", tmp


def upload_file(notion: Client, path: Path) -> str:
    upload_path, filename, content_type, tmp = uploadable_file(path)
    try:
        created = notion.file_uploads.create(filename=filename, content_type=content_type)
        upload_id = created["id"]
        with upload_path.open("rb") as handle:
            notion.file_uploads.send(upload_id, file=(filename, handle, content_type))
        return upload_id
    finally:
        if tmp is not None:
            tmp.cleanup()


def clear_children(notion: Client, page_id: str) -> None:
    cursor: str | None = None
    block_ids: list[str] = []
    while True:
        kwargs: dict[str, Any] = {"block_id": page_id, "page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor
        result = notion.blocks.children.list(**kwargs)
        block_ids.extend(block["id"] for block in result.get("results", []))
        if not result.get("has_more"):
            break
        cursor = result.get("next_cursor")
    for block_id in block_ids:
        try:
            notion.blocks.delete(block_id=block_id)
        except Exception:
            pass


def upload_trip_gpx(notion: Client, trip: Trip) -> tuple[str, str] | None:
    name = trip.gpx_wgs84_name()
    if not name:
        return None
    upload_id = upload_file(notion, trip.export_dir / name)
    upload_filename = f"{Path(name).stem}.gpx.json"
    time.sleep(0.35)
    return upload_filename, upload_id


def sync_exports(notion: Client, database_id: str, export_dir: Path, limit: int = 0, force: bool = False) -> dict[str, int]:
    data_source_id = resolve_data_source_id(notion, database_id)
    notion.data_sources.update(data_source_id=data_source_id, properties=database_schema())
    trips = load_trips(export_dir)
    if limit > 0:
        trips = trips[:limit]
    counts = {"found": len(trips), "created": 0, "updated": 0, "skipped": 0}
    for idx, trip in enumerate(trips, start=1):
        digest = trip_digest(trip)
        existing = query_page_by_stable_key(notion, data_source_id, trip.stable_key)
        if existing and not force and page_digest(existing) == digest:
            counts["skipped"] += 1
            print(f"skip {idx}/{len(trips)} {trip.title}", flush=True)
            continue
        if existing:
            page_id = existing["id"]
            notion.pages.update(page_id=page_id, properties=trip_properties(trip))
            clear_children(notion, page_id)
            counts["updated"] += 1
            action = "update"
        else:
            page = notion.pages.create(parent={"data_source_id": data_source_id}, properties=trip_properties(trip))
            page_id = page["id"]
            counts["created"] += 1
            action = "create"
        gpx_upload = upload_trip_gpx(notion, trip)
        notion.pages.update(page_id=page_id, properties=trip_properties(trip, digest, gpx_upload))
        print(f"{action} {idx}/{len(trips)} {trip.title}", flush=True)
    return counts


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--env-file", default=".env")
    pre_args, _ = pre.parse_known_args(list(argv) if argv is not None else None)
    load_env_file(Path(pre_args.env_file))

    parser = argparse.ArgumentParser(description="Sync Ninebot trips to a Notion child database")
    parser.add_argument("--env-file", default=pre_args.env_file)
    parser.add_argument("--export-dir", default=os.getenv("NINEBOT_EXPORT_DIR", "data/cloud-export"))
    parser.add_argument("--token", default=os.getenv("NOTION_TOKEN", ""))
    parser.add_argument("--parent-page-id", default=os.getenv("NOTION_PARENT_PAGE_ID", DEFAULT_PARENT_PAGE_ID))
    parser.add_argument("--database-id", default=os.getenv("NOTION_DATABASE_ID", ""))
    parser.add_argument("--database-title", default=os.getenv("NOTION_DATABASE_TITLE", DEFAULT_DATABASE_TITLE))
    parser.add_argument("--ensure-database", action="store_true")
    parser.add_argument("--sync", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--timeout-ms", type=int, default=int(os.getenv("NOTION_TIMEOUT_MS", "120000")))
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.token:
        raise ConfigError("Set NOTION_TOKEN")
    notion = Client(options={"auth": args.token, "timeout_ms": args.timeout_ms})
    database_id = args.database_id
    if args.ensure_database or not database_id:
        database_id = ensure_database(notion, format_id(args.parent_page_id), args.database_title)
        print(json.dumps({"database_id": database_id, "database_title": args.database_title}, ensure_ascii=False), flush=True)
    if args.sync:
        if not database_id:
            raise ConfigError("Set NOTION_DATABASE_ID or use --ensure-database")
        counts = sync_exports(notion, format_id(database_id), Path(args.export_dir), limit=args.limit, force=args.force)
        print(json.dumps(counts, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
