"""Pedometer analysis helpers for the Python prototype."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone


STEP_VALUE_CANDIDATES = ("stepCount", "step_count", "steps", "step", "counter", "value")


@dataclass(slots=True)
class PedometerReview:
    records: int
    subjects: int
    distinct_days: int
    total_steps: int
    mean_daily_steps: float
    median_daily_steps: float
    peak_daily_steps: int
    goal_days_10000: int


def _is_pedometer_row(row: dict) -> bool:
    sensor_name = str(row.get("sensorname") or "").strip().upper()
    wearable = str(row.get("wearable_sensor") or "").strip().upper()
    return sensor_name in {"PEDOMETER", "STEPS"} or wearable in {"PEDOMETER", "STEPS"}


def _safe_float(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_step_value(row: dict) -> float | None:
    populated: list[tuple[str, float]] = []
    for key in STEP_VALUE_CANDIDATES:
        if key not in row:
            continue
        num = _safe_float(row.get(key))
        if num is not None:
            populated.append((key, num))
    if not populated:
        return None
    for preferred in STEP_VALUE_CANDIDATES[:-1]:
        for key, num in populated:
            if key == preferred:
                return num
    return populated[0][1]


def _day_label_from_row(row: dict) -> str:
    ts_ms = row.get("analysis_time_ms")
    if ts_ms is None:
        return "Unknown"
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def select_pedometer_rows(rows: list[dict]) -> list[dict]:
    out = []
    for row in rows:
        if not _is_pedometer_row(row):
            continue
        value = _extract_step_value(row)
        if value is None or value < 0:
            continue
        enriched = dict(row)
        enriched["_step_value"] = value
        out.append(enriched)
    return out


def _daily_steps_from_values(values: list[float]) -> tuple[float, bool]:
    if not values:
        return 0.0, False
    if len(values) == 1:
        return max(values[0], 0.0), True
    diffs = [values[i] - values[i - 1] for i in range(1, len(values))]
    nonneg_ratio = sum(1 for d in diffs if d >= 0) / len(diffs) if diffs else 1.0
    cumulative_mode = nonneg_ratio >= 0.8
    if cumulative_mode:
        total = max(values[0], 0.0)
        for diff in diffs:
            total += max(diff, 0.0)
        return total, True
    return sum(max(v, 0.0) for v in values), False


def extract_pedometer_daily_features(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    ped_rows = select_pedometer_rows(rows)
    if not ped_rows:
        return [], []

    grouped: dict[tuple[str, int | None, str], list[dict]] = defaultdict(list)
    for row in ped_rows:
        grouped[(str(row.get("username") or ""), row.get("study_day"), _day_label_from_row(row))].append(row)

    daily = []
    for (subject, study_day, day_label), group_rows in grouped.items():
        ordered = sorted(group_rows, key=lambda item: item.get("analysis_time_ms") or 0)
        values = [float(row["_step_value"]) for row in ordered]
        daily_steps, cumulative_mode = _daily_steps_from_values(values)

        hour_bins: dict[int, float] = defaultdict(float)
        prev_value = None
        prev_hour = None
        for row in ordered:
            ts_ms = row.get("analysis_time_ms")
            hour = None
            if ts_ms is not None:
                hour = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).hour
            value = float(row["_step_value"])
            if cumulative_mode:
                inc = value if prev_value is None else max(value - prev_value, 0.0)
            else:
                inc = max(value, 0.0)
            if hour is not None:
                hour_bins[hour] += inc
                prev_hour = hour
            prev_value = value

        active_hours = len([hour for hour, steps in hour_bins.items() if steps > 0])
        observed_hours = len(hour_bins)
        zero_step_hours = len([hour for hour, steps in hour_bins.items() if steps == 0])
        mean_hourly_steps = (sum(hour_bins.values()) / observed_hours) if observed_hours else 0.0
        daily.append(
            {
                "Subject_ID": subject,
                "Study_day": study_day,
                "Date": day_label,
                "daily_steps": int(round(daily_steps)),
                "step_goal_met_10000": int(daily_steps >= 10000),
                "active_hours": active_hours,
                "observed_hours": observed_hours,
                "sedentary_hours": zero_step_hours,
                "mean_hourly_steps": round(mean_hourly_steps, 3),
                "peak_hourly_steps": int(round(max(hour_bins.values()))) if hour_bins else 0,
                "records": len(ordered),
                "timing_mode": "cumulative" if cumulative_mode else "incremental",
            }
        )

    daily.sort(key=lambda item: (item["Subject_ID"], item["Study_day"] if item["Study_day"] is not None else 10**9))
    return daily, daily


def review_pedometer(rows: list[dict]) -> tuple[PedometerReview | None, list[dict]]:
    daily, _ = extract_pedometer_daily_features(rows)
    if not daily:
        return None, []

    steps = [int(row["daily_steps"]) for row in daily]
    ordered_steps = sorted(steps)
    review = PedometerReview(
        records=len(select_pedometer_rows(rows)),
        subjects=len({row["Subject_ID"] for row in daily if row["Subject_ID"]}),
        distinct_days=len({row["Study_day"] for row in daily if row["Study_day"] is not None}),
        total_steps=sum(steps),
        mean_daily_steps=round(sum(steps) / len(steps), 3) if steps else 0.0,
        median_daily_steps=float(ordered_steps[len(ordered_steps) // 2]) if ordered_steps else 0.0,
        peak_daily_steps=max(steps) if steps else 0,
        goal_days_10000=sum(int(row["step_goal_met_10000"]) for row in daily),
    )
    return review, daily
