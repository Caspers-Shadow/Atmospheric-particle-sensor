#!/usr/bin/env python3
"""Analyse local logger CSVs or ThingSpeak exports without network access.

Timestamps are stored in UTC and displayed/extracted in Africa/Johannesburg.
Gas values are relative indices, not individual gas concentrations.
"""

import argparse
import csv
from datetime import datetime
import hashlib
import io
import json
from numbers import Real
from pathlib import Path
import platform
import tempfile
from zoneinfo import ZoneInfo

import pandas as pd

from thingspeak_schema import FIELD_MAP, LOCAL_HEADER, decode_status

JOHANNESBURG_TZ = ZoneInfo("Africa/Johannesburg")
DATA_DIR = Path(__file__).resolve().parent / "data"
CLEANED_OUTPUT = "cleaned_thingspeak_data.csv"
EXPERIMENT_OUTPUT = "experiment.csv"
GAS_REPORT_OUTPUT = "gas_report.csv"
SUMMARY_OUTPUT = "summary_statistics.csv"
EXCEL_OUTPUT = "analysis.xlsx"

THINGSPEAK_FIELD_MAP = FIELD_MAP
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
    "pm1_0", "pm2_5", "pm10", "oxidising_raw", "reducing_raw", "nh3_raw",
    "oxidising_index", "reducing_index",
    "nh3_index", "co_index", "no2_index",
]
GAS_COLUMNS = ["oxidising_index", "reducing_index", "nh3_index", "co_index", "no2_index"]
LEGACY_EXPORT_HEADER = [
    "created_at", "entry_id", "temperature", "humidity", "pressure", "pm1",
    "pm2.5", "pm10", "oxidising index", "reducing index", "", "", "", "",
]


def load_dataset(path: str, legacy_mixed=False, *, source_bytes=None) -> pd.DataFrame:
    """Normalise both CSV schemas, including repeated headers in old exports."""
    # CLI analysis reads once so the saved snapshot and analysed data match,
    # even if acquisition is still appending to the original file.
    source = (io.StringIO(source_bytes.decode("utf-8-sig"))
              if source_bytes is not None else None)
    if legacy_mixed:
        # Explicit recovery for the exact mixed file committed to this repo.
        # Do not infer measurement layouts from their values or timestamps.
        records = []
        recovered = 0
        with (source if source is not None else
              open(path, newline="", encoding="utf-8-sig")) as handle:
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
        df = pd.read_csv(source if source is not None else path)
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
    if "status" in df.columns:
        # The recovery uploader stores the five measurements that do not fit
        # ThingSpeak's eight fields, plus the precise source time, in status.
        for index, row in df.iterrows():
            channel_time = str(row["timestamp_utc"]).strip()
            if channel_time.endswith(" UTC"):
                channel_time = channel_time[:-4] + "+00:00"
            metadata = decode_status(row["status"], created_at=channel_time)
            for column, value in metadata.items():
                if column == "timestamp_utc" or column not in df.columns or pd.isna(df.at[index, column]):
                    df.at[index, column] = value
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
    for column in STAT_COLUMNS:
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
    return df.sort_values("timestamp_utc", kind="stable").reset_index(drop=True)


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


def sast_timestamp(value):
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp):
        raise ValueError("Experiment bounds must be valid timestamps")
    return (timestamp.tz_localize(JOHANNESBURG_TZ) if timestamp.tzinfo is None
            else timestamp.tz_convert(JOHANNESBURG_TZ))


def experiment_bounds(start: str, end: str):
    start_ts, end_ts = sast_timestamp(start), sast_timestamp(end)
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


def select_readings(cleaned, start=None, end=None):
    """Select one window, ending at the latest readable sample by default."""
    if cleaned.empty:
        raise ValueError("No valid timestamped readings found; no outputs written")
    columns = [column for column in STAT_COLUMNS if column in cleaned.columns]
    if not columns:
        raise ValueError("No recognised sensor measurements found; no outputs written")
    readable = cleaned.loc[cleaned[columns].notna().any(axis=1)].copy()
    if readable.empty:
        raise ValueError("No readable sensor measurements found; no outputs written")
    start_ts = sast_timestamp(start) if start else readable.timestamp_sast.min()
    end_ts = sast_timestamp(end) if end else readable.timestamp_sast.max()
    if start_ts > end_ts:
        raise ValueError("Experiment start must be before or equal to end; no outputs written")
    selected = extract_experiment(readable, start_ts, end_ts)
    if selected.empty:
        raise ValueError("No readable readings in the selected experiment window; no outputs written")
    return selected, start_ts, end_ts, len(cleaned) - len(readable)


def write_workbook(path, selected, summary, gas_report, report, xlsxwriter):
    """Portable Excel export; CSV and timestamp text retain source precision."""
    with xlsxwriter.Workbook(path, {"strings_to_formulas": False,
                                   "strings_to_urls": False}) as workbook:
        title = workbook.add_format({"bold": True, "font_size": 17, "font_color": "#17365D"})
        number = workbook.add_format({"num_format": "0.000"})
        heading = workbook.add_format({"bold": True, "bg_color": "#DCE6F1"})
        overview = workbook.add_worksheet("Summary")
        overview.set_column("A:A", 26)
        overview.set_column("B:E", 18)
        overview.write_string(0, 0, "Experiment analysis", title)
        overview.write_string(2, 0, "First reading (SAST)")
        overview.merge_range(2, 1, 2, 4, report["first_reading_sast"])
        overview.write_string(3, 0, "Last reading (SAST)")
        overview.merge_range(3, 1, 3, 4, report["last_reading_sast"])
        overview.write_string(4, 0, "Selected readings")
        overview.write_number(4, 1, len(selected))
        overview.write_string(5, 0, "Source SHA-256")
        overview.merge_range(5, 1, 5, 4, report["source_sha256"])
        overview.merge_range(7, 0, 7, 4,
                             "Gas indices are relative; CO/NO2 columns are aliases, not separate sensors.")
        for sheet_name, frame in (("Experiment", selected), ("Gas trends", gas_report)):
            sheet = workbook.add_worksheet(sheet_name)
            sheet.freeze_panes(1, 2)
            sheet.set_column(0, len(frame.columns) - 1, 18)
            for column_index, column in enumerate(frame.columns):
                if column.startswith("timestamp_"):
                    sheet.set_column(column_index, column_index, 36)
                else:
                    sheet.set_column(column_index, column_index, max(18, len(column) + 2))
            for row_index, row in enumerate(frame.itertuples(index=False, name=None), start=1):
                for column_index, value in enumerate(row):
                    if pd.isna(value):
                        continue
                    if isinstance(value, pd.Timestamp):
                        sheet.write_string(row_index, column_index, value.isoformat())
                    elif isinstance(value, Real):
                        sheet.write_number(row_index, column_index, float(value), number)
                    else:
                        sheet.write_string(row_index, column_index, str(value))
            sheet.add_table(0, 0, len(frame), len(frame.columns) - 1, {
                "name": "ExperimentReadings" if sheet_name == "Experiment" else "GasTrends",
                "style": "Table Style Medium 2",
                "columns": [{"header": column} for column in frame.columns],
            })
        overview.write_row(9, 0, ["Measurement", "Count", "Mean", "Minimum", "Maximum"], heading)
        for row_index, row in enumerate(summary.itertuples(index=False), start=10):
            overview.write_string(row_index, 0, row.measurement)
            reference = f"ExperimentReadings[{row.measurement}]"
            overview.write_formula(row_index, 1, f"=COUNT({reference})", None, int(row.count))
            for col, function, value in ((2, "AVERAGE", row.mean), (3, "MIN", row.min), (4, "MAX", row.max)):
                expression = f"{function}({reference})"
                if function == "AVERAGE":
                    expression = f"ROUND({expression},3)"
                overview.write_formula(row_index, col,
                                       f'=IF(COUNT({reference})=0,"",{expression})',
                                       number, "" if pd.isna(value) else float(value))
        overview.freeze_panes(10, 1)


def save_analysis(args, source_bytes, loaded, selected, start, end, invalid_count,
                  unreadable_count, xlsxwriter=None):
    summary = compute_summary_statistics(selected)
    gas_report = build_gas_trend_report(selected)
    dataset_name = EXPERIMENT_OUTPUT if args.start else CLEANED_OUTPUT
    created = datetime.now(JOHANNESBURG_TZ)
    files = [dataset_name, SUMMARY_OUTPUT, GAS_REPORT_OUTPUT, "report.txt", "report.json", "source.csv"]
    if args.excel:
        files.append(EXCEL_OUTPUT)
    repeat_arguments = ["--input", "source.csv"]
    if args.start:
        repeat_arguments += ["--start", start.isoformat()]
    repeat_arguments += ["--end", end.isoformat()] if args.start else []
    if args.legacy_mixed:
        repeat_arguments.append("--legacy-mixed")
    if args.excel:
        repeat_arguments.append("--excel")
    report = {
        "schema_version": 1, "result": "PASS", "created_at_sast": created.isoformat(),
        "source_path": str(args.input.resolve()),
        "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "code_sha256": {name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
                        for name in ("analyse.py", "thingspeak_schema.py")},
        "versions": {"python": platform.python_version(), "pandas": pd.__version__,
                     "xlsxwriter": xlsxwriter.__version__ if xlsxwriter else None},
        "parsed_source_readings": len(loaded), "invalid_timestamps": invalid_count,
        "unreadable_measurement_rows": unreadable_count, "selected_readings": len(selected),
        "requested_start": args.start, "requested_end": args.end,
        "window_start_sast": start.isoformat(), "window_end_sast": end.isoformat(),
        "end_selection": "explicit" if args.end else "last_readable_record",
        "first_reading_sast": selected.timestamp_sast.iloc[0].isoformat(),
        "last_reading_sast": selected.timestamp_sast.iloc[-1].isoformat(),
        "first_reading_utc": selected.timestamp_utc.iloc[0].isoformat(),
        "last_reading_utc": selected.timestamp_utc.iloc[-1].isoformat(),
        "max_gap_seconds": (float(selected.timestamp_utc.diff().dt.total_seconds().max())
                            if len(selected) > 1 else None),
        "repeat_arguments": repeat_arguments, "files": files,
    }
    # Validate everything before creating a results directory. A fresh directory
    # prevents a failed/repeated run from mixing old and new reports.
    output_dir = args.output_dir
    if output_dir is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        output_dir = Path(tempfile.mkdtemp(prefix=f"analysis-{created:%Y%m%d-%H%M%S}-", dir=DATA_DIR))
    else:
        if output_dir.exists() and any(output_dir.iterdir()):
            raise ValueError("Output directory is not empty; choose a new directory or omit --output-dir")
        output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "source.csv").write_bytes(source_bytes)
    selected.to_csv(output_dir / dataset_name, index=False)
    summary.to_csv(output_dir / SUMMARY_OUTPUT, index=False)
    gas_report.to_csv(output_dir / GAS_REPORT_OUTPUT, index=False)
    if args.excel:
        try:
            write_workbook(output_dir / EXCEL_OUTPUT, selected, summary, gas_report, report, xlsxwriter)
        except xlsxwriter.exceptions.XlsxWriterException as error:
            raise ValueError(f"Excel export failed: {error}") from error
    text = (f"PASS: {len(selected)} selected readings\n"
            f"SAST range: {report['first_reading_sast']} -> {report['last_reading_sast']}\n"
            f"End selection: {report['end_selection']}\n"
            f"Source SHA-256: {report['source_sha256']}\n"
            f"Invalid timestamps dropped: {invalid_count}\n"
            f"Rows without readable measurements dropped: {unreadable_count}\n"
            f"Largest selected sample gap (seconds): {report['max_gap_seconds']}\n\n"
            + summary.to_string(index=False)
            + "\n\nGas indices are relative; CO/NO2 are aliases, not separate sensors.\n"
            + f"\n{dataset_name}: selected measurements, precise UTC and SAST timestamps.\n"
            + f"{SUMMARY_OUTPUT}: statistics for these selected readings only.\n"
            + f"{GAS_REPORT_OUTPUT}: relative gas trends for these selected readings only.\n"
            + (f"{EXCEL_OUTPUT}: summary, measurements and gas trends in one workbook.\n" if args.excel else "")
            + "source.csv: unchanged source snapshot.\nreport.json: selection, versions, hashes and repeat arguments.\n")
    (output_dir / "report.txt").write_text(text, encoding="utf-8")
    # Write the PASS receipt last so incomplete outputs cannot appear complete.
    (output_dir / "report.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(f"Selected {len(selected)} readings; discarded {invalid_count} invalid timestamps "
          f"and {unreadable_count} rows without readable measurements")
    print(f"SAST range: {report['first_reading_sast']} -> {report['last_reading_sast']}")
    print_summary(summary)
    print(f"Outputs: {output_dir.resolve()}")
    return output_dir


def main(argv=None):
    parser = argparse.ArgumentParser(description="Analyse local logger CSVs or ThingSpeak exports.")
    parser.add_argument("--input", "-i", type=Path, default=DATA_DIR / "readings.csv")
    parser.add_argument("--output-dir", type=Path, help="Empty results directory; default creates a new data/analysis-... folder")
    parser.add_argument("--excel", action="store_true", help="Also create analysis.xlsx (requires XlsxWriter)")
    parser.add_argument("--legacy-mixed", action="store_true",
                        help="Explicitly recover the repository's mixed historical CSV layouts")
    parser.add_argument("--start", help="Optional experiment start in SAST (YYYY-MM-DD HH:MM:SS)")
    parser.add_argument("--end", help="Optional inclusive end in SAST; default is the last readable record")
    args = parser.parse_args(argv)
    if args.end and not args.start:
        parser.error("--end requires --start")
    try:
        if args.start:
            sast_timestamp(args.start)
        if args.end:
            experiment_bounds(args.start, args.end)
        xlsxwriter = None
        if args.excel:
            try:
                import xlsxwriter
            except ImportError:
                raise ValueError("Excel export requires XlsxWriter. Install requirements-analysis.txt while online, "
                                 "or omit --excel for CSV reports") from None
        source_bytes = args.input.read_bytes()
        df = convert_timestamps(load_dataset(args.input, legacy_mixed=args.legacy_mixed,
                                             source_bytes=source_bytes))
        invalid_count = int(df["timestamp_utc"].isna().sum())
        cleaned = clean_dataset(df)
        selected, start, end, unreadable_count = select_readings(cleaned, args.start, args.end)
        selected = selected[["timestamp_sast", "timestamp_utc",
                             *[column for column in selected.columns if not column.startswith("timestamp_")]]]
        save_analysis(args, source_bytes, df, selected, start, end, invalid_count,
                      unreadable_count, xlsxwriter)
    except (OSError, ValueError) as error:
        print(f"ERROR: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
