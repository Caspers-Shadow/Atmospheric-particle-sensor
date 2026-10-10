#!/usr/bin/env python3
"""Check a Pi HIL capture using only Python's standard library."""

import argparse
import csv
import math
from dataclasses import fields
from datetime import datetime, timedelta
from pathlib import Path

from readings import SensorReading


def check_capture(path, min_rows=1, max_gap=35, require_sensors=False):
    expected_header = [field.name for field in fields(SensorReading)]
    timestamps = []
    unavailable = {}
    issues = []
    rows = 0
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != expected_header:
            raise ValueError("CSV header does not match the local readings.py schema")
        for line, row in enumerate(reader, start=2):
            rows += 1
            if None in row or any(value is None for value in row.values()):
                issues.append(f"line {line}: wrong number of columns")
                continue
            try:
                timestamp = datetime.fromisoformat(row["timestamp_utc"])
                if timestamp.utcoffset() != timedelta(0):
                    raise ValueError("timestamp must explicitly include UTC offset")
                timestamps.append(timestamp)
                for column in expected_header[1:]:
                    value = float(row[column])
                    missing = not math.isfinite(value)
                    if column.startswith("pm") or column.endswith("_index"):
                        missing |= value < 0
                    elif column.endswith("_raw"):
                        missing |= value <= 0
                    if missing:
                        unavailable[column] = unavailable.get(column, 0) + 1
            except ValueError as error:
                issues.append(f"line {line}: {error}")
    gaps = [(later - earlier).total_seconds()
            for earlier, later in zip(timestamps, timestamps[1:])]
    if rows < min_rows:
        issues.append(f"expected at least {min_rows} rows, found {rows}")
    if any(gap <= 0 for gap in gaps):
        issues.append("timestamps repeat or go backwards; check the Pi clock")
    largest_gap = max(gaps, default=0)
    if largest_gap > max_gap:
        issues.append(f"largest gap {largest_gap:.2f}s exceeds {max_gap:.2f}s")
    if require_sensors and unavailable:
        issues.append("one or more sensor values are unavailable")
    return {"rows": rows, "largest_gap_seconds": largest_gap,
            "unavailable": unavailable, "issues": issues,
            "first_timestamp": timestamps[0].isoformat() if timestamps else None,
            "last_timestamp": timestamps[-1].isoformat() if timestamps else None}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--min-rows", type=int, default=1)
    parser.add_argument("--max-gap", type=float, default=35)
    parser.add_argument("--require-sensors", action="store_true")
    args = parser.parse_args(argv)
    if args.min_rows < 1 or not math.isfinite(args.max_gap) or args.max_gap <= 0:
        parser.error("--min-rows and --max-gap must be positive")
    try:
        report = check_capture(args.input, args.min_rows, args.max_gap, args.require_sensors)
    except (OSError, ValueError) as error:
        print(f"FAIL: {error}")
        return 1
    print(f"Rows: {report['rows']}")
    print(f"UTC range: {report['first_timestamp']} -> {report['last_timestamp']}")
    print(f"Largest gap: {report['largest_gap_seconds']:.2f} seconds")
    print(f"Unavailable values: {report['unavailable'] or 'none'}")
    for issue in report["issues"]:
        print(f"FAIL: {issue}")
    print("PASS" if not report["issues"] else "FAIL")
    return int(bool(report["issues"]))


if __name__ == "__main__":
    raise SystemExit(main())
