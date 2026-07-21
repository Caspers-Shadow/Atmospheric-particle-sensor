#!/usr/bin/env python3
"""
analyse.py
==========

Atmospheric Monitoring System - Data Analysis Layer.

Reads ThingSpeak CSV exports (or the local thingspeak_data.csv produced
by readings.py), and produces the analysis outputs required by the PRD:

    1. Import ThingSpeak CSV exports.
    2. Convert UTC timestamps to Africa/Johannesburg.
    3. Produce summary statistics (count, mean, min, max).
    4. Generate cleaned datasets.
    5. Extract experiments by timestamp.
    6. Produce gas trend reports.

Generated files:
    - cleaned_thingspeak_data.csv
    - experiment.csv           (only when --start/--end is supplied)
    - gas_report.csv

Usage:
    python analyse.py --input thingspeak_data.csv
    python analyse.py --input thingspeak_data.csv --start "2026-07-01 08:00:00" --end "2026-07-01 10:00:00"
"""

import argparse
import sys

import pandas as pd

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9 fallback
    from backports.zoneinfo import ZoneInfo  # type: ignore

JOHANNESBURG_TZ = ZoneInfo("Africa/Johannesburg")

CLEANED_OUTPUT = "cleaned_thingspeak_data.csv"
EXPERIMENT_OUTPUT = "experiment.csv"
GAS_REPORT_OUTPUT = "gas_report.csv"

# Columns expected from either a ThingSpeak channel export or the local
# thingspeak_data.csv written by readings.py. ThingSpeak exports use
# "created_at" plus field1..field8; the local CSV uses named columns and
# also carries raw gas resistance + the NH3 index directly.
THINGSPEAK_FIELD_MAP = {
    "field1": "temperature_c",
    "field2": "humidity_pct",
    "field3": "pressure_hpa",
    "field4": "pm1_0",
    "field5": "pm2_5",
    "field6": "pm10",
    "field7": "oxidising_index",
    "field8": "reducing_index",
}

python
# Replace your existing load_dataset(), STAT_COLUMNS, and
# build_gas_trend_report() sections with the following.

STAT_COLUMNS = [
    "temperature_c",
    "humidity_pct",
    "pressure_hpa",
    "pm1_0",
    "pm2_5",
    "pm10",
    "oxidising_index",
    "reducing_index",
    "nh3_index",
    "co_index",
    "no2_index",
    "light"
]


def load_dataset(path: str) -> pd.DataFrame:
    """
    Import a ThingSpeak CSV export or local readings.py CSV.

    Supports:
    - Old schema (10 columns)
    - New schema (14 columns)
    - ThingSpeak exports
    """

    try:
        df = pd.read_csv(
            path,
            on_bad_lines="skip"
        )

    except Exception as e:
        raise ValueError(f"Failed to read CSV: {e}")

    # ThingSpeak export
    if "created_at" in df.columns:

        if "field1" in df.columns:

            df = df.rename(
                columns={
                    "created_at": "timestamp_utc",
                    "field1": "temperature_c",
                    "field2": "humidity_pct",
                    "field3": "pressure_hpa",
                    "field4": "pm1_0",
                    "field5": "pm2_5",
                    "field6": "pm10",
                    "field7": "oxidising_index",
                    "field8": "reducing_index"
                }
            )

    # Old local dataset
    elif len(df.columns) == 10:

        df.columns = [
            "timestamp_utc",
            "entry_id",
            "temperature_c",
            "humidity_pct",
            "pressure_hpa",
            "pm1_0",
            "pm2_5",
            "pm10",
            "nh3_index",
            "light"
        ]

    # New local dataset
    elif len(df.columns) == 14:

        df.columns = [
            "timestamp_utc",
            "temperature_c",
            "humidity_pct",
            "pressure_hpa",
            "oxidising_index",
            "pm1_0",
            "pm2_5",
            "pm10",
            "reducing_index",
            "nh3_raw",
            "nh3_index",
            "co_index",
            "no2_index",
            "light"
        ]

    else:

        print("\nDetected columns:")
        print(df.columns)

        raise ValueError(
            f"Unsupported dataset format ({len(df.columns)} columns)."
        )

    return df


def build_gas_trend_report(df: pd.DataFrame) -> pd.DataFrame:
    """
    Produce gas trend reports for all available gases.
    """

    report_cols = ["timestamp_sast"]

    for col in [
        "oxidising_index",
        "reducing_index",
        "nh3_index",
        "co_index",
        "no2_index"
    ]:

        if col in df.columns:
            report_cols.append(col)

    report = df[report_cols].copy()

    window = min(5, max(1, len(report)))

    for col in report.columns:

        if col == "timestamp_sast":
            continue

        rolling_mean = report[col].rolling(
            window=window,
            min_periods=1
        ).mean()

        delta = report[col] - rolling_mean

        report[f"{col}_trend"] = delta.apply(
            lambda d:
                "rising"
                if d > 2 else
                ("falling"
                 if d < -2 else
                 "stable")
        )

    return report


def convert_timestamps(df: pd.DataFrame) -> pd.DataFrame:
    """Convert UTC timestamps to Africa/Johannesburg local time."""
    df = df.copy()
    df["timestamp_utc"] = pd.to_datetime(df["timestamp_utc"], utc=True, errors="coerce")
    df["timestamp_sast"] = df["timestamp_utc"].dt.tz_convert(JOHANNESBURG_TZ)
    return df


def clean_dataset(df: pd.DataFrame) -> pd.DataFrame:
    """
    Produce a cleaned dataset:
      - drop rows with an unparseable timestamp
      - coerce measurement columns to numeric, invalid parses -> NaN
      - drop exact duplicate rows
      - sort chronologically
    """
    df = df.dropna(subset=["timestamp_utc"]).copy()

    for col in STAT_COLUMNS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
            if col in ("pm1_0", "pm2_5", "pm10"):
                # -1 is readings.py's sentinel for "PMS5003 unavailable this
                # cycle" - treat it as missing, not a real zero-adjacent value.
                df.loc[df[col] == -1, col] = pd.NA

    df = df.drop_duplicates()
    df = df.sort_values("timestamp_utc").reset_index(drop=True)
    return df


def compute_summary_statistics(df: pd.DataFrame) -> pd.DataFrame:
    """Count, mean, min, max for each required measurement column."""
    rows = []
    for col in STAT_COLUMNS:
        if col not in df.columns:
            continue
        series = df[col].dropna()
        rows.append({
            "measurement": col,
            "count": series.count(),
            "mean": round(series.mean(), 3) if not series.empty else None,
            "min": series.min() if not series.empty else None,
            "max": series.max() if not series.empty else None,
        })
    return pd.DataFrame(rows)


def extract_experiment(df: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    """
    Extract a sub-experiment by SAST timestamp range (inclusive).
    start / end are expected as "YYYY-MM-DD HH:MM:SS" in Africa/Johannesburg
    local time.
    """
    start_ts = pd.Timestamp(start, tz=JOHANNESBURG_TZ)
    end_ts = pd.Timestamp(end, tz=JOHANNESBURG_TZ)
    mask = (df["timestamp_sast"] >= start_ts) & (df["timestamp_sast"] <= end_ts)
    return df.loc[mask].reset_index(drop=True)


def print_summary(summary_df: pd.DataFrame):
    print("===== SUMMARY STATISTICS =====\n")
    if summary_df.empty:
        print("No measurement columns found in dataset.\n")
    else:
        print(summary_df.to_string(index=False))
    print("\n===============================\n")


def main():
    parser = argparse.ArgumentParser(
        description="Analyse ThingSpeak atmospheric monitoring CSV exports."
    )
    parser.add_argument(
        "--input", "-i", default="thingspeak_data.csv",
        help="Path to the ThingSpeak export or local readings.py CSV (default: thingspeak_data.csv)"
    )
    parser.add_argument(
        "--start", help="Experiment window start, SAST, e.g. '2026-07-01 08:00:00'"
    )
    parser.add_argument(
        "--end", help="Experiment window end, SAST, e.g. '2026-07-01 10:00:00'"
    )
    args = parser.parse_args()

    try:
        df = load_dataset(args.input)
    except FileNotFoundError:
        print(f"ERROR: input file not found: {args.input}")
        sys.exit(1)
    except ValueError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    df = convert_timestamps(df)
    cleaned = clean_dataset(df)
    cleaned.to_csv(CLEANED_OUTPUT, index=False)
    print(f"Cleaned dataset written to {CLEANED_OUTPUT} ({len(cleaned)} rows)")

    summary = compute_summary_statistics(cleaned)
    print_summary(summary)

    gas_report = build_gas_trend_report(cleaned)
    gas_report.to_csv(GAS_REPORT_OUTPUT, index=False)
    print(f"Gas trend report written to {GAS_REPORT_OUTPUT} ({len(gas_report)} rows)")

    if args.start and args.end:
        experiment = extract_experiment(cleaned, args.start, args.end)
        experiment.to_csv(EXPERIMENT_OUTPUT, index=False)
        print(f"Experiment extract written to {EXPERIMENT_OUTPUT} "
              f"({len(experiment)} rows, {args.start} -> {args.end} SAST)")
    else:
        print("No --start/--end supplied; skipping experiment.csv extraction.")


if __name__ == "__main__":
    main()
