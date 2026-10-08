# Pi hardware-in-the-loop tests

Run these from a keyboard and screen connected to the Pi. SSH is unnecessary.
The two explicit offline captures in `hil-results/` passed on 8 October 2026.
The result table below distinguishes completed checks from remaining tests.

## Get the updated code onto the Pi

The source kit `data/offline-hil-kit.zip` can be copied to a USB stick and
extracted into a new folder on the Pi. It contains source, requirements, tests
and instructions; it does not contain API keys, historical experiment files,
or installed dependencies. An existing configured Python environment can be
used from this new folder.

The changes are available on the GitHub branch `codex/offline-reliability-review`.
To use a fresh checkout while the Pi is still online:

```bash
git clone --branch codex/offline-reliability-review https://github.com/Caspers-Shadow/Atmospheric-particle-sensor.git atmospheric-offline-test
cd atmospheric-offline-test
```

For an existing checkout, preserve any Pi-side modifications before fetching
and switching to this branch. A fresh folder keeps the current sensor program
available for comparison. USB copying is also sufficient for these tests.

From the new project folder, activate the **same environment used by the
working sensor program**. If the Pimoroni installer created it:

```bash
source ~/.virtualenvs/pimoroni/bin/activate
```

Do dependency installation and interface configuration while online, before
the disconnected tests. The source kit is not an offline installer. Do not
replace a working hardware environment just to run these tests.

## 1. Record the setup

```bash
cat /etc/os-release
python3 --version
date -Is
python3 -m pip show pimoroni-bme280 enviroplus pms5003 st7735 ltr559 requests pandas
ls -l /dev/serial* /dev/ttyAMA* /dev/ttyS* 2>/dev/null
df -h .
```

Send the OS, Python and date output back first if setup needs tailoring. The
clock should show the current date/time and a valid offset. Do not change
UART overlays speculatively: report startup errors and actual device paths.

## 2. Explicit offline mode, five minutes

Run the helper from the project folder:

```bash
bash run_offline_test.sh
```

It uses your active Python environment, or the project's `.venv`, or the
Pimoroni environment if present, otherwise `python3`. To select another
interpreter, use `ATMO_PYTHON=/path/to/python bash run_offline_test.sh`.

The script asks you to disable Wi-Fi and unplug Ethernet, then starts when
you press Enter. It does not change network settings or install packages.
It creates a unique `hil-results/offline-...` directory, runs the five-minute
capture and checker, includes the log tail, and records your LCD observation.
Send back `report.txt` from the directory printed at the end. The CSV, full
console output and log are preserved alongside the report.

Reports, CSVs and console output under `hil-results/` can be committed to this
branch. Raw `.log` files remain ignored; the report includes the relevant log
tail. After reconnecting, upload the existing results with:

```bash
git add -- hil-results
git commit -m "Add offline HIL test results"
git push
```

For the manual commands below, replace `RUN_DIR` with that printed directory.

Expected:

- Startup succeeds and the console says where readings are saved.
- Roughly 15 CSV rows; the checker accepts at least 12 to allow sensor delays.
- All ten LCD pages work. Wave near the LIGHT sensor and move away between
  waves; holding a hand there should advance only once.
- No upload attempts. The log says uploads are disabled because of offline mode.
- The checker reports `PASS` and no unavailable sensor values. A failed checker
  is useful evidence: report its output and the log tail rather than changing
  thresholds to make it pass.

The gas sensor may take 10 minutes or longer to stabilise. This five-minute
test checks operation and recording, not calibrated gas accuracy.

## 3. Unexpected loss of internet and recovery

Use a fresh Write API key after rotating the previously exposed one. To enter
it without showing it on screen or putting its value in shell history:

```bash
read -rs -p "ThingSpeak Write API key: " THINGSPEAK_WRITE_API_KEY
export THINGSPEAK_WRITE_API_KEY
printf '\n'
python3 readings.py --duration 660 --data-dir hil-results/recovery
```

Start with connectivity enabled. After roughly one minute, disable Wi-Fi and
unplug Ethernet using the local screen. Keep it disconnected for three
minutes, then reconnect and allow the run to finish. No `--offline` flag is
used here: this test exercises failure handling while uploads are enabled.

Expected:

- At least one successful upload before disconnection (if the channel/key works).
- CSV rows continue during disconnection and LCD navigation remains responsive
  between sensor reads. Network warnings are expected; the program stays running.
- Upload retry spacing increases, capped at 300 seconds. A fresh upload should
  succeed within about five minutes after reconnecting, subject to working DNS,
  server access and a valid key.
- Outage history remains in the CSV. ThingSpeak shows gaps because historical
  samples are not automatically replayed.

After the run:

```bash
python3 check_run.py hil-results/recovery/readings.csv --min-rows 28 --max-gap 35 --require-sensors
tail -n 40 hil-results/recovery/atmo_system.log
```

If no key is available, skip cloud recovery and record it as untested. Running
without a key tests automatic local-only operation, not upload failures.

## 4. Restart while still offline

Disconnect all network links, then use the desktop's normal shutdown/reboot
controls. After boot, reactivate the Python environment and run the helper
again. It records `date -Is` and creates a fresh capture directory.

Expected: start and log without DNS, Wi-Fi or NTP. Check that the date remains
correct. A successful capture with an incorrect date is not a timestamp pass.
This program does not automatically start on boot yet.

## 5. Analysis while offline

```bash
python3 analyse.py --input RUN_DIR/readings.csv --output-dir RUN_DIR/analysis
```

Expected: one summary, cleaned data and gas report, no date prompts and no
network calls. If pandas is absent, install it while online using
`requirements-analysis.txt`, then repeat the offline test.

Optional: extract a window using `--start` and `--end`, both in SAST, taken
from the reported capture range. Avoid copying the old experiment dates.

## 6. Missing particulate sensor (optional resilience test)

Shut down and remove Pi power **before** disconnecting or reconnecting the
PMS5003. Pimoroni warns that hot-plugging this sensor can reboot the Pi.
Boot with it disconnected and try a fresh offline capture.

Expected if sensor initialisation succeeds: PM timeout warnings, rows still
written, PM values `-1`, gas/environment readings continue. Run the checker
without `--require-sensors` for this deliberate-failure test. If the vendor
driver refuses initialisation, record that startup failure; it remains a
hardware dependency to address separately.

## Result record

| Test | Result | Notes |
| --- | --- | --- |
| OS / Python / clock recorded | Recorded | Debian 13 (Trixie), Python 3.13.5, timestamp offset +02:00 |
| Explicit offline capture + LCD | PASS | Two five-minute runs, 15 rows each, max gap 20.02s, no unavailable values; LCD confirmed by operator |
| Unexpected outage + reconnect | Pending | |
| Offline reboot + correct clock | Pending | |
| Offline analysis | Pi run pending | Uploaded CSV analysed locally without network access |
| Missing PMS5003 (optional) | Pending | |

Evidence is in commit `a8c73ba`, under
`hil-results/offline-20261008-114552-kSJTBP/` and
`hil-results/offline-20261008-115334-YqyObx/`. Both runs used acquisition code
from `3e9a0a2`. Network disconnection was confirmed by the operator.

Both first samples have the same outlying temperature/humidity/pressure values
(23.61 °C / 81.52% / 681.62 hPa), followed by readings near 871 hPa and much lower
humidity. The cause is unconfirmed; flag startup samples before interpreting
statistics. A capture `PASS` checks recording continuity, availability and the
operator's LCD observation, not calibration accuracy.

Send back the checker output, whether the LCD remained responsive, and the
last 20–40 relevant log lines. Keep the Write API key private. Preserve the
CSV and log from each test for diagnosis.

References: [Pimoroni setup and sensor handling](https://learn.pimoroni.com/article/getting-started-with-enviro-plus),
[Pi 5 RTC](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html#real-time-clock-rtc).
