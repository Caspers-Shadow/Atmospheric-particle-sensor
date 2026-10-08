#!/usr/bin/env python3
"""
readings.py
============

Atmospheric Monitoring System - Sensor Acquisition, LCD Visualization,
and ThingSpeak Upload Layer.

Hardware target: Raspberry Pi 5 + Pimoroni Enviro+ (BME280, LTR559,
MICS6814, ST7735 LCD) + PMS5003 particulate matter sensor.

This module is organised into the layers required by the PRD:

    - Sensor acquisition layer   -> SensorManager
    - Gas interpretation layer   -> GasIndexCalculator
    - LCD visualization layer    -> LCDDisplay
    - ThingSpeak integration     -> ThingSpeakUploader
    - Reporting / console output -> print_console_report / write_csv_row
    - Orchestration              -> main()

IMPORTANT (per PRD "Important Limitations"):
This system does NOT provide laboratory-grade gas concentration
measurements. Gas readings are expressed only as normalized 0-100
relative indices, derived from raw MICS6814 sensor resistance. They
are intended for relative trend analysis only, not absolute ppm
accuracy - especially at stratospheric altitude (~30 km) where the
sensor has not been characterised.
"""

import argparse
import csv
import logging
import math
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass, asdict, fields, replace
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from thingspeak_schema import FIELD_MAP, make_update

# ---------------------------------------------------------------------------
# Hardware library imports
#
# These are the Pimoroni libraries used on real Enviro+ / PMS5003 hardware.
# They are imported lazily / defensively so that the analysis-only parts of
# this project (and basic syntax checking) can run on a machine without the
# physical sensors attached. On the actual Raspberry Pi 5 payload computer,
# make sure these packages are installed (see requirements.txt / README).
# ---------------------------------------------------------------------------
# Hardware is imported only when SensorManager / LCDDisplay are constructed.
# CLI help, offline upload checks and analysis do not initialise GPIO or UART.


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

THINGSPEAK_URL = "https://api.thingspeak.com/update"

UPLOAD_INTERVAL_SECONDS = 20        # Full acquisition and local logging cadence.
LCD_PAGE_DWELL_SECONDS = 0.1         # LCD navigation + cheap I2C sensor refresh.

DATA_DIR = Path(__file__).resolve().parent / "data"
CSV_FILE = DATA_DIR / "readings.csv"
LOG_FILE = DATA_DIR / "atmo_system.log"

# Gas index calibration baselines (raw sensor resistance, ohms).
# These represent a "clean air" baseline captured during sensor warm-up /
# ground calibration. They should be re-measured for each physical sensor
# unit and flight, and are NOT laboratory-calibrated values - see PRD
# limitations. Override via environment variables if a fresh calibration
# has been performed before launch.
GAS_BASELINE_OXIDISING = 20000
GAS_BASELINE_REDUCING = 200000
GAS_BASELINE_NH3 = 200000


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def configure_logging(log_path=LOG_FILE):
    logger = logging.getLogger("atmo_system")
    logger.setLevel(logging.DEBUG)

    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    file_handler = RotatingFileHandler(
        log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.WARNING)

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    )
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger


logger = logging.getLogger("atmo_system")


# ---------------------------------------------------------------------------
# Data container for a single reading cycle
# ---------------------------------------------------------------------------

@dataclass
class SensorReading:
    timestamp_utc: str
    temperature_c: float
    humidity_pct: float
    pressure_hpa: float
    light_lux: float
    pm1_0: float
    pm2_5: float
    pm10: float
    oxidising_raw: float
    reducing_raw: float
    nh3_raw: float
    oxidising_index: float
    reducing_index: float
    nh3_index: float

    def as_csv_row(self):
        return asdict(self)


# ---------------------------------------------------------------------------
# Gas interpretation layer
# ---------------------------------------------------------------------------

class GasIndexCalculator:
    """
    Converts raw MICS6814 sensor element resistance into a normalized
    0-100 relative index, per the PRD's "Gas Interpretation Model".

    This is intentionally NOT a ppm calculation. Index values only
    indicate relative deviation from a clean-air baseline:

        - Higher Oxidising index  -> more NO2-like oxidising gas present
        - Higher Reducing index   -> more CO / H2 / CH4 / ethanol-like
                                      reducing gas present
        - Higher NH3 index        -> more NH3 / propane / iso-butane-like
                                      gas present

    The MICS6814 oxidising element INCREASES resistance in the presence
    of oxidising gases, while the reducing and NH3 elements DECREASE
    resistance in the presence of their target gases. The index formulas
    below reflect that directionality relative to the calibrated
    baseline resistance.
    """

    def __init__(self, baseline_ox=None, baseline_red=None, baseline_nh3=None):
        baseline_ox = float(os.environ.get("GAS_BASELINE_OXIDISING", GAS_BASELINE_OXIDISING)
                            if baseline_ox is None else baseline_ox)
        baseline_red = float(os.environ.get("GAS_BASELINE_REDUCING", GAS_BASELINE_REDUCING)
                             if baseline_red is None else baseline_red)
        baseline_nh3 = float(os.environ.get("GAS_BASELINE_NH3", GAS_BASELINE_NH3)
                             if baseline_nh3 is None else baseline_nh3)
        self.baseline_ox = baseline_ox
        self.baseline_red = baseline_red
        self.baseline_nh3 = baseline_nh3
        if any(not math.isfinite(value) or value <= 0 for value in
               (baseline_ox, baseline_red, baseline_nh3)):
            raise ValueError("Gas baselines must be finite, positive resistances")

    @staticmethod
    def _clamp(value, low=0.0, high=100.0):
        return max(low, min(high, value))

    def oxidising_index(self, raw_ohms):
        # Resistance rises with oxidising gas concentration.
        if not math.isfinite(raw_ohms) or raw_ohms <= 0:
            return -1.0
        ratio = (raw_ohms - self.baseline_ox) / self.baseline_ox
        return self._clamp(ratio * 100.0)

    def reducing_index(self, raw_ohms):
        # Resistance falls with reducing gas concentration.
        if not math.isfinite(raw_ohms) or raw_ohms <= 0:
            return -1.0
        ratio = (self.baseline_red - raw_ohms) / self.baseline_red
        return self._clamp(ratio * 100.0)

    def nh3_index(self, raw_ohms):
        # Resistance falls with NH3 / propane / iso-butane concentration.
        if not math.isfinite(raw_ohms) or raw_ohms <= 0:
            return -1.0
        ratio = (self.baseline_nh3 - raw_ohms) / self.baseline_nh3
        return self._clamp(ratio * 100.0)

    def confidence_label(self, oxidising_idx, reducing_idx, nh3_idx):
        """
        A coarse, non-scientific confidence label for the console gas
        report. Per PRD limitations, this system cannot claim laboratory
        accuracy, so confidence is always capped at LOW/MEDIUM.
        """
        if min(oxidising_idx, reducing_idx, nh3_idx) < 0:
            return "UNAVAILABLE"
        spread = max(oxidising_idx, reducing_idx, nh3_idx) - min(
            oxidising_idx, reducing_idx, nh3_idx
        )
        if spread > 40:
            return "MEDIUM"
        return "LOW"

    def full_gas_report(self, oxidising_idx, reducing_idx, nh3_idx):
        """
        Produces the sub-gas index breakdown shown in the PRD's example
        GAS REPORT. Because the MICS6814 only exposes three sensing
        elements, individual gas rows within the same element share
        that element's index value - this system cannot distinguish
        between gases sharing a sensing element (documented limitation).
        """
        return {
            "NO2 Index": round(oxidising_idx),
            "CO Index": round(reducing_idx),
            "H2 Index": round(reducing_idx),
            "NH3 Index": round(nh3_idx),
            "CH4 Index": round(reducing_idx),
            "C3H8 Index": round(nh3_idx),
            "C4H10 Index": round(nh3_idx),
            "Ethanol Index": round(reducing_idx),
        }


# ---------------------------------------------------------------------------
# Sensor acquisition layer
# ---------------------------------------------------------------------------

class SensorManager:
    """
    Wraps all physical sensor interactions: BME280 (temp/humidity/
    pressure), LTR559 (light/proximity), MICS6814 (gas, via the
    enviroplus library), and PMS5003 (particulate matter over UART).
    """

    def __init__(self):
        try:
            from bme280 import BME280
            from smbus2 import SMBus
            from ltr559 import LTR559
            from enviroplus import gas
            from pms5003 import PMS5003, ReadTimeoutError
        except ImportError as import_error:
            raise RuntimeError(
                f"Required hardware libraries are not installed: {import_error}. "
                "See requirements.txt / README for setup on the Raspberry Pi."
            ) from import_error

        self._gas = gas
        self._pms_timeout = ReadTimeoutError
        self._bus = SMBus(1)
        self.bme280 = BME280(i2c_dev=self._bus)
        self.ltr559 = LTR559()
        self.pms5003 = PMS5003()
        self.gas_calc = GasIndexCalculator()

        # PMS5003 needs a short warm-up before reads are meaningful.
        logger.info("Warming up PMS5003 particulate sensor...")
        time.sleep(2)

    def read_environment(self):
        """Read temperature, humidity, pressure, light. Cheap I2C reads -
        safe to call on every fast tick."""
        temperature_c = self.bme280.get_temperature()
        pressure_hpa = self.bme280.get_pressure()
        humidity_pct = self.bme280.get_humidity()
        light_lux = self.ltr559.get_lux()
        return temperature_c, humidity_pct, pressure_hpa, light_lux

    def read_particulates(self, retries=3):
        """
        Read PM1.0 / PM2.5 / PM10 from the PMS5003, tolerating sensor
        timeouts as required by the PRD ("Recover from PMS5003 timeouts").

        Keep PM reads at the 20-second logging cadence so timeout retries
        do not run on every LCD tick. The sensor library handles UART frames.

        Returns (pm1_0, pm2_5, pm10) as floats, using -1 as a sentinel for
        "sensor unavailable this cycle" (matches the proven reference
        implementation) so downstream consumers always get a valid number.
        """
        for attempt in range(1, retries + 1):
            try:
                data = self.pms5003.read()
                return (
                    float(data.pm_ug_per_m3(1.0)),
                    float(data.pm_ug_per_m3(2.5)),
                    float(data.pm_ug_per_m3(10)),
                )
            except self._pms_timeout:
                logger.warning(
                    "PMS5003 read timeout (attempt %d/%d)", attempt, retries
                )
                time.sleep(0.5)
            except Exception:
                logger.exception("Unexpected PMS5003 read failure")
                time.sleep(0.5)
        logger.error("PMS5003 read failed after %d attempts; using -1 sentinel", retries)
        return -1.0, -1.0, -1.0

    def read_gas_raw(self):
        """Read raw MICS6814 element resistances (ohms) via enviroplus.gas.
        Read this at the local logging cadence, independently of networking."""
        try:
            readings = self._gas.read_all()
            return readings.oxidising, readings.reducing, readings.nh3
        except Exception:
            logger.exception("Gas sensor read failure; returning -1 sentinels")
            return -1.0, -1.0, -1.0

    def take_fast_reading(self):
        """
        Cheap I2C-only reading (temperature/humidity/pressure/light) safe
        to call many times per second for LCD navigation ticks. Does NOT
        touch the PMS5003 or gas sensor.
        """
        temperature_c, humidity_pct, pressure_hpa, light_lux = self.read_environment()
        return temperature_c, humidity_pct, pressure_hpa, light_lux

    def take_reading(self):
        """
        Assemble a full SensorReading for one acquisition/upload cycle.
        Call this at the local logging cadence; LCD ticks refresh only I2C data.
        """
        temperature_c, humidity_pct, pressure_hpa, light_lux = self.read_environment()
        pm1_0, pm2_5, pm10 = self.read_particulates()
        ox_raw, red_raw, nh3_raw = self.read_gas_raw()

        ox_idx = self.gas_calc.oxidising_index(ox_raw)
        red_idx = self.gas_calc.reducing_index(red_raw)
        nh3_idx = self.gas_calc.nh3_index(nh3_raw)

        return SensorReading(
            timestamp_utc=datetime.now(timezone.utc).isoformat(),
            temperature_c=round(temperature_c, 2),
            humidity_pct=round(humidity_pct, 2),
            pressure_hpa=round(pressure_hpa, 2),
            light_lux=round(light_lux, 2),
            pm1_0=pm1_0,
            pm2_5=pm2_5,
            pm10=pm10,
            oxidising_raw=round(ox_raw, 2),
            reducing_raw=round(red_raw, 2),
            nh3_raw=round(nh3_raw, 2),
            oxidising_index=round(ox_idx, 1),
            reducing_index=round(red_idx, 1),
            nh3_index=round(nh3_idx, 1),
        )

    def close(self):
        self._bus.close()


# ---------------------------------------------------------------------------
# LCD visualization layer
# ---------------------------------------------------------------------------

class LCDDisplay:
    """
    Drives the ST7735 LCD on the Enviro+, cycling through the ten
    required display pages. Navigation between pages is driven by the
    LTR559 proximity sensor, per PRD requirement.
    """

    PAGES = [
        "Temperature",
        "Pressure",
        "Humidity",
        "Light",
        "Oxidising Index",
        "Reducing Index",
        "NH3 Index",
        "PM1.0",
        "PM2.5",
        "PM10",
    ]

    PROXIMITY_TRIGGER = 1500  # empirically reasonable LTR559 proximity threshold

    def __init__(self, ltr559_sensor):
        import st7735
        from PIL import Image, ImageDraw, ImageFont

        self._image = Image
        self._draw = ImageDraw
        self.ltr559 = ltr559_sensor
        self.disp = st7735.ST7735(
            port=0,
            cs=1,
            dc="GPIO9",
            backlight="GPIO12",
            rotation=270,
            spi_speed_hz=10000000,
        )
        self.disp.begin()
        self.width = self.disp.width
        self.height = self.disp.height
        self.font = ImageFont.load_default()
        self.page_index = 0
        self._last_proximity_state = False

    def _check_navigation(self):
        """Advance the page if the proximity sensor is triggered."""
        proximity = self.ltr559.get_proximity()
        triggered = proximity > self.PROXIMITY_TRIGGER
        if triggered and not self._last_proximity_state:
            self.page_index = (self.page_index + 1) % len(self.PAGES)
        self._last_proximity_state = triggered

    def render(self, reading: SensorReading):
        """Draw the current page using the latest sensor reading."""
        self._check_navigation()

        label = self.PAGES[self.page_index]
        value_map = {
            "Temperature": f"{reading.temperature_c} C",
            "Pressure": f"{reading.pressure_hpa} hPa",
            "Humidity": f"{reading.humidity_pct} %",
            "Light": f"{reading.light_lux} Lux",
            "Oxidising Index": f"{reading.oxidising_index}",
            "Reducing Index": f"{reading.reducing_index}",
            "NH3 Index": f"{reading.nh3_index}",
            "PM1.0": f"{reading.pm1_0} ug/m3",
            "PM2.5": f"{reading.pm2_5} ug/m3",
            "PM10": f"{reading.pm10} ug/m3",
        }
        value_text = value_map.get(label, "N/A")

        image = self._image.new("RGB", (self.width, self.height), color=(0, 0, 0))
        draw = self._draw.Draw(image)
        draw.text((5, 30), label, font=self.font, fill=(255, 255, 255))
        draw.text((5, 60), value_text, font=self.font, fill=(0, 255, 120))
        self.disp.display(image)


# ---------------------------------------------------------------------------
# ThingSpeak integration layer
# ---------------------------------------------------------------------------

class ThingSpeakUploader:
    """
    Uploads a SensorReading to ThingSpeak using the field mapping
    defined in the PRD. Upload failures are logged and swallowed so the
    main acquisition loop keeps running (PRD: "Continue operating after
    upload failures").
    """

    def __init__(self, api_key=None, url=THINGSPEAK_URL, timeout=(3, 5), offline=False):
        self.api_key = (os.environ.get("THINGSPEAK_WRITE_API_KEY", "")
                        if api_key is None else api_key).strip()
        self.url = url
        self.timeout = timeout
        self.enabled = bool(self.api_key) and not offline
        self._next_attempt = 0.0
        self._retry_delay = UPLOAD_INTERVAL_SECONDS
        self._requests = None
        if self.enabled:
            import requests
            self._requests = requests
        else:
            logger.info("Cloud uploads disabled (%s); local logging continues",
                        "offline mode" if offline else "no API key configured")

    def upload(self, reading: SensorReading):
        if not self.enabled or time.monotonic() < self._next_attempt:
            return False
        payload = {"api_key": self.api_key, **make_update(reading.as_csv_row())}
        dropped = [field for field in FIELD_MAP if field not in payload]
        if dropped:
            logger.warning(
                "Omitting invalid fields from upload: %s",
                dropped
            )

        try:

            response = self._requests.post(
                self.url,
                data=payload,
                timeout=self.timeout,
                allow_redirects=False,
            )

            if not response.ok:

                logger.error(
                    "ThingSpeak upload failed: HTTP %s",
                    response.status_code,
                )
                self._back_off()
                return False

            entry_id = response.text.strip()

            if not entry_id.isascii() or not entry_id.isdigit() or int(entry_id) <= 0:

                logger.warning(
                    "ThingSpeak rejected update "
                    "(rate limit, invalid key or unexpected response)."
                )
                self._back_off()
                return False

            logger.info(
                "ThingSpeak upload successful. Entry ID=%s",
                entry_id
            )

            self._retry_delay = UPLOAD_INTERVAL_SECONDS
            self._next_attempt = time.monotonic() + UPLOAD_INTERVAL_SECONDS
            return True

        except self._requests.exceptions.RequestException as error:
            # Exception text can include request URLs or credentials. Log type only.
            logger.warning("ThingSpeak upload failed (%s); local logging continues",
                           type(error).__name__)
            self._back_off()
            return False

    def _back_off(self):
        self._next_attempt = time.monotonic() + self._retry_delay
        logger.info("Next upload attempt in at least %s seconds", self._retry_delay)
        self._retry_delay = min(300, self._retry_delay * 2)


class BackgroundUploader:
    """Keep network waits off the acquisition/LCD loop; retain latest sample only.

    Every sample is already on disk. This bounded queue is for live telemetry,
    not a durable backfill queue. A daemon worker also prevents a stalled DNS
    resolver from blocking operator shutdown.
    """

    def __init__(self, uploader):
        self.uploader = uploader
        self._pending = queue.Queue(maxsize=1)
        self._stop = threading.Event()
        self._thread = None
        if uploader.enabled:
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name="thingspeak-upload")
            self._thread.start()

    def submit(self, reading):
        if self._thread is None:
            return
        # The LCD refreshes its cached reading; the upload must be a snapshot.
        snapshot = replace(reading)
        try:
            self._pending.put_nowait(snapshot)
        except queue.Full:
            try:
                self._pending.get_nowait()
            except queue.Empty:
                pass
            self._pending.put_nowait(snapshot)

    def _run(self):
        while not self._stop.is_set():
            try:
                reading = self._pending.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self.uploader.upload(reading)
            except Exception as error:
                logger.error("Upload worker error (%s); local logging continues",
                             type(error).__name__)

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)


# ---------------------------------------------------------------------------
# Reporting layer - console output and local CSV storage
# ---------------------------------------------------------------------------

def print_console_report(reading: SensorReading,
                         gas_calc: GasIndexCalculator):

    print("===== SENSOR READINGS =====\n")

    print(f"Temperature : {reading.temperature_c} C")
    print(f"Humidity    : {reading.humidity_pct} %")
    print(f"Pressure    : {reading.pressure_hpa} hPa")
    print(f"Light       : {reading.light_lux} Lux\n")

    print(f"PM1.0       : {reading.pm1_0}")
    print(f"PM2.5       : {reading.pm2_5}")
    print(f"PM10        : {reading.pm10}\n")

    print(f"NO2 Index   : {round(reading.oxidising_index)}")
    print(f"CO Index    : {round(reading.reducing_index)}")
    print(f"NH3 Index   : {round(reading.nh3_index)}")

    print("\n===========================\n")

    gas_report = gas_calc.full_gas_report(
        reading.oxidising_index,
        reading.reducing_index,
        reading.nh3_index
    )

    confidence = gas_calc.confidence_label(
        reading.oxidising_index,
        reading.reducing_index,
        reading.nh3_index
    )

    print("===== GAS REPORT =====\n")

    for gas_name, value in gas_report.items():
        print(f"{gas_name:<15}: {value}")

    print(f"\nConfidence:\n{confidence}\n")
    print("=====================\n")


def validate_csv_header(csv_path):
    """Refuse to append logger rows to a ThingSpeak export / different schema."""
    path = Path(csv_path)
    if path.exists() and path.stat().st_size:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            header = next(csv.reader(handle), None)
        if header != [field.name for field in fields(SensorReading)]:
            raise ValueError(f"CSV header mismatch in {path}; use a new --data-dir")


def write_csv_row(reading: SensorReading, csv_path=CSV_FILE):
    """Append and flush a sample to disk before attempting a cloud upload."""
    validate_csv_header(csv_path)
    path = Path(csv_path)
    file_exists = path.exists() and path.stat().st_size > 0
    row = reading.as_csv_row()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=row.keys())
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)
        f.flush()
        os.fsync(f.fileno())


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def positive_seconds(value):
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than zero")
    return seconds


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read sensors and log locally; optionally upload.")
    parser.add_argument("--offline", action="store_true", help="Never attempt cloud requests")
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR,
                        help="Directory for readings.csv and rotating logs (default: repo/data)")
    parser.add_argument("--duration", type=positive_seconds,
                        help="Stop after this many seconds of acquisition (default: continuous)")
    parser.add_argument("--no-lcd", action="store_true", help="Run without LCD updates")
    args = parser.parse_args(argv)
    data_dir = args.data_dir.expanduser().resolve()
    csv_path = data_dir / "readings.csv"
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        configure_logging(data_dir / "atmo_system.log")
        validate_csv_header(csv_path)
        # Fail before hardware startup if the local output cannot be opened.
        with csv_path.open("a", encoding="utf-8"):
            pass
    except (OSError, ValueError) as error:
        print(f"FATAL: Cannot prepare local storage: {error}", file=sys.stderr)
        return 1

    print(f"Local readings: {csv_path}")
    logger.info("Starting atmospheric monitoring system; data directory=%s", data_dir)
    try:
        uploader = ThingSpeakUploader(offline=args.offline)
        sensors = SensorManager()
    except Exception as error:
        logger.exception("Startup failed")
        print(f"FATAL: {error}", file=sys.stderr)
        return 1

    lcd = None
    if not args.no_lcd:
        try:
            lcd = LCDDisplay(sensors.ltr559)
        except Exception:
            logger.exception("LCD unavailable; continuing without display output")

    worker = BackgroundUploader(uploader)
    started = time.monotonic()
    next_reading_time = started
    latest_reading = None
    rows_written = 0
    logger.info("Entering main loop; cloud uploads %s", "enabled" if uploader.enabled else "disabled")
    try:
        while args.duration is None or time.monotonic() - started < args.duration:
            cycle_start = time.monotonic()
            if cycle_start >= next_reading_time:
                # Advance even on failure; avoid a sensor retry/log storm at 10 Hz.
                next_reading_time = cycle_start + UPLOAD_INTERVAL_SECONDS
                try:
                    latest_reading = sensors.take_reading()
                except Exception:
                    logger.exception("Sensor acquisition failed; retrying next scheduled cycle")
                else:
                    # Local storage is essential during an outage. Stop visibly if
                    # it fails instead of silently discarding the flight data.
                    try:
                        write_csv_row(latest_reading, csv_path)
                    except (OSError, ValueError):
                        logger.exception("FATAL: Local CSV write failed; stopping acquisition")
                        return 1
                    rows_written += 1
                    print_console_report(latest_reading, sensors.gas_calc)
                    worker.submit(latest_reading)

            if lcd is not None and latest_reading is not None:
                try:
                    temperature_c, humidity_pct, pressure_hpa, light_lux = sensors.take_fast_reading()
                    display_reading = replace(
                        latest_reading, temperature_c=round(temperature_c, 2),
                        humidity_pct=round(humidity_pct, 2), pressure_hpa=round(pressure_hpa, 2),
                        light_lux=round(light_lux, 2),
                    )
                    lcd.render(display_reading)
                except Exception:
                    logger.exception("LCD refresh failed; disabling display for this run")
                    lcd = None

            time.sleep(max(0.0, LCD_PAGE_DWELL_SECONDS - (time.monotonic() - cycle_start)))
    except KeyboardInterrupt:
        logger.info("Shutdown requested by operator")
    finally:
        worker.close()
        sensors.close()
        print(f"\nStopped. Saved {rows_written} readings to {csv_path}")
        logger.info("Stopped; saved %d readings", rows_written)
    if rows_written == 0:
        logger.error("No readings were saved during this run")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
