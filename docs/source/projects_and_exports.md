# Projects and Exports

## Project Philosophy

The application is being built around a local, resumable workflow. A user
should be able to:

- load a dataset
- compute features
- review the resulting analysis
- export tables for statistics or publication
- resume work later

## Current Export Focus

The main exportable artifact today is the filtered feature table from Step 5.
Depending on the selected table, this can include:

- generated sensor features
- transformed feature values
- app-category summaries
- raw rows for inspection

## Recommended Export Practice

For publication workflows, it is usually best to export:

1. the reviewed feature table
2. the exact transformation used
3. the cohort labels used in group analysis
4. the filtering rules that defined the final analysis set

## Distribution of Results

When sharing output with collaborators, it helps to save:

- exported CSV files
- screenshots or exported figures
- the exact app version
- notes about comparison against the R reference
