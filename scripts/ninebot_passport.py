#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import requests

from ninebot_raw_travel_info import DEFAULT_CLIENT_VER, load_env_file, load_ninecli_cache

DEFAULT_PASSPORT_BASE = "https://api-passport-bj.ninebot.com"
DEFAULT_CLIENT_ID = "vehicle_app_prod"
DEFAULT_CLIENT_KEY = "e177176a-3b3e-1513-e26e-d1123034cb66"
DEFAULT_OS = "Android"
DEFAULT_OS_LANGUAGE = "zh-hans-cn"
DEFAULT_OS_VERSION = "13"


class PassportRefreshError(RuntimeError):
    def __init__(self, message: str, response: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.response = response


@dataclass(frozen=True)
class PassportConfig:
    base_url: str = DEFAULT_PASSPORT_BASE
    client_id: str = DEFAULT_CLIENT_ID
    client_key: str = DEFAULT_CLIENT_KEY
    app_version: str = DEFAULT_CLIENT_VER
    os_name: str = DEFAULT_OS
    os_language: str = DEFAULT_OS_LANGUAGE
    os_version: str = DEFAULT_OS_VERSION
    timeout: int = 30

    @property
    def common_params(self) -> dict[str, str]:
        return {
            "app_version": self.app_version,
            "os": self.os_name,
            "os_language": self.os_language,
            "os_version": self.os_version,
        }

    @property
    def common_headers(self) -> dict[str, str]:
        # The Passport SDK sends these as headers, while also including them in the signature string.
        return {
            "App_version": self.app_version,
            "Os": self.os_name,
            "Os_language": self.os_language,
            "Os_version": self.os_version,
        }


def passport_canonical_string(
    path: str,
    body: Mapping[str, Any],
    common: Mapping[str, Any],
    client_key: str,
    timestamp_ms: str,
) -> str:
    values: dict[str, Any] = {"clientKey": client_key, "url": path}
    values.update(common)
    values.update(body)
    values["timestamp"] = timestamp_ms
    return "&".join(f"{key}={values[key]}" for key in sorted(values))


def passport_sign(path: str, body: Mapping[str, Any], common: Mapping[str, Any], client_key: str, timestamp_ms: str) -> str:
    canonical = passport_canonical_string(path, body, common, client_key, timestamp_ms)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _success_code(payload: Mapping[str, Any]) -> bool:
    code = payload.get("code", payload.get("resultCode"))
    if code is None:
        return False
    return str(code) in {"0", "90000"}


def _response_message(payload: Mapping[str, Any]) -> str:
    code = payload.get("code", payload.get("resultCode", ""))
    desc = payload.get("desc", payload.get("resultDesc", payload.get("message", "")))
    return f"code={code} desc={desc!r}"


def refresh_tokens(access_token: str, refresh_token: str, config: PassportConfig | None = None) -> dict[str, Any]:
    if not access_token or not refresh_token:
        raise PassportRefreshError("missing access_token or refresh_token")
    config = config or PassportConfig()
    path = "/v3/user/refresh"
    body = {
        "accessToken": access_token,
        "device": "ANDROID",
        "refreshToken": refresh_token,
    }
    timestamp_ms = str(int(time.time() * 1000))
    sign = passport_sign(path, body, config.common_params, config.client_key, timestamp_ms)
    headers = {
        "User-Agent": "Go-http-client/1.1",
        "Content-Type": "application/json; charset=UTF-8",
        "Clientid": config.client_id,
        "Timestamp": timestamp_ms,
        "Sign": sign,
        **config.common_headers,
    }
    try:
        response = requests.post(
            config.base_url.rstrip("/") + path,
            headers=headers,
            data=json.dumps(body, separators=(",", ":")),
            timeout=config.timeout,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise PassportRefreshError(f"passport refresh request failed: {exc}") from exc
    try:
        payload = response.json()
    except Exception as exc:
        raise PassportRefreshError(f"passport refresh response is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise PassportRefreshError("passport refresh returned a non-object response")
    if not _success_code(payload):
        raise PassportRefreshError(f"passport refresh failed: {_response_message(payload)}", payload)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise PassportRefreshError("passport refresh response is missing data", payload)
    new_access = data.get("access_token") or data.get("accessToken")
    new_refresh = data.get("refresh_token") or data.get("refreshToken")
    if not new_access or not new_refresh:
        raise PassportRefreshError("passport refresh response is missing tokens", payload)
    return {"access_token": str(new_access), "refresh_token": str(new_refresh), "raw": payload}


def parse_args() -> argparse.Namespace:
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--env-file", default=".env")
    pre_args, _ = pre.parse_known_args()
    load_env_file(Path(pre_args.env_file))

    parser = argparse.ArgumentParser(description="Refresh Ninebot Passport access/refresh tokens.")
    parser.add_argument("--env-file", default=pre_args.env_file)
    parser.add_argument("--config-dir", default=os.getenv("NINEBOT_NINECLI_CONFIG", ".cache/ninecli"))
    parser.add_argument("--access-token", default=os.getenv("NINEBOT_ACCESS_TOKEN", ""))
    parser.add_argument("--refresh-token", default=os.getenv("NINEBOT_REFRESH_TOKEN", ""))
    parser.add_argument("--passport-base", default=os.getenv("NINEBOT_PASSPORT_BASE", DEFAULT_PASSPORT_BASE))
    parser.add_argument("--client-id", default=os.getenv("NINEBOT_PASSPORT_CLIENT_ID", DEFAULT_CLIENT_ID))
    parser.add_argument("--client-key", default=os.getenv("NINEBOT_PASSPORT_CLIENT_KEY", DEFAULT_CLIENT_KEY))
    parser.add_argument("--app-version", default=os.getenv("NINEBOT_CLIENT_VER", DEFAULT_CLIENT_VER))
    parser.add_argument("--os-version", default=os.getenv("NINEBOT_PASSPORT_OS_VERSION", os.getenv("NINEBOT_PLATFORM_VERSION_HEADER", DEFAULT_OS_VERSION)))
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--write-cache", action="store_true")
    args = parser.parse_args()

    cached_tokens, _cached_config = load_ninecli_cache(Path(args.config_dir))
    args.access_token = args.access_token or str(cached_tokens.get("access_token") or "")
    args.refresh_token = args.refresh_token or str(cached_tokens.get("refresh_token") or "")
    if not args.access_token or not args.refresh_token:
        raise SystemExit("Missing access or refresh token")
    return args


def main() -> int:
    args = parse_args()
    config = PassportConfig(
        base_url=args.passport_base,
        client_id=args.client_id,
        client_key=args.client_key,
        app_version=args.app_version,
        os_version=args.os_version,
        timeout=args.timeout,
    )
    try:
        refreshed = refresh_tokens(args.access_token, args.refresh_token, config)
    except PassportRefreshError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.write_cache:
        config_dir = Path(args.config_dir)
        tokens_path = config_dir / "tokens.json"
        current = json.loads(tokens_path.read_text(encoding="utf-8")) if tokens_path.exists() else {}
        current.update(
            {
                "access_token": refreshed["access_token"],
                "refresh_token": refreshed["refresh_token"],
                "saved_at": int(time.time()),
            }
        )
        tokens_path.parent.mkdir(parents=True, exist_ok=True)
        tokens_path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(tokens_path, 0o600)
    print(json.dumps({"ok": True, "refreshed": True}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
