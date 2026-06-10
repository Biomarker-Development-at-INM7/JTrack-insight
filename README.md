# TrackAutism Python App

This folder is the start of a Python version of the current R workflow in
the reference workflow.

The goal is to keep the R app as the scientific reference while building a new
standalone application that can:

- run fully offline as a desktop app
- optionally connect to a backend later
- reuse the same analysis core in both modes
- be packaged as a standalone app

## Design Principles

- `R app stays alive`
  The R app remains the active reference implementation while Python grows next
  to it.
- `Core logic is UI-independent`
  All parsing, QC, feature extraction, plotting prep, statistics, and report
  logic should live in reusable Python modules.
- `Desktop first, backend optional`
  The desktop app should be able to run locally without a server. If needed
  later, the same core can be exposed through a FastAPI backend.
- `Validation before replacement`
  Each Python module should be compared against the current R output before it
  is considered production-ready.

## Planned Modes

### Local standalone mode

- local browser UI served from Python
- local file loading
- local project save/resume
- local report export
- no external server required beyond the local Python process

### Connected mode

- same Python core
- optional `FastAPI` backend
- useful for heavier computation, shared deployments, or institutional hosting

## Suggested Next Milestones

1. Port metadata indexing and folder parsing.
2. Port QC scanning and exclusion logic.
3. Port Step 5 feature extraction sensor by sensor.
4. Validate Python outputs against the R app.
5. Build local web workflow screens.
6. Add optional backend endpoints.

See [docs/architecture.md](docs/architecture.md) for the module map.
