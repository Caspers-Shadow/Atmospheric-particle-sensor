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

39 simulated regression tests passed on Windows, Python 3.12.14 and pandas
3.0.1, with requests 2.34.2. The initial tests made no live channel writes; the
authorised live recovery check below subsequently uploaded the Pi fixture.
The default Windows
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
  historical bulk replay, uncached read requests, conservative timestamp validation, restart by
  reading existing entries, conflict checks and complete read-back verification.
- Shared eight-field mapping plus versioned JSON status containing light,
  raw gas resistances, NH3 index and precise source time. `analyse.py` restores
  these columns without changing the established field numbering.
- `MATLAB_Visualization.m`: an explicit UTC flight window, aligned fields in
  one read per window, split reads at the 8,000-point cap, sorted acquisition
  times and SAST display. This replaces the incorrectly named plaintext `.mat`
  file and removes its embedded Read API key from current source.

A read-only live check confirmed channel `3429238` has the expected eight field
names and is accessible with the configured Read key.
Nineteen new automated tests exercise the actual Pi CSV through simulated cloud
storage and analysis, all 14 columns including exact microsecond timestamps,
1,001-sample batching, repeat recovery, ambiguous timeouts, partial acceptance,
conflicts, read failures, missing metadata and capped reads. They block external
socket connections. All 39 tests pass. The recovery client's read-only UTC range
requests were also checked against the live channel (four July entries and an
empty range for the selected Pi fixture). The shell preview completed with
15 rows on Windows Bash using a local adapter for the missing `tee` utility.
MATLAB R2025a executed the production dashboard against a local channel-reader
stub: all 15 Pi fixture points were aligned, an 8,005-point flight was retrieved
by split windows without duplicate boundaries, unavailable values became
plot gaps, and metadata automatically selected the newest recovered flight.
No requests were made by those local MATLAB tests. The separate live
visualization check below exercised MATLAB's real channel reader.

### Authorised live recovery check on 8 October 2026

Both 15-reading Pi captures were uploaded to channel `3429238` with the supplied
channel keys (30 readings total). All rows were read back and every one of the 14 source columns,
including the microsecond UTC timestamps restored from status, matched local
analysis. `analyse.py` completed for both captures with 15 rows and a gas report
each. Repeating the second capture found all 15 already present and posted
zero write batches. Keys are absent
from the committed code, exports and reports. Evidence is under
`hil-results/thingspeak-recovery-20261008/`.

Replay was performed on the development computer using the Pi capture; this
does not establish Pi-side replay, larger live batches or interrupted live
upload behaviour. The first upload predates optional `fs`/`fe` window hints,
so its dashboard uses the known fixture window as a fallback. Later recovery
uploads include the full flight bounds for automatic dashboard selection.

The saved private cloud MATLAB visualization `Plots of tests` was updated,
saved and run. Both captures rendered all 15 readings across eight plots in
SAST. Recovering the earlier capture after the later one exposed a selection
bug: `results=1` chooses the newest acquisition time, not the newest inserted
entry. The dashboard now reads `channel.last_entry_id` followed by that specific
entry's status, successfully selecting 11:46:09–11:50:49 automatically.
The screenshot and read-back receipts are committed with the reports.

Immediate recovery verification now requests at most 100 results per JSON
read and splits saturated ranges. Larger requests are cached for five minutes,
which could otherwise make a resumed or post-write check use stale rows.
The 1,001-row test exercises complete recovery using these uncached requests.
[ThingSpeak API caching](https://www.mathworks.com/help/thingspeak/channel-control.html),
[reading a specific entry](https://www.mathworks.com/help/thingspeak/readspecificentryid.html).

After future recovery uploads, run the saved visualization using **Save and
Run**; automatic image refresh is a paid-license feature. Code updates in Git
do not replace the cloud source. The local CSV remains the primary record;
old channel entries without status cannot be backfilled in place with the five
extra measurements, and startup outliers still require investigation.

### Repeatable experiment analysis — 9 October 2026

`analyse.py --input CSV --start "YYYY-MM-DD HH:MM:SS" --excel` now performs
the experiment analysis in one offline run. The end defaults to the latest
timestamp with an available measurement. The previous bounds option extracted
an experiment but calculated statistics and gas trends over the whole source;
selection now happens before every report is calculated. Raw gas resistances
are also included in the statistics.

Each default run creates a separate results directory. It contains an Excel
workbook, selected measurements, statistics, gas trends, an unchanged input
snapshot, and reports recording the bounds, source/code hashes and dependency
versions. Explicit nonempty output directories are rejected. Invalid/empty
selections fail before creating files. XlsxWriter is an analysis dependency;
CSV-only operation does not import it. Timestamp text in Excel and CSV retains
the precise UTC and SAST times.

All 46 simulated tests passed, including seven new experiment-analysis checks.
The supplied 654-row flight CSV was analysed twice from 9 October 11:00 SAST.
Both runs selected 398 readings, starting at 11:00:00.366145 and ending at
13:12:25.742104 SAST, in separate directories. Their measurement/statistics/gas
CSVs and source snapshots were identical. All selected numeric values, precise
timestamps and 15 cached workbook summaries matched the selected source data;
the workbook had no formula errors. All three worksheet previews were checked.
These checks ran on Windows with Python 3.12.14, pandas 3.0.1 and XlsxWriter
3.2.9. Running the updated analysis on the Pi remains an operator check.

The complete source CSV contains several recording sessions. Always provide
the experiment's actual date/time when selecting a window; use `--end` as well
if the file includes later experiments. The source snapshot preserves rows
outside the selected window for later analysis.

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
