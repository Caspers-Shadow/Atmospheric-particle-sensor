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

# Replace your existing load_dataset(), STAT_COLUMNS, and
# build_gas_trend_report() sections with the following.

STAT_COLUMNS = [
    "temperature_c",
    "humidity_pct",
    "pressure_hpa",
    "light_lux",
    "pm1_0",
    "pm2_5",
    "pm10",
    "oxidising_index",
    "reducing_index",
    "nh3_index",
    "co_index",
    "no2_index"
]


def load_dataset(path: str):

    import os

    print("\nReading file:")
    print(os.path.abspath(path))

    df = pd.read_csv(path, on_bad_lines="skip")
    
    print(df.iloc[95:105])
    
    # Remove duplicated headers embedded in the CSV
    df = df[
        df["created_at"] != "created_at"
    ]

    print(f"\nRows found: {len(df)}")

    print("\nColumns found:")
    print(df.columns)

    print("\nLast 5 rows:")
    print(df.tail())

    # Remove empty columns
    df = df.loc[:, ~df.columns.str.contains("^Unnamed")]

    # -------------------------------
    # ThingSpeak export
    # -------------------------------
    if "created_at" in df.columns:

        df = df.rename(
            columns={
                "created_at": "timestamp_utc",
                "temperature": "temperature_c",
                "humidity": "humidity_pct",
                "pressure": "pressure_hpa",
                "pm1": "pm1_0",
                "pm2.5": "pm2_5",
                "pm10": "pm10",
                "oxidising index": "oxidising_index",
                "reducing index": "reducing_index",
            }
        )

        # Derive missing gas aliases
        df["co_index"] = df["reducing_index"]
        df["no2_index"] = df["oxidising_index"]

    # -------------------------------
    # New readings.py output
    # -------------------------------
    elif "timestamp_utc" in df.columns:

        # Add aliases if missing
        if "co_index" not in df.columns:
            df["co_index"] = df["reducing_index"]

        if "no2_index" not in df.columns:
            df["no2_index"] = df["oxidising_index"]

    else:

        raise ValueError(
            f"Unsupported dataset format ({len(df.columns)} columns)"
        )

    print("\nFinal columns:")
    print(df.columns)

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


def convert_timestamps(df):

    df = df.copy()

    # Everything as strings
    timestamps = (
        df["timestamp_utc"]
        .astype(str)
        .str.strip()
    )

    # Empty datetime series
    converted = pd.Series(
        pd.NaT,
        index=df.index,
        dtype="datetime64[ns, UTC]"
    )

    # Old ThingSpeak format
    old_mask = timestamps.str.contains(
        " UTC",
        na=False
    )

    converted.loc[old_mask] = pd.to_datetime(
        timestamps.loc[old_mask],
        format="%Y-%m-%d %H:%M:%S UTC",
        utc=True,
        errors="coerce"
    )

    # New readings.py format
    converted.loc[~old_mask] = pd.to_datetime(
        timestamps.loc[~old_mask],
        utc=True,
        errors="coerce"
    )

    # Replace column
    df["timestamp_utc"] = converted

    print("\nInvalid timestamps:")
    print(df["timestamp_utc"].isna().sum())

    if df["timestamp_utc"].isna().any():

        print("\nProblem rows:")
        print(
            df.loc[
                df["timestamp_utc"].isna(),
                ["timestamp_utc"]
            ].head(20)
        )

    # Convert to Johannesburg time
    df["timestamp_sast"] = (
        df["timestamp_utc"]
        .dt.tz_convert(JOHANNESBURG_TZ)
    )

    print("\nTimestamp Range:")
    print(
        "First:",
        df["timestamp_sast"].min()
    )
    print(
        "Last:",
        df["timestamp_sast"].max()
    )

    return df


def clean_dataset(df):

    # Remove bad timestamps
    df = df.dropna(
        subset=["timestamp_utc"]
    ).copy()

    for col in STAT_COLUMNS:

        if col in df.columns:

            df[col] = pd.to_numeric(
                df[col],
                errors="coerce"
            )

    # DO NOT DROP DUPLICATES
    # Your logger records every 20 seconds.
    # Two measurements can legitimately be identical.

    df = df.sort_values(
        by="timestamp_utc"
    ).reset_index(drop=True)

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

    import os

    print("\nCurrent Directory:")
    print(os.getcwd())

    parser = argparse.ArgumentParser(
        description="Analyse ThingSpeak atmospheric monitoring CSV exports."
    )

    parser.add_argument(
        "--input",
        "-i",
        default="thingspeak_data.csv"
    )

    parser.add_argument(
        "--start",
        help="Experiment start (YYYY-MM-DD HH:MM:SS)"
    )

    parser.add_argument(
        "--end",
        help="Experiment end (YYYY-MM-DD HH:MM:SS)"
    )

    args = parser.parse_args()

    try:
        df = load_dataset(args.input)

    except FileNotFoundError:

        print(f"ERROR: {args.input} not found.")
        sys.exit(1)

    except ValueError as e:

        print(f"ERROR: {e}")
        sys.exit(1)

    # Convert timestamps
    df = convert_timestamps(df)
    
    print("\nBefore cleaning:")
    print("Rows:", len(df))
    print("First timestamp:", df["timestamp_sast"].min())
    print("Last timestamp:", df["timestamp_sast"].max())

    # Clean dataset
    cleaned = clean_dataset(df)

    print("\nTimestamp Types:")
    print(cleaned[["timestamp_utc", "timestamp_sast"]].dtypes)

    print("\nAvailable data:")
    print(
        f"From: {cleaned['timestamp_sast'].iloc[0]}"
    )

    print(
        f"To:   {cleaned['timestamp_sast'].iloc[-1]}"
    )

    if not args.start:

        args.start = input(
            "\nStart date (YYYY-MM-DD HH:MM:SS): "
        )

    if not args.end:

        args.end = input(
            "End date (YYYY-MM-DD HH:MM:SS): "
        )

    # Save cleaned dataset
    cleaned.to_csv(
        CLEANED_OUTPUT,
        index=False
    )

    print(
        f"\nCleaned dataset written to "
        f"{CLEANED_OUTPUT} ({len(cleaned)} rows)"
    )

    # Statistics
    summary = compute_summary_statistics(cleaned)
    print_summary(summary)

    # Gas report
    gas_report = build_gas_trend_report(cleaned)

    gas_report.to_csv(
        GAS_REPORT_OUTPUT,
        index=False
    )

    print(
        f"Gas trend report written to "
        f"{GAS_REPORT_OUTPUT} ({len(gas_report)} rows)"
    )

    # Experiment extraction
    experiment = extract_experiment(
        cleaned,
        args.start,
        args.end
    )

    experiment.to_csv(
        EXPERIMENT_OUTPUT,
        index=False
    )

    print(
        f"\nExperiment extract written to "
        f"{EXPERIMENT_OUTPUT}"
    )

    print(
        f"Rows extracted: {len(experiment)}"
    )

    print(
        f"Range: {args.start} -> {args.end}"
    )


if __name__ == "__main__":
    main()
if __name__ == "__main__":
    main()
