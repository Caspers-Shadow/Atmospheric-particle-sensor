#!/usr/bin/env bash
# Run the five-minute Pi HIL capture and collect a report. No installs or uploads.
set -u
set -o pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)" || exit 1
cd -- "$project_dir" || exit 1

# Honour an active environment first, then the usual local/Pimoroni environments.
# ATMO_PYTHON can select a different interpreter without changing the script.
if [[ -n "${ATMO_PYTHON:-}" ]]; then
    python_command="$ATMO_PYTHON"
elif [[ -n "${VIRTUAL_ENV:-}" && -x "$VIRTUAL_ENV/bin/python" ]]; then
    python_command="$VIRTUAL_ENV/bin/python"
elif [[ -x "$project_dir/.venv/bin/python" ]]; then
    python_command="$project_dir/.venv/bin/python"
elif [[ -x "${HOME}/.virtualenvs/pimoroni/bin/python" ]]; then
    python_command="${HOME}/.virtualenvs/pimoroni/bin/python"
else
    python_command="python3"
fi
if ! command -v "$python_command" >/dev/null 2>&1; then
    printf 'Python was not found. Activate your sensor environment and run again.\n' >&2
    exit 1
fi

mkdir -p -- "$project_dir/hil-results" || exit 1
run_dir="$(mktemp -d "$project_dir/hil-results/offline-$(date +%Y%m%d-%H%M%S)-XXXXXX")" || exit 1
report_file="$run_dir/report.txt"

report() {
    printf '%s\n' "$*" | tee -a "$report_file"
}

{
    printf 'Atmospheric sensor offline HIL test\n'
    printf 'Started: '
    date -Is
    printf 'Python: %s\n' "$python_command"
    "$python_command" --version
    if [[ -r /etc/os-release ]]; then
        cat /etc/os-release
    fi
    if command -v git >/dev/null 2>&1; then
        git log -1 --format='Commit: %h %s' 2>/dev/null || true
    fi
    printf 'Results: %s\n\n' "$run_dir"
} | tee "$report_file"

printf '\nDisable Wi-Fi and unplug Ethernet on the Pi.\n'
if ! read -r -p 'Press Enter when disconnected to start the five-minute test: ' ready; then
    report 'NOT RUN: No start confirmation received.'
    exit 1
fi
report 'Network disconnected: confirmed by operator (not automatically verified).'
report 'During capture, wave near LIGHT and move away to test LCD page changes.'
report 'Starting 300-second acquisition with cloud uploads explicitly disabled.'

"$python_command" -u "$project_dir/readings.py" \
    --offline --duration 300 --data-dir "$run_dir" \
    2>&1 | tee "$run_dir/capture-console.txt"
capture_status=("${PIPESTATUS[@]}")
report "Acquisition exit code: ${capture_status[0]}"
result=0
if [[ "${capture_status[0]}" -ne 0 || "${capture_status[1]}" -ne 0 ]]; then
    result=1
    report 'FAIL: Acquisition or console recording failed. Last console lines:'
    tail -n 40 "$run_dir/capture-console.txt" | tee -a "$report_file"
fi

if [[ -s "$run_dir/readings.csv" ]]; then
    report 'Checking row count, timestamps and sensor availability:'
    "$python_command" "$project_dir/check_run.py" "$run_dir/readings.csv" \
        --min-rows 12 --max-gap 35 --require-sensors \
        2>&1 | tee "$run_dir/check.txt" | tee -a "$report_file"
    checker_status=("${PIPESTATUS[@]}")
    if [[ "${checker_status[0]}" -ne 0 || "${checker_status[1]}" -ne 0 || "${checker_status[2]}" -ne 0 ]]; then
        result=1
    fi
else
    report 'FAIL: No readings were saved.'
    result=1
fi

if [[ -f "$run_dir/atmo_system.log" ]]; then
    report 'Last 20 system log lines:'
    tail -n 20 "$run_dir/atmo_system.log" | tee -a "$report_file"
fi

if [[ "${capture_status[0]}" -eq 0 ]]; then
    printf '\n'
    if read -r -p 'Did the LCD pages respond to hand waves? [y/n]: ' lcd_answer; then
        case "$lcd_answer" in
            y|Y|yes|YES) report 'LCD navigation: PASS (operator observed).' ;;
            n|N|no|NO) report 'LCD navigation: FAIL (operator observed).'; result=1 ;;
            *) report 'LCD navigation: NOT RECORDED.'; result=1 ;;
        esac
    else
        report 'LCD navigation: NOT RECORDED.'
        result=1
    fi
fi

if [[ "$result" -eq 0 ]]; then
    report 'OVERALL: PASS'
else
    report 'OVERALL: FAIL or incomplete; keep these files for diagnosis.'
fi
report "Send back this report: $report_file"
exit "$result"
