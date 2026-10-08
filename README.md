# Atmospheric Monitoring System (Raspberry Pi 5 + Enviro+ + PMS5003)

Collect temperature, humidity, pressure, light, particulate matter and relative
gas indices. Display readings on the Enviro+ LCD and save them locally, with
optional ThingSpeak uploads. Acquisition and analysis can run without internet
once the dependencies and Pi interfaces are configured.

## Files

| File | Purpose |
| --- | --- |
| `readings.py` | Sensor acquisition, local CSV, LCD and optional background uploads |
| `analyse.py` | Offline cleaning, statistics, gas trends and experiment extraction |
| `check_run.py` | Check a HIL capture using only the Python standard library |
| `run_offline_test.sh` | Five-minute offline HIL capture, checker and saved report |
| `requirements.txt` | Pi hardware, networking and analysis dependencies |
| `requirements-analysis.txt` | Analysis dependencies for a computer without hardware |
| `requirements-dev.txt` | Dependencies for the simulated regression tests |
| `HIL_TESTING.md` | Local-terminal test sequence for the Pi |
| `REVIEW.md` | Fixed problems, verification and recommended next changes |

The committed CSVs and log are historical experiment records. New acquisition
uses `data/readings.csv`, and analysis outputs go to `data/analysis/`, both
ignored by Git. Existing tracked records remain tracked.

## Pi setup

1. Enable I2C, SPI and UART hardware in `sudo raspi-config`. Disable the UART
   login shell. Follow [Pimoroni's Enviro+ setup instructions](https://learn.pimoroni.com/article/getting-started-with-enviro-plus)
   for the installed Raspberry Pi OS. Their installer can prepare a virtual
   environment and interfaces. Reboot after interface changes.
2. Activate the environment containing the hardware libraries. For the
   Pimoroni installer this is normally:

   ```bash
   source ~/.virtualenvs/pimoroni/bin/activate
   ```

   For a separate environment, create it once while online:

   ```bash
   python3 -m venv --system-site-packages .venv
   source .venv/bin/activate
   ```

3. From this repository, install dependencies while online:

   ```bash
   python3 -m pip install -r requirements.txt
   ```

   The BME280 distribution is **pimoroni-bme280**, imported as `bme280`.
   Current LCD code imports `st7735` and uses `GPIO9` / `GPIO12` pin names,
   matching Pimoroni's current examples. A working Pi installation still needs
   to be verified on the actual board; installing Python packages alone does
   not configure its interfaces.

4. Optional: export clean-air gas baselines measured after warm-up:

   ```bash
   export GAS_BASELINE_OXIDISING=21500
   export GAS_BASELINE_REDUCING=185000
   export GAS_BASELINE_NH3=190000
   ```

   Baselines must be finite positive resistances in ohms. Pimoroni describes
   gas stabilisation as taking 10 minutes or longer; use stabilised values
   for calibration. The short startup delay is for the particulate sensor.
   See [the manufacturer's guide](https://learn.pimoroni.com/article/getting-started-with-enviro-plus).

## Acquisition

Local-only operation, with no cloud requests:

```bash
python3 readings.py --offline
```

A five-minute HIL capture in a separate directory:

```bash
python3 readings.py --offline --duration 300 --data-dir hil-results/offline
```

For cloud uploads, supply your own ThingSpeak Write API key and omit `--offline`:

```bash
export THINGSPEAK_WRITE_API_KEY="your_key_here"
python3 readings.py
```

Without an API key, uploads are disabled and local acquisition continues.
The old source contained a hardcoded key: rotate that key in ThingSpeak if it
was real, because removing it from the latest source does not remove history.

Runtime behaviour:

- Full sensor readings and CSV rows every 20 seconds; the first read starts
  immediately after hardware startup. LCD environment refresh and navigation
  run on a 0.1-second tick. Sensor reads and disk writes can still delay a tick.
- Each CSV row is flushed and synchronised to disk before being offered for
  upload. A header mismatch or storage failure stops acquisition visibly.
- Uploads run on a separate worker. Failures back off from 20 seconds up to
  300 seconds, then reset after success. No connection check is required to
  start acquisition. Upload requests use POST and carry the original sample
  timestamp, following [ThingSpeak's write API](https://www.mathworks.com/help/thingspeak/writedata.html).
- The upload queue retains only the latest waiting sample. **Outage history
  stays in the local CSV; it is not automatically replayed to ThingSpeak.**
- PM and gas failures use `-1` for unavailable values. Analysis excludes these
  values from statistics and trends; uploads omit unavailable fields.
- Logs rotate at 2 MB with three backups. CSV data is not rotated or deleted.

Use `--no-lcd` for acquisition without the display. Stop with `Ctrl+C`, or use
`--duration` for an automatic stop. Cadence and duration use a monotonic timer,
so a system-clock correction cannot reset the acquisition schedule. UTC sample
timestamps still depend on the Pi's clock being correct.

## Analysis

Analyse a new local capture without internet:

```bash
python3 analyse.py --input data/readings.csv
```

Analyse a normal ThingSpeak export:

```bash
python3 analyse.py --input channel-export.csv --output-dir data/channel-analysis
```

Optional experiment bounds are inclusive and interpreted in SAST when no
offset is provided. Supply both bounds; without them there are no prompts and
no experiment extract:

```bash
python3 analyse.py --input data/readings.csv \
  --start "2026-07-01 08:00:00" --end "2026-07-01 10:00:00"
```

The committed `thingspeak_data.csv` contains 100 export rows followed by
1,328 local rows under the export header. Standard loading deliberately
rejects this mixture. Recover its known historical layouts explicitly:

```bash
python3 analyse.py --input thingspeak_data.csv --legacy-mixed \
  --output-dir data/historical-analysis
```

This writes corrected outputs and leaves the original file untouched. The
historical gas indices include values above 100, indicating an older scale;
do not compare those directly with current baseline-derived 0–100 indices.

Analysis creates `cleaned_thingspeak_data.csv`, `gas_report.csv`, and, when
bounds are supplied, `experiment.csv`. Invalid timestamps are counted and
dropped; an empty valid dataset produces a clear error.

## Development checks

Use Python 3.9 or later. Install development dependencies on a computer
without the sensors, then run the tests:

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m unittest discover -s tests -v
```

These tests simulate hardware and block external socket connections in the
test process. They cover CSV formats, failures, backoff, offline mode,
local storage and display progress during a stalled upload. Actual Pi
compatibility and disconnected operation require the HIL tests.

## Measurement limitations

Gas indices indicate changes relative to a clean-air resistance baseline;
they are not ppm concentrations or proof that an individual gas is present.
Gas names sharing a MICS6814 sensing element have the same index. The console
confidence label is an unvalidated heuristic, not an accuracy estimate.
Sensor behaviour at stratospheric pressure and temperature is not validated.
