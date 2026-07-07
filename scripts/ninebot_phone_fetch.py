#!/usr/bin/env python3
"""Fetch Ninebot trip details from the installed Android app via ADB.

Why this exists:
- Ninebot trip endpoints are known, but request bodies are signed/encrypted by the
  app's native tokenRequest bridge.
- This local script lets the real app perform the request, then extracts the
  decrypted travel-info JSON from logcat/heap and exports GPX/CSV/JSON.

Prerequisites:
- Android phone connected over adb.
- Ninebot app installed and logged in.
- Root is recommended for reliable heap dumping.
- Reqable/VPN should be stopped because it may break the RN Track bundle.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Iterable
from xml.sax.saxutils import escape

TZ = timezone(timedelta(hours=8))
NINEBOT_PACKAGE = "cn.ninebot.ninebot"
REQABLE_PACKAGE = "com.reqable.android"
TRACK_TITLE = "轨迹记录"


@dataclass
class UINode:
    text: str
    bounds: tuple[int, int, int, int]

    @property
    def cx(self) -> int:
        return (self.bounds[0] + self.bounds[2]) // 2

    @property
    def cy(self) -> int:
        return (self.bounds[1] + self.bounds[3]) // 2


@dataclass
class TripRow:
    date_label: str
    time_label: str
    mileage_label: str
    energy_label: str
    tap_x: int
    tap_y: int

    @property
    def key(self) -> str:
        return f"{self.date_label} {self.time_label} {self.mileage_label}"


class ADB:
    def __init__(self, serial: str | None = None) -> None:
        self.serial = serial

    def cmd(self, *args: str, check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        base = ["adb"]
        if self.serial:
            base += ["-s", self.serial]
        proc = subprocess.run(base + list(args), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        if check and proc.returncode != 0:
            raise RuntimeError(f"adb {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
        return proc

    def shell(self, command: str, check: bool = True, timeout: int = 120) -> str:
        return self.cmd("shell", command, check=check, timeout=timeout).stdout

    def tap(self, x: int, y: int) -> None:
        self.shell(f"input tap {x} {y}")

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 600) -> None:
        self.shell(f"input swipe {x1} {y1} {x2} {y2} {duration_ms}")

    def pull(self, remote: str, local: Path) -> None:
        local.parent.mkdir(parents=True, exist_ok=True)
        self.cmd("pull", remote, str(local))

    def dump_ui(self, local_path: Path) -> list[UINode]:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        self.shell("uiautomator dump /sdcard/window.xml >/dev/null 2>&1", check=False)
        self.pull("/sdcard/window.xml", local_path)
        return parse_ui(local_path)


def parse_bounds(raw: str) -> tuple[int, int, int, int]:
    nums = [int(x) for x in re.findall(r"\d+", raw)]
    if len(nums) != 4:
        raise ValueError(f"bad bounds: {raw}")
    return nums[0], nums[1], nums[2], nums[3]


def parse_ui(path: Path) -> list[UINode]:
    root = ET.parse(path).getroot()
    nodes: list[UINode] = []
    for elem in root.iter("node"):
        text = html.unescape(elem.attrib.get("text", "")).strip()
        if not text:
            continue
        bounds = elem.attrib.get("bounds")
        if not bounds:
            continue
        nodes.append(UINode(text=text, bounds=parse_bounds(bounds)))
    return nodes


def find_text(nodes: list[UINode], text: str) -> UINode | None:
    for node in nodes:
        if node.text == text:
            return node
    return None


def require_text(nodes: list[UINode], text: str) -> UINode:
    node = find_text(nodes, text)
    if node is None:
        raise RuntimeError(f"Cannot find UI text: {text}")
    return node


def open_track_page(adb: ADB, work_dir: Path) -> list[UINode]:
    adb.shell(f"am force-stop {REQABLE_PACKAGE}", check=False)
    adb.shell(f"am force-stop {NINEBOT_PACKAGE}", check=False)
    adb.shell(f"monkey -p {NINEBOT_PACKAGE} -c android.intent.category.LAUNCHER 1 >/dev/null", check=False)
    time.sleep(8)
    nodes = adb.dump_ui(work_dir / "ui_home.xml")
    if find_text(nodes, TRACK_TITLE):
        return nodes
    node = find_text(nodes, "今日里程")
    if not node:
        raise RuntimeError("Ninebot home loaded, but cannot find 今日里程. Is the app logged in and on the device page?")
    # The text itself may not be clickable; tap the metric card center to the right.
    adb.tap(max(node.cx, 760), node.cy + 120)
    time.sleep(6)
    nodes = adb.dump_ui(work_dir / "ui_track.xml")
    require_text(nodes, TRACK_TITLE)
    return nodes


def select_month(adb: ADB, nodes: list[UINode], month: str, work_dir: Path) -> list[UINode]:
    label = month if month.endswith("月") else f"{int(month):02d}月"
    node = find_text(nodes, label)
    if not node:
        raise RuntimeError(f"Month tab {label} is not visible. Swipe month tabs manually or use a visible month.")
    adb.tap(node.cx, node.cy)
    time.sleep(3)
    return adb.dump_ui(work_dir / f"ui_month_{label}.xml")


def parse_visible_rows(nodes: list[UINode]) -> list[TripRow]:
    rows: list[TripRow] = []
    date_nodes = [n for n in nodes if re.fullmatch(r"\d{2}月\d{2}日", n.text)]
    time_nodes = [n for n in nodes if re.fullmatch(r"\d{2}:\d{2}", n.text)]
    for time_node in time_nodes:
        previous_dates = [d for d in date_nodes if d.cy <= time_node.cy]
        if not previous_dates:
            continue
        date_node = max(previous_dates, key=lambda n: n.cy)
        nearby = [n for n in nodes if abs(n.cy - time_node.cy) <= 75 and n.cx > time_node.cx]
        mileage = next((n.text for n in nearby if re.fullmatch(r"\d+(?:\.\d+)?km", n.text)), "")
        energy_nodes = [n for n in nodes if 0 <= n.cy - time_node.cy <= 115 and "耗电量" in n.text]
        energy = energy_nodes[0].text if energy_nodes else ""
        if mileage:
            rows.append(TripRow(date_node.text, time_node.text, mileage, energy, 540, time_node.cy))
    # De-dupe while preserving screen order.
    out: list[TripRow] = []
    seen: set[str] = set()
    for row in sorted(rows, key=lambda r: r.tap_y):
        if row.key not in seen:
            seen.add(row.key)
            out.append(row)
    return out


def out_of_china(lng: float, lat: float) -> bool:
    return not (72.004 <= lng <= 137.8347 and 0.8293 <= lat <= 55.8271)


def transformlat(lng: float, lat: float) -> float:
    ret = -100.0 + 2.0 * lng + 3.0 * lat + 0.2 * lat * lat + 0.1 * lng * lat + 0.2 * math.sqrt(abs(lng))
    ret += (20.0 * math.sin(6.0 * lng * math.pi) + 20.0 * math.sin(2.0 * lng * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(lat * math.pi) + 40.0 * math.sin(lat / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(lat / 12.0 * math.pi) + 320 * math.sin(lat * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def transformlng(lng: float, lat: float) -> float:
    ret = 300.0 + lng + 2.0 * lat + 0.1 * lng * lng + 0.1 * lng * lat + 0.1 * math.sqrt(abs(lng))
    ret += (20.0 * math.sin(6.0 * lng * math.pi) + 20.0 * math.sin(2.0 * lng * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(lng * math.pi) + 40.0 * math.sin(lng / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(lng / 12.0 * math.pi) + 300.0 * math.sin(lng / 30.0 * math.pi)) * 2.0 / 3.0
    return ret


def wgs84_to_gcj02(lng: float, lat: float) -> tuple[float, float]:
    if out_of_china(lng, lat):
        return lng, lat
    a = 6378245.0
    ee = 0.00669342162296594323
    dlat = transformlat(lng - 105.0, lat - 35.0)
    dlng = transformlng(lng - 105.0, lat - 35.0)
    radlat = lat / 180.0 * math.pi
    magic = math.sin(radlat)
    magic = 1 - ee * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((a * (1 - ee)) / (magic * sqrtmagic) * math.pi)
    dlng = (dlng * 180.0) / (a / sqrtmagic * math.cos(radlat) * math.pi)
    return lng + dlng, lat + dlat


def gcj02_to_wgs84(lng: float, lat: float) -> tuple[float, float]:
    if out_of_china(lng, lat):
        return lng, lat
    gcj_lng, gcj_lat = wgs84_to_gcj02(lng, lat)
    dlng = gcj_lng - lng
    dlat = gcj_lat - lat
    return lng * 2 - (lng + dlng), lat * 2 - (lat + dlat)


def dt(ts: int) -> datetime:
    return datetime.fromtimestamp(int(ts), TZ)


def haversine_m(lng1: float, lat1: float, lng2: float, lat2: float) -> float:
    radius = 6371008.8
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def bearing_deg(lng1: float, lat1: float, lng2: float, lat2: float) -> float:
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dl = math.radians(lng2 - lng1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def parse_points(trail: str) -> list[tuple[float, float, float | None, float | None]]:
    points = []
    for part in trail.strip().split(";"):
        vals = part.split(",")
        if len(vals) < 2:
            continue
        lng, lat = float(vals[0]), float(vals[1])
        speed = float(vals[2]) if len(vals) > 2 and vals[2] else None
        alt = float(vals[3]) if len(vals) > 3 and vals[3] else None
        points.append((lng, lat, speed, alt))
    return points


def write_points_csv(path: Path, points: list[tuple[float, float, float | None, float | None]], coord: str) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([f"lng_{coord}", f"lat_{coord}", "speed_kmh", "altitude_m"])
        for lng, lat, speed, alt in points:
            out_lng, out_lat = wgs84_to_gcj02(lng, lat) if coord == "gcj02" else (lng, lat)
            writer.writerow([out_lng, out_lat, speed, alt])


def write_life_footprint_csv(
    path: Path,
    points: list[tuple[float, float, float | None, float | None]],
    start_ts: int,
    end_ts: int,
    coord: str,
) -> None:
    converted = [(*wgs84_to_gcj02(lng, lat), speed, alt) if coord == "gcj02" else (lng, lat, speed, alt) for lng, lat, speed, alt in points]
    cumulative = 0.0
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["dataTime", "locType", "longitude", "latitude", "heading", "accuracy", "speed", "distance", "isBackForeground", "stepType", "altitude"])
        for idx, (lng, lat, speed, alt) in enumerate(converted):
            if idx > 0:
                prev_lng, prev_lat = converted[idx - 1][0], converted[idx - 1][1]
                cumulative += haversine_m(prev_lng, prev_lat, lng, lat)
                heading = bearing_deg(prev_lng, prev_lat, lng, lat)
            else:
                heading = 0.0
            ts = int(start_ts + (end_ts - start_ts) * idx / max(len(converted) - 1, 1))
            writer.writerow([ts, 1, f"{lng:.6f}", f"{lat:.6f}", f"{heading:.6f}", "0.000000", speed or 0, f"{cumulative:.6f}", 1, 0, alt or 0])


def write_gpx(path: Path, name: str, points: list[tuple[float, float, float | None, float | None]], start_ts: int, end_ts: int, coord: str) -> None:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="ninebot-phone-fetch" xmlns="http://www.topografix.com/GPX/1/1">',
        f"  <trk><name>{escape(name)}</name><trkseg>",
    ]
    for idx, (lng, lat, speed, alt) in enumerate(points):
        out_lng, out_lat = wgs84_to_gcj02(lng, lat) if coord == "gcj02" else (lng, lat)
        ts = int(start_ts + (end_ts - start_ts) * idx / max(len(points) - 1, 1))
        lines.append(f'    <trkpt lat="{out_lat:.8f}" lon="{out_lng:.8f}">')
        if alt is not None:
            lines.append(f"      <ele>{alt:.1f}</ele>")
        lines.append(f"      <time>{dt(ts).astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')}</time>")
        if speed is not None:
            lines.append(f"      <extensions><speed_kmh>{speed:.1f}</speed_kmh></extensions>")
        lines.append("    </trkpt>")
    lines += ["  </trkseg></trk>", "</gpx>"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def balanced_objects(text: str, marker: str) -> list[str]:
    starts = [m.start() for m in re.finditer(re.escape(marker), text)]
    out: list[str] = []
    for st in starts:
        obj_start = text.rfind("{", 0, st)
        if obj_start < 0:
            continue
        depth = 0
        in_str = False
        esc_ch = False
        for i in range(obj_start, min(len(text), obj_start + 100_000)):
            c = text[i]
            if in_str:
                if esc_ch:
                    esc_ch = False
                elif c == "\\":
                    esc_ch = True
                elif c == '"':
                    in_str = False
            else:
                if c == '"':
                    in_str = True
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        out.append(text[obj_start : i + 1])
                        break
    return out


def extract_detail_objects(paths: Iterable[Path]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in paths:
        if not path.exists():
            continue
        raw = path.read_bytes().decode("utf-8", errors="ignore")
        variants = [raw, raw.replace('\\"', '"').replace("\\/", "/")]
        for text in variants:
            candidates = balanced_objects(text, '"data":{"img"')
            candidates += balanced_objects(text, '"img":"https://steeldust-image-cdn.ninebot.com/image/thumb/trackthumbnail.png"')
            for obj in candidates:
                if obj in seen:
                    continue
                seen.add(obj)
                try:
                    parsed = json.loads(obj)
                except Exception:
                    continue
                if parsed.get("trail"):
                    parsed = {"data": parsed, "t": None}
                data = parsed.get("data") if isinstance(parsed, dict) else None
                if isinstance(data, dict) and data.get("trail") and data.get("start_time"):
                    items.append(parsed)
    return items


def match_detail(items: list[dict[str, Any]], row: TripRow, year: int) -> dict[str, Any] | None:
    month, day = [int(x) for x in re.findall(r"\d+", row.date_label)]
    wanted_prefix = f"{year:04d}-{month:02d}-{day:02d} {row.time_label}"
    wanted_mileage = row.mileage_label.removesuffix("km")
    candidates = []
    for item in items:
        data = item["data"]
        start = dt(int(data["start_time"])).strftime("%Y-%m-%d %H:%M")
        if start == wanted_prefix and str(data.get("mileages")) == wanted_mileage:
            candidates.append(item)
    if candidates:
        return max(candidates, key=lambda x: len(x["data"].get("trail", "")))
    for item in items:
        data = item["data"]
        start = dt(int(data["start_time"])).strftime("%H:%M")
        if start == row.time_label and str(data.get("mileages")) == wanted_mileage:
            candidates.append(item)
    return max(candidates, key=lambda x: len(x["data"].get("trail", ""))) if candidates else None


def export_detail(item: dict[str, Any], export_dir: Path) -> dict[str, Any]:
    data = item["data"]
    start_ts, end_ts = int(data["start_time"]), int(data["end_time"])
    start = dt(start_ts)
    label = start.strftime("%Y-%m-%d_%H%M")
    mileage = str(data.get("mileages", "unknown"))
    base = f"trip_{label}_{mileage.replace('.', 'p')}km"
    points = parse_points(data["trail"])
    export_dir.mkdir(parents=True, exist_ok=True)
    raw_json = export_dir / f"{base}.json"
    raw_json.write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")
    points_csv = export_dir / f"{base}_points_gcj02.csv"
    points_wgs84_csv = export_dir / f"{base}_points_wgs84.csv"
    life_footprint_gcj02_csv = export_dir / f"{base}_life_footprint_gcj02.csv"
    life_footprint_wgs84_csv = export_dir / f"{base}_life_footprint_wgs84.csv"
    write_points_csv(points_csv, points, "gcj02")
    write_points_csv(points_wgs84_csv, points, "wgs84")
    write_life_footprint_csv(life_footprint_gcj02_csv, points, start_ts, end_ts, "gcj02")
    write_life_footprint_csv(life_footprint_wgs84_csv, points, start_ts, end_ts, "wgs84")
    gpx_wgs84 = export_dir / f"{base}_wgs84.gpx"
    gpx_gcj02 = export_dir / f"{base}_gcj02.gpx"
    write_gpx(gpx_wgs84, f"Ninebot {label} {mileage}km", points, start_ts, end_ts, "wgs84")
    write_gpx(gpx_gcj02, f"Ninebot {label} {mileage}km GCJ02", points, start_ts, end_ts, "gcj02")
    return {
        "date": start.strftime("%Y-%m-%d"),
        "start_time": start.strftime("%Y-%m-%d %H:%M:%S"),
        "end_time": dt(end_ts).strftime("%Y-%m-%d %H:%M:%S"),
        "duration_sec": int(data.get("duration") or (end_ts - start_ts)),
        "mileage_km": data.get("mileages"),
        "max_speed_kmh": data.get("speed"),
        "energy_percent": data.get("used_electricity"),
        "energy_wh": data.get("ec"),
        "points": len(points),
        "raw_json": raw_json.name,
        "gpx_wgs84": gpx_wgs84.name,
        "gpx_gcj02": gpx_gcj02.name,
        "points_csv": points_csv.name,
        "points_wgs84_csv": points_wgs84_csv.name,
        "life_footprint_gcj02_csv": life_footprint_gcj02_csv.name,
        "life_footprint_wgs84_csv": life_footprint_wgs84_csv.name,
    }


def write_trips_csv(export_dir: Path) -> None:
    rows: dict[tuple[str, str, str], dict[str, Any]] = {}
    for json_path in export_dir.glob("trip_*.json"):
        try:
            item = json.loads(json_path.read_text(encoding="utf-8"))
            row = export_detail(item, export_dir)
        except Exception as exc:
            print(f"warn: skip {json_path}: {exc}", file=sys.stderr)
            continue
        rows[(row["start_time"], row["end_time"], str(row["mileage_km"]))] = row
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
        "points_wgs84_csv",
        "life_footprint_gcj02_csv",
        "life_footprint_wgs84_csv",
    ]
    with (export_dir / "trips.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in sorted(rows.values(), key=lambda r: r["start_time"]):
            writer.writerow(row)


def process_row(adb: ADB, row: TripRow, year: int, export_dir: Path, work_dir: Path, keep_debug: bool) -> dict[str, Any] | None:
    safe_key = re.sub(r"[^0-9A-Za-z_-]+", "_", f"{row.date_label}_{row.time_label}_{row.mileage_label}")
    adb.shell("logcat -c", check=False)
    adb.tap(row.tap_x, row.tap_y)
    time.sleep(8)
    log_path = work_dir / f"logcat_{safe_key}.txt"
    log_path.write_text(adb.cmd("logcat", "-d", check=False, timeout=60).stdout, encoding="utf-8", errors="ignore")
    hprof_remote = f"/data/local/tmp/ninebot-{safe_key}.hprof"
    hprof_path = work_dir / f"{safe_key}.hprof"
    adb.shell(f"rm -f {hprof_remote}", check=False)
    adb.shell(f"am dumpheap {NINEBOT_PACKAGE} {hprof_remote}", check=False, timeout=30)
    time.sleep(5)
    adb.pull(hprof_remote, hprof_path)
    items = extract_detail_objects([log_path, hprof_path])
    matched = match_detail(items, row, year)
    if not matched:
        print(f"warn: no detail JSON matched {row.key}", file=sys.stderr)
        return None
    exported = export_detail(matched, export_dir)
    print(f"exported {exported['start_time']} {exported['mileage_km']}km points={exported['points']}")
    if not keep_debug:
        hprof_path.unlink(missing_ok=True)
    adb.shell("input keyevent KEYCODE_BACK", check=False)
    time.sleep(3)
    return exported


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch Ninebot trips from the Android app via ADB")
    parser.add_argument("--serial", default=os.getenv("ADB_SERIAL"))
    parser.add_argument("--month", default=os.getenv("NINEBOT_MONTH", datetime.now(TZ).strftime("%Y-%m")), help="Month like 2026-07, 07, or 07月")
    parser.add_argument("--year", type=int, default=datetime.now(TZ).year)
    parser.add_argument("--export-dir", default=os.getenv("NINEBOT_EXPORT_DIR", "data/export"))
    parser.add_argument("--work-dir", default=".cache/ninebot-phone-fetch")
    parser.add_argument("--max-scrolls", type=int, default=8)
    parser.add_argument("--max-trips", type=int, default=0, help="0 means no limit")
    parser.add_argument("--list-only", action="store_true")
    parser.add_argument("--keep-debug", action="store_true", help="Keep pulled hprof files for debugging; they may contain secrets")
    args = parser.parse_args(list(argv) if argv is not None else None)

    month_match = re.search(r"(\d{1,2})(?:月)?$", args.month)
    if not month_match:
        raise SystemExit(f"bad month: {args.month}")
    month_label = f"{int(month_match.group(1)):02d}月"
    year = int(args.month[:4]) if re.match(r"\d{4}-", args.month) else args.year

    adb = ADB(args.serial)
    export_dir = Path(args.export_dir)
    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    nodes = open_track_page(adb, work_dir)
    nodes = select_month(adb, nodes, month_label, work_dir)

    processed: set[str] = set()
    listed: dict[str, TripRow] = {}
    exported_count = 0
    stale_scrolls = 0
    for scroll_idx in range(args.max_scrolls + 1):
        nodes = adb.dump_ui(work_dir / f"ui_scroll_{scroll_idx}.xml")
        rows = parse_visible_rows(nodes)
        new_rows = [row for row in rows if row.key not in processed]
        for row in new_rows:
            listed[row.key] = row
        if args.list_only:
            print(f"screen {scroll_idx}: {len(rows)} rows")
            for row in rows:
                print(f"- {row.key} | {row.energy_label} | tap=({row.tap_x},{row.tap_y})")
        else:
            for row in new_rows:
                processed.add(row.key)
                process_row(adb, row, year, export_dir, work_dir, args.keep_debug)
                exported_count += 1
                if args.max_trips and exported_count >= args.max_trips:
                    write_trips_csv(export_dir)
                    return 0
                # Refresh after returning from detail.
                nodes = adb.dump_ui(work_dir / f"ui_after_{exported_count}.xml")
        before_count = len(listed)
        adb.swipe(540, 2050, 540, 1050)
        time.sleep(2)
        after_nodes = adb.dump_ui(work_dir / f"ui_after_scroll_{scroll_idx}.xml")
        for row in parse_visible_rows(after_nodes):
            listed[row.key] = row
        if len(listed) == before_count:
            stale_scrolls += 1
        else:
            stale_scrolls = 0
        if stale_scrolls >= 2:
            break

    if args.list_only:
        print(f"total listed rows: {len(listed)}")
    else:
        write_trips_csv(export_dir)
        print(f"done: exported/updated {exported_count} trips; trips.csv refreshed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
