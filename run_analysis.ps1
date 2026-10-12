<#
.SYNOPSIS
Run the offline analysis on Windows, optionally opening desktop MATLAB.
.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\run_analysis.ps1 -Start "2026-10-09 11:00:00" -Matlab
.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\run_analysis.ps1 -InputCsv data\flight-02\readings.csv -Start "2026-10-15 11:00:00"
#>
[CmdletBinding()]
param(
    [string]$InputCsv = 'data\readings.csv',
    [string]$Start,
    [string]$End,
    [switch]$Matlab,
    [switch]$OpenResults,
    [string]$Python,
    [string]$MatlabExe
)

$ErrorActionPreference = 'Stop'
$projectDir = $PSScriptRoot
if (-not [IO.Path]::IsPathRooted($InputCsv)) {
    $InputCsv = Join-Path $projectDir $InputCsv
}
$InputCsv = (Resolve-Path -LiteralPath $InputCsv).Path
if ($End -and -not $Start) { throw '-End requires -Start.' }

if (-not $Python) { $Python = Join-Path $projectDir '.venv\Scripts\python.exe' }
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw 'Python environment missing. Run: py -3 -m venv .venv; .\.venv\Scripts\python.exe -m pip install -r requirements-analysis.txt. Or supply -Python with your Python executable.'
}

if ($Matlab) {
    if (-not $MatlabExe) {
        $matlabCommand = Get-Command matlab.exe -ErrorAction SilentlyContinue
        if ($matlabCommand) { $MatlabExe = $matlabCommand.Source }
    }
    if (-not $MatlabExe -or -not (Test-Path -LiteralPath $MatlabExe -PathType Leaf)) {
        throw 'MATLAB was not found. Supply -MatlabExe with the path to matlab.exe, or omit -Matlab.'
    }
}

$pythonArgs = @((Join-Path $projectDir 'analyse.py'), '--input', $InputCsv, '--excel')
if ($Start) { $pythonArgs += @('--start', $Start) }
if ($End) { $pythonArgs += @('--end', $End) }
# Analyse.py creates a fresh directory for every run. Read its printed path
# instead of guessing the latest folder (which could belong to another run).
$analysisOutput = @(& $Python @pythonArgs)
$analysisExitCode = $LASTEXITCODE
$analysisOutput | ForEach-Object { Write-Host $_ }
if ($analysisExitCode -ne 0) { throw "Analysis failed (exit $analysisExitCode)." }
$folderLine = $analysisOutput | Where-Object { $_ -like 'Outputs: *' } | Select-Object -Last 1
if (-not $folderLine) { throw 'The analysis did not report a results folder.' }
$resultDir = $folderLine.Substring('Outputs: '.Length)
$report = Get-Content -LiteralPath (Join-Path $resultDir 'report.json') -Raw | ConvertFrom-Json
if ($report.result -ne 'PASS') { throw 'The analysis did not finish successfully.' }

if ($Matlab) {
    $datasetName = if ($Start) { 'experiment.csv' } else { 'cleaned_thingspeak_data.csv' }
    $datasetPath = (Join-Path $resultDir $datasetName).Replace('\', '/').Replace("'", "''")
    $dashboardPath = (Join-Path $projectDir 'MATLAB_Visualization.m').Replace('\', '/').Replace("'", "''")
    $startupPath = Join-Path $resultDir 'open_matlab.m'
    @"
% This run's selected data. Change analysisCSV to view a different analysis.
analysisCSV = '$datasetPath';
run('$dashboardPath');
edit('$dashboardPath');
openvar('readings');
fprintf('Ready. Edit MATLAB_Visualization.m and press Run to test changes.\n');
"@ | Set-Content -LiteralPath $startupPath -Encoding UTF8
    $startupForMatlab = $startupPath.Replace('\', '/').Replace("'", "''")
    $logPath = Join-Path $resultDir 'matlab.log'
    $matlabArgs = '-desktop -sd "{0}" -logfile "{1}" -r "run(''{2}'')"' -f $projectDir, $logPath, $startupForMatlab
    # The user requested an interactive program, so keep MATLAB visible.
    Start-Process -FilePath $MatlabExe -ArgumentList $matlabArgs -WindowStyle Normal | Out-Null
    Write-Host 'MATLAB is opening with the selected data, dashboard source and readings table.'
}
if ($OpenResults) {
    Start-Process -FilePath explorer.exe -ArgumentList ('"{0}"' -f $resultDir) -WindowStyle Normal | Out-Null
}
Write-Host "Repeat with a new -Start/-End to test another time window. Results: $resultDir"
