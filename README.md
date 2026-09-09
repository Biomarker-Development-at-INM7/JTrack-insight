# JTrack Insight

JTrack Insight is a local-first application for quality control and analysis of
digital phenotyping data from smartphones and wearable devices.

## What it does

- Indexes JSON-based study data by participant, device, and sensor stream.
- Detects invalid JSON and duplicate file content before analysis.
- Computes selected, interpretable features for application use, activity,
  location, pedometer, lock/unlock, and Garmin-derived sensor streams.
- Supports feature-level quality control, transformations, visual review, and
  CSV export.
- Creates analysis reports and supports cohort-based group comparisons.
- Runs locally, keeping study data on the user's own computer.

## Workflow

1. Load and index a dataset.
2. Run file-level quality control.
3. Select a sensor and compute features.
4. Apply feature-level inclusion rules.
5. Review, visualize, and export results.
6. Generate an analysis report.
7. Compare cohorts or groups when metadata are available.

## Run locally

```bash
git clone https://github.com/Biomarker-Development-at-INM7/JTrack-insight.git
cd JTrack-insight
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[server,science,storage]"
python3 main.py --mode browser
```

## Documentation

The end-user documentation covers setup, quality control, feature computation,
visualization, export, and group analysis. It can be built locally with:

```bash
pip install -r docs/requirements.txt
sphinx-build -b html docs/source docs/_build/html
```

## License

JTrack Insight is source-available under the
[PolyForm Noncommercial License 1.0.0](LICENSE). It may be used for
non-commercial research and scientific purposes; commercial use requires
separate permission from the copyright holder.
