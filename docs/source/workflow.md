# Workflow Reference

This page is a compact reference version of the full tutorial. Use it when you
already know the app and just want a reminder of what each step is for.

## Step 1 - Load / Index

Purpose:

- scan the dataset root
- identify users, devices, and sensors
- prepare metadata for later filtering

## Step 2 - File QC

Purpose:

- detect invalid JSON
- detect duplicates
- define exclusion decisions before feature computation

## Step 3 - Feature Computation

Purpose:

- choose the scope
- choose one sensor domain
- compute the selected feature set

Typical outputs:

- application-usage summaries
- app-category tables
- location features
- pedometer features
- activity-recognition summaries
- Garmin daily summaries

## Step 4 - Feature QC

Purpose:

- remove implausible feature rows
- restrict study-day range
- require minimum coverage

## Step 5 - Review / Export

Purpose:

- inspect the generated table
- apply transformations
- visualize the data
- export a filtered CSV

## Step 6 - Reports

Purpose:

- produce a readable summary of the current analysis state

## Step 7 - Group Analysis

Purpose:

- connect the feature table to cohort labels
- compare groups
- fit longitudinal or descriptive group models
