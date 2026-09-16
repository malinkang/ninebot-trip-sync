#!/usr/bin/env python3
"""Sync fresh Ninebot access and refresh tokens from a connected rooted Android phone to GitHub Secrets."""

from __future__ import annotations

import argparse
import base64
import datetime
import json
import re
import subprocess
import sys
import time


def get_default_device() -> str:
    res = subprocess.run(["adb", "devices"], text=True, capture_output=True, check=True)
    devices = []
    for line in res.stdout.strip().splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            devices.append(parts[0])
    if not devices:
        raise RuntimeError("No connected Android device found via adb. Please connect your phone with USB debugging enabled.")
    return devices[0]


def decode_jwt_payload(token: str) -> dict:
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    return json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))


def fetch_tokens_from_phone(serial: str) -> tuple[str, str, int | None]:
    cmd = ["adb", "-s", serial, "shell", "su -c 'cat /data/data/cn.ninebot.ninebot/shared_prefs/passport_account.xml'"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"Failed to read passport_account.xml from phone via root: {res.stderr.strip()}")
    xml_content = res.stdout
    m_acc = re.search(r'<string name="access_token">([^<]+)</string>', xml_content)
    m_ref = re.search(r'<string name="refresh_token">([^<]+)</string>', xml_content)
    if not m_acc or not m_ref:
        raise RuntimeError("access_token or refresh_token not found in phone's passport_account.xml.")
    access_token = m_acc.group(1).strip()
    refresh_token = m_ref.group(1).strip()

    payload = decode_jwt_payload(access_token)
    exp = payload.get("exp")
    return access_token, refresh_token, int(exp) if exp else None


def update_gh_secret(secret_name: str, secret_value: str, repo: str | None = None) -> None:
    cmd = ["gh", "secret", "set", secret_name]
    if repo:
        cmd += ["--repo", repo]
    proc = subprocess.run(cmd, input=secret_value, text=True, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"Failed to set GitHub Secret {secret_name}: {proc.stderr.strip()}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync Ninebot tokens from connected phone to GitHub Secrets")
    parser.add_argument("--serial", default=None, help="Target Android device serial")
    parser.add_argument("--repo", default="malinkang/ninebot-trip-sync", help="Target GitHub repository")
    parser.add_argument("--check-only", action="store_true", help="Only check token validity on phone without setting secrets")
    parser.add_argument("--trigger", action="store_true", default=True, help="Trigger GitHub Actions workflow after updating secrets")
    args = parser.parse_args()

    serial = args.serial or get_default_device()
    print(f"[*] Reading tokens from Android device: {serial}...")
    try:
        access_token, refresh_token, exp = fetch_tokens_from_phone(serial)
    except Exception as e:
        print(f"❌ Error reading tokens: {e}", file=sys.stderr)
        return 1

    now = time.time()
    if exp is not None:
        exp_dt = datetime.datetime.fromtimestamp(exp, datetime.timezone.utc).astimezone()
        print(f"[*] Access token expiration time: {exp_dt} (timestamp: {exp})")
        if exp < now:
            print(f"\n❌ 手机上的 Access Token 已过期！（过期时间: {exp_dt}）")
            print("👉 解决步骤：")
            print("   1. 请在手机上打开【九号】App。")
            print("   2. 进入【我的】-> 右上角【设置】（或进入账号管理）->【退出登录】，然后重新登录。")
            print("   3. 重新登录后，再次运行本脚本即可自动同步新 Token 到 GitHub Secrets！\n")
            return 1
        else:
            remaining_days = (exp - now) / 86400.0
            print(f"✅ 手机上的 Access Token 仍然有效（剩余有效期: {remaining_days:.1f} 天）")
    else:
        print("[!] Warning: Could not determine token expiration timestamp.")

    if args.check_only:
        return 0

    print(f"[*] Updating GitHub Secrets for repo: {args.repo}...")
    try:
        update_gh_secret("NINEBOT_ACCESS_TOKEN", access_token, args.repo)
        print("  ✓ Updated secret: NINEBOT_ACCESS_TOKEN")
        update_gh_secret("NINEBOT_REFRESH_TOKEN", refresh_token, args.repo)
        print("  ✓ Updated secret: NINEBOT_REFRESH_TOKEN")
    except Exception as e:
        print(f"❌ Failed to update GitHub Secrets: {e}", file=sys.stderr)
        return 1

    print("🎉 GitHub Secrets 更新成功！")

    if args.trigger:
        print("[*] Triggering GitHub Actions workflow...")
        res = subprocess.run(["gh", "workflow", "run", "Print Ninebot Data", "--repo", args.repo], capture_output=True, text=True)
        if res.returncode == 0:
            print("🚀 已成功触发 GitHub Action 'Print Ninebot Data'！")
            time.sleep(2)
            subprocess.run(["gh", "run", "list", "--repo", args.repo, "--limit", "1"])
        else:
            print(f"⚠️ 触发工作流失败: {res.stderr.strip()}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
