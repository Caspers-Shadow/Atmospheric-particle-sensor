"""Flight recovery tests emulate ThingSpeak; no live writes or sockets allowed."""

import contextlib
import csv
import io
import json
import os
import socket
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd
import requests

import analyse
import readings
import upload_flight
from thingspeak_schema import FIELD_MAP, LOCAL_HEADER, make_update, timestamp_key

ROOT = Path(__file__).resolve().parents[1]
HIL_CSV = ROOT / "hil-results/offline-20261008-115334-YqyObx/readings.csv"


class FakeClock:
    def __init__(self):
        self.now = 0

    def clock(self):
        return self.now

    def sleep(self, delay):
        self.now += delay


class FakeThingSpeak:
    """Apply channel timestamp uniqueness, capped reads and lossy API formatting."""

    def __init__(self, clock):
        self.clock = clock
        self.entries = []
        self.posts = []
        self.gets = []
        self.timeout_after_store = False
        self.partial_once = False
        self.ignore_writes = False
        self.drop_status = False
        self.bad_read_key = False
        self.labels = ["Temperature", "Humidity", "Pressure", "PM1.0", "PM2.5", "PM10.0",
                       "Oxidising Index", "Reducing Index"]

    def close(self):
        pass

    def store(self, update):
        if any(timestamp_key(row["created_at"]) == timestamp_key(update["created_at"]) for row in self.entries):
            return
        entry = dict(update)
        entry["entry_id"] = len(self.entries) + 1
        entry["created_at"] = timestamp_key(entry["created_at"]).isoformat().replace("+00:00", "Z")
        for field in FIELD_MAP:
            entry[field] = str(entry[field]) if field in entry else None
        if self.drop_status:
            entry.pop("status", None)
        self.entries.append(entry)

    def request(self, method, url, **kwargs):
        assert kwargs["allow_redirects"] is False
        assert kwargs["timeout"] == (3, 30)
        if method == "GET":
            self.gets.append(kwargs["params"])
            if self.bad_read_key:
                return Mock(ok=False, status_code=401, text="private-read-key")
            params = kwargs["params"]
            assert params["status"] == "true"
            assert params["timezone"] == "UTC"
            start = datetime.strptime(params["start"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            end = datetime.strptime(params["end"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            rows = [dict(row) for row in self.entries if start <= timestamp_key(row["created_at"]) <= end]
            channel = {"id": 3429238, **{f"field{i}": name for i, name in enumerate(self.labels, 1)}}
            return Mock(ok=True, json=lambda: {"channel": channel, "feeds": rows[-params["results"]:]})
        assert method == "POST" and url.endswith("bulk_update.json")
        updates = kwargs["json"]["updates"]
        self.posts.append((self.clock.now, list(updates)))
        if not self.ignore_writes:
            chosen = updates[:len(updates) // 2] if self.partial_once else updates
            for update in chosen:
                self.store(update)
            self.partial_once = False
        if self.timeout_after_store:
            self.timeout_after_store = False
            raise requests.Timeout("URL contains private-write-key")
        return Mock(ok=True, json=lambda: {"success": True})


def synthetic_record(timestamp):
    return dict(zip(LOCAL_HEADER, [timestamp, 24, 45, 870, 80, 1, 2, 3,
                                    22000, 180000, 160000, 10, 11, 20]))


class FlightReplayTests(unittest.TestCase):
    def setUp(self):
        guard = patch.object(socket.socket, "connect", side_effect=AssertionError("External networking forbidden"))
        guard.start()
        self.addCleanup(guard.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.clock = FakeClock()
        self.server = FakeThingSpeak(self.clock)
        self.client = upload_flight.ThingSpeakClient(3429238, "private-write-key", "private-read-key",
            session=self.server, sleep=self.clock.sleep, clock=self.clock.clock)

    def run_replay(self, updates):
        return upload_flight.replay(self.client, updates, progress=lambda text: None)

    def fixture(self, records):
        path = self.directory / "readings.csv"
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=LOCAL_HEADER)
            writer.writeheader()
            writer.writerows(records)
        return path

    def test_actual_pi_capture_roundtrips_all_14_columns_through_cloud_analysis(self):
        source_bytes = HIL_CSV.read_bytes()
        _, updates = upload_flight.load_flight(HIL_CSV)
        verified, result = self.run_replay(updates)
        export = self.directory / "verified.csv"
        upload_flight.write_export(export, verified)
        cloud = analyse.clean_dataset(analyse.convert_timestamps(analyse.load_dataset(export)))
        local = analyse.clean_dataset(analyse.convert_timestamps(analyse.load_dataset(HIL_CSV)))
        for column in LOCAL_HEADER:
            pd.testing.assert_series_equal(cloud[column], local[column], check_dtype=False)
        self.assertEqual(result["verified"], 15)
        self.assertEqual(len(self.server.posts), 1)
        self.assertEqual(HIL_CSV.read_bytes(), source_bytes)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(analyse.main(["--input", str(export), "--output-dir", str(self.directory / "analysis")]), 0)
        self.assertTrue((self.directory / "analysis/gas_report.csv").exists())

    def test_more_than_960_samples_batch_and_repeat_without_duplicates(self):
        start = datetime(2026, 10, 1, tzinfo=timezone.utc)
        updates = [make_update(synthetic_record((start + timedelta(seconds=20 * i)).isoformat()))
                   for i in range(1001)]
        verified, result = self.run_replay(updates)
        self.assertEqual(len(verified), 1001)
        self.assertEqual([len(post[1]) for post in self.server.posts], [960, 41])
        self.assertGreaterEqual(self.server.posts[0][0], 15)
        self.assertGreaterEqual(self.server.posts[1][0] - self.server.posts[0][0], 15)
        _, repeated = self.run_replay(updates)
        self.assertEqual(repeated["already_present"], 1001)
        self.assertEqual(repeated["recovered"], 0)
        self.assertEqual(len(self.server.posts), 2)

    def test_timeout_after_accepted_batch_is_verified_without_second_post(self):
        _, updates = upload_flight.load_flight(HIL_CSV)
        self.server.timeout_after_store = True
        verified, result = self.run_replay(updates)
        self.assertEqual(len(verified), 15)
        self.assertEqual(len(self.server.posts), 1)
        self.assertEqual(result["recovered"], 15)

    def test_partial_upload_retries_only_missing_entries(self):
        _, updates = upload_flight.load_flight(HIL_CSV)
        self.server.partial_once = True
        verified, _ = self.run_replay(updates)
        self.assertEqual(len(verified), 15)
        self.assertEqual([len(post[1]) for post in self.server.posts], [15, 8])

    def test_late_existing_conflict_prevents_any_upload(self):
        _, updates = upload_flight.load_flight(HIL_CSV)
        self.server.store(updates[-1])
        self.server.entries[0]["field1"] = "99"
        with self.assertRaisesRegex(upload_flight.UploadError, "Conflicting"):
            self.run_replay(updates)
        self.assertEqual(self.server.posts, [])

    def test_old_live_entry_without_extra_measurements_is_not_claimed_complete(self):
        _, updates = upload_flight.load_flight(HIL_CSV)
        self.server.store(updates[0])
        self.server.entries[0].pop("status")
        with self.assertRaisesRegex(upload_flight.UploadError, "Conflicting"):
            self.run_replay(updates)
        self.assertEqual(self.server.posts, [])

    def test_success_response_without_stored_rows_fails(self):
        _, updates = upload_flight.load_flight(HIL_CSV)
        self.server.ignore_writes = True
        with self.assertRaisesRegex(upload_flight.UploadError, "unverified"):
            self.run_replay(updates)
        self.assertEqual(len(self.server.posts), 3)

    def test_missing_status_after_upload_fails_verification(self):
        _, updates = upload_flight.load_flight(HIL_CSV)
        self.server.drop_status = True
        with self.assertRaisesRegex(upload_flight.UploadError, "Conflicting"):
            self.run_replay(updates)

    def test_bad_read_key_and_wrong_field_mapping_fail_before_writing(self):
        _, updates = upload_flight.load_flight(HIL_CSV)
        self.server.bad_read_key = True
        with self.assertRaises(upload_flight.UploadError) as captured:
            self.run_replay(updates)
        self.assertNotIn("private-read-key", str(captured.exception))
        self.server.bad_read_key = False
        self.server.labels[0] = "Pressure"
        with self.assertRaisesRegex(upload_flight.UploadError, "mapping"):
            self.run_replay(updates)
        self.assertEqual(self.server.posts, [])

    def test_capped_reads_split_ranges_and_deduplicate_boundaries(self):
        start = datetime(2026, 10, 1, tzinfo=timezone.utc)
        for i in range(8):
            self.server.store(make_update(synthetic_record((start + timedelta(seconds=i)).isoformat())))
        with patch.object(upload_flight, "READ_LIMIT", 3):
            entries = self.client.read_range(start, start + timedelta(seconds=8))
        self.assertEqual(len(entries), 8)
        self.assertEqual(len({row["entry_id"] for row in entries}), 8)
        self.assertGreater(len(self.server.gets), 1)

    def test_invalid_input_is_rejected_before_output_or_network(self):
        for timestamps in (["2026-10-01T00:00:00", "2026-10-01T00:00:20Z"],
                           ["2026-10-01T00:00:00.1Z", "2026-10-01T00:00:00.9Z"],
                           ["2026-10-01T00:00:20Z", "2026-10-01T00:00:00Z"],
                           ["2099-10-01T00:00:00Z", "2099-10-01T00:00:20Z"]):
            with self.subTest(timestamps=timestamps):
                path = self.fixture([synthetic_record(stamp) for stamp in timestamps])
                with self.assertRaises(ValueError):
                    upload_flight.load_flight(path)
        bad = synthetic_record("2026-10-01T00:00:00Z")
        bad["temperature_c"] = "temperature_c"
        with self.assertRaisesRegex(ValueError, "numeric"):
            upload_flight.load_flight(self.fixture([bad]))

    def test_unavailable_values_remain_unavailable_after_cloud_roundtrip(self):
        record = synthetic_record("2026-10-01T00:00:00.123456Z")
        record.update(pm1_0=-1, oxidising_raw=-1, nh3_raw=float("nan"), nh3_index=-1)
        update = make_update(record, whole_seconds=True)
        self.assertNotIn("field4", update)
        self.assertNotIn("NaN", update["status"])
        self.server.store(update)
        path = self.directory / "missing.csv"
        upload_flight.write_export(path, self.server.entries)
        frame = analyse.clean_dataset(analyse.convert_timestamps(analyse.load_dataset(path)))
        for field in ("pm1_0", "oxidising_raw", "nh3_raw", "nh3_index"):
            self.assertTrue(pd.isna(frame.loc[0, field]))
        self.assertEqual(frame.loc[0, "timestamp_utc"].microsecond, 123456)

    def test_corrupt_metadata_does_not_silently_reassign_timestamps(self):
        update = make_update(synthetic_record("2026-10-01T00:00:00Z"))
        metadata = json.loads(update["status"])
        metadata["ts"] = "2026-10-02T00:00:00Z"
        update["status"] = json.dumps(metadata)
        path = self.directory / "corrupt.csv"
        upload_flight.write_export(path, [update])
        with self.assertRaisesRegex(ValueError, "timestamp"):
            analyse.load_dataset(path)

    def test_live_upload_uses_same_full_measurement_metadata_as_recovery(self):
        record = synthetic_record("2026-10-01T00:00:00.123456Z")
        uploader = readings.ThingSpeakUploader(api_key="private-write-key")
        with patch.object(requests, "post", return_value=Mock(ok=True, text="123")) as post:
            self.assertTrue(uploader.upload(readings.SensorReading(**record)))
        live = post.call_args.kwargs["data"]
        recovery = make_update(record, whole_seconds=True)
        self.assertEqual(live["status"], recovery["status"])
        self.assertTrue(upload_flight.matching_entry(recovery, live))

    def test_cli_default_preview_requires_no_requests_import_or_credentials(self):
        output = self.directory / "preview"
        original_import = __import__
        def guard_import(name, *args, **kwargs):
            if name == "requests":
                raise AssertionError("Networking imported in preview")
            return original_import(name, *args, **kwargs)
        with patch("builtins.__import__", side_effect=guard_import), patch.dict(os.environ, {}, clear=True), \
             contextlib.redirect_stdout(io.StringIO()):
            code = upload_flight.main(["--input", str(HIL_CSV), "--output-dir", str(output)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads((output / "report.json").read_text())["result"], "PREVIEW ONLY")
        self.assertFalse((output / "thingspeak_verified.csv").exists())
        self.assertEqual((output / "source_readings.csv").read_bytes(), HIL_CSV.read_bytes())

    def test_upload_cli_writes_only_verified_export_and_key_free_report(self):
        output = self.directory / "upload"
        with patch.object(upload_flight, "ThingSpeakClient", return_value=self.client), \
             patch.dict(os.environ, {"THINGSPEAK_WRITE_API_KEY": "private-write-key",
                                     "THINGSPEAK_READ_API_KEY": "private-read-key"}), \
             contextlib.redirect_stdout(io.StringIO()):
            code = upload_flight.main(["--upload", "--channel-id", "3429238", "--input", str(HIL_CSV),
                                       "--output-dir", str(output)])
        self.assertEqual(code, 0)
        report = json.loads((output / "report.json").read_text())
        self.assertEqual(report["result"], "PASS")
        self.assertEqual(report["verified"], 15)
        self.assertEqual(len(pd.read_csv(output / "thingspeak_verified.csv")), 15)
        self.assertEqual((output / "source_readings.csv").read_bytes(), HIL_CSV.read_bytes())
        for path in output.iterdir():
            self.assertNotIn("private-write-key", path.read_text())
            self.assertNotIn("private-read-key", path.read_text())

    def test_upload_cli_failure_never_leaves_success_export_or_key_in_error(self):
        output = self.directory / "failed-upload"
        self.server.bad_read_key = True
        with patch.object(upload_flight, "ThingSpeakClient", return_value=self.client), \
             patch.dict(os.environ, {"THINGSPEAK_WRITE_API_KEY": "private-write-key",
                                     "THINGSPEAK_READ_API_KEY": "private-read-key"}), \
             contextlib.redirect_stdout(io.StringIO()) as captured:
            code = upload_flight.main(["--upload", "--channel-id", "3429238", "--input", str(HIL_CSV),
                                       "--output-dir", str(output)])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads((output / "report.json").read_text())["result"], "FAIL")
        self.assertFalse((output / "thingspeak_verified.csv").exists())
        self.assertNotIn("private-read-key", captured.getvalue())
        self.assertNotIn("private-read-key", (output / "report.json").read_text())


if __name__ == "__main__":
    unittest.main()
