"""Repeatable, offline experiment selection and export checks."""

import contextlib
import csv
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

import pandas as pd

import analyse


class AnalysisRunTests(unittest.TestCase):
    def setUp(self):
        guard = patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.source = self.directory / "readings.csv"
        with self.source.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["timestamp_utc", "temperature_c", "oxidising_index", "oxidising_raw"])
            writer.writerows([
                ["2026-10-08T09:00:00+00:00", 999, 90, 1000],
                ["2026-10-09T08:59:40+00:00", 999, 90, 1000],
                ["2026-10-09T09:00:00.366145+00:00", 10, 5, 100],
                # Deliberately unsorted; auto-end must use the latest readable time.
                ["2026-10-09T11:12:25.742104+00:00", 30, 25, 300],
                ["2026-10-09T09:00:20+00:00", 20, 15, 200],
                ["2026-10-09T11:13:00+00:00", "", -1, -1],
                ["bad timestamp", 100, 50, 500],
            ])

    def run_analysis(self, *arguments):
        with contextlib.redirect_stdout(io.StringIO()) as captured:
            result = analyse.main(["--input", str(self.source), *arguments])
        return result, captured.getvalue()

    def test_start_alone_scopes_all_reports_and_preserves_source(self):
        before = self.source.read_bytes()
        output = self.directory / "analysis"
        code, text = self.run_analysis("--start", "2026-10-09 11:00:00", "--output-dir", str(output))
        self.assertEqual(code, 0, text)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual((output / "source.csv").read_bytes(), before)
        frame = pd.read_csv(output / analyse.EXPERIMENT_OUTPUT)
        self.assertEqual(frame.temperature_c.tolist(), [10, 20, 30])
        self.assertEqual(pd.Timestamp(frame.timestamp_utc.iloc[-1]),
                         pd.Timestamp("2026-10-09T11:12:25.742104+00:00"))
        summary = pd.read_csv(output / analyse.SUMMARY_OUTPUT).set_index("measurement")
        self.assertEqual(summary.loc["temperature_c", "mean"], 20)
        self.assertEqual(summary.loc["oxidising_raw", "mean"], 200)
        gas = pd.read_csv(output / analyse.GAS_REPORT_OUTPUT)
        self.assertEqual(len(gas), 3)
        self.assertEqual(gas.oxidising_index_trend.iloc[0], "stable")
        report = json.loads((output / "report.json").read_text())
        self.assertEqual(report["selected_readings"], 3)
        self.assertEqual(report["invalid_timestamps"], 1)
        self.assertEqual(report["unreadable_measurement_rows"], 1)
        self.assertEqual(report["end_selection"], "last_readable_record")
        self.assertEqual(report["last_reading_sast"], "2026-10-09T13:12:25.742104+02:00")
        self.assertEqual(report["source_sha256"], hashlib.sha256(before).hexdigest())

    def test_explicit_bounds_apply_to_statistics_and_are_inclusive(self):
        output = self.directory / "bounded"
        code, text = self.run_analysis("--start", "2026-10-09 11:00:00.366145",
                                       "--end", "2026-10-09T09:00:20+00:00",
                                       "--output-dir", str(output))
        self.assertEqual(code, 0, text)
        frame = pd.read_csv(output / analyse.EXPERIMENT_OUTPUT)
        self.assertEqual(frame.temperature_c.tolist(), [10, 20])
        summary = pd.read_csv(output / analyse.SUMMARY_OUTPUT).set_index("measurement")
        self.assertEqual(summary.loc["temperature_c", "mean"], 15)
        self.assertEqual(len(pd.read_csv(output / analyse.GAS_REPORT_OUTPUT)), 2)

    def test_default_runs_create_separate_folders_with_repeatable_results(self):
        root = self.directory / "runs"
        with patch.object(analyse, "DATA_DIR", root):
            for _ in range(2):
                code, text = self.run_analysis("--start", "2026-10-09 11:00:00")
                self.assertEqual(code, 0, text)
        runs = sorted(root.iterdir())
        self.assertEqual(len(runs), 2)
        for filename in (analyse.EXPERIMENT_OUTPUT, analyse.SUMMARY_OUTPUT, analyse.GAS_REPORT_OUTPUT, "source.csv"):
            self.assertEqual((runs[0] / filename).read_bytes(), (runs[1] / filename).read_bytes())
        # Replay the saved snapshot/settings even if the original source is gone.
        report = json.loads((runs[0] / "report.json").read_text())
        arguments = report["repeat_arguments"]
        arguments[1] = str(runs[0] / arguments[1])
        self.source.unlink()
        with contextlib.redirect_stdout(io.StringIO()):
            code = analyse.main([*arguments, "--output-dir", str(self.directory / "repeat")])
        self.assertEqual(code, 0)
        self.assertEqual((runs[0] / analyse.EXPERIMENT_OUTPUT).read_bytes(),
                         (self.directory / "repeat" / analyse.EXPERIMENT_OUTPUT).read_bytes())

    def test_invalid_or_empty_windows_create_no_outputs(self):
        for index, arguments in enumerate([
            ["--start", "not-a-date"],
            ["--start", "2026-10-10 11:00:00"],
            ["--start", "2026-10-09 10:00:00", "--end", "2026-10-09 10:30:00"],
            ["--start", "2026-10-09 12:00:00", "--end", "2026-10-09 11:00:00"],
        ]):
            with self.subTest(arguments=arguments):
                output = self.directory / f"invalid-{index}"
                code, text = self.run_analysis(*arguments, "--output-dir", str(output))
                self.assertEqual(code, 1, text)
                self.assertFalse(output.exists())

    def test_existing_results_are_not_overwritten(self):
        output = self.directory / "existing"
        output.mkdir()
        marker = output / "report.txt"
        marker.write_text("old results")
        code, text = self.run_analysis("--start", "2026-10-09 11:00:00", "--output-dir", str(output))
        self.assertEqual(code, 1)
        self.assertIn("not empty", text)
        self.assertEqual(marker.read_text(), "old results")
        self.assertEqual(list(output.iterdir()), [marker])

    def test_missing_excel_dependency_fails_before_writing(self):
        output = self.directory / "no-excel"
        with patch.dict("sys.modules", {"xlsxwriter": None}):
            code, text = self.run_analysis("--excel", "--output-dir", str(output))
        self.assertEqual(code, 1)
        self.assertIn("omit --excel", text)
        self.assertFalse(output.exists())

    @unittest.skipUnless(importlib.util.find_spec("xlsxwriter"), "XlsxWriter not installed")
    def test_workbook_contains_exact_timestamps_and_matching_cached_statistics(self):
        output = self.directory / "excel"
        code, text = self.run_analysis("--start", "2026-10-09 11:00:00", "--excel",
                                       "--output-dir", str(output))
        self.assertEqual(code, 0, text)
        ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        with zipfile.ZipFile(output / analyse.EXCEL_OUTPUT) as archive:
            strings = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            values = ["".join(item.itertext()) for item in strings]
            self.assertIn("2026-10-09T11:12:25.742104+00:00", values)
            self.assertIn("2026-10-09T13:12:25.742104+02:00", values)
            summary = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
            cells = {cell.attrib["r"]: cell for cell in summary.findall(".//m:c", ns)}
            self.assertEqual(cells["B11"].find("m:v", ns).text, "3")
            self.assertEqual(cells["C11"].find("m:v", ns).text, "20.0")
            for name in archive.namelist():
                if name.startswith("xl/worksheets/sheet"):
                    sheet = ET.fromstring(archive.read(name))
                    self.assertFalse(sheet.findall('.//m:c[@t="e"]', ns))


if __name__ == "__main__":
    unittest.main()
