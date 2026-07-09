#!/usr/bin/env python3
"""Export existing Ninebot Notion Stable Key values for an optional month."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from notion_client import Client

from ninebot_notion_sync import format_id, resolve_data_source_id
from ninebot_raw_travel_info import load_env_file

TZ = timezone(timedelta(hours=8))


def normalize_month(value: str) -> str:
    return value.replace("-", "").strip()


def month_bounds(month: str) -> tuple[str, str]:
    month = normalize_month(month)
    year = int(month[:4])
    mon = int(month[4:6])
    start = datetime(year, mon, 1, tzinfo=timezone.utc)
    if mon == 12:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, mon + 1, 1, tzinfo=timezone.utc)
    return start.isoformat(), end.isoformat()


def stable_key_from_page(page: dict[str, Any]) -> str | None:
    prop = page.get("properties", {}).get("Stable Key", {})
    texts = prop.get("rich_text", [])
    if not texts:
        return None
    text = texts[0].get("plain_text") or ""
    return text.strip() or None


def query_existing_keys(notion: Client, data_source_id: str, month: str = "") -> set[str]:
    keys: set[str] = set()
    cursor: str | None = None
    query: dict[str, Any] = {"data_source_id": data_source_id, "page_size": 100}
    if month:
        start, end = month_bounds(month)
        query["filter"] = {
            "and": [
                {"property": "Start Time", "date": {"on_or_after": start}},
                {"property": "Start Time", "date": {"before": end}},
            ]
        }

    while True:
        kwargs = dict(query)
        if cursor:
            kwargs["start_cursor"] = cursor
        result = notion.data_sources.query(**kwargs)
        for page in result.get("results", []):
            key = stable_key_from_page(page)
            if key:
                keys.add(key)
        if not result.get("has_more"):
            break
        cursor = result.get("next_cursor")
    return keys


def write_keys(path: Path, keys: Iterable[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(sorted(keys)) + ("\n" if keys else ""), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--env-file", default=".env")
    pre_args, _ = pre.parse_known_args()
    load_env_file(Path(pre_args.env_file))

    import os

    now_cn = datetime.now(TZ)
    parser = argparse.ArgumentParser(description="Write existing Ninebot Notion Stable Key values to a text file.")
    parser.add_argument("--env-file", default=pre_args.env_file)
    parser.add_argument("--token", default=os.getenv("NOTION_TOKEN", ""))
    parser.add_argument("--database-id", default=os.getenv("NOTION_DATABASE_ID", ""))
    parser.add_argument("--month", default=os.getenv("NINEBOT_MONTH") or now_cn.strftime("%Y%m"))
    parser.add_argument("--all", action="store_true", help="Query all keys instead of filtering to --month.")
    parser.add_argument("--output", default="data/cloud-export/existing-stable-keys.txt")
    parser.add_argument("--timeout-ms", type=int, default=int(os.getenv("NOTION_TIMEOUT_MS", "120000")))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.token:
        raise SystemExit("Set NOTION_TOKEN")
    if not args.database_id:
        raise SystemExit("Set NOTION_DATABASE_ID")

    notion = Client(options={"auth": args.token, "timeout_ms": args.timeout_ms})
    data_source_id = resolve_data_source_id(notion, format_id(args.database_id))
    keys = query_existing_keys(notion, data_source_id, "" if args.all else args.month)
    write_keys(Path(args.output), keys)
    scope = "all" if args.all else normalize_month(args.month)
    print(json.dumps({"scope": scope, "existing_keys": len(keys), "output": args.output}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
