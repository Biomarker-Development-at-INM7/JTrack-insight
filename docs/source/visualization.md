# Visualization Guide for End Users

This page explains which plot types are most useful in practice and when to use
them.

## Best First Plot

If you are unsure where to start, use:

- **Mean trajectory with 95% confidence interval**

This is usually the easiest plot for understanding how a feature behaves across
study day.

## When to Use Each Plot

### Mean trajectory with 95% CI

Use when you want:

- a clean longitudinal summary
- a publication-friendly view
- a quick sense of overall trend and uncertainty

Best for:

- daily steps
- app-use duration
- mobility features
- wearable summary features

### Individual trajectories

Use when you want:

- subject-level heterogeneity
- to identify outlier participants
- to see whether one group is driven by only a few subjects

### Heatmap

Use when you want:

- a participant-by-time overview
- to inspect missingness
- to identify dense versus sparse participation

### Box or violin plot

Use when you want:

- a snapshot of spread
- to compare subjects or groups visually
- to review outliers before statistics

### Sensor dashboard

Use when you want:

- raw signal review and feature review together
- a quick diagnostic summary for one sensor

This is especially useful for wearable streams.

## Garmin-Specific Advice

For Garmin sensors, use wear-state information when available.

Why:

- physiology values are easier to interpret when the device was actually worn
- off-wrist periods can create misleading summaries

Recommended approach:

1. review the raw or dashboard plot
2. filter to **on-wrist only** if needed
3. compare the cleaned view with the unfiltered view

## Application-Usage Plots

For application usage, the most useful views are usually:

- total foreground time trajectories
- app-category stacked summaries
- category trends over time

These help answer both behavioral and compliance questions.

## Publication-Oriented Defaults

If you want clean figures for a report or manuscript, start with:

1. mean trajectory with 95% CI
2. heatmap
3. box or violin plot

These are usually easier to explain than dense raw plots.
