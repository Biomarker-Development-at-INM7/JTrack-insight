# Quick Start

This guide takes you from a dataset folder to a reviewed, exportable feature
table. Work through one sensor at a time and keep a record of the choices made
at each step.

## Before you begin

Prepare a root folder containing the study JSON files. Keep the original files
unchanged while analysing them. A cohort CSV is optional and is only needed for
Step 7 group analysis.

## Start JTrack Insight

Open the installed **JTrack Insight** application. It starts a local service and
opens the interface in your default browser. The address is local to your
computer; routine analysis does not require an internet connection.

If you run the development version, use:

```bash
cd "JTrack-insight"
source .venv/bin/activate
python3 main.py --mode browser
```

## First analysis in seven actions

1. **Load and index.** In **Step 1**, choose the dataset root and select
   **Load Dataset**. Indexing identifies available participants, devices, and
   sensor streams without computing features.
2. **Run file QC.** In **Step 2**, select **Run File QC**. Review invalid JSON
   files and duplicate-content groups before proceeding.
3. **Compute selected features.** In **Step 3**, choose a participant scope,
   sensor, feature mode, feature(s), and temporal frequency, then compute.
4. **Apply feature QC.** In **Step 4**, restrict study days or remove rows that
   do not meet the study's pre-specified plausibility criteria.
5. **Review and visualize.** In **Step 5**, inspect a table or plot after
   selecting the feature, participant, frequency, and plot type.
6. **Export an audit trail.** In **Step 6**, generate the analysis report and
   download the reviewed table or report when appropriate.
7. **Compare groups.** In **Step 7**, load cohort labels, choose a feature, and
   run the requested descriptive or group-level analysis.

## Recommended first sensor

For a first run, select one sensor with an easily interpretable daily feature:

- `pedometer` for daily steps;
- `application_usage` for daily foreground time;
- `location` for daily distance; or
- a Garmin stream such as `HEART_RATE`, `HRV`, or `STRESS` for daily summaries.

Use the **Core clinical features** mode first. Add QC or advanced features only
when they address a defined research question.

## Before reporting a result

- Review the feature table and at least one visualization.
- Record file exclusions, feature QC rules, transformations, and study-day
  limits.
- Export the final reviewed table used in analysis.
- For publication-critical outcomes, validate the feature definition against
  the R reference workflow or source-device documentation.
