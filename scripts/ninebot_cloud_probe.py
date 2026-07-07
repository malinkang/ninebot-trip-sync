#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


DEFAULT_BIZ_HOST = "https://api-jhcx-v6-bj.ninebot.com"
DEFAULT_TRAVEL_HOST = "https://cn-cbu-gateway.ninebot.com"
PACKAGE = "cn.ninebot.ninebot"


def month_to_yyyymm(value: str) -> str:
    value = value.strip()
    if len(value) == 7 and value[4] == "-":
        return value[:4] + value[5:7]
    return value


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"Missing env: {name}")
    return value


def run_command(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"{command[0]} exited {result.returncode}")
    return result


def find_ninecli(explicit: str | None) -> list[str]:
    if explicit:
        return [explicit]
    env_bin = os.getenv("NINECLI_BIN")
    if env_bin:
        return [env_bin]
    return [sys.executable, "-m", "ninecli"]


def ninecli_json(
    ninecli: list[str],
    config_dir: Path,
    args: list[str],
    *,
    biz_host: str,
    travel_host: str,
    check: bool = True,
) -> Any:
    command = [
        *ninecli,
        "--config",
        str(config_dir),
        "--biz-host",
        biz_host,
        "--travel-host",
        travel_host,
        "--json",
        *args,
    ]
    result = run_command(command, check=check)
    if result.returncode != 0:
        return {"_error": result.stderr.strip() or result.stdout.strip()}
    stdout = result.stdout.strip()
    return json.loads(stdout) if stdout else None


def adb_cat(serial: str | None, remote_path: str) -> str:
    command = ["adb"]
    if serial:
        command.extend(["-s", serial])
    command.extend(["shell", "su", "-c", f"cat {remote_path}"])
    return run_command(command).stdout


def read_android_passport_tokens(serial: str | None) -> dict[str, str]:
    xml_text = adb_cat(serial, f"/data/data/{PACKAGE}/shared_prefs/passport_account.xml")
    root = ET.fromstring(xml_text)
    values: dict[str, str] = {}
    for child in root:
        name = child.attrib.get("name")
        value = child.attrib.get("value") if "value" in child.attrib else (child.text or "")
        if name:
            values[name] = value
    tokens = {key: values[key] for key in ("access_token", "refresh_token") if values.get(key)}
    if set(tokens) != {"access_token", "refresh_token"}:
        raise RuntimeError(
            "passport_account.xml does not contain both access_token and refresh_token; "
            "open the Ninebot app and make sure it is logged in, then retry"
        )
    return tokens


def read_env_passport_tokens() -> dict[str, str]:
    access_token = os.getenv("NINEBOT_ACCESS_TOKEN")
    refresh_token = os.getenv("NINEBOT_REFRESH_TOKEN")
    if not access_token or not refresh_token:
        raise RuntimeError("Set both NINEBOT_ACCESS_TOKEN and NINEBOT_REFRESH_TOKEN")
    return {"access_token": access_token, "refresh_token": refresh_token}


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(path, 0o600)


def ensure_config(config_dir: Path) -> None:
    config_path = config_dir / "config.json"
    if config_path.exists():
        return
    write_json(
        config_path,
        {
            "device_id": secrets.token_hex(16),
            "region": "bj",
            "area_code": "86",
            "os_version": "13",
            "os_model": "Xiaomi",
        },
    )


def bootstrap_tokens(
    *,
    tokens: dict[str, str],
    ninecli: list[str],
    config_dir: Path,
    biz_host: str,
    travel_host: str,
) -> dict[str, Any]:
    ensure_config(config_dir)
    tokens_path = config_dir / "tokens.json"
    write_json(tokens_path, tokens)

    # Use Passport /v5/user to fill non-secret metadata required by ninecli.
    whoami = ninecli_json(ninecli, config_dir, ["whoami"], biz_host=biz_host, travel_host=travel_host)
    data = whoami.get("data") if isinstance(whoami, dict) else {}
    if not isinstance(data, dict):
        raise RuntimeError("ninecli whoami did not return user data")
    tokens.update(
        {
            "uuid": data.get("uuid") or "",
            "username": data.get("username") or "",
            "phone": data.get("phone") or "",
            "region": data.get("region") or "bj",
            "areaCode": data.get("areaCode") or "86",
            "saved_at": int(time.time()),
        }
    )
    write_json(tokens_path, tokens)

    # Trigger business_login once; some accounts return an empty vehicle list,
    # so the caller seeds vehicles.json afterwards with the known SN.
    ninecli_json(ninecli, config_dir, ["vehicles"], biz_host=biz_host, travel_host=travel_host, check=False)
    return {"uuid_present": bool(tokens.get("uuid")), "phone_present": bool(tokens.get("phone"))}


def seed_vehicle_cache(config_dir: Path, wnumber: str, business_line: str) -> None:
    write_json(config_dir / "vehicles.json", {"vehicles": [{"wnumber": wnumber, "business_line": business_line}]})


def summarize_travel(payload: dict[str, Any]) -> dict[str, Any]:
    rides = payload.get("list") if isinstance(payload.get("list"), list) else []
    return {
        "month": payload.get("month"),
        "times": payload.get("times"),
        "returned_rides": len(rides),
        "total_mileages": payload.get("total_mileages"),
        "duration": payload.get("duration"),
        "ec": payload.get("ec"),
        "first_rides": [
            {
                "travel_id": ride.get("travel_id"),
                "start_time": ride.get("start_time"),
                "end_time": ride.get("end_time"),
                "mileages": ride.get("mileages"),
                "duration": ride.get("duration"),
                "speed": ride.get("speed"),
                "ec": ride.get("ec"),
                "used_electricity": ride.get("used_electricity"),
            }
            for ride in rides[:5]
            if isinstance(ride, dict)
        ],
    }


def summarize_detail(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"ok": False, "reason": "not_dict"}
    trail = payload.get("trail")
    points = trail if isinstance(trail, list) else []
    non_null = [key for key, value in payload.items() if value is not None]
    return {
        "non_null_keys": non_null,
        "trail_points": len(points),
        "has_track": bool(points),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe Ninebot cloud APIs through hasscc/ninebot's ninecli backend.")
    parser.add_argument("--wnumber", default=os.getenv("NINEBOT_WNUMBER"), help="Vehicle SN/wnumber")
    parser.add_argument("--month", default=os.getenv("NINEBOT_MONTH", time.strftime("%Y-%m")), help="YYYYMM or YYYY-MM")
    parser.add_argument("--config-dir", default=os.getenv("NINEBOT_NINECLI_CONFIG", ".cache/ninecli"))
    parser.add_argument("--ninecli-bin", default=None, help="Path to the ninecli binary; defaults to python -m ninecli")
    parser.add_argument("--serial", default=os.getenv("ADB_SERIAL"), help="ADB serial for --bootstrap-from-android")
    parser.add_argument("--business-line", default=os.getenv("NINEBOT_BUSINESS_LINE", "ebike"), choices=["ebike", "motor"])
    parser.add_argument("--biz-host", default=os.getenv("NINEBOT_BIZ_HOST", DEFAULT_BIZ_HOST))
    parser.add_argument("--travel-host", default=os.getenv("NINEBOT_TRAVEL_HOST", DEFAULT_TRAVEL_HOST))
    parser.add_argument("--bootstrap-from-android", action="store_true", help="Read Passport tokens from the rooted Android app into the local ninecli config")
    parser.add_argument("--bootstrap-from-env", action="store_true", help="Read NINEBOT_ACCESS_TOKEN/NINEBOT_REFRESH_TOKEN into the local ninecli config")
    parser.add_argument("--detail-first", action="store_true", help="Also probe the first ride detail endpoint")
    parser.add_argument("--save-raw-list", default=None, help="Optional path to save decrypted travel-list JSON")
    args = parser.parse_args()

    wnumber = args.wnumber or require_env("NINEBOT_WNUMBER")
    month = month_to_yyyymm(args.month)
    config_dir = Path(args.config_dir)
    ninecli = find_ninecli(args.ninecli_bin)

    bootstrap_result: dict[str, Any] | None = None
    if args.bootstrap_from_android:
        bootstrap_result = bootstrap_tokens(
            tokens=read_android_passport_tokens(args.serial),
            ninecli=ninecli,
            config_dir=config_dir,
            biz_host=args.biz_host,
            travel_host=args.travel_host,
        )
    elif args.bootstrap_from_env:
        bootstrap_result = bootstrap_tokens(
            tokens=read_env_passport_tokens(),
            ninecli=ninecli,
            config_dir=config_dir,
            biz_host=args.biz_host,
            travel_host=args.travel_host,
        )

    seed_vehicle_cache(config_dir, wnumber, args.business_line)
    travel = ninecli_json(
        ninecli,
        config_dir,
        ["travel", wnumber, "--month", month],
        biz_host=args.biz_host,
        travel_host=args.travel_host,
    )
    if not isinstance(travel, dict) or "_error" in travel:
        raise RuntimeError(f"travel probe failed: {travel}")

    if args.save_raw_list:
        write_json(Path(args.save_raw_list), travel)

    result: dict[str, Any] = {
        "bootstrap": bootstrap_result,
        "business_line": args.business_line,
        "travel_list": summarize_travel(travel),
    }

    rides = travel.get("list") if isinstance(travel.get("list"), list) else []
    if args.detail_first and rides:
        first = rides[0]
        detail_id = first.get("travel_id") if isinstance(first, dict) else None
        if detail_id:
            detail = ninecli_json(
                ninecli,
                config_dir,
                ["travel", wnumber, "--detail", str(detail_id), "--month", month],
                biz_host=args.biz_host,
                travel_host=args.travel_host,
            )
            result["travel_detail_first"] = summarize_detail(detail)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
