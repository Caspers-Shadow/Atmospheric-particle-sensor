#!/usr/bin/env python3

import colorsys
import sys
import time
import logging
import requests

import st7735

try:
    from ltr559 import LTR559
    ltr559 = LTR559()
except ImportError:
    import ltr559

from bme280 import BME280
from fonts.ttf import RobotoMedium as UserFont
from PIL import Image, ImageDraw, ImageFont
from pms5003 import PMS5003
from pms5003 import ReadTimeoutError as pmsReadTimeoutError
from enviroplus import gas

logging.basicConfig(
    format="%(asctime)s.%(msecs)03d %(levelname)-8s %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S"
)

logging.info("""
Atmospheric Sensor Dashboard

Displays:
- Temperature
- Pressure
- Humidity
- Light
- Oxidising Gas
- Reducing Gas
- NH3 Index
- PM1.0
- PM2.5
- PM10

Uploads data to ThingSpeak every 20 seconds.

Press Ctrl+C to exit!
""")

# ----------------------------------------------------------------------
# THINGSPEAK CONFIGURATION
# ----------------------------------------------------------------------

THINGSPEAK_API_KEY = "2FZWRBB132P5J6KX"
THINGSPEAK_URL = "https://api.thingspeak.com/update"

UPLOAD_INTERVAL = 20
last_upload = 0

# ----------------------------------------------------------------------
# SENSORS
# ----------------------------------------------------------------------

bme280 = BME280()
pms5003 = PMS5003()

# LCD
st7735 = st7735.ST7735(
    port=0,
    cs=1,
    dc="GPIO9",
    backlight="GPIO12",
    rotation=270,
    spi_speed_hz=10000000
)

st7735.begin()

WIDTH = st7735.width
HEIGHT = st7735.height

img = Image.new("RGB", (WIDTH, HEIGHT), color=(0, 0, 0))
draw = ImageDraw.Draw(img)

font_size = 20
font = ImageFont.truetype(UserFont, font_size)

top_pos = 25

# ----------------------------------------------------------------------
# VARIABLES
# ----------------------------------------------------------------------

variables = [
    "temperature",
    "pressure",
    "humidity",
    "light",
    "oxidised",
    "reduced",
    "nh3",
    "pm1",
    "pm25",
    "pm10"
]

values = {}

for v in variables:
    values[v] = [1] * WIDTH

# ----------------------------------------------------------------------
# FUNCTIONS
# ----------------------------------------------------------------------


def get_cpu_temperature():
    with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
        return int(f.read()) / 1000.0


def get_nh3_level(nh3_value):
    """
    Convert raw NH3 resistance into:
    - kOhm
    - Index (0-100)
    - Human-readable level

    NOTE:
    This is NOT ppm or molecule count.
    """

    nh3_kohm = nh3_value / 1000

    nh3_index = min(100, max(0, nh3_kohm))

    if nh3_kohm < 30:
        level = "LOW"
    elif nh3_kohm < 60:
        level = "MEDIUM"
    else:
        level = "HIGH"

    return nh3_kohm, nh3_index, level


def upload_to_thingspeak(
    temperature,
    humidity,
    pressure,
    pm1,
    pm25,
    pm10,
    nh3_index,
    light
):

    payload = {
        "api_key": THINGSPEAK_API_KEY,
        "field1": temperature,
        "field2": humidity,
        "field3": pressure,
        "field4": pm1,
        "field5": pm25,
        "field6": pm10,
        "field7": nh3_index,
        "field8": light
    }

    try:
        response = requests.get(
            THINGSPEAK_URL,
            params=payload,
            timeout=10
        )

        logging.info(
            f"ThingSpeak upload successful! Entry #{response.text}"
        )

    except Exception as e:
        logging.error(f"ThingSpeak Error: {e}")


def display_text(variable, data, unit):

    values[variable] = values[variable][1:] + [data]

    vmin = min(values[variable])
    vmax = max(values[variable])

    colours = [
        (v - vmin + 1) / (vmax - vmin + 1)
        for v in values[variable]
    ]

    message = f"{variable[:4]}: {data:.1f} {unit}"

    logging.info(message)

    draw.rectangle((0, 0, WIDTH, HEIGHT), (255, 255, 255))

    for i in range(len(colours)):

        colour = (1.0 - colours[i]) * 0.6

        r, g, b = [
            int(x * 255.0)
            for x in colorsys.hsv_to_rgb(
                colour,
                1.0,
                1.0
            )
        ]

        draw.rectangle(
            (i, top_pos, i + 1, HEIGHT),
            (r, g, b)
        )

        line_y = (
            HEIGHT
            - (top_pos + (colours[i] * (HEIGHT - top_pos)))
            + top_pos
        )

        draw.rectangle(
            (i, line_y, i + 1, line_y + 1),
            (0, 0, 0)
        )

    draw.text(
        (0, 0),
        message,
        font=font,
        fill=(0, 0, 0)
    )

    st7735.display(img)


# ----------------------------------------------------------------------
# TEMPERATURE COMPENSATION
# ----------------------------------------------------------------------

factor = 2.25
cpu_temps = [get_cpu_temperature()] * 5

delay = 0.5
mode = 0
last_page = 0

# ----------------------------------------------------------------------
# MAIN LOOP
# ----------------------------------------------------------------------

try:

    while True:

        proximity = ltr559.get_proximity()

        # --------------------------------------------------------------
        # THINGSPEAK UPLOAD
        # --------------------------------------------------------------

        if time.time() - last_upload >= UPLOAD_INTERVAL:

            cpu_temp = get_cpu_temperature()

            cpu_temps = cpu_temps[1:] + [cpu_temp]

            avg_cpu_temp = (
                sum(cpu_temps)
                / len(cpu_temps)
            )

            raw_temp = bme280.get_temperature()

            temperature = (
                raw_temp
                - ((avg_cpu_temp - raw_temp) / factor)
            )

            humidity = bme280.get_humidity()
            pressure = bme280.get_pressure()

            gas_data = gas.read_all()

            nh3_kohm, nh3_index, nh3_level = (
                get_nh3_level(gas_data.nh3)
            )

            try:

                pm_data = pms5003.read()

                pm1 = float(
                    pm_data.pm_ug_per_m3(1.0)
                )

                pm25 = float(
                    pm_data.pm_ug_per_m3(2.5)
                )

                pm10 = float(
                    pm_data.pm_ug_per_m3(10)
                )

            except pmsReadTimeoutError:

                pm1 = -1
                pm25 = -1
                pm10 = -1

            light = (
                ltr559.get_lux()
                if proximity < 10
                else 0
            )

            print("\n===== SENSOR READINGS =====")
            print(f"Temperature : {temperature:.2f} C")
            print(f"Humidity    : {humidity:.2f} %")
            print(f"Pressure    : {pressure:.2f} hPa")
            print(f"PM1.0       : {pm1:.2f} ug/m3")
            print(f"PM2.5       : {pm25:.2f} ug/m3")
            print(f"PM10        : {pm10:.2f} ug/m3")
            print(f"NH3         : {nh3_kohm:.2f} kOhm")
            print(f"NH3 Index   : {nh3_index:.1f}/100")
            print(f"NH3 Level   : {nh3_level}")
            print("===========================\n")

            upload_to_thingspeak(
                temperature,
                humidity,
                pressure,
                pm1,
                pm25,
                pm10,
                nh3_index,
                light
            )

            last_upload = time.time()

        # --------------------------------------------------------------
        # CHANGE DISPLAY PAGE
        # --------------------------------------------------------------

        if proximity > 1500 and (
            time.time() - last_page > delay
        ):
            mode += 1
            mode %= len(variables)
            last_page = time.time()

        # --------------------------------------------------------------
        # DISPLAY MODES
        # --------------------------------------------------------------

        if mode == 0:

            cpu_temp = get_cpu_temperature()
            cpu_temps = cpu_temps[1:] + [cpu_temp]

            avg_cpu_temp = (
                sum(cpu_temps)
                / len(cpu_temps)
            )

            raw_temp = bme280.get_temperature()

            data = (
                raw_temp
                - ((avg_cpu_temp - raw_temp) / factor)
            )

            display_text(
                "temperature",
                data,
                "C"
            )

        elif mode == 1:

            display_text(
                "pressure",
                bme280.get_pressure(),
                "hPa"
            )

        elif mode == 2:

            display_text(
                "humidity",
                bme280.get_humidity(),
                "%"
            )

        elif mode == 3:

            light = (
                ltr559.get_lux()
                if proximity < 10
                else 0
            )

            display_text(
                "light",
                light,
                "Lux"
            )

        elif mode == 4:

            display_text(
                "oxidised",
                gas.read_all().oxidising / 1000,
                "kOhm"
            )

        elif mode == 5:

            display_text(
                "reduced",
                gas.read_all().reducing / 1000,
                "kOhm"
            )

        elif mode == 6:

            nh3_kohm, nh3_index, nh3_level = (
                get_nh3_level(
                    gas.read_all().nh3
                )
            )

            logging.info(
                f"NH3 Level: {nh3_level}"
            )

            display_text(
                "nh3",
                nh3_index,
                "/100"
            )

        elif mode == 7:

            try:
                data = float(
                    pms5003.read().pm_ug_per_m3(1.0)
                )

                display_text(
                    "pm1",
                    data,
                    "ug/m3"
                )

            except pmsReadTimeoutError:
                logging.warning(
                    "Failed to read PMS5003"
                )

        elif mode == 8:

            try:
                data = float(
                    pms5003.read().pm_ug_per_m3(2.5)
                )

                display_text(
                    "pm25",
                    data,
                    "ug/m3"
                )

            except pmsReadTimeoutError:
                logging.warning(
                    "Failed to read PMS5003"
                )

        elif mode == 9:

            try:
                data = float(
                    pms5003.read().pm_ug_per_m3(10)
                )

                display_text(
                    "pm10",
                    data,
                    "ug/m3"
                )

            except pmsReadTimeoutError:
                logging.warning(
                    "Failed to read PMS5003"
                )

except KeyboardInterrupt:
    sys.exit(0)
