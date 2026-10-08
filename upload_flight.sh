#!/usr/bin/env bash
# Offline preview by default. --upload sends a stopped flight and verifies it.
set +x
set -u
set -o pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)" || exit 1
cd -- "$project_dir" || exit 1
mode="${1:---dry-run}"
case "$mode" in
    --upload|--dry-run) ;;
    *) printf 'Usage: bash upload_flight.sh [--dry-run|--upload] [readings.csv]\n' >&2; exit 1 ;;
esac
if [[ "$#" -gt 2 ]]; then
    printf 'Too many arguments. Supply the mode and optionally the CSV path.\n' >&2
    exit 1
fi
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
    printf 'Python was not found. Activate your configured environment and rerun.\n' >&2
    exit 1
fi
if [[ "$#" -eq 2 ]]; then
    input_csv="$2"
else
    read -r -p 'Saved flight CSV [data/readings.csv]: ' input_csv || exit 1
    input_csv="${input_csv:-data/readings.csv}"
fi
mkdir -p -- "$project_dir/hil-results" || exit 1
run_dir="$(mktemp -d "$project_dir/hil-results/replay-$(date +%Y%m%d-%H%M%S)-XXXXXX")" || exit 1
report_file="$run_dir/report.txt"
report() { printf '%s\n' "$*" | tee -a "$report_file"; }
report 'Atmospheric sensor flight recovery'
report "Mode: $mode"
report "Input: $input_csv"
report "Results: $run_dir"
report 'Stop sensor acquisition before recovery. Check that the following UTC range is the actual flight.'

"$python_command" -u "$project_dir/upload_flight.py" --dry-run --input "$input_csv" \
    --output-dir "$run_dir/preview" 2>&1 | tee -a "$report_file"
preview_status=("${PIPESTATUS[@]}")
if [[ "${preview_status[0]}" -ne 0 || "${preview_status[1]}" -ne 0 ]]; then
    report 'OVERALL: FAIL (CSV preparation). No upload attempted.'
    exit 1
fi
"$python_command" "$project_dir/analyse.py" --input "$run_dir/preview/prepared_thingspeak.csv" \
    --output-dir "$run_dir/preview-analysis" 2>&1 | tee -a "$report_file"
analysis_status=("${PIPESTATUS[@]}")
if [[ "${analysis_status[0]}" -ne 0 || "${analysis_status[1]}" -ne 0 ]]; then
    report 'OVERALL: FAIL (offline pipeline preview). Install requirements-analysis.txt while online if needed.'
    exit 1
fi
if [[ "$mode" == --dry-run ]]; then
    report 'OVERALL: PREVIEW PASS. No upload attempted; live recovery remains untested.'
    report "Send back this report: $report_file"
    exit 0
fi

read -r -p 'ThingSpeak channel ID [3429238]: ' channel_id || exit 1
channel_id="${channel_id:-3429238}"
if [[ ! "$channel_id" =~ ^[1-9][0-9]*$ ]]; then
    report 'OVERALL: FAIL (channel ID must be a positive integer).'
    exit 1
fi
printf 'Use this channel\047s current keys. Typed keys are hidden and not saved.\n'
if [[ -z "${THINGSPEAK_WRITE_API_KEY:-}" ]]; then
    read -r -s -p 'Write API key: ' THINGSPEAK_WRITE_API_KEY || exit 1
    printf '\n'
fi
if [[ -z "${THINGSPEAK_READ_API_KEY:-}" ]]; then
    read -r -s -p 'Read API key: ' THINGSPEAK_READ_API_KEY || exit 1
    printf '\n'
fi
export THINGSPEAK_WRITE_API_KEY THINGSPEAK_READ_API_KEY
report "Uploading to channel $channel_id; matching stored entries will be skipped."
"$python_command" -u "$project_dir/upload_flight.py" --upload --input "$run_dir/preview/source_readings.csv" \
    --channel-id "$channel_id" --output-dir "$run_dir/upload" 2>&1 | tee -a "$report_file"
upload_status=("${PIPESTATUS[@]}")
unset THINGSPEAK_WRITE_API_KEY THINGSPEAK_READ_API_KEY
if [[ "${upload_status[0]}" -ne 0 || "${upload_status[1]}" -ne 0 ]]; then
    report 'OVERALL: FAIL or incomplete. Keep the original CSV and rerun to verify and resume.'
    report "Send back this report: $report_file"
    exit 1
fi
"$python_command" "$project_dir/analyse.py" --input "$run_dir/upload/thingspeak_verified.csv" \
    --output-dir "$run_dir/analysis" 2>&1 | tee -a "$report_file"
analysis_status=("${PIPESTATUS[@]}")
if [[ "${analysis_status[0]}" -ne 0 || "${analysis_status[1]}" -ne 0 ]]; then
    report 'OVERALL: UPLOAD VERIFIED; analysis failed. Keep the downloaded CSV and report.'
    exit 1
fi
report 'OVERALL: PASS (all cloud entries verified and analyse.py completed). MATLAB display still needs operator confirmation.'
report "MATLAB UTC window: $run_dir/upload/matlab_flight_window.txt"
report "Send back this report: $report_file"
