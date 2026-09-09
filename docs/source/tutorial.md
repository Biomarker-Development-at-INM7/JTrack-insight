# Step-by-Step Tutorial

Use this tutorial for a complete, auditable analysis session. The same pattern
works across smartphone and Garmin-derived sensor streams: define scope, check
quality, compute only needed features, review the result, then export or compare
groups.

## Step 1 - Load and index

Choose the study root with **Choose Folder**, confirm the displayed path, then
click **Load Dataset**. This creates a lightweight index of JSON files,
participants, devices, and sensor streams. It does not calculate features yet.

Wait for the operation banner to finish. Large studies may take time to index;
the banner reports that indexing is still active and refreshes progress.

## Step 2 - File QC

Select **Run File QC** before loading a sensor stream. The application checks:

- JSON validity;
- duplicate file content using an MD5 hash of file contents; and
- the current list of files excluded from analysis.

Use **Exclude Duplicate Copies** to retain one file from each identical-content
group. Use **Exclude Invalid JSON** to keep unreadable or malformed JSON out of
later analysis. These are analysis exclusions: the original dataset files are
not deleted.

## Step 3 - Feature computation

1. Select the participant scope, device, and sensor.
2. For `application_usage`, load or choose an app-category mapping if category
   features are required.
3. Choose **Core clinical features** first. Use **QC** or **Advanced** only for
   a pre-defined reason.
4. Tick only the required feature(s) and choose the temporal frequency.
5. Select **Compute selected features**.

Computing only selected features makes the table easier to review and reduces
unnecessary processing. See :doc:`feature_computation` for examples by sensor.

## Step 4 - Quality control and data inclusion

Step 4 applies QC to computed rows, not to source files. Typical decisions are:

- retain a defined study-day range;
- require a minimum number of rows or active time bins per participant; and
- set a defensible lower or upper bound for a numeric feature.

Apply a rule only when it is justified by the measurement, protocol, or a
pre-specified analysis plan. The page records the number of rows removed and
participants retained.

## Step 5 - Review, transform, visualize, and export

Choose the data source, temporal frequency, participant, and feature before
choosing a plot. Plots are generated only after **Visualize plot** is selected.

Transformations such as `log(x + 1)`, `log10(x + 1)`, square root, and z-score
are applied to the selected Step 5 analysis view. Record any transformation in
your analysis notes and report.

```{image} _static/images/review-export.png
:alt: JTrack Insight Step 5 Review Export showing a daily mean trajectory with a confidence interval
:class: screenshot
```

Export the filtered CSV only after confirming the included participants,
frequency, feature, and transformation.

## Step 6 - Reports

Generate an analysis report to preserve the current analytic context. The
report summarizes the selected feature table, feature-level QC, file QC, row
and participant counts, and any available cohort/group information. Download
the HTML report with the final CSV as an audit trail.

## Step 7 - Group analysis

Load a cohort CSV containing participant identifiers and group labels. Check the
recognized columns and select the group variable, feature table, numeric metric,
and transformation. Use descriptive plots first, then inspect test or model
output in the context of group sizes, missingness, and repeated measurements.

The group-analysis page supports cohort comparisons and longitudinal summaries;
it does not replace a protocol or a statistician's review for confirmatory
clinical analyses.

## Recommended study workflow

1. Run Steps 1-2 once per dataset version.
2. Repeat Steps 3-5 for each sensor domain.
3. Use Step 6 to preserve the analytic record for each feature table.
4. Move to Step 7 only after the relevant feature tables have passed review.
