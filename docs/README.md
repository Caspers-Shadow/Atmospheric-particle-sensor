# Atmospheric Particle Sensor — research exhibit

An editorial research website by Mariska Adriaanzen, North-West University.
Supervisor: Prof. GR Drevin.

The approved website is `index.html`. It includes the ASCII balloon, photographs,
QR code, verified laboratory observations and interactive chart in one file.
It runs directly in a browser and requires no build or external dependencies.

**Pause balloon** keeps the balloon still while you scroll and read. **Resume
balloon** restores the descent. The page and scientific chart remain usable
while the balloon is paused. System reduced-motion preferences start it paused.

## GitHub Pages

This repository's existing Pages source is the `project-day-website` branch,
with `/docs` as its publishing folder. Updating this folder publishes the site.
The `.nojekyll` file serves the static website directly.

Website: https://caspers-shadow.github.io/Atmospheric-particle-sensor/

If the source ever needs to be restored, go to repository **Settings → Pages**,
select **Deploy from a branch**, choose **project-day-website** and **/docs**,
then save.

`assets/` preserves the original hardware photographs and repository QR code.
`APS_ground_test_2026-07-21.csv` contains the 525 verified observations.
`CONTENT-SOURCES.txt` documents the dissertation evidence and field mapping.
`VERIFICATION.txt` records the browser and contrast checks.

The animated descent is an artistic interpretation. The chart contains ground
measurements, not flight telemetry. Reduced motion and phone layouts are supported.
