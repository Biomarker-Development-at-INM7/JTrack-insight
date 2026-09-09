# Sensor Coverage

## Smartphone-Derived Streams

### Application Usage

Current Python support includes:

- daily foreground time summaries
- top apps
- app-category aggregation
- category-based export tables

### Activity Recognition

Activity labels are based on Android activity-recognition codes and can be
summarized as:

- time per label
- proportion per label
- transition burden
- confidence summaries

### Location

Location support focuses on scientific mobility measures such as:

- daily distance
- mobility radius
- location variability
- trajectory review

### Pedometer

Current pedometer support includes:

- daily steps
- active hours
- peak hourly steps
- sedentary or zero-step periods

## Garmin Streams

Garmin-derived analysis is currently handled through daily summary features for
streams such as:

- BBI
- enhanced BBI
- HRV
- heart rate
- respiration
- SpO2
- stress
- calories
- actigraphy 1, 2, and 3
- zero crossing
- wrist status

## Wrist-Status Integration

The Python app can load Garmin `WRIST_STATUS` together with other Garmin
streams from the same subject and device context. This supports:

- wrist-aware filtering in review steps
- adherence summaries
- more interpretable raw plots for wearable data

This is especially useful for distinguishing plausible physiology from periods
when the watch was not worn.

## Validation Note

Sensor support is still under active scientific refinement. When using a stream
for publication-grade work, compare its Python output with the R reference app
or with source-device expectations.
