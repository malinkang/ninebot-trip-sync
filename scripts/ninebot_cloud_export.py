#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ninebot_phone_fetch import export_detail
from ninebot_raw_travel_info import (
    DEFAULT_CLIENT_VER,
    DEFAULT_TRAVEL_HOST,
    build_travel_info_payload,
    build_travel_list_payload,
    load_env_file,
    load_ninecli_cache,
    post_encrypted,
)

TZ = timezone(timedelta(hours=8))
DEFAULT_CONFIG_DIR = ".cache/ninecli"


def decode_jwt_exp(token: str) -> int | None:
    try:
        import base64

        parts = token.split(".")
        if len(parts) < 2:
            return None
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        exp = payload.get("exp")
        return int(exp) if exp is not None else None
    except Exception:
        return None


def write_json(path: Path, payload: Any, *, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if private:
        os.chmod(path, 0o600)


def build_args(base: argparse.Namespace, *, page: int | None = None, ride: dict[str, Any] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        travel_host=base.travel_host,
        access_token=base.access_token,
        uid=base.uid,
        wnumber=base.wnumber,
        vehicle_type=base.vehicle_type,
        rn_version=base.rn_version,
        business_type=base.business_type,
        client_ver=base.client_ver,
        device_id=base.device_id,
        platform_ver=base.platform_ver,
        platform_version_header=base.platform_version_header,
        month=base.month,
        page=page or 1,
        travel_id=(ride or {}).get("travel_id"),
        start_time=(ride or {}).get("start_time"),
        end_time=(ride or {}).get("end_time"),
        timeout=base.timeout,
        print_http=base.print_http,
        print_request=False,
    )


def unwrap_data(response: dict[str, Any]) -> dict[str, Any]:
    data = response.get("data") if isinstance(response, dict) else None
    return data if isinstance(data, dict) else response


def fetch_page(base: argparse.Namespace, page: int) -> dict[str, Any]:
    args = build_args(base, page=page)
    payload = build_travel_list_payload(args, int(time.time() * 1000))
    return post_encrypted(args, "/app-api/travel/v6/travel-list2", payload)


def fetch_detail(base: argparse.Namespace, ride: dict[str, Any]) -> dict[str, Any]:
    args = build_args(base, ride=ride)
    payload = build_travel_info_payload(args, int(time.time() * 1000))
    return post_encrypted(args, "/app-api/travel/v6/travel-info", payload)


def write_trips_csv(export_dir: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "date",
        "start_time",
        "end_time",
        "duration_sec",
        "mileage_km",
        "max_speed_kmh",
        "energy_percent",
        "energy_wh",
        "points",
        "raw_json",
        "gpx_wgs84",
        "gpx_gcj02",
        "points_csv",
    ]
    with (export_dir / "trips.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in sorted(rows, key=lambda r: r["start_time"]):
            writer.writerow(row)


def find_ninecli(explicit: str | None) -> list[str] | None:
    if explicit:
        return [explicit]
    env_bin = os.getenv("NINECLI_BIN")
    if env_bin:
        return [env_bin]
    return [sys.executable, "-m", "ninecli"]


def seed_ninecli_config(config_dir: Path, base: argparse.Namespace) -> None:
    tokens = {
        "access_token": base.access_token,
        "refresh_token": base.refresh_token,
        "business_uid": base.uid,
        "region": "bj",
        "areaCode": os.getenv("NINEBOT_AREA_CODE", "86"),
        "saved_at": int(time.time()),
    }
    config = {
        "device_id": base.device_id,
        "region": "bj",
        "area_code": os.getenv("NINEBOT_AREA_CODE", "86"),
        "os_version": base.platform_version_header,
        "os_model": base.platform_ver.replace(base.platform_version_header, "", 1).strip() or "Xiaomi",
    }
    write_json(config_dir / "tokens.json", tokens, private=True)
    write_json(config_dir / "config.json", config, private=True)


def best_effort_refresh_with_ninecli(base: argparse.Namespace) -> None:
    if not base.refresh_token:
        print("token refresh skipped: NINEBOT_REFRESH_TOKEN missing", file=sys.stderr)
        return
    exp = decode_jwt_exp(base.access_token)
    if exp and exp - int(time.time()) > base.refresh_before_seconds:
        print(f"token refresh skipped: access_token valid for {exp - int(time.time())}s", file=sys.stderr)
        return
    ninecli = find_ninecli(base.ninecli_bin)
    if not ninecli:
        print("token refresh skipped: ninecli binary not configured", file=sys.stderr)
        return

    with tempfile.TemporaryDirectory(prefix="ninebot-refresh-") as tmp:
        config_dir = Path(tmp)
        seed_ninecli_config(config_dir, base)
        bind = f"127.0.0.1:{base.refresh_port}"
        server = subprocess.Popen(
            [*ninecli, "--config", str(config_dir), "serve", "--bind", bind, "--quiet"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            import requests

            deadline = time.time() + 10
            while time.time() < deadline:
                try:
                    requests.get(f"http://{bind}/healthz", timeout=1)
                    break
                except Exception:
                    time.sleep(0.2)
            response = requests.post(f"http://{bind}/auth/refresh", timeout=30)
            if response.ok:
                updated = json.loads((config_dir / "tokens.json").read_text(encoding="utf-8"))
                base.access_token = updated.get("access_token") or base.access_token
                base.refresh_token = updated.get("refresh_token") or base.refresh_token
                base.uid = str(updated.get("business_uid") or base.uid)
                print("token refresh ok", file=sys.stderr)
            else:
                print(f"token refresh skipped/failed: HTTP {response.status_code} {response.text[:200]}", file=sys.stderr)
        finally:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()


def parse_args() -> argparse.Namespace:
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--env-file", default=".env")
    pre_args, _ = pre.parse_known_args()
    load_env_file(Path(pre_args.env_file))

    now_cn = datetime.now(TZ)
    parser = argparse.ArgumentParser(description="Fetch all Ninebot cloud trips and travel-info details.")
    parser.add_argument("--env-file", default=pre_args.env_file)
    parser.add_argument("--export-dir", default=os.getenv("NINEBOT_EXPORT_DIR", "data/cloud-export"))
    parser.add_argument("--config-dir", default=os.getenv("NINEBOT_NINECLI_CONFIG", DEFAULT_CONFIG_DIR))
    parser.add_argument("--month", default=os.getenv("NINEBOT_MONTH", now_cn.strftime("%Y%m")))
    parser.add_argument("--travel-host", default=os.getenv("NINEBOT_TRAVEL_HOST", DEFAULT_TRAVEL_HOST))
    parser.add_argument("--access-token", default=os.getenv("NINEBOT_ACCESS_TOKEN", ""))
    parser.add_argument("--refresh-token", default=os.getenv("NINEBOT_REFRESH_TOKEN", ""))
    parser.add_argument("--uid", default=os.getenv("NINEBOT_BUSINESS_UID", os.getenv("NINEBOT_UID", "")))
    parser.add_argument("--wnumber", default=os.getenv("NINEBOT_WNUMBER", ""))
    parser.add_argument("--vehicle-type", default=os.getenv("NINEBOT_VEHICLE_TYPE", "14356"))
    parser.add_argument("--rn-version", default=os.getenv("NINEBOT_RN_VERSION", "753"))
    parser.add_argument("--business-type", type=int, default=int(os.getenv("NINEBOT_BUSINESS_TYPE", "2")))
    parser.add_argument("--client-ver", default=os.getenv("NINEBOT_CLIENT_VER", DEFAULT_CLIENT_VER))
    parser.add_argument("--device-id", default=os.getenv("NINEBOT_DEVICE_ID", ""))
    parser.add_argument("--platform-ver", default=os.getenv("NINEBOT_PLATFORM_VER", "13 Xiaomi"))
    parser.add_argument("--platform-version-header", default=os.getenv("NINEBOT_PLATFORM_VERSION_HEADER", "13"))
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--max-pages", type=int, default=int(os.getenv("NINEBOT_MAX_PAGES", "20")))
    parser.add_argument("--max-details", type=int, default=int(os.getenv("NINEBOT_MAX_DETAILS", "0")), help="0 means no limit")
    parser.add_argument("--sleep", type=float, default=float(os.getenv("NINEBOT_REQUEST_SLEEP", "0.25")))
    parser.add_argument("--print-http", action="store_true")
    parser.add_argument("--refresh-with-ninecli", action="store_true", default=os.getenv("NINEBOT_REFRESH_WITH_NINECLI", "0") == "1")
    parser.add_argument("--refresh-before-seconds", type=int, default=int(os.getenv("NINEBOT_REFRESH_BEFORE_SECONDS", str(7 * 24 * 3600))))
    parser.add_argument("--ninecli-bin", default=os.getenv("NINECLI_BIN", ""))
    parser.add_argument("--refresh-port", type=int, default=int(os.getenv("NINEBOT_REFRESH_PORT", "18129")))
    args = parser.parse_args()

    cached_tokens, cached_config = load_ninecli_cache(Path(args.config_dir))
    args.access_token = args.access_token or str(cached_tokens.get("access_token") or "")
    args.refresh_token = args.refresh_token or str(cached_tokens.get("refresh_token") or "")
    args.uid = args.uid or str(cached_tokens.get("business_uid") or cached_tokens.get("uid") or "")
    args.device_id = args.device_id or str(cached_config.get("device_id") or "")
    missing = [name for name in ("access_token", "uid", "wnumber", "device_id") if not getattr(args, name)]
    if missing:
        raise SystemExit("Missing required values: " + ", ".join(missing))
    args.month = args.month.replace("-", "")
    return args


def main() -> int:
    args = parse_args()
    if args.refresh_with_ninecli:
        best_effort_refresh_with_ninecli(args)

    export_dir = Path(args.export_dir)
    export_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = export_dir / "raw"
    rows: list[dict[str, Any]] = []
    rides_by_id: dict[str, dict[str, Any]] = {}
    total_expected: int | None = None

    for page in range(1, args.max_pages + 1):
        response = fetch_page(args, page)
        write_json(raw_dir / f"travel-list-page-{page}.json", response)
        data = unwrap_data(response)
        rides = data.get("list") if isinstance(data.get("list"), list) else []
        if total_expected is None and data.get("times") is not None:
            try:
                total_expected = int(data["times"])
            except Exception:
                total_expected = None
        for ride in rides:
            if isinstance(ride, dict) and ride.get("travel_id"):
                rides_by_id[str(ride["travel_id"])] = ride
        print(f"page {page}: rides={len(rides)} collected={len(rides_by_id)} expected={total_expected}")
        if not rides or (total_expected is not None and len(rides_by_id) >= total_expected):
            break
        time.sleep(args.sleep)

    rides = list(rides_by_id.values())
    detail_limit = len(rides) if args.max_details <= 0 else min(args.max_details, len(rides))
    for idx, ride in enumerate(rides[:detail_limit], start=1):
        try:
            response = fetch_detail(args, ride)
            write_json(raw_dir / f"travel-info-{ride['travel_id']}.json", response)
            detail = unwrap_data(response)
            if not detail.get("start_time"):
                detail["start_time"] = ride.get("start_time")
            if not detail.get("end_time"):
                detail["end_time"] = ride.get("end_time")
            item = {"data": detail, "t": response.get("t"), "travel_id": ride.get("travel_id")}
            row = export_detail(item, export_dir)
            rows.append(row)
            print(f"detail {idx}/{detail_limit}: {ride['travel_id']} points={row['points']} mileage={row['mileage_km']}")
        except Exception as exc:
            print(f"warn: detail failed {ride.get('travel_id')}: {exc}", file=sys.stderr)
        time.sleep(args.sleep)

    write_trips_csv(export_dir, rows)
    summary = {
        "month": args.month,
        "list_count": len(rides),
        "expected_count": total_expected,
        "detail_count": len(rows),
        "total_mileage_km": round(sum(float(row.get("mileage_km") or 0) for row in rows), 3),
        "generated_at": datetime.now(TZ).isoformat(),
    }
    write_json(export_dir / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
