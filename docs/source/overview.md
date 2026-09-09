# Overview

## What JTrack Insight Does

JTrack Insight is a local analysis environment for smartphone and wearable
sensor datasets such as:

- application usage
- Android activity recognition
- location
- pedometer
- Garmin streams such as BBI, HRV, heart rate, respiration, SpO2, stress,
  calories, actigraphy, and wrist-status

The app is designed for stepwise scientific work rather than one-click black
box processing. Users move through loading, quality control, feature
computation, review, visualization, and group analysis as separate stages.

## Current Scientific Position

The Python app is a rebuild of the R workflow, not yet a scientific
replacement. The R application remains the reference implementation for method
comparison and validation.

That means the Python app is intended to support:

- reproducible local analysis
- transparent feature generation
- iterative validation against the R outputs
- packaging as a standalone local application

## Main Workflow

The current Python application follows this sequence:

1. Load and index a dataset folder.
2. Run file-level QC checks.
3. Select analysis scope and compute features for a chosen sensor.
4. Apply feature-level quality control.
5. Review, visualize, transform, and export results.
6. Build reporting outputs.
7. Run group-level analyses from the reviewed feature table.

## Local-First Design

JTrack Insight is intentionally local-first:

- the app runs on the user’s own machine
- data do not need to leave the institution or clinic
- the packaged desktop app starts a local server and opens the interface in the
  system browser

This supports privacy-sensitive research workflows while keeping the
architecture compatible with a future backend if needed.
