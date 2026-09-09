# Troubleshooting

## Indexing or QC appears to take a long time

Keep the application page open and check the progress banner. Indexing and file
QC can take longer for large studies because the application reads each JSON
file, and file QC hashes file contents as well as validates JSON.

Wait for the banner to finish before starting another load, QC, or scope-load
operation. If the message stops changing for an extended period, confirm that
the dataset folder remains connected and that you can read its files.

## A sensor is not listed in Step 3

Return to Step 1 and confirm that the intended dataset root was indexed. Then
check Step 2 exclusions. A sensor name is derived from the folder and indexed
metadata, so a non-standard folder layout may need review.

## App-category mapping is not visible

The mapping panel appears only when `application_usage` is selected as the
sensor. Select that sensor again after loading or choosing a mapping file. If
the panel reports unmatched apps, inspect the mapping preview and update the
mapping CSV as needed.

## A plot is empty or unexpected

In Step 5, check all of the following:

1. The data source is the intended generated feature table or raw sensor data.
2. The selected frequency exists for that table.
3. The participant filter includes rows.
4. The selected feature is numeric when using a numeric plot.
5. The Step 4 QC rules did not remove the rows of interest.

For Garmin data, compare all wear states with on-wrist-only data before drawing
conclusions about an unexpected physiological pattern.

## Group analysis cannot find participants

Check that the cohort CSV has a participant identifier column that matches the
subject identifiers in the selected feature table. Ensure group labels are not
empty and that the selected Step 5 table includes the intended participants.

## The local browser does not open

The packaged application should open the default browser automatically after
starting its local service. If it does not, wait briefly and open the local URL
shown by the launcher. The service is intended for local use, not public network
access. Use the app's **Stop Server** control when finished.
