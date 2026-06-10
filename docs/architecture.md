# Architecture Map

## Purpose

This Python app is a parallel rebuild of the R workflow, not a replacement yet.
The current R app remains the scientific reference:

- the reference workflow

## High-Level Layout

```text
python_app/
  src/trackautism_app/
    core/
    desktop/
    server/
    models/
    services/
    utils/
  tests/
  docs/
```

## Layer Responsibilities

### `core/`

Pure analysis logic with no UI dependency.

- file indexing
- metadata extraction
- QC scanning
- timestamp parsing
- sensor normalization
- subject-level feature extraction
- visualization-ready summaries
- group statistics
- report assembly

### `desktop/`

Standalone user interface.

- home screen
- new project
- resume project
- stepwise workflow screens
- local export actions

### `server/`

Optional online layer for the same core logic.

- REST endpoints
- remote execution
- shared project access

### `models/`

Typed data contracts shared by desktop and server.

- project state
- filter configuration
- analysis scope
- QC result summaries

### `services/`

Cross-cutting orchestration.

- load project
- save project
- run workflow step
- build reports

### `utils/`

Small helpers that should not hold domain logic.

- time helpers
- filesystem helpers
- safe casting

## Mapping From Current R Steps

### Home

- desktop landing page
- choose `New Analysis` or `Resume Saved Analysis`

### Step 1: Load/index data

- `core.indexing`
- `services.projects`

### Step 2: QC control

- `core.qc`
- `services.projects`

### Step 3: Scope selection and loading

- `core.scope`
- `core.indexing`
- `services.workflow`

### Step 4: Measure review

- `core.review`
- sensor-specific functions in `core.sensors`

### Step 5: Feature extraction

- `core.features`
- `core.sensors`

### Step 6: Visualization

- `core.plots`

### Step 8: Group analysis

- `core.groups`
- `core.stats`

## Validation Strategy

Each Python module should be compared to the R app on the same input.

Suggested order:

1. metadata indexing
2. QC
3. application usage
4. Garmin sensors
5. location and activity
6. lock/unlock
7. group analysis and reports

The Python app should not replace the R app until those outputs are matched or
the differences are understood and documented.
