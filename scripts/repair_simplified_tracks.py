#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from ninebot_phone_fetch import write_gpx, write_life_footprint_csv, write_points_csv

TZ = timezone(timedelta(hours=8))


Point = tuple[float, float]


@dataclass(frozen=True)
class TripRow:
    csv_path: Path
    export_dir: Path
    row: dict[str, str]
    points: list[Point]

    @property
    def key(self) -> str:
        return f"{self.row.get('start_time')}|{self.row.get('end_time')}|{self.row.get('mileage_km')}"

    @property
    def start_time(self) -> str:
        return self.row.get("start_time", "")

    @property
    def mileage_km(self) -> float:
        try:
            return float(self.row.get("mileage_km") or 0)
        except ValueError:
            return 0.0


@dataclass(frozen=True)
class Match:
    source: TripRow
    template: TripRow
    reverse: bool
    avg_endpoint_m: float
    max_endpoint_m: float
    distance_delta_ratio: float
    score: float


def haversine_m(a: Point, b: Point) -> float:
    lon1, lat1 = a
    lon2, lat2 = b
    radius = 6371000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.atan2(math.sqrt(h), math.sqrt(1 - h))


def load_gpx_points(path: Path) -> list[Point]:
    if not path.exists():
        return []
    root = ET.parse(path).getroot()
    points: list[Point] = []
    for elem in root.iter():
        if elem.tag.endswith("trkpt"):
            lat = elem.attrib.get("lat")
            lon = elem.attrib.get("lon")
            if lat is None or lon is None:
                continue
            points.append((float(lon), float(lat)))
    return points


def write_csv(csv_path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def find_csv_files(export_dir: Path) -> list[Path]:
    if (export_dir / "trips.csv").exists():
        return [export_dir / "trips.csv"]
    return sorted(export_dir.glob("*/trips.csv"))


def load_trips(export_dir: Path) -> tuple[list[TripRow], dict[Path, tuple[list[str], list[dict[str, str]]]]]:
    trips: list[TripRow] = []
    csv_rows: dict[Path, tuple[list[str], list[dict[str, str]]]] = {}
    for csv_path in find_csv_files(export_dir):
        with csv_path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            fieldnames = list(reader.fieldnames or [])
            rows = [dict(row) for row in reader]
        csv_rows[csv_path] = (fieldnames, rows)
        for row in rows:
            name = row.get("gpx_wgs84") or ""
            points = load_gpx_points(csv_path.parent / name) if name else []
            trips.append(TripRow(csv_path=csv_path, export_dir=csv_path.parent, row=row, points=points))
    return trips, csv_rows


def choose_match(
    source: TripRow,
    templates: list[TripRow],
    *,
    max_endpoint_m: float,
    max_avg_endpoint_m: float,
    max_distance_delta_ratio: float,
) -> Match | None:
    if len(source.points) < 2:
        return None
    src_start = source.points[0]
    src_end = source.points[-1]
    best: Match | None = None
    for template in templates:
        if template.key == source.key or len(template.points) < 2:
            continue
        distance_base = max(source.mileage_km, 0.1)
        distance_delta_ratio = abs(template.mileage_km - source.mileage_km) / distance_base
        if distance_delta_ratio > max_distance_delta_ratio:
            continue

        direct_start = haversine_m(src_start, template.points[0])
        direct_end = haversine_m(src_end, template.points[-1])
        reverse_start = haversine_m(src_start, template.points[-1])
        reverse_end = haversine_m(src_end, template.points[0])

        candidates = [
            (False, direct_start, direct_end),
            (True, reverse_start, reverse_end),
        ]
        for reverse, start_m, end_m in candidates:
            avg_endpoint_m = (start_m + end_m) / 2
            max_pair_m = max(start_m, end_m)
            if max_pair_m > max_endpoint_m or avg_endpoint_m > max_avg_endpoint_m:
                continue
            score = avg_endpoint_m + distance_delta_ratio * 250
            match = Match(
                source=source,
                template=template,
                reverse=reverse,
                avg_endpoint_m=avg_endpoint_m,
                max_endpoint_m=max_pair_m,
                distance_delta_ratio=distance_delta_ratio,
                score=score,
            )
            if best is None or match.score < best.score:
                best = match
    return best


def parse_ts(value: str) -> int:
    return int(datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ).timestamp())


def repair_trip(match: Match) -> int:
    source = match.source
    row = source.row
    template_points = list(reversed(match.template.points)) if match.reverse else list(match.template.points)
    repaired_points = [source.points[0], *template_points[1:-1], source.points[-1]]
    point_rows = [(lon, lat, None, None) for lon, lat in repaired_points]
    start_ts = parse_ts(row["start_time"])
    end_ts = parse_ts(row["end_time"])

    if row.get("gpx_wgs84"):
        write_gpx(source.export_dir / row["gpx_wgs84"], f"Ninebot inferred {row['start_time']} {row.get('mileage_km')}km", point_rows, start_ts, end_ts, "wgs84")
    if row.get("gpx_gcj02"):
        write_gpx(source.export_dir / row["gpx_gcj02"], f"Ninebot inferred {row['start_time']} {row.get('mileage_km')}km GCJ02", point_rows, start_ts, end_ts, "gcj02")
    if row.get("points_wgs84_csv"):
        write_points_csv(source.export_dir / row["points_wgs84_csv"], point_rows, "wgs84")
    if row.get("points_csv"):
        write_points_csv(source.export_dir / row["points_csv"], point_rows, "gcj02")
    if row.get("life_footprint_wgs84_csv"):
        write_life_footprint_csv(source.export_dir / row["life_footprint_wgs84_csv"], point_rows, start_ts, end_ts, "wgs84")
    if row.get("life_footprint_gcj02_csv"):
        write_life_footprint_csv(source.export_dir / row["life_footprint_gcj02_csv"], point_rows, start_ts, end_ts, "gcj02")

    row["points"] = str(len(repaired_points))
    row["inferred_from_start_time"] = match.template.start_time
    row["inferred_track"] = "1"
    return len(repaired_points)


def ensure_fields(csv_rows: dict[Path, tuple[list[str], list[dict[str, str]]]]) -> None:
    extra = ["inferred_track", "inferred_from_start_time"]
    for csv_path, (fieldnames, rows) in csv_rows.items():
        for field in extra:
            if field not in fieldnames:
                fieldnames.append(field)
        for row in rows:
            row.setdefault("inferred_track", "")
            row.setdefault("inferred_from_start_time", "")


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Infer old simplified Ninebot tracks from repeated full routes.")
    parser.add_argument("--export-dir", default="data/cloud-export")
    parser.add_argument("--apply", action="store_true", help="write repaired GPX/CSV files; otherwise only report")
    parser.add_argument("--simple-max-points", type=int, default=2)
    parser.add_argument("--min-template-points", type=int, default=8)
    parser.add_argument("--max-endpoint-m", type=float, default=450)
    parser.add_argument("--max-avg-endpoint-m", type=float, default=250)
    parser.add_argument("--max-distance-delta-ratio", type=float, default=0.45)
    parser.add_argument("--report", default="repair-report.json")
    args = parser.parse_args(list(argv) if argv is not None else None)

    export_dir = Path(args.export_dir)
    trips, csv_rows = load_trips(export_dir)
    simplified = [trip for trip in trips if 0 < len(trip.points) <= args.simple_max_points]
    templates = [trip for trip in trips if len(trip.points) >= args.min_template_points]

    matches: list[Match] = []
    for source in simplified:
        match = choose_match(
            source,
            templates,
            max_endpoint_m=args.max_endpoint_m,
            max_avg_endpoint_m=args.max_avg_endpoint_m,
            max_distance_delta_ratio=args.max_distance_delta_ratio,
        )
        if match:
            matches.append(match)

    repaired = 0
    if args.apply:
        ensure_fields(csv_rows)
        for match in matches:
            repair_trip(match)
            repaired += 1
        for csv_path, (fieldnames, rows) in csv_rows.items():
            write_csv(csv_path, rows, fieldnames)

    report = {
        "export_dir": str(export_dir),
        "apply": args.apply,
        "trips": len(trips),
        "simplified": len(simplified),
        "templates": len(templates),
        "matched": len(matches),
        "repaired": repaired,
        "unmatched": len(simplified) - len(matches),
        "thresholds": {
            "simple_max_points": args.simple_max_points,
            "min_template_points": args.min_template_points,
            "max_endpoint_m": args.max_endpoint_m,
            "max_avg_endpoint_m": args.max_avg_endpoint_m,
            "max_distance_delta_ratio": args.max_distance_delta_ratio,
        },
        "matches": [
            {
                "source_start_time": m.source.start_time,
                "source_mileage_km": m.source.mileage_km,
                "template_start_time": m.template.start_time,
                "template_mileage_km": m.template.mileage_km,
                "template_points": len(m.template.points),
                "reverse": m.reverse,
                "avg_endpoint_m": round(m.avg_endpoint_m, 1),
                "max_endpoint_m": round(m.max_endpoint_m, 1),
                "distance_delta_ratio": round(m.distance_delta_ratio, 3),
                "score": round(m.score, 1),
            }
            for m in matches
        ],
    }
    report_path = export_dir / args.report
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ["trips", "simplified", "templates", "matched", "repaired", "unmatched"]}, ensure_ascii=False, indent=2))
    print(f"report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
