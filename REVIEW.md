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

Original experiment files were preserved. `.gitignore` excludes default runtime
data, raw logs, environment files and editor lock files. HIL reports, CSVs and
console output can be committed under `hil-results/`.

## Verification

20 simulated regression tests passed on Windows, Python 3.12.14 and pandas
3.0.1, with requests 2.34.2. No live channel was updated. The default Windows
sandbox denied temporary test-file writes; tests were rerun with approved
temporary-folder access.

Historical recovery parsed all 1,428 readings, including 1,328 local logger
rows. A selected July 16 SAST experiment window produced 96 rows. Corrected
humidity has mean 14.498% and maximum 81.52%. The raw source CSV is unchanged.
Existing historical gas scales differ from the current normalised indices.

### Pi HIL results received in commit `a8c73ba`

Two five-minute runs on 8 October 2026 passed explicit offline acquisition on
the Pi, using Debian 13 (Trixie) and Python 3.13.5 in the Pimoroni environment.
Both ran acquisition code from commit `3e9a0a2`.

| Capture (SAST) | Rows saved | Largest sample gap | Unavailable values | LCD navigation |
| --- | --- | --- | --- | --- |
| 11:46:09–11:50:49 | 15 | 20.02 seconds | None | Operator confirmed PASS |
| 11:53:45–11:58:25 | 15 | 20.02 seconds | None | Operator confirmed PASS |

The operator confirmed the network was disconnected; the script does not
independently verify network interfaces. Both logs explicitly show cloud
uploads disabled, clean shutdown and 15 saved readings. Independent checks of
the uploaded CSVs also passed. The second uploaded capture was analysed locally
without network access, producing cleaned data and a gas report.

These runs establish local recording and observed LCD navigation in explicit
offline mode on this Pi. Upload-enabled network failure/recovery, offline cold
boot clock retention, Pi-side offline analysis and deliberately missing-sensor
behaviour remain untested. Measurement accuracy and extended power stability
are not established by these short runs.

**Startup data quality needs follow-up.** Both first rows contain the identical
BME280 values 23.61 °C, 81.52% humidity and 681.62 hPa. Later rows have humidity
20.63–25.59% and pressure 871.17–871.33 hPa. The second run also starts with large
gas resistance spikes. These are observable startup outliers; their cause has
not been established. Preserve the raw records, investigate initial sensor
readiness and flag initial/warm-up samples before using them for statistics.
The capture checker tests continuity and availability, not sensor accuracy.

See `HIL_TESTING.md` and the committed `hil-results/` reports for evidence.

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
