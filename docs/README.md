# Atmospheric Particle Sensor — research exhibit

An editorial research website by Mariska Adriaanzen, North-West University.
Supervisor: Prof. GR Drevin.

The approved website is `index.html`. It includes the ASCII balloon, photographs,
QR code, verified laboratory and launch observations, and interactive charts in one file.
It runs directly in a browser and requires no build or external dependencies.

The ground-test chart has **Play record** and **Pause record** controls.
Replay advances through the original observations at ten rows per second.
Playback starts when the chart becomes visible and pauses off-screen.
Returning resumes it unless the visitor paused or inspected a reading manually.
Click the chart on desktop or tap it on a phone to pause and inspect a reading.
Desktop hover and scrolling over the chart leave playback running.
Manual inspection or leaving the chart pauses playback. **Replay record**
starts again after the final reading. The balloon follows scrolling and
respects system reduced-motion preferences.

## GitHub Pages

This repository's existing Pages source is the `project-day-website` branch,
with `/docs` as its publishing folder. Updating this folder publishes the site.
The `.nojekyll` file serves the static website directly.

Website: https://caspers-shadow.github.io/Atmospheric-particle-sensor/

If the source ever needs to be restored, go to repository **Settings → Pages**,
select **Deploy from a branch**, choose **project-day-website** and **/docs**,
then save.

`assets/` preserves the original hardware photographs and repository QR code.
`APS_ground_test_2026-07-21.csv` contains the 525 verified laboratory observations.
`APS_launch_2026-10-09.csv` contains the 398 launch-window observations,
with all 13 measurement columns plus the original UTC and derived SAST timestamps.
`CONTENT-SOURCES.txt` documents the dissertation evidence and field mapping.
`VERIFICATION.txt` records the browser and contrast checks.

## Launch record

The Launch section covers 9 October 2026, 11:00:00.366–13:12:25.742 SAST.
It uses the same replay, click/tap inspection, legend and keyboard slider
controls as the ground chart. Measurement buttons select pressure, temperature,
humidity, particles, relative gas indices or light. Both charts play only while
visible, respect manual pause and disable automatic playback for reduced motion.

The 398 rows are selected from `data/readings.csv` on `main`, excluding 256
pre-launch rows. Source data and summary statistics were independently checked
against Chapter 4, §4.11 and Tables 4.10–4.14 of the supplied project report.
The CSV and observation data are embedded, so the exhibit remains standalone.
A static pressure chart, tables and download are available without JavaScript.

The record ends before the estimated landing; pressure minimum is not a
confirmed burst time. Temperature is uncompensated and gas indices are not
ppm concentrations. See the interpretation below each measurement view.

The animated descent is an artistic interpretation, independent of both
measurement records. Reduced motion and phone layouts are supported.
