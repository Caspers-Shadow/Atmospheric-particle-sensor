# Repository review — 8 October 2026

Branch: `codex/offline-reliability-review`, based on local `dev` at `a3c0614`.

## Fixed obvious problems

| Problem | Effect | Change |
| --- | --- | --- |
| Logger appends named rows to an export-format file | Misaligned readings and misleading analysis | New `data/readings.csv` default; refuse mismatched headers |
| Historical CSV already mixes two layouts | Humidity mean incorrectly appears near 809%; gas resistances appear as indices | Reject mixed layouts by default; explicit `--legacy-mixed` recovery |
| Analysis unconditionally accesses `created_at` | Crashes on local `timestamp_utc` CSVs | Support both schemas and `field1`–`field8` exports |
| Duplicate `main()` calls and mandatory prompts | Analysis runs twice and blocks unattended use | One entry point; optional paired experiment bounds |
| Empty cleaned dataset is indexed without checking | Crash after invalid timestamps | Clear error without writing outputs |
| Hardcoded Write API key and payload/exception logging | Source/logs can expose credentials | Environment-only key, POST body, no payload or request-exception text in logs |
| Upload runs on sensor/LCD thread | Outages freeze LCD updates while the request waits | Background worker, bounded latest-sample queue, retry backoff and explicit offline mode |
| Wall-clock timer schedules readings | NTP/time corrections can disrupt acquisition | Monotonic cadence and test duration |
| CSV failure is swallowed | System appears operational while losing local samples | Visible fatal storage error; flush and synchronise every row |
| Empty existing CSV gets no header | File cannot be analysed correctly | Header written for empty files |
| Failed gas read becomes zero/clean air | Failure biases statistics and gas reporting | Unavailable sentinel; exclude failed channels from analysis/uploads |
| BME280 package and LCD API mismatch | Fresh installs may fail | `pimoroni-bme280`, lowercase `st7735`, current GPIO pin names |
| Every failure immediately retries at LCD cadence | Persistent errors can flood logs | Schedule the next acquisition before reading; disable LCD after a refresh failure; rotating logs |

Original experiment files were preserved. `.gitignore` prevents new runtime
data, logs, environment files and editor lock files being added accidentally;
it does not untrack historical files.

## Verification

20 simulated regression tests passed on Windows, Python 3.12.14 and pandas
3.0.1, with requests 2.34.2. No live channel was updated. The default Windows
sandbox denied temporary test-file writes; tests were rerun with approved
temporary-folder access.

Historical recovery parsed all 1,428 readings, including 1,328 local logger
rows. A selected July 16 SAST experiment window produced 96 rows. Corrected
humidity has mean 14.498% and maximum 81.52%. The raw source CSV is unchanged.
Existing historical gas scales differ from the current normalised indices.

Pi hardware validation is pending. The tests simulate sensor objects, UART
timeouts, LCD calls and network failures; they cannot establish GPIO/UART
compatibility, power stability, physical sensor accuracy or actual recovery
when the Pi's internet connection is restored. See `HIL_TESTING.md`.

## Recommended next changes

1. **Rotate the exposed ThingSpeak key now.** The previous key remains in Git
   history. Use a new environment-provided key for the HIL recovery test.
2. **Add a persistent upload outbox if cloud history matters.** Current offline
   data is durable locally, but missed samples are not replayed automatically.
   Add a disk-backed queue, acknowledgement tracking and a tested bulk replay
   path that preserves timestamps. ThingSpeak supports original timestamps;
   they must be unique within the channel.
   [Write API documentation](https://www.mathworks.com/help/thingspeak/writedata.html).
3. **Add boot/restart supervision after HIL passes.** A service should start
   without waiting for internet, run as the normal sensor user with explicit
   working/output paths, restart after genuine process failure, and expose a
   clear local health indicator. Include disk-space checks and a retention or
   archive policy before prolonged unattended runs. Isolate each sensor's
   failures so one I2C device does not prevent unrelated measurements.
4. **Record calibration and protect offline time.** Save the baselines,
   warm-up state, software/dependency versions and sensor availability with
   each run. Add a monotonic elapsed-time column for auditing clock changes.
   Validate CPU heat effects on temperature against a reference thermometer.
   The Pi 5's RTC accepts a backup battery for time retention across power
   removal; test the clock after an offline cold boot.
   [Pimoroni calibration guidance](https://learn.pimoroni.com/article/getting-started-with-enviro-plus),
   [Raspberry Pi RTC documentation](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html#real-time-clock-rtc).

After confirming the installed Pi OS and a successful HIL run, freeze its
working dependency versions. This branch uses vendor-compatible minimums;
it does not claim that every future release will work unchanged.
