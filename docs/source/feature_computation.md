# Feature Computation Guide

Step 3 turns selected raw sensor records into a feature table. Select only the
features needed to answer the current research question. This keeps the output
interpretable and makes later quality control and reporting clearer.

## Choose a feature mode

**Core clinical features** is the recommended starting point. It focuses on
concise, interpretable measures for the selected sensor.

**QC features** provides coverage, sampling, and completeness measures that
help evaluate data quality.

**Advanced features** provides a wider exploratory feature set. Use it when the
feature definition is part of the planned analysis or explicitly exploratory.

## Examples by sensor

| Sensor domain | Useful starting features | Typical question |
| --- | --- | --- |
| Application usage | Total foreground time, unique apps, selected app categories | How does digital engagement vary over study days? |
| Activity recognition | Time per activity label, label percentage | How much time is classified as walking, still, or other activity? |
| Location | Daily distance, mobility radius, location quality | Does day-to-day mobility change over time? |
| Pedometer | Daily steps, active hours | How does ambulatory activity vary across the study? |
| Lock/unlock | Screen-time proxy, unlock count, session summaries | What is the pattern of phone interaction? |
| Garmin streams | Daily central tendency and variability summaries | How do wearable measures change across time? |

## Application-category mapping

When the selected sensor is `application_usage`, the **App category mapping**
panel becomes available. Select **Use Default Mapping** for the included mapping
or choose a study-specific CSV. The panel reports the number of loaded apps and
matched apps and lists examples of uncategorized apps in the current scope.

Category summaries should be interpreted as mapping-dependent. Keep the mapping
file with the exported analysis materials.

## Temporal frequency

Choose a frequency that matches the scientific question and the sensor's
sampling characteristics. Daily summaries are a useful default for longitudinal
clinical interpretation. Use hourly output for circadian or within-day questions
only when the source coverage supports it.

Do not compare a daily feature against an hourly feature without an explicit
aggregation strategy.
