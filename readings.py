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

import csv
import logging
import os
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone

import requests

# ---------------------------------------------------------------------------
# Hardware library imports
#
# These are the Pimoroni libraries used on real Enviro+ / PMS5003 hardware.
# They are imported lazily / defensively so that the analysis-only parts of
# this project (and basic syntax checking) can run on a machine without the
# physical sensors attached. On the actual Raspberry Pi 5 payload computer,
# make sure these packages are installed (see requirements.txt / README).
# ---------------------------------------------------------------------------
try:
    from bme280 import BME280
    from smbus2 import SMBus
    from ltr559 import LTR559
    from enviroplus import gas
    from pms5003 import PMS5003, ReadTimeoutError as PMS5003ReadTimeoutError
    import ST7735
    from PIL import Image, ImageDraw, ImageFont
    HARDWARE_AVAILABLE = True
except ImportError as import_error:  # pragma: no cover - hardware not present
    HARDWARE_AVAILABLE = False
    _IMPORT_ERROR = import_error


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

THINGSPEAK_WRITE_API_KEY = os.environ.get("2FZWRBB132P5J6KX", "YOUR_WRITE_API_KEY")
THINGSPEAK_URL = "https://api.thingspeak.com/update"

UPLOAD_INTERVAL_SECONDS = 20        # ThingSpeak upload cadence (PRD requirement)
SENSOR_REFRESH_SECONDS = 1          # Sensor refresh must be < 1 second (PRD requirement)
LCD_PAGE_DWELL_SECONDS = 0.1        # LCD loop tick, navigation driven by proximity

CSV_FILE = "thingspeak_data.csv"
LOG_FILE = "atmo_system.log"

# Gas index calibration baselines (raw sensor resistance, ohms).
# These represent a "clean air" baseline captured during sensor warm-up /
# ground calibration. They should be re-measured for each physical sensor
# unit and flight, and are NOT laboratory-calibrated values - see PRD
# limitations. Override via environment variables if a fresh calibration
# has been performed before launch.
GAS_BASELINE_OXIDISING = float(os.environ.get("GAS_BASELINE_OXIDISING", 20000))
GAS_BASELINE_REDUCING = float(os.environ.get("GAS_BASELINE_REDUCING", 200000))
GAS_BASELINE_NH3 = float(os.environ.get("GAS_BASELINE_NH3", 200000))


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def configure_logging():
    logger = logging.getLogger("atmo_system")
    logger.setLevel(logging.DEBUG)

    file_handler = logging.FileHandler(LOG_FILE)
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


logger = configure_logging()


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

    def __init__(self, baseline_ox=GAS_BASELINE_OXIDISING,
                 baseline_red=GAS_BASELINE_REDUCING,
                 baseline_nh3=GAS_BASELINE_NH3):
        self.baseline_ox = baseline_ox
        self.baseline_red = baseline_red
        self.baseline_nh3 = baseline_nh3

    @staticmethod
    def _clamp(value, low=0.0, high=100.0):
        return max(low, min(high, value))

    def oxidising_index(self, raw_ohms):
        # Resistance rises with oxidising gas concentration.
        if raw_ohms <= 0 or self.baseline_ox <= 0:
            return 0.0
        ratio = (raw_ohms - self.baseline_ox) / self.baseline_ox
        return self._clamp(ratio * 100.0)

    def reducing_index(self, raw_ohms):
        # Resistance falls with reducing gas concentration.
        if raw_ohms <= 0 or self.baseline_red <= 0:
            return 0.0
        ratio = (self.baseline_red - raw_ohms) / self.baseline_red
        return self._clamp(ratio * 100.0)

    def nh3_index(self, raw_ohms):
        # Resistance falls with NH3 / propane / iso-butane concentration.
        if raw_ohms <= 0 or self.baseline_nh3 <= 0:
            return 0.0
        ratio = (self.baseline_nh3 - raw_ohms) / self.baseline_nh3
        return self._clamp(ratio * 100.0)

    def confidence_label(self, oxidising_idx, reducing_idx, nh3_idx):
        """
        A coarse, non-scientific confidence label for the console gas
        report. Per PRD limitations, this system cannot claim laboratory
        accuracy, so confidence is always capped at LOW/MEDIUM.
        """
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
        if not HARDWARE_AVAILABLE:
            raise RuntimeError(
                f"Required hardware libraries are not installed: {_IMPORT_ERROR}. "
                "See requirements.txt / README for setup on the Raspberry Pi."
            )

        self._bus = SMBus(1)
        self.bme280 = BME280(i2c_dev=self._bus)
        self.ltr559 = LTR559()
        self.pms5003 = PMS5003()
        self.gas_calc = GasIndexCalculator()

        # PMS5003 needs a short warm-up before reads are meaningful.
        logger.info("Warming up PMS5003 particulate sensor...")
        time.sleep(2)

    def read_environment(self):
        """Read temperature, humidity, pressure, light."""
        temperature_c = self.bme280.get_temperature()
        pressure_hpa = self.bme280.get_pressure()
        humidity_pct = self.bme280.get_humidity()
        light_lux = self.ltr559.get_lux()
        return temperature_c, humidity_pct, pressure_hpa, light_lux

    def read_particulates(self, retries=3):
        """
        Read PM1.0 / PM2.5 / PM10 from the PMS5003, tolerating sensor
        timeouts as required by the PRD ("Recover from PMS5003 timeouts").
        Returns (pm1_0, pm2_5, pm10) or (None, None, None) on repeated
        failure, so the caller can still upload the other measurements.
        """
        for attempt in range(1, retries + 1):
            try:
                data = self.pms5003.read()
                return (
                    data.pm_ug_per_m3(1.0),
                    data.pm_ug_per_m3(2.5),
                    data.pm_ug_per_m3(10),
                )
            except PMS5003ReadTimeoutError:
                logger.warning(
                    "PMS5003 read timeout (attempt %d/%d)", attempt, retries
                )
                time.sleep(0.5)
            except Exception:
                logger.exception("Unexpected PMS5003 read failure")
                time.sleep(0.5)
        logger.error("PMS5003 read failed after %d attempts; using None", retries)
        return None, None, None

    def read_gas_raw(self):
        """Read raw MICS6814 element resistances (ohms) via enviroplus.gas."""
        try:
            readings = gas.read_all()
            return readings.oxidising, readings.reducing, readings.nh3
        except Exception:
            logger.exception("Gas sensor read failure; returning zeros")
            return 0.0, 0.0, 0.0

    def take_reading(self):
        """Assemble a full SensorReading for one acquisition cycle."""
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
            pm1_0=pm1_0 if pm1_0 is not None else "",
            pm2_5=pm2_5 if pm2_5 is not None else "",
            pm10=pm10 if pm10 is not None else "",
            oxidising_raw=round(ox_raw, 2),
            reducing_raw=round(red_raw, 2),
            nh3_raw=round(nh3_raw, 2),
            oxidising_index=round(ox_idx, 1),
            reducing_index=round(red_idx, 1),
            nh3_index=round(nh3_idx, 1),
        )


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
        if not HARDWARE_AVAILABLE:
            raise RuntimeError("LCD hardware libraries are not available.")

        self.ltr559 = ltr559_sensor
        self.disp = ST7735.ST7735(
            port=0,
            cs=1,
            dc=9,
            backlight=12,
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

        image = Image.new("RGB", (self.width, self.height), color=(0, 0, 0))
        draw = ImageDraw.Draw(image)
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

    def __init__(self, api_key=THINGSPEAK_WRITE_API_KEY, url=THINGSPEAK_URL, timeout=10):
        self.api_key = api_key
        self.url = url
        self.timeout = timeout

    def upload(self, reading: SensorReading):
        payload = {
            "api_key": self.api_key,
            "field1": reading.temperature_c,
            "field2": reading.humidity_pct,
            "field3": reading.pressure_hpa,
            "field4": reading.pm1_0,
            "field5": reading.pm2_5,
            "field6": reading.pm10,
            "field7": reading.oxidising_index,
            "field8": reading.reducing_index,
        }
        try:
            response = requests.post(self.url, data=payload, timeout=self.timeout)
            response.raise_for_status()
            entry_id = response.text.strip()
            if entry_id == "0":
                logger.warning("ThingSpeak accepted the request but rejected the update "
                                "(rate limit or invalid key). Response: %s", entry_id)
                return False
            logger.info("Uploaded to ThingSpeak, entry_id=%s", entry_id)
            return True
        except requests.exceptions.RequestException:
            logger.exception("ThingSpeak upload failed; continuing operation")
            return False


# ---------------------------------------------------------------------------
# Reporting layer - console output and local CSV storage
# ---------------------------------------------------------------------------

def print_console_report(reading: SensorReading, gas_calc: GasIndexCalculator):
    print("===== SENSOR READINGS =====\n")
    print(f"Temperature : {reading.temperature_c} C")
    print(f"Humidity    : {reading.humidity_pct} %")
    print(f"Pressure    : {reading.pressure_hpa} hPa\n")
    print(f"PM1.0       : {reading.pm1_0}")
    print(f"PM2.5       : {reading.pm2_5}")
    print(f"PM10        : {reading.pm10}\n")
    print(f"Oxidising   : {round(reading.oxidising_index)}")
    print(f"Reducing    : {round(reading.reducing_index)}")
    print(f"NH3         : {round(reading.nh3_index)}\n")
    print(f"Light       : {reading.light_lux} Lux\n")
    print("===========================\n")

    gas_report = gas_calc.full_gas_report(
        reading.oxidising_index, reading.reducing_index, reading.nh3_index
    )
    confidence = gas_calc.confidence_label(
        reading.oxidising_index, reading.reducing_index, reading.nh3_index
    )
    print("===== GAS REPORT =====\n")
    for gas_name, value in gas_report.items():
        print(f"{gas_name:<15}: {value}")
    print(f"\nConfidence:\n{confidence}\n")
    print("=====================\n")


def write_csv_row(reading: SensorReading, csv_path=CSV_FILE):
    """Append a reading to the local CSV store, writing a header on first use."""
    file_exists = os.path.isfile(csv_path)
    row = reading.as_csv_row()
    with open(csv_path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=row.keys())
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def main():
    logger.info("Starting atmospheric monitoring system")

    try:
        sensors = SensorManager()
    except RuntimeError as e:
        logger.critical(str(e))
        print(f"FATAL: {e}")
        sys.exit(1)

    try:
        lcd = LCDDisplay(sensors.ltr559)
    except RuntimeError:
        logger.warning("LCD unavailable; continuing without display output")
        lcd = None

    uploader = ThingSpeakUploader()
    last_upload_time = 0.0

    logger.info("Entering main acquisition loop")
    while True:
        cycle_start = time.time()
        try:
            reading = sensors.take_reading()
        except Exception:
            logger.exception("Unhandled error during sensor acquisition; "
                              "skipping this cycle and continuing")
            time.sleep(SENSOR_REFRESH_SECONDS)
            continue

        print_console_report(reading, sensors.gas_calc)

        try:
            write_csv_row(reading)
        except Exception:
            logger.exception("Failed to write local CSV row")

        if lcd is not None:
            try:
                lcd.render(reading)
            except Exception:
                logger.exception("LCD render failure; continuing without display")

        now = time.time()
        if now - last_upload_time >= UPLOAD_INTERVAL_SECONDS:
            uploader.upload(reading)
            last_upload_time = now

        elapsed = time.time() - cycle_start
        sleep_time = max(0.0, SENSOR_REFRESH_SECONDS - elapsed)
        time.sleep(sleep_time)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Shutdown requested by operator (KeyboardInterrupt)")
        print("\nShutting down atmospheric monitoring system.")
        sys.exit(0)
