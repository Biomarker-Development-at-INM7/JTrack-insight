# Quality Control Guide

Quality control occurs at two levels in JTrack Insight. Keeping these levels
separate prevents a source-file problem from being confused with a feature-level
decision.

## Step 2 - File-level QC

File-level QC inspects every indexed JSON file.

| Check | What it identifies | Recommended action |
| --- | --- | --- |
| Invalid JSON | Empty, unreadable, or malformed JSON content | Review the flagged path; exclude it from analysis if it cannot be recovered. |
| Duplicate content | Files with identical MD5 content hashes | Retain one file per duplicate group and exclude duplicate copies. |
| Existing exclusions | Files already excluded in the current analysis session | Clear exclusions only when you have re-reviewed the source files. |

An MD5 hash is calculated from the contents of each file, not from its name or
folder. Therefore, renamed identical copies are still detected.

## Step 4 - Feature-level QC

Feature QC operates on the table created in Step 3. It is intended for explicit,
scientifically justified inclusion rules.

Possible controls include:

- minimum and maximum study day;
- minimum records per participant;
- minimum active bins per participant; and
- a lower and/or upper value threshold for one numeric feature.

## A defensible QC workflow

1. Define the planned rule before looking at group differences when possible.
2. Apply it in Step 4 and record the retained rows and participants.
3. Review a Step 5 plot for unexpected discontinuities or implausible values.
4. Save the Step 6 report and final exported CSV together.

## Avoiding common mistakes

- Do not interpret a missing day as a value of zero unless the sensor definition
  supports that interpretation.
- Do not use a visually chosen cutoff solely because it improves a group result.
- Do not mix different temporal frequencies in one comparison table.
- Review Garmin physiology with wrist-status information when it is available;
  an off-wrist period may not represent meaningful physiology.
