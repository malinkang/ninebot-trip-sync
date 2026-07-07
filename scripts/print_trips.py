#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import xml.etree.ElementTree as ET
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--export-dir", default="data/export")
    args = parser.parse_args()
    root = Path(args.export_dir)
    rows = list(csv.DictReader((root / "trips.csv").open(encoding="utf-8")))
    total = sum(float(r["mileage_km"]) for r in rows)
    print(json.dumps({"trip_count": len(rows), "total_mileage_km": round(total, 3)}, ensure_ascii=False, indent=2))
    print("\nTrips:")
    for r in rows:
        print(
            f"- {r['start_time']} -> {r['end_time']} | "
            f"{r['mileage_km']} km | {r['duration_sec']}s | "
            f"max {r['max_speed_kmh']} km/h | points {r['points']}"
        )
        for key in ("gpx_wgs84", "gpx_gcj02", "points_csv", "raw_json"):
            p = root / r[key]
            if not p.exists():
                raise FileNotFoundError(p)
        ET.parse(root / r["gpx_wgs84"])
        ET.parse(root / r["gpx_gcj02"])
    print("\nGPX validation: ok")
    print("CSV rows:")
    print((root / "trips.csv").read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
