#!/usr/bin/env python3
"""Analyse local logger CSVs or ThingSpeak exports without network access.

Timestamps are stored in UTC and displayed/extracted in Africa/Johannesburg.
Gas values are relative indices, not individual gas concentrations.
"""

import argparse
import csv
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

JOHANNESBURG_TZ = ZoneInfo("Africa/Johannesburg")
DATA_DIR = Path(__file__).resolve().parent / "data"
CLEANED_OUTPUT = "cleaned_thingspeak_data.csv"
EXPERIMENT_OUTPUT = "experiment.csv"
GAS_REPORT_OUTPUT = "gas_report.csv"

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
EXPORT_COLUMN_MAP = {
    **THINGSPEAK_FIELD_MAP,
    "created_at": "timestamp_utc",
    "temperature": "temperature_c",
    "humidity": "humidity_pct",
    "pressure": "pressure_hpa",
    "pm1": "pm1_0",
    "pm2.5": "pm2_5",
    "oxidising index": "oxidising_index",
    "reducing index": "reducing_index",
}
STAT_COLUMNS = [
    "temperature_c", "humidity_pct", "pressure_hpa", "light_lux",
    "pm1_0", "pm2_5", "pm10", "oxidising_index", "reducing_index",
    "nh3_index", "co_index", "no2_index",
]
GAS_COLUMNS = ["oxidising_index", "reducing_index", "nh3_index", "co_index", "no2_index"]
LEGACY_EXPORT_HEADER = [
    "created_at", "entry_id", "temperature", "humidity", "pressure", "pm1",
    "pm2.5", "pm10", "oxidising index", "reducing index", "", "", "", "",
]
LOCAL_HEADER = [
    "timestamp_utc", "temperature_c", "humidity_pct", "pressure_hpa", "light_lux",
    "pm1_0", "pm2_5", "pm10", "oxidising_raw", "reducing_raw", "nh3_raw",
    "oxidising_index", "reducing_index", "nh3_index",
]


def load_dataset(path: str, legacy_mixed=False) -> pd.DataFrame:
    """Normalise both CSV schemas, including repeated headers in old exports."""
    if legacy_mixed:
        # Explicit recovery for the exact mixed file committed to this repo.
        # Do not infer measurement layouts from their values or timestamps.
        records = []
        recovered = 0
        with open(path, newline="", encoding="utf-8-sig") as handle:
            reader = csv.reader(handle)
            if next(reader, None) != LEGACY_EXPORT_HEADER:
                raise ValueError("--legacy-mixed requires the repository's 14-column historical header")
            for line, row in enumerate(reader, start=2):
                if not row or row[0] in ("created_at", "timestamp_utc"):
                    continue
                if len(row) != 14:
                    raise ValueError(f"Unexpected historical row width at CSV line {line}")
                if all(value.strip() for value in row[10:]):
                    records.append(dict(zip(LOCAL_HEADER, row)))
                    recovered += 1
                elif not any(value.strip() for value in row[10:]):
                    records.append(dict(zip(LEGACY_EXPORT_HEADER[:10], row[:10])))
                else:
                    raise ValueError(f"Ambiguous historical layout at CSV line {line}")
        # Normalise each layout before combining, rather than making duplicate
        # created_at/timestamp_utc or measurement columns in the combined frame.
        records = [{EXPORT_COLUMN_MAP.get(key, key): value for key, value in record.items()}
                   for record in records]
        df = pd.DataFrame(records, columns=list(dict.fromkeys(
            [EXPORT_COLUMN_MAP.get(key, key) for key in LEGACY_EXPORT_HEADER[:10]] + LOCAL_HEADER
        )))
        print(f"Recovered {recovered} local logger rows from the historical mixed layout")
    else:
        df = pd.read_csv(path)
        unnamed = df.columns.str.startswith("Unnamed")
        if df.loc[:, unnamed].notna().any().any():
            raise ValueError("Values in unnamed columns indicate mixed CSV layouts. "
                             "For the repository's historical thingspeak_data.csv, use --legacy-mixed")
    df.columns = df.columns.str.strip()
    df = df.loc[:, ~df.columns.str.startswith("Unnamed")]
    timestamp_column = "created_at" if "created_at" in df.columns else "timestamp_utc"
    if timestamp_column not in df.columns:
        raise ValueError("Dataset must contain created_at or timestamp_utc")
    df = df.loc[df[timestamp_column].astype(str).str.strip() != timestamp_column].copy()
    df = df.rename(columns=EXPORT_COLUMN_MAP)
    if df.columns.duplicated().any():
        raise ValueError("Dataset has multiple columns for the same measurement")
    for alias, source in (("co_index", "reducing_index"), ("no2_index", "oxidising_index")):
        if alias not in df.columns and source in df.columns:
            df[alias] = df[source]
    return df


def convert_timestamps(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    timestamps = df["timestamp_utc"].astype(str).str.strip()
    timestamps = timestamps.str.replace(r"\s+UTC$", "+00:00", regex=True)
    df["timestamp_utc"] = pd.to_datetime(timestamps, format="mixed", utc=True, errors="coerce")
    df["timestamp_sast"] = df["timestamp_utc"].dt.tz_convert(JOHANNESBURG_TZ)
    return df


def clean_dataset(df: pd.DataFrame) -> pd.DataFrame:
    df = df.dropna(subset=["timestamp_utc"]).copy()
    numeric_columns = STAT_COLUMNS + ["oxidising_raw", "reducing_raw", "nh3_raw"]
    for column in numeric_columns:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")
            df[column] = df[column].replace([float("inf"), float("-inf")], float("nan"))
    # -1 marks unavailable sensors; exclude it from averages and trends.
    for column in ["pm1_0", "pm2_5", "pm10", *GAS_COLUMNS]:
        if column in df.columns:
            df.loc[df[column] < 0, column] = float("nan")
    for column in ["oxidising_raw", "reducing_raw", "nh3_raw"]:
        if column in df.columns:
            df.loc[df[column] <= 0, column] = float("nan")
    # Identical values at different timestamps are legitimate measurements.
    return df.sort_values("timestamp_utc").reset_index(drop=True)


def compute_summary_statistics(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for column in STAT_COLUMNS:
        if column not in df.columns:
            continue
        series = df[column].dropna()
        rows.append({
            "measurement": column, "count": series.count(),
            "mean": round(series.mean(), 3) if not series.empty else None,
            "min": series.min() if not series.empty else None,
            "max": series.max() if not series.empty else None,
        })
    return pd.DataFrame(rows)


def build_gas_trend_report(df: pd.DataFrame) -> pd.DataFrame:
    columns = [column for column in GAS_COLUMNS if column in df.columns]
    report = df[["timestamp_sast", *columns]].copy()
    for column in columns:
        delta = report[column] - report[column].rolling(window=5, min_periods=1).mean()
        trend = pd.Series("stable", index=report.index)
        trend.loc[delta > 2] = "rising"
        trend.loc[delta < -2] = "falling"
        trend.loc[delta.isna()] = "unavailable"
        report[f"{column}_trend"] = trend
    return report


def experiment_bounds(start: str, end: str):
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    if pd.isna(start_ts) or pd.isna(end_ts):
        raise ValueError("Experiment bounds must be valid timestamps")
    start_ts = (start_ts.tz_localize(JOHANNESBURG_TZ) if start_ts.tzinfo is None
                else start_ts.tz_convert(JOHANNESBURG_TZ))
    end_ts = (end_ts.tz_localize(JOHANNESBURG_TZ) if end_ts.tzinfo is None
              else end_ts.tz_convert(JOHANNESBURG_TZ))
    if start_ts > end_ts:
        raise ValueError("Experiment start must be before or equal to end")
    return start_ts, end_ts


def extract_experiment(df: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    start_ts, end_ts = experiment_bounds(start, end)
    mask = df["timestamp_sast"].between(start_ts, end_ts)
    return df.loc[mask].reset_index(drop=True)


def print_summary(summary_df: pd.DataFrame):
    print("===== SUMMARY STATISTICS =====\n")
    print(summary_df.to_string(index=False) if not summary_df.empty
          else "No measurement columns found in dataset.")
    print("\n===============================\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Analyse local logger CSVs or ThingSpeak exports.")
    parser.add_argument("--input", "-i", type=Path, default=DATA_DIR / "readings.csv")
    parser.add_argument("--output-dir", type=Path, default=DATA_DIR / "analysis")
    parser.add_argument("--legacy-mixed", action="store_true",
                        help="Explicitly recover the repository's mixed historical CSV layouts")
    parser.add_argument("--start", help="Optional experiment start in SAST (YYYY-MM-DD HH:MM:SS)")
    parser.add_argument("--end", help="Optional experiment end in SAST (YYYY-MM-DD HH:MM:SS)")
    args = parser.parse_args(argv)
    if bool(args.start) != bool(args.end):
        parser.error("--start and --end must be supplied together")
    try:
        if args.start:
            experiment_bounds(args.start, args.end)
        df = convert_timestamps(load_dataset(args.input, legacy_mixed=args.legacy_mixed))
        invalid_count = int(df["timestamp_utc"].isna().sum())
        cleaned = clean_dataset(df)
        if cleaned.empty:
            raise ValueError("No valid timestamped readings found; no outputs written")
        args.output_dir.mkdir(parents=True, exist_ok=True)
        cleaned.to_csv(args.output_dir / CLEANED_OUTPUT, index=False)
        print(f"Cleaned {len(cleaned)} readings; discarded {invalid_count} invalid timestamps")
        print(f"SAST range: {cleaned['timestamp_sast'].min()} -> {cleaned['timestamp_sast'].max()}")
        print_summary(compute_summary_statistics(cleaned))
        build_gas_trend_report(cleaned).to_csv(args.output_dir / GAS_REPORT_OUTPUT, index=False)
        if args.start:
            experiment = extract_experiment(cleaned, args.start, args.end)
            experiment.to_csv(args.output_dir / EXPERIMENT_OUTPUT, index=False)
            print(f"Experiment extract: {len(experiment)} readings")
        print(f"Outputs: {args.output_dir.resolve()}")
    except (OSError, ValueError) as error:
        print(f"ERROR: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
