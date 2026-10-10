"""Shared, hardware-free mapping for live telemetry, flight replay and analysis."""

import json
import math
from datetime import datetime, timezone


FIELD_MAP = {
    "field1": "temperature_c", "field2": "humidity_pct",
    "field3": "pressure_hpa", "field4": "pm1_0", "field5": "pm2_5",
    "field6": "pm10", "field7": "oxidising_index", "field8": "reducing_index",
}
EXTRA_MAP = {
    "lux": "light_lux", "ox": "oxidising_raw", "red": "reducing_raw",
    "nh3": "nh3_raw", "nh3i": "nh3_index",
}
LOCAL_HEADER = [
    "timestamp_utc", "temperature_c", "humidity_pct", "pressure_hpa", "light_lux",
    "pm1_0", "pm2_5", "pm10", "oxidising_raw", "reducing_raw", "nh3_raw",
    "oxidising_index", "reducing_index", "nh3_index",
]
EXPORT_HEADER = ["created_at", "entry_id", *FIELD_MAP, "status"]


def utc_timestamp(value):
    """Require an explicit UTC offset; never assume the laptop's local timezone."""
    if not isinstance(value, str):
        raise ValueError("Timestamp must be a UTC ISO 8601 string")
    try:
        stamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Timestamp must be a UTC ISO 8601 string") from None
    if stamp.utcoffset() is None or stamp.utcoffset().total_seconds() != 0:
        raise ValueError("Timestamp must have an explicit UTC offset")
    return stamp.astimezone(timezone.utc)


def timestamp_key(value):
    # Use whole-second channel identity; original microseconds live in status.
    return utc_timestamp(value).replace(microsecond=0)


def finite_number(value):
    if value is None or value == "":
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def make_update(record, whole_seconds=False):
    stamp = utc_timestamp(record["timestamp_utc"])
    update = {"created_at": (stamp.replace(microsecond=0) if whole_seconds else stamp).isoformat()}
    for field, column in FIELD_MAP.items():
        value = finite_number(record[column])
        if value is not None and (field in ("field1", "field2", "field3") or value >= 0):
            update[field] = value
    # All five extra measurements, even unavailable sentinels, stay attached to
    # the same entry. No second channel or changed field numbering is needed.
    status = {"atmo": 1, "ts": stamp.isoformat()}
    status.update({key: finite_number(record[column]) for key, column in EXTRA_MAP.items()})
    update["status"] = json.dumps(status, separators=(",", ":"), allow_nan=False)
    return update


def decode_status(value, created_at=None):
    """Ignore unrelated status text, but refuse corrupted atmospheric metadata."""
    if not isinstance(value, str):
        return {}
    try:
        metadata = json.loads(value)
    except (ValueError, TypeError):
        if '"atmo"' in value:
            raise ValueError("Malformed atmospheric status metadata") from None
        return {}
    if not isinstance(metadata, dict) or "atmo" not in metadata:
        return {}
    if type(metadata["atmo"]) is not int or metadata["atmo"] != 1:
        raise ValueError("Unsupported atmospheric status version")
    if not {"ts", *EXTRA_MAP}.issubset(metadata):
        raise ValueError("Incomplete atmospheric status metadata")
    stamp = utc_timestamp(metadata["ts"])
    if created_at is not None and timestamp_key(created_at) != stamp.replace(microsecond=0):
        raise ValueError("Status timestamp does not match its ThingSpeak entry")
    result = {"timestamp_utc": stamp.isoformat()}
    for key, column in EXTRA_MAP.items():
        value = metadata[key]
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
            raise ValueError("Invalid measurement in atmospheric status metadata")
        result[column] = value
    return result
