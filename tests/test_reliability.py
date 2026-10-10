"""Regression tests use simulated sensors and block external connections."""

import contextlib
import csv
import io
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd
import requests

import analyse
import check_run
import readings


ROOT = Path(__file__).resolve().parents[1]


def sample(**changes):
    values = dict(
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
        temperature_c=24.0, humidity_pct=45.0, pressure_hpa=870.0, light_lux=80.0,
        pm1_0=2.0, pm2_5=3.0, pm10=4.0, oxidising_raw=22000.0,
        reducing_raw=180000.0, nh3_raw=160000.0, oxidising_index=10.0,
        reducing_index=10.0, nh3_index=20.0,
    )
    values.update(changes)
    return readings.SensorReading(**values)


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        # Accidental live requests must fail, even if a developer has an API key.
        self.network_guard = patch.object(socket.socket, "connect",
                                          side_effect=AssertionError("External networking forbidden"))
        self.network_guard.start()
        self.addCleanup(self.network_guard.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def write_fixture(self, header, rows):
        path = self.directory / "fixture.csv"
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(header)
            writer.writerows(rows)
        return path

    def test_local_csv_roundtrip_and_empty_file_header(self):
        path = self.directory / "readings.csv"
        path.touch()
        readings.write_csv_row(sample(), path)
        readings.write_csv_row(sample(), path)
        frame = analyse.clean_dataset(analyse.convert_timestamps(analyse.load_dataset(path)))
        self.assertEqual(len(frame), 2)
        self.assertEqual(frame.temperature_c.tolist(), [24, 24])
        self.assertEqual(frame.timestamp_sast.iloc[0].utcoffset(), timedelta(hours=2))

    def test_logger_refuses_export_header_without_modifying_file(self):
        path = self.write_fixture(["created_at", "field1"], [["2026-07-16 13:30:00 UTC", 24]])
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "header mismatch"):
            readings.write_csv_row(sample(), path)
        self.assertEqual(path.read_bytes(), before)

    def test_thingspeak_field_mapping_repeated_headers_and_mixed_timestamps(self):
        header = ["created_at", "field1", "field7", "field8"]
        path = self.write_fixture(header, [
            ["2026-07-16 13:30:00 UTC", "24", "10", "12"], header,
            ["2026-07-16T13:30:20.123+00:00", "25", "13", "15"],
            ["bad timestamp", "26", "20", "30"],
        ])
        frame = analyse.clean_dataset(analyse.convert_timestamps(analyse.load_dataset(path)))
        self.assertEqual(len(frame), 2)
        self.assertEqual(frame.co_index.tolist(), [12, 15])

    def test_repository_historical_layout_requires_explicit_recovery(self):
        local = sample(timestamp_utc="2026-07-21T10:39:10.649859+00:00", temperature_c=34.05,
                       pressure_hpa=870.57, oxidising_raw=1677.9, oxidising_index=0)
        path = self.write_fixture(analyse.LEGACY_EXPORT_HEADER, [
            ["2026-07-16 13:30:00 UTC", 27, 24, 45, 870, 2, 3, 4, 10, 12, "", "", "", ""],
            list(local.as_csv_row().values()),
        ])
        with self.assertRaisesRegex(ValueError, "mixed CSV layouts"):
            analyse.load_dataset(path)
        with contextlib.redirect_stdout(io.StringIO()):
            frame = analyse.clean_dataset(analyse.convert_timestamps(
                analyse.load_dataset(path, legacy_mixed=True)))
        self.assertEqual(len(frame), 2)
        self.assertLessEqual(frame.humidity_pct.max(), 100)
        row = frame.loc[frame.timestamp_utc == pd.Timestamp("2026-07-21T10:39:10.649859+00:00")].iloc[0]
        self.assertEqual(row.temperature_c, 34.05)
        self.assertEqual(row.pressure_hpa, 870.57)
        self.assertEqual(row.oxidising_raw, 1677.9)
        self.assertEqual(row.oxidising_index, 0)

    def test_missing_gas_columns_are_allowed(self):
        path = self.write_fixture(["created_at", "field1"], [["2026-07-16 13:30:00 UTC", 24]])
        frame = analyse.clean_dataset(analyse.convert_timestamps(analyse.load_dataset(path)))
        self.assertEqual(list(analyse.build_gas_trend_report(frame)), ["timestamp_sast"])

    def test_sentinels_and_nonfinite_values_do_not_bias_statistics(self):
        path = self.directory / "readings.csv"
        readings.write_csv_row(sample(pm1_0=-1, reducing_index=-1, reducing_raw=-1), path)
        readings.write_csv_row(sample(pm1_0=4, reducing_index=20), path)
        frame = analyse.clean_dataset(analyse.convert_timestamps(analyse.load_dataset(path)))
        summary = analyse.compute_summary_statistics(frame).set_index("measurement")
        self.assertEqual(summary.loc["pm1_0", "count"], 1)
        self.assertEqual(summary.loc["pm1_0", "mean"], 4)
        self.assertEqual(analyse.build_gas_trend_report(frame).reducing_index_trend.iloc[0], "unavailable")

    def test_analysis_cli_runs_once_without_prompts(self):
        path = self.directory / "readings.csv"
        readings.write_csv_row(sample(), path)
        output = self.directory / "analysis"
        result = subprocess.run([sys.executable, str(ROOT / "analyse.py"), "--input", str(path),
                                 "--output-dir", str(output)], input="", text=True,
                                capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.count("SUMMARY STATISTICS"), 1)
        self.assertTrue((output / analyse.GAS_REPORT_OUTPUT).exists())
        self.assertFalse((output / analyse.EXPERIMENT_OUTPUT).exists())

    def test_analysis_empty_valid_dataset_returns_clear_error(self):
        path = self.write_fixture(["created_at", "field1"], [["bad timestamp", 24]])
        output = self.directory / "analysis"
        with contextlib.redirect_stdout(io.StringIO()) as captured:
            code = analyse.main(["--input", str(path), "--output-dir", str(output)])
        self.assertEqual(code, 1)
        self.assertIn("No valid timestamped", captured.getvalue())
        self.assertFalse(output.exists())

    def test_experiment_bounds_are_inclusive_in_sast(self):
        frame = analyse.convert_timestamps(pd.DataFrame({"timestamp_utc": [
            "2026-07-16T13:30:00+00:00", "2026-07-16T13:30:20+00:00"]}))
        self.assertEqual(len(analyse.extract_experiment(frame, "2026-07-16 15:30:00",
                                                       "2026-07-16 15:30:20")), 2)
        with self.assertRaisesRegex(ValueError, "before"):
            analyse.extract_experiment(frame, "2026-07-17", "2026-07-16")

    def test_explicit_offline_and_missing_key_never_import_requests(self):
        original_import = __import__
        def guard_import(name, *args, **kwargs):
            if name == "requests":
                raise AssertionError("requests imported in local-only mode")
            return original_import(name, *args, **kwargs)
        with patch("builtins.__import__", side_effect=guard_import), patch.dict(os.environ, {}, clear=True):
            offline = readings.ThingSpeakUploader(api_key="test-key", offline=True)
            missing = readings.ThingSpeakUploader()
            self.assertFalse(offline.upload(sample()))
            self.assertFalse(missing.upload(sample()))
            worker = readings.BackgroundUploader(offline)
            self.assertIsNone(worker._thread)
            worker.close()

    def test_network_failure_backoff_and_recovery(self):
        uploader = readings.ThingSpeakUploader(api_key="test-key")
        success = Mock(ok=True, text="123")
        with patch.object(requests, "post", side_effect=[requests.ConnectionError("offline"), success]) as post:
            with patch.object(readings.time, "monotonic", return_value=100):
                self.assertFalse(uploader.upload(sample()))
            with patch.object(readings.time, "monotonic", return_value=110):
                self.assertFalse(uploader.upload(sample()))
                self.assertEqual(post.call_count, 1)
            with patch.object(readings.time, "monotonic", return_value=121):
                self.assertTrue(uploader.upload(sample()))
        self.assertEqual(uploader._retry_delay, readings.UPLOAD_INTERVAL_SECONDS)

    def test_rejected_and_malformed_responses_are_failures_and_do_not_log_key(self):
        for response in [Mock(ok=False, status_code=400, text="secret-key"),
                         Mock(ok=True, text="0"), Mock(ok=True, text="<html>error</html>"),
                         Mock(ok=True, text="")]:
            uploader = readings.ThingSpeakUploader(api_key="secret-key")
            with patch.object(requests, "post", return_value=response), self.assertLogs("atmo_system") as logs:
                self.assertFalse(uploader.upload(sample()))
            self.assertNotIn("secret-key", "\n".join(logs.output))
        uploader = readings.ThingSpeakUploader(api_key="secret-key")
        with patch.object(requests, "post", side_effect=requests.Timeout("URL contains secret-key")):
            with self.assertLogs("atmo_system") as logs:
                self.assertFalse(uploader.upload(sample()))
        self.assertNotIn("secret-key", "\n".join(logs.output))

    def test_upload_uses_post_original_timestamp_and_omits_invalid_sensor_fields(self):
        reading = sample(pm1_0=-1, pm2_5=float("nan"), pm10=float("inf"), reducing_index=-1)
        uploader = readings.ThingSpeakUploader(api_key="test-key")
        with patch.object(requests, "post", return_value=Mock(ok=True, text="123")) as post:
            self.assertTrue(uploader.upload(reading))
        payload = post.call_args.kwargs["data"]
        self.assertEqual(payload["created_at"], reading.timestamp_utc)
        self.assertFalse(any(field in payload for field in ["field4", "field5", "field6", "field8"]))
        self.assertNotIn("params", post.call_args.kwargs)
        self.assertEqual(post.call_args.kwargs["timeout"], (3, 5))

    def test_sensor_failure_uses_missing_gas_indices(self):
        sensors = object.__new__(readings.SensorManager)
        sensors._gas = Mock()
        sensors._gas.read_all.side_effect = OSError("sensor unavailable")
        with self.assertLogs("atmo_system"):
            self.assertEqual(sensors.read_gas_raw(), (-1, -1, -1))
        calculator = readings.GasIndexCalculator()
        self.assertEqual(calculator.oxidising_index(-1), -1)
        self.assertEqual(calculator.reducing_index(0), -1)
        self.assertEqual(calculator.nh3_index(float("nan")), -1)
        with self.assertRaises(ValueError):
            readings.GasIndexCalculator(baseline_ox=0)

    def test_bad_calibration_does_not_break_cli_help(self):
        environment = dict(os.environ, GAS_BASELINE_OXIDISING="invalid")
        result = subprocess.run([sys.executable, str(ROOT / "readings.py"), "--help"],
                                env=environment, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        with patch.dict(os.environ, {"GAS_BASELINE_OXIDISING": "invalid"}):
            with self.assertRaises(ValueError):
                readings.GasIndexCalculator()

    def test_acquisition_failure_waits_for_next_cycle_and_reports_no_data(self):
        sensors = Mock()
        sensors.take_reading.side_effect = OSError("I2C read failed")
        with patch.object(readings, "SensorManager", return_value=sensors), \
             patch.object(readings, "configure_logging"), \
             patch.object(readings, "LCD_PAGE_DWELL_SECONDS", 0.005), \
             self.assertLogs("atmo_system"), contextlib.redirect_stdout(io.StringIO()):
            code = readings.main(["--offline", "--no-lcd", "--data-dir", str(self.directory),
                                  "--duration", "0.05"])
        self.assertEqual(code, 1)
        self.assertEqual(sensors.take_reading.call_count, 1)
        sensors.close.assert_called_once()

    def test_pms_timeout_retries_preserve_a_row_with_sentinels(self):
        sensors = object.__new__(readings.SensorManager)
        sensors._pms_timeout = TimeoutError
        sensors.pms5003 = Mock()
        sensors.pms5003.read.side_effect = TimeoutError()
        with patch.object(readings.time, "sleep"), self.assertLogs("atmo_system"):
            self.assertEqual(sensors.read_particulates(), (-1, -1, -1))
        self.assertEqual(sensors.pms5003.read.call_count, 3)

    def test_slow_network_does_not_block_logging_or_lcd_and_worker_uses_snapshot(self):
        entered = threading.Event()
        release = threading.Event()
        uploaded = []
        def slow_post(*args, **kwargs):
            uploaded.append(dict(kwargs["data"]))
            entered.set()
            release.wait(timeout=3)
            return Mock(ok=True, text="123")
        sensors = Mock(gas_calc=readings.GasIndexCalculator())
        sensors.take_reading.side_effect = lambda: sample()
        sensors.take_fast_reading.return_value = (99, 45, 870, 80)
        lcd = Mock()
        uploader = readings.ThingSpeakUploader(api_key="test-key")
        try:
            with patch.object(requests, "post", side_effect=slow_post), \
                 patch.object(readings, "SensorManager", return_value=sensors), \
                 patch.object(readings, "LCDDisplay", return_value=lcd), \
                 patch.object(readings, "ThingSpeakUploader", return_value=uploader), \
                 patch.object(readings, "configure_logging"), \
                 patch.object(readings, "print_console_report"), \
                 patch.object(readings, "UPLOAD_INTERVAL_SECONDS", 0.05), \
                 patch.object(readings, "LCD_PAGE_DWELL_SECONDS", 0.005), \
                 contextlib.redirect_stdout(io.StringIO()):
                code = readings.main(["--data-dir", str(self.directory), "--duration", "0.3"])
            self.assertTrue(entered.is_set())
            self.assertEqual(code, 0)
            with (self.directory / "readings.csv").open(newline="") as handle:
                self.assertGreaterEqual(len(list(csv.DictReader(handle))), 3)
            self.assertGreater(lcd.render.call_count, 3)
            self.assertEqual(uploaded[0]["field1"], 24)
        finally:
            release.set()

    def test_main_storage_failure_is_visible_and_stops_before_upload(self):
        sensors = Mock(gas_calc=readings.GasIndexCalculator())
        sensors.take_reading.return_value = sample()
        with patch.object(readings, "SensorManager", return_value=sensors), \
             patch.object(readings, "configure_logging"), \
             patch.object(readings, "write_csv_row", side_effect=OSError("disk full")), \
             self.assertLogs("atmo_system") as logs, contextlib.redirect_stdout(io.StringIO()):
            code = readings.main(["--offline", "--no-lcd", "--data-dir", str(self.directory), "--duration", "1"])
        self.assertEqual(code, 1)
        self.assertIn("FATAL", "\n".join(logs.output))
        sensors.close.assert_called_once()

    def test_hil_checker_detects_missing_values_gaps_and_row_count(self):
        path = self.directory / "readings.csv"
        readings.write_csv_row(sample(timestamp_utc="2026-10-08T08:00:00+00:00"), path)
        readings.write_csv_row(sample(timestamp_utc="2026-10-08T08:01:00+00:00", pm1_0=-1), path)
        report = check_run.check_capture(path, min_rows=3, max_gap=35, require_sensors=True)
        self.assertEqual(report["rows"], 2)
        self.assertEqual(len(report["issues"]), 3)
        self.assertEqual(report["unavailable"], {"pm1_0": 1})


if __name__ == "__main__":
    unittest.main()
