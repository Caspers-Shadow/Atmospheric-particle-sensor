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

37 simulated regression tests passed on Windows, Python 3.12.14 and pandas
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

### Post-recovery workflow review

The former workflow would not send a full offline flight: the live queue only
retains the newest sample, and the MATLAB source requested only 100 points
with eight separate reads. The additional five measurements were also absent
from the cloud schema. Those gaps are now addressed by:

- `upload_flight.sh` / `upload_flight.py`: offline preview, source snapshot,
  historical bulk replay, conservative timestamp validation, restart by
  reading existing entries, conflict checks and complete read-back verification.
- Shared eight-field mapping plus versioned JSON status containing light,
  raw gas resistances, NH3 index and precise source time. `analyse.py` restores
  these columns without changing the established field numbering.
- `MATLAB_Visualization.m`: an explicit UTC flight window, aligned fields in
  one read per window, split reads at the 8,000-point cap, sorted acquisition
  times and SAST display. This replaces the incorrectly named plaintext `.mat`
  file and removes its embedded Read API key from current source.

A read-only live check confirmed channel `3429238` has the expected eight field
names and is accessible with the existing Read key. No live data was uploaded.
Seventeen new automated tests exercise the actual Pi CSV through simulated cloud
storage and analysis, all 14 columns including exact microsecond timestamps,
1,001-sample batching, repeat recovery, ambiguous timeouts, partial acceptance,
conflicts, read failures, missing metadata and capped reads. They block external
socket connections. All 37 tests pass. The recovery client's read-only UTC range
requests were also checked against the live channel (four July entries and an
empty range for the selected Pi fixture). The shell preview completed with
15 rows on Windows Bash using a local adapter for the missing `tee` utility.
MATLAB R2025a executed the production dashboard against a local channel-reader
stub: all 15 Pi fixture points were aligned, an 8,005-point flight was retrieved
by split windows without duplicate boundaries, and unavailable values became
plot gaps. No requests were made by those MATLAB tests. The saved cloud
visualization and MATLAB's real channel reader still need a HIL check.

Real recovery, repeat uploads and the MATLAB display remain HIL checks. Code
updates in Git do not replace the saved script in the ThingSpeak visualization
app: that source must also be updated. The local CSV remains the primary record;
old channel entries without status cannot be backfilled in place with the five
extra measurements, and startup outliers still require investigation.

## Recommended next changes

1. **Regenerate exposed ThingSpeak keys.** Previous Write and Read keys remain
   in Git history. Supply the current channel keys privately for recovery.
2. **Validate recovery before launch.** The stopped-flight CSV is now the
   durable source for manual replay, with the channel used to verify restart
   progress. Run HIL test 7, repeat it, and confirm the full-flight MATLAB view.
   An automatic disk-backed outbox would only be needed if uploads must resume
   unattended during acquisition, which is beyond the collect-then-upload
   workflow. Preserve the source CSV even after successful verification.
   [Bulk API documentation](https://www.mathworks.com/help/thingspeak/bulkwritejsondata.html).
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
