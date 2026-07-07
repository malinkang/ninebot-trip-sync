#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

import requests

from ninebot_crypto import build_payload_json, decrypt_response_body, encrypt_request

DEFAULT_TRAVEL_HOST = "https://cn-cbu-gateway.ninebot.com"
DEFAULT_CLIENT_VER = "610063322"


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_ninecli_cache(config_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    tokens: dict[str, Any] = {}
    config: dict[str, Any] = {}
    tokens_path = config_dir / "tokens.json"
    config_path = config_dir / "config.json"
    if tokens_path.exists():
        tokens = json.loads(tokens_path.read_text(encoding="utf-8"))
    if config_path.exists():
        config = json.loads(config_path.read_text(encoding="utf-8"))
    return tokens, config


def env(name: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise SystemExit(f"Missing env: {name}")
    return value or ""


def common_fields(access_token: str, uid: str, device_id: str, platform_ver: str, client_ver: str) -> OrderedDict[str, Any]:
    return OrderedDict(
        [
            ("sys_language", "zh-hans-cn"),
            ("client_ver", client_ver),
            ("device_id", device_id),
            ("regionx", "bj"),
            ("language", "zh"),
            ("ostype", "and"),
            ("lang", "zh"),
            ("platform_ver", platform_ver),
            ("platform", "android"),
            ("login_country", "CN"),
            ("access_token", access_token),
            ("uid", uid),
        ]
    )


def build_travel_list_payload(args: argparse.Namespace, service_time_ms: int) -> str:
    fields = common_fields(args.access_token, args.uid, args.device_id, args.platform_ver, args.client_ver)
    fields.update(
        [
            ("wnumber", args.wnumber),
            ("rnVersion", args.rn_version),
            ("vehicle_type", args.vehicle_type),
            ("month", args.month.replace("-", "")),
            ("page", args.page),
            ("current_version", args.client_ver),
        ]
    )
    return build_payload_json(fields, service_time_ms=service_time_ms)


def build_travel_info_payload(args: argparse.Namespace, service_time_ms: int) -> str:
    if not args.travel_id:
        raise SystemExit("--travel-id is required for detail mode")
    fields = common_fields(args.access_token, args.uid, args.device_id, args.platform_ver, args.client_ver)
    fields.update(
        [
            ("wnumber", args.wnumber),
            ("rnVersion", args.rn_version),
            ("vehicle_type", args.vehicle_type),
            ("travel_id", args.travel_id),
        ]
    )
    if args.start_time is not None:
        fields["startTime"] = args.start_time
    if args.end_time is not None:
        fields["endTime"] = args.end_time
    if args.business_type is not None:
        fields["businessType"] = args.business_type
    fields["current_version"] = args.client_ver
    return build_payload_json(fields, service_time_ms=service_time_ms)


def post_encrypted(args: argparse.Namespace, path: str, payload_json: str) -> dict[str, Any]:
    envelope, key_data, _request_key, wrapper = encrypt_request(payload_json, platform=2, timestamp_s=int(time.time()))
    headers = {
        "User-Agent": "okhttp/4.9.1",
        "Content-Type": "text/html;charset=UTF-8",
        "Accept": "application/json",
        "Access_token": args.access_token,
        "Business-Type": str(args.business_type or 2),
        "Client-Ver": args.client_ver,
        "Device-Id": args.device_id,
        "Need_decrypt": "1",
        "Ninebot-Version": "2",
        "Platform": "Android",
        "Platform-Ver": args.platform_version_header,
        "Regionx": "bj",
        "Request-Id": os.urandom(16).hex(),
        "Rn-Module": "Track",
        "Rn-Ver": args.rn_version,
        "Rn-Version": "0",
        "Service-Time": str(int(time.time() * 1000)),
        "Sys-Language": "zh-hans-cn",
        "Uid": args.uid,
    }
    if args.print_request:
        print(json.dumps({"payload": json.loads(payload_json), "wrapper_md5": envelope["h"], "wrapper": wrapper}, ensure_ascii=False, indent=2))
    url = args.travel_host.rstrip("/") + path
    response = None
    last_error: Exception | None = None
    for attempt in range(1, int(getattr(args, "retries", 5)) + 1):
        try:
            response = requests.post(
                url,
                headers=headers,
                data=json.dumps(envelope, separators=(",", ":")),
                timeout=args.timeout,
            )
            if args.print_http:
                print(f"HTTP {response.status_code} {response.url}", file=sys.stderr)
                print(response.text[:1000], file=sys.stderr)
            response.raise_for_status()
            break
        except requests.RequestException as exc:
            last_error = exc
            if attempt >= int(getattr(args, "retries", 5)):
                raise
            wait = min(2 ** (attempt - 1), 8)
            print(f"warn: request failed ({exc}); retrying in {wait}s [{attempt}]", file=sys.stderr)
            time.sleep(wait)
    if response is None:
        raise RuntimeError(f"request failed without response: {last_error}")
    parsed = response.json()
    if isinstance(parsed, dict) and isinstance(parsed.get("r"), str):
        plaintext = decrypt_response_body(parsed, key_data)
        try:
            return json.loads(plaintext.decode("utf-8"))
        except json.JSONDecodeError:
            return {"_plaintext": plaintext.decode("utf-8", "replace")}
    return parsed


def parse_args() -> argparse.Namespace:
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--env-file", default=".env")
    pre_args, _ = pre.parse_known_args()
    load_env_file(Path(pre_args.env_file))

    parser = argparse.ArgumentParser(description="Call Ninebot travel APIs with reconstructed ninecli encryption.")
    parser.add_argument("--env-file", default=pre_args.env_file)
    parser.add_argument("mode", choices=["list", "detail"])
    parser.add_argument("--travel-host", default=env("NINEBOT_TRAVEL_HOST", DEFAULT_TRAVEL_HOST))
    parser.add_argument("--access-token", default=env("NINEBOT_ACCESS_TOKEN"), required=False)
    parser.add_argument("--uid", default=env("NINEBOT_BUSINESS_UID", env("NINEBOT_UID")))
    parser.add_argument("--wnumber", default=env("NINEBOT_WNUMBER"))
    parser.add_argument("--vehicle-type", default=env("NINEBOT_VEHICLE_TYPE", "14356"))
    parser.add_argument("--rn-version", default=env("NINEBOT_RN_VERSION", "753"))
    parser.add_argument("--business-type", type=int, default=int(env("NINEBOT_BUSINESS_TYPE", "2")))
    parser.add_argument("--client-ver", default=env("NINEBOT_CLIENT_VER", DEFAULT_CLIENT_VER))
    parser.add_argument("--device-id", default=env("NINEBOT_DEVICE_ID", "0123456789abcdef0123456789abcdef"))
    parser.add_argument("--platform-ver", default=env("NINEBOT_PLATFORM_VER", "13 Xiaomi"))
    parser.add_argument("--platform-version-header", default=env("NINEBOT_PLATFORM_VERSION_HEADER", "13"))
    parser.add_argument("--month", default=env("NINEBOT_MONTH", time.strftime("%Y%m")))
    parser.add_argument("--page", type=int, default=int(env("NINEBOT_PAGE", "1")))
    parser.add_argument("--travel-id")
    parser.add_argument("--start-time", type=int)
    parser.add_argument("--end-time", type=int)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--retries", type=int, default=int(env("NINEBOT_REQUEST_RETRIES", "5")))
    parser.add_argument("--print-request", action="store_true")
    parser.add_argument("--print-http", action="store_true")
    args = parser.parse_args()

    cache_dir = Path(os.getenv("NINEBOT_NINECLI_CONFIG", ".cache/ninecli"))
    cached_tokens, cached_config = load_ninecli_cache(cache_dir)
    if not args.access_token:
        args.access_token = str(cached_tokens.get("access_token") or "")
    if not args.uid:
        args.uid = str(cached_tokens.get("business_uid") or cached_tokens.get("uid") or "")
    if args.device_id == "0123456789abcdef0123456789abcdef" and cached_config.get("device_id"):
        args.device_id = str(cached_config["device_id"])

    missing = [name for name in ("access_token", "uid", "wnumber") if not getattr(args, name)]
    if missing:
        raise SystemExit("Missing required values: " + ", ".join(missing))
    return args


def main() -> int:
    args = parse_args()
    service_time_ms = int(time.time() * 1000)
    if args.mode == "list":
        payload = build_travel_list_payload(args, service_time_ms)
        result = post_encrypted(args, "/app-api/travel/v6/travel-list2", payload)
    else:
        payload = build_travel_info_payload(args, service_time_ms)
        result = post_encrypted(args, "/app-api/travel/v6/travel-info", payload)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
