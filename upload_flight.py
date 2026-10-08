#!/usr/bin/env python3
"""Replay a stopped logger's CSV, verifying historical entries before success.

Default operation is an offline preview. Only --upload enables network writes.
Credentials come from the environment and are never included in output files.
"""

import argparse
import csv
import hashlib
import io
import json
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from thingspeak_schema import (
    EXPORT_HEADER, FIELD_MAP, LOCAL_HEADER, decode_status, finite_number,
    make_update, timestamp_key, utc_timestamp,
)

ROOT = Path(__file__).resolve().parent
BATCH_SIZE = 960
READ_LIMIT = 8000
WRITE_INTERVAL = 16  # ThingSpeak requires at least 15 seconds between bulk calls.
FIELD_LABELS = {
    "field1": {"temperature", "temperaturec", "temp"},
    "field2": {"humidity", "humiditypct"},
    "field3": {"pressure", "pressurehpa"},
    "field4": {"pm1", "pm10"},
    "field5": {"pm25"},
    "field6": {"pm10", "pm100"},
    "field7": {"oxidisingindex"},
    "field8": {"reducingindex"},
}


class UploadError(ValueError):
    """A safe operator message: never embed an HTTP body, URL or credentials."""

    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable


def load_flight(path, now=None):
    """Read one immutable snapshot and validate the entire file before writing."""
    source = path.read_bytes()
    reader = csv.reader(io.StringIO(source.decode("utf-8-sig"), newline=""))
    if next(reader, None) != LOCAL_HEADER:
        raise ValueError("Input must be the logger's 14-column readings.csv, not a cloud export")
    updates = []
    previous = None
    current_time = now or datetime.now(timezone.utc)
    for line, row in enumerate(reader, start=2):
        if len(row) != len(LOCAL_HEADER):
            raise ValueError(f"CSV line {line}: incorrect number of columns")
        record = dict(zip(LOCAL_HEADER, row))
        try:
            stamp = utc_timestamp(record["timestamp_utc"])
            update = make_update(record, whole_seconds=True)
        except (ValueError, TypeError, OverflowError):
            raise ValueError(f"CSV line {line}: invalid UTC timestamp or numeric measurement") from None
        key = stamp.replace(microsecond=0)
        if previous is not None and key <= previous:
            raise ValueError(f"CSV line {line}: timestamps must increase with at most one sample per second; "
                             "check for duplicate rows or a clock correction")
        if stamp > current_time + timedelta(minutes=1):
            raise ValueError(f"CSV line {line}: sample time is in the future; check the Pi/recovery computer clock")
        previous = key
        updates.append(update)
    if not updates:
        raise ValueError("No readings in the input CSV")
    return source, updates


def write_export(path, entries):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=EXPORT_HEADER, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(entries)


def matching_entry(expected, actual):
    """Compare every stored measurement, including metadata, without rounding."""
    if timestamp_key(expected["created_at"]) != timestamp_key(actual.get("created_at")):
        return False
    for field in FIELD_MAP:
        try:
            value = finite_number(actual.get(field))
        except (ValueError, TypeError, OverflowError):
            return False
        wanted = expected.get(field)
        if wanted is None:
            if value is not None:
                return False
        elif value is None or not math.isclose(wanted, value, rel_tol=1e-9, abs_tol=1e-7):
            return False
    try:
        # This also proves that status was returned and belongs to this entry.
        return bool(decode_status(actual.get("status"), actual["created_at"])) and (
            json.loads(expected["status"]) == json.loads(actual["status"]))
    except (ValueError, TypeError, KeyError):
        return False


def partition_updates(updates, feeds):
    by_time = {}
    for feed in feeds:
        key = timestamp_key(feed.get("created_at"))
        if key in by_time:
            raise UploadError("Channel has multiple entries in the same second; use a separate flight channel")
        by_time[key] = feed
    pending, verified = [], []
    for update in updates:
        existing = by_time.get(timestamp_key(update["created_at"]))
        if existing is None:
            pending.append(update)
        elif matching_entry(update, existing):
            verified.append(existing)
        else:
            raise UploadError(f"Conflicting channel entry at {update['created_at']}. "
                              "Existing data was preserved. Use a separate channel or analyse the local CSV")
    return pending, verified


class ThingSpeakClient:
    def __init__(self, channel_id, write_key, read_key, session=None, sleep=time.sleep, clock=time.monotonic):
        import requests
        self.requests = requests
        self.session = session or requests.Session()
        self.channel_id = channel_id
        self.write_key = write_key
        self.read_key = read_key
        self.sleep = sleep
        self.clock = clock
        # Wait on the first write too: a previous invocation may have just sent
        # a batch. Stop live acquisition and other writers during recovery.
        self.last_write = clock()
        self.checked_fields = False

    def _request_json(self, method, endpoint, **kwargs):
        try:
            response = self.session.request(
                method, f"https://api.thingspeak.com/channels/{self.channel_id}/{endpoint}",
                timeout=(3, 30), allow_redirects=False, **kwargs,
            )
        except self.requests.RequestException as error:
            raise UploadError(f"ThingSpeak request failed ({type(error).__name__}); rerun to resume", True) from None
        if not response.ok:
            raise UploadError(f"ThingSpeak returned HTTP {response.status_code}; check keys, channel and quota",
                              response.status_code == 429 or response.status_code >= 500)
        try:
            return response.json()
        except ValueError:
            raise UploadError("ThingSpeak returned invalid JSON; rerun to resume", True) from None

    def _read_window(self, start, end):
        for attempt in range(3):
            try:
                data = self._request_json("GET", "feeds.json", params={
                    "api_key": self.read_key, "start": start.strftime("%Y-%m-%d %H:%M:%S"),
                    "end": end.strftime("%Y-%m-%d %H:%M:%S"), "timezone": "UTC",
                    "results": READ_LIMIT, "status": "true",
                })
                break
            except UploadError as error:
                if not error.retryable or attempt == 2:
                    raise
                self.sleep(3 * (attempt + 1))
        if not isinstance(data, dict) or not isinstance(data.get("channel"), dict) or not isinstance(data.get("feeds"), list):
            raise UploadError("Cannot read channel; check its Read API key")
        if data["channel"].get("id") != self.channel_id:
            raise UploadError("Read response belongs to a different channel")
        if not self.checked_fields:
            for field, names in FIELD_LABELS.items():
                label = re.sub(r"[^a-z0-9]", "", str(data["channel"].get(field, "")).lower())
                if label not in names:
                    raise UploadError(f"Channel {field} must be enabled and named for {FIELD_MAP[field]}; "
                                      "check the mapping before uploading")
            self.checked_fields = True
        for feed in data["feeds"]:
            if not isinstance(feed, dict) or not isinstance(feed.get("entry_id"), int) or feed["entry_id"] <= 0:
                raise UploadError("Invalid entry in channel read response")
            try:
                stamp = timestamp_key(feed.get("created_at"))
            except ValueError:
                raise UploadError("Invalid timestamp in channel read response") from None
            if not start <= stamp <= end:
                raise UploadError("Channel read returned data outside the requested UTC range")
        return data["feeds"]

    def read_range(self, start, end):
        """Split saturated windows so the API's 8000-row cap cannot truncate a flight."""
        windows = [(start, end)]
        by_id = {}
        while windows:
            low, high = windows.pop()
            feeds = self._read_window(low, high)
            if len(feeds) >= READ_LIMIT:
                span = int((high - low).total_seconds())
                if span < 2:
                    raise UploadError("Too many channel entries in a single second to verify safely")
                midpoint = low + timedelta(seconds=span // 2)
                # Overlap boundaries to accommodate inclusive/exclusive ends.
                windows.extend([(low, midpoint), (midpoint, high)])
                continue
            for feed in feeds:
                by_id[feed["entry_id"]] = feed
        return sorted(by_id.values(), key=lambda entry: timestamp_key(entry["created_at"]))

    def write_batch(self, updates):
        if not 1 <= len(updates) <= BATCH_SIZE:
            raise ValueError("Bulk batches must contain 1 to 960 entries")
        self.sleep(max(0, WRITE_INTERVAL - (self.clock() - self.last_write)))
        try:
            data = self._request_json("POST", "bulk_update.json",
                                      json={"write_api_key": self.write_key, "updates": updates})
        finally:
            self.last_write = self.clock()
        if not isinstance(data, dict) or data.get("success") is not True:
            raise UploadError("ThingSpeak did not acknowledge the bulk upload", True)


def read_updates(client, updates):
    return client.read_range(timestamp_key(updates[0]["created_at"]),
                             timestamp_key(updates[-1]["created_at"]) + timedelta(seconds=1))


def replay(client, updates, progress=print):
    # Check the entire flight for conflicts before sending the first batch.
    pending, existing = partition_updates(updates, read_updates(client, updates))
    already = len(existing)
    progress(f"Already verified: {already}; waiting to upload: {len(pending)}")
    batches = 0
    for offset in range(0, len(pending), BATCH_SIZE):
        batch = pending[offset:offset + BATCH_SIZE]
        for attempt in range(3):
            remaining, _ = partition_updates(batch, read_updates(client, batch))
            if not remaining:
                break
            write_error = None
            try:
                client.write_batch(remaining)
                batches += 1
            except UploadError as error:
                write_error = error
            # A timeout can occur after the server has stored the batch. Always
            # read it back before retrying; HTTP 200 alone proves too little.
            remaining, _ = partition_updates(batch, read_updates(client, batch))
            if not remaining:
                break
            if write_error is not None and not write_error.retryable:
                raise write_error
            if attempt == 2:
                raise UploadError(f"{len(remaining)} entries remain unverified; keep the CSV and rerun to resume")
        progress(f"Verified {min(offset + BATCH_SIZE, len(pending))} / {len(pending)} recovery readings")
    remaining, verified = partition_updates(updates, read_updates(client, updates))
    if remaining:
        raise UploadError("Final read-back is incomplete; keep the CSV and rerun to resume")
    return verified, {"verified": len(verified), "already_present": already,
                      "recovered": len(verified) - already, "acknowledged_batches": batches}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", "-i", type=Path, default=ROOT / "data" / "readings.csv")
    parser.add_argument("--output-dir", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--upload", action="store_true", help="Write to ThingSpeak and verify every entry")
    mode.add_argument("--dry-run", action="store_true", help="Offline preview only (the default)")
    parser.add_argument("--channel-id", type=int, help="Required with --upload")
    args = parser.parse_args(argv)
    report_path = None
    report = {"mode": "upload" if args.upload else "dry-run", "result": "INCOMPLETE"}
    try:
        source, updates = load_flight(args.input)
        first = decode_status(updates[0]["status"])["timestamp_utc"]
        last = decode_status(updates[-1]["status"])["timestamp_utc"]
        print(f"Readings: {len(updates)}; UTC range: {first} -> {last}")
        print("Fields 1-8 keep the existing mapping; status carries light, raw gas, NH3 index and precise time.")
        report.update({"source": str(args.input.resolve()), "source_sha256": hashlib.sha256(source).hexdigest(),
                       "rows": len(updates), "start_utc": first, "end_utc": last,
                       "channel_id": args.channel_id})
        if args.upload:
            if not args.channel_id or args.channel_id <= 0:
                raise ValueError("--upload requires a positive --channel-id")
            write_key = os.environ.get("THINGSPEAK_WRITE_API_KEY", "").strip()
            read_key = os.environ.get("THINGSPEAK_READ_API_KEY", "").strip()
            if not write_key or not read_key:
                raise ValueError("Set THINGSPEAK_WRITE_API_KEY and THINGSPEAK_READ_API_KEY for verified recovery")
        output = args.output_dir or ROOT / "data" / ("upload-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f"))
        # A new output directory prevents stale success/export files surviving
        # a failed rerun. The channel itself supplies the restart checkpoint.
        output.mkdir(parents=True, exist_ok=False)
        report_path = output / "report.json"
        (output / "source_readings.csv").write_bytes(source)
        write_export(output / "prepared_thingspeak.csv", updates)
        # The full UTC interval and key-free MATLAB setup can be pasted directly
        # into the visualization. Account keys are supplied separately there.
        with (output / "matlab_flight_window.txt").open("w", encoding="utf-8") as handle:
            handle.write(f"readChannelID = {args.channel_id or 3429238};\n")
            handle.write(f"flightStartUTC = '{timestamp_key(first).strftime('%Y-%m-%d %H:%M:%S')}';\n")
            handle.write(f"flightEndUTC = '{(timestamp_key(last) + timedelta(seconds=1)).strftime('%Y-%m-%d %H:%M:%S')}';\n")
        if args.upload:
            client = ThingSpeakClient(args.channel_id, write_key, read_key)
            try:
                verified, counts = replay(client, updates)
            finally:
                client.session.close()
            write_export(output / "thingspeak_verified.csv", verified)
            report.update(counts)
            report["result"] = "PASS"
            print(f"PASS: All {len(updates)} flight readings were read back and matched.")
        else:
            report["result"] = "PREVIEW ONLY"
            print("PREVIEW ONLY: No network connection or upload was attempted.")
        print(f"Results: {output.resolve()}")
        return 0
    except (OSError, ValueError, ImportError) as error:
        # Network failures are converted to safe UploadError messages above.
        report["error"] = str(error)
        report["result"] = "FAIL"
        print(f"ERROR: {error}")
        return 1
    except KeyboardInterrupt:
        report["result"] = "INTERRUPTED"
        print("Interrupted. Keep the CSV; rerun to verify and resume.")
        return 130
    finally:
        if report_path is not None:
            report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
