"""Application-usage analysis helpers for the Python prototype."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from collections import defaultdict
import math
import re


MAX_FOREGROUND_MS = 21_600_000  # 6 hours, same broad guardrail used in the R app


@dataclass(slots=True)
class ApplicationUsageReview:
    records: int
    subjects: int
    distinct_days: int
    unique_apps: int
    total_foreground_hours: float
    mean_daily_foreground_hours: float
    top_app: str | None
    top_app_hours: float
    unique_categories: int
    top_category: str | None
    top_category_hours: float


def _is_application_usage_row(row: dict) -> bool:
    sensor_name = str(row.get("sensorname") or "").strip().upper()
    wearable = str(row.get("wearable_sensor") or "").strip().upper()
    return sensor_name == "APPLICATION_USAGE" or wearable == "APPLICATION_USAGE"


def select_application_usage_rows(rows: list[dict]) -> list[dict]:
    """Keep only application-usage rows with usable timing and foreground fields."""
    out = []
    seen = set()
    for row in rows:
        if not _is_application_usage_row(row):
            continue
        app_name = str(row.get("appName") or "").strip()
        last_used = row.get("lastTimeUsed")
        total_foreground = row.get("totalTimeinForeground")
        subject = row.get("username")
        dedup_key = (subject, app_name, last_used, total_foreground)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        out.append(dict(row))
    return out


def _safe_float(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _day_label_from_row(row: dict) -> str:
    ts_ms = row.get("analysis_time_ms")
    if ts_ms is None:
        return "Unknown"
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _category_slug(category: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", category.casefold()).strip("_")
    return slug or "uncat"


def _category_label(category: str | None) -> str:
    """Return the user-facing category name used as a feature column.

    Older intermediate tables used names like category_books_hours.  Category
    feature tables should expose the category itself as the feature name, e.g.
    Books, Education, or DeveloperTools.
    """
    text = str(category or "Unknown").strip() or "Unknown"
    lower = text.casefold()
    if lower.startswith("category_"):
        text = text[len("category_"):]
    lower = text.casefold()
    if lower.endswith("_hours"):
        text = text[:-len("_hours")]
    text = text.replace("_", " ").strip()
    if not text:
        return "Unknown"
    return "".join(part[:1].upper() + part[1:] for part in text.split())


def _app_usage_datetime_from_row(row: dict) -> datetime | None:
    """Use usage-time fields before file extraction time for temporal binning."""
    for key in ("lastTimeUsed", "endTimeStamp", "beginTimeStamp", "analysis_time_ms", "timestamp", "timestamp_start", "timestamp_end"):
        value = _safe_float(row.get(key))
        if value is None:
            continue
        if abs(value) < 1e11:
            value *= 1000
        try:
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            continue
    return None


def extract_application_usage_daily_features(
    rows: list[dict],
    app_categories: dict[str, str] | None = None,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Build daily application-usage features per subject."""
    app_categories = app_categories or {}
    app_rows = select_application_usage_rows(rows)
    if not app_rows:
        return [], [], []

    grouped: dict[tuple[str, int | None, str, str, str], list[dict]] = defaultdict(list)
    for row in app_rows:
        subject = str(row.get("username") or "")
        study_day = row.get("study_day")
        day_label = _day_label_from_row(row)
        app_name = str(row.get("appName") or "Unknown")
        app_category = _category_label(app_categories.get(app_name.casefold(), "Unknown") or "Unknown")
        grouped[(subject, study_day, day_label, app_name, app_category)].append(row)

    per_app_daily: list[dict] = []
    for (subject, study_day, day_label, app_name, app_category), app_group in grouped.items():
        ordered = sorted(
            app_group,
            key=lambda item: (
                item.get("analysis_time_ms") if item.get("analysis_time_ms") is not None else 0,
                str(item.get("lastTimeUsed") or ""),
            ),
        )
        increments_ms: list[float] = []
        prev_total: float | None = None
        for row in ordered:
            total_ms = _safe_float(row.get("totalTimeinForeground"))
            if total_ms is None or total_ms < 0 or total_ms > MAX_FOREGROUND_MS:
                continue
            if prev_total is None:
                inc = max(total_ms, 0.0)
            elif total_ms >= prev_total:
                inc = max(total_ms - prev_total, 0.0)
            else:
                inc = max(total_ms, 0.0)
            prev_total = total_ms
            increments_ms.append(inc)

        if not increments_ms:
            continue
        per_app_daily.append(
            {
                "Subject_ID": subject,
                "Study_day": study_day,
                "Date": day_label,
                "appName": app_name,
                "app_category": app_category,
                "foreground_minutes": round(sum(increments_ms) / 60_000, 3),
                "records": len(ordered),
            }
        )

    collapsed: dict[tuple[str, int | None, str], dict] = {}
    category_daily: dict[tuple[str, int | None, str, str], float] = defaultdict(float)
    for row in per_app_daily:
        key = (row["Subject_ID"], row["Study_day"], row["Date"])
        current = collapsed.setdefault(
            key,
            {
                "Subject_ID": row["Subject_ID"],
                "Study_day": row["Study_day"],
                "Date": row["Date"],
                "total_foreground_hours": 0.0,
                "unique_apps": 0,
                "category_set": set(),
                "top_app": None,
                "top_app_minutes": 0.0,
                "top_category": None,
                "top_category_minutes": 0.0,
                "app_minutes": [],
                "records": 0,
            },
        )
        current["total_foreground_hours"] += row["foreground_minutes"] / 60
        current["app_minutes"].append(float(row["foreground_minutes"]))
        current["unique_apps"] += 1
        current["category_set"].add(row["app_category"])
        current["records"] += row["records"]
        if row["foreground_minutes"] > current["top_app_minutes"]:
            current["top_app_minutes"] = row["foreground_minutes"]
            current["top_app"] = row["appName"]
        category_key = (row["Subject_ID"], row["Study_day"], row["Date"], row["app_category"])
        category_daily[category_key] += row["foreground_minutes"] / 60

    category_columns = sorted(
        {
            str(category)
            for (_, _, _, category) in category_daily
        }
        | {
            _category_label(str(category).strip())
            for category in app_categories.values()
            if str(category).strip()
        }
        | {"Unknown"}
    )
    for row in collapsed.values():
        for category in category_columns:
            row[_category_label(category)] = 0.0

    for (subject, study_day, day_label, category), hours in category_daily.items():
        key = (subject, study_day, day_label)
        current = collapsed[key]
        current[_category_label(category)] = round(hours, 3)
        if hours * 60 > current["top_category_minutes"]:
            current["top_category_minutes"] = hours * 60
            current["top_category"] = _category_label(category)

    out = []
    for row in collapsed.values():
        minutes = [m for m in row.get("app_minutes", []) if m > 0]
        total_minutes = sum(minutes)
        shares = [m / total_minutes for m in minutes] if total_minutes > 0 else []
        app_entropy = -sum(p * math.log(p) for p in shares) if shares else 0.0
        app_hhi = sum(p * p for p in shares) if shares else 0.0
        top_app_share = (row["top_app_minutes"] / total_minutes) if total_minutes > 0 else 0.0
        base = {
            "Subject_ID": row["Subject_ID"],
            "Study_day": row["Study_day"],
            "Date": row["Date"],
            "total_foreground_hours": round(row["total_foreground_hours"], 3),
            "unique_apps": row["unique_apps"],
            "app_entropy": round(app_entropy, 4),
            "app_hhi": round(app_hhi, 4),
            "top_app_share": round(top_app_share, 4),
            "unique_categories": len(row["category_set"]),
            "top_app": row["top_app"],
            "top_app_hours": round(row["top_app_minutes"] / 60, 3),
            "top_category": row["top_category"],
            "top_category_hours": round(row["top_category_minutes"] / 60, 3),
            "records": row["records"],
        }
        for category in category_columns:
            base[_category_label(category)] = row[_category_label(category)]
        out.append(base)
    out.sort(key=lambda item: (item["Subject_ID"], item["Study_day"] if item["Study_day"] is not None else 10**9))
    category_rows_long = [
        {
            "Subject_ID": subject,
            "Study_day": study_day,
            "Date": day_label,
            "category": category,
            "foreground_hours": round(hours, 3),
        }
        for (subject, study_day, day_label, category), hours in sorted(category_daily.items())
    ]

    # Previous JTrack Insight category-preview format: one row per subject/day,
    # one column per application category, and the cell value is foreground usage
    # in hours for that category.
    category_rows_wide = []
    for row in out:
        wide_row = {
            "Subject_ID": row["Subject_ID"],
            "Study_day": row["Study_day"],
            "Date": row["Date"],
        }
        for category in category_columns:
            wide_row[_category_label(category)] = round(row.get(_category_label(category), 0.0), 3)
        category_rows_wide.append(wide_row)

    return out, category_rows_long, category_rows_wide


def _datetime_from_row(row: dict) -> datetime | None:
    """Best-effort UTC datetime from application-usage event fields."""
    return _app_usage_datetime_from_row(row)


def _temporal_bin_from_row(row: dict, temporal_frequency: str) -> tuple[object, str, str]:
    """Return a sortable key, display label, and study-day value for app usage rows."""
    freq = (temporal_frequency or "daily").lower()
    if freq == "study_duration":
        return "study_duration", "Full study duration", ""
    dt = _datetime_from_row(row)
    if dt is not None:
        if freq == "hourly":
            return dt.strftime("%Y-%m-%d %H:00"), dt.strftime("%Y-%m-%d %H:00"), str(row.get("study_day") or "")
        if freq == "monthly":
            return dt.strftime("%Y-%m"), dt.strftime("%Y-%m"), str(row.get("study_day") or "")
        return dt.strftime("%Y-%m-%d"), dt.strftime("%Y-%m-%d"), str(row.get("study_day") or "")
    study_day = row.get("study_day")
    if freq == "hourly":
        return f"study_day_{study_day}_hour_unknown", f"Study day {study_day}, hour unknown", str(study_day or "")
    if freq == "monthly":
        return "month_unknown", "Unknown month", str(study_day or "")
    return study_day, _day_label_from_row(row), str(study_day or "")



def _floor_hour(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)




def _datetime_from_epoch_ms(value: object) -> datetime | None:
    num = _safe_float(value)
    if num is None:
        return None
    if abs(num) < 1e11:
        num *= 1000
    try:
        return datetime.fromtimestamp(num / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _add_interval_increment_to_bins(
    bins: dict[tuple[str, object, str], dict],
    category_names: set[str],
    *,
    subject: str,
    row: dict,
    app_name: str,
    app_category: str,
    temporal_frequency: str,
    inc_ms: float,
    interval_start: datetime | None,
    interval_end: datetime | None,
) -> None:
    """Distribute foreground time over hourly bins using the available usage window.

    App-usage exports can represent either hourly windows or longer cumulative
    snapshots. When an interval is available, distributing proportionally across
    crossed hours prevents daily totals from being placed at 00:00 in hourly
    output. If no reliable interval exists, the value is assigned to the event
    hour as a conservative fallback.
    """
    freq = (temporal_frequency or "daily").lower()
    hours_total = inc_ms / 3_600_000
    if hours_total <= 0:
        return

    if freq != "hourly":
        _add_category_hours_to_bins(
            bins, category_names, subject=subject, row=row, app_name=app_name,
            app_category=app_category, temporal_frequency=freq, hours=hours_total,
            bin_dt=interval_end or interval_start,
        )
        return

    if interval_start is None or interval_end is None or interval_end <= interval_start:
        _add_category_hours_to_bins(
            bins, category_names, subject=subject, row=row, app_name=app_name,
            app_category=app_category, temporal_frequency=freq, hours=hours_total,
            bin_dt=interval_end or interval_start,
        )
        return

    elapsed_sec = (interval_end - interval_start).total_seconds()
    if elapsed_sec <= 0 or elapsed_sec > 7 * 24 * 3600:
        _add_category_hours_to_bins(
            bins, category_names, subject=subject, row=row, app_name=app_name,
            app_category=app_category, temporal_frequency=freq, hours=hours_total,
            bin_dt=interval_end,
        )
        return

    cursor = interval_start
    while cursor < interval_end:
        hour_start = _floor_hour(cursor)
        next_hour = hour_start + timedelta(hours=1)
        segment_end = min(next_hour, interval_end)
        segment_sec = max((segment_end - cursor).total_seconds(), 0)
        if segment_sec > 0:
            part_hours = hours_total * (segment_sec / elapsed_sec)
            _add_category_hours_to_bins(
                bins, category_names, subject=subject, row=row, app_name=app_name,
                app_category=app_category, temporal_frequency=freq, hours=part_hours,
                bin_dt=cursor,
            )
        cursor = segment_end

def _add_category_hours_to_bins(
    bins: dict[tuple[str, object, str], dict],
    category_names: set[str],
    *,
    subject: str,
    row: dict,
    app_name: str,
    app_category: str,
    temporal_frequency: str,
    hours: float,
    bin_dt: datetime | None = None,
) -> None:
    """Add foreground hours to the selected temporal bin without changing feature names."""
    freq = (temporal_frequency or "daily").lower()
    category_col = _category_label(app_category)
    category_names.add(category_col)

    if freq == "study_duration":
        sort_key, label, study_day_text = "study_duration", "Full study duration", ""
    elif bin_dt is not None:
        if freq == "hourly":
            sort_key = bin_dt.strftime("%Y-%m-%d %H:00")
            label = sort_key
        elif freq == "monthly":
            sort_key = bin_dt.strftime("%Y-%m")
            label = sort_key
        else:
            sort_key = bin_dt.strftime("%Y-%m-%d")
            label = sort_key
        study_day_text = str(row.get("study_day") or "")
    else:
        sort_key, label, study_day_text = _temporal_bin_from_row(row, freq)

    key = (subject, sort_key, label)
    current = bins.setdefault(
        key,
        {
            "Subject_ID": subject,
            "Study_day": row.get("study_day") if study_day_text else "",
            "Date": label,
            "time_bin": label,
            "temporal_frequency": freq,
            "total_foreground_hours": 0.0,
            "unique_apps": set(),
            "unique_categories": set(),
            "top_category": None,
            "top_category_hours": 0.0,
            "records": 0,
            "_sort_key": sort_key,
        },
    )
    current[category_col] = current.get(category_col, 0.0) + hours
    current["total_foreground_hours"] += hours
    current["unique_apps"].add(app_name)
    current["unique_categories"].add(category_col)
    current["records"] += 1
    if current[category_col] > current["top_category_hours"]:
        current["top_category"] = category_col
        current["top_category_hours"] = current[category_col]


def _add_hourly_increment_to_bins(
    bins: dict[tuple[str, object, str], dict],
    category_names: set[str],
    *,
    subject: str,
    row: dict,
    app_name: str,
    app_category: str,
    temporal_frequency: str,
    inc_ms: float,
    previous_dt: datetime | None,
    current_dt: datetime | None,
    begin_dt: datetime | None = None,
    end_dt: datetime | None = None,
) -> None:
    """Allocate app-usage increments into the requested temporal bin.

    For hourly output, a valid begin/end usage window is preferred. This avoids
    collapsing a full day of foreground time into the 00:00 hour when exports
    contain daily snapshots. If no window is available, cumulative increments are
    allocated between consecutive app events, and finally to the event hour as a
    fallback.
    """
    freq = (temporal_frequency or "daily").lower()
    if freq == "hourly" and begin_dt is not None and end_dt is not None and end_dt > begin_dt:
        _add_interval_increment_to_bins(
            bins, category_names, subject=subject, row=row, app_name=app_name,
            app_category=app_category, temporal_frequency=freq, inc_ms=inc_ms,
            interval_start=begin_dt, interval_end=end_dt,
        )
        return

    interval_start = previous_dt if previous_dt is not None and current_dt is not None and previous_dt < current_dt else None
    interval_end = current_dt
    _add_interval_increment_to_bins(
        bins, category_names, subject=subject, row=row, app_name=app_name,
        app_category=app_category, temporal_frequency=freq, inc_ms=inc_ms,
        interval_start=interval_start, interval_end=interval_end,
    )

def extract_application_usage_category_features(
    rows: list[dict],
    app_categories: dict[str, str] | None = None,
    temporal_frequency: str = "daily",
) -> list[dict]:
    """Return application-usage category features in the original wide format.

    This restores the earlier Step 5 style: one row per participant and temporal
    bin, with one numeric column per app category. The temporal resolution is
    stored in ``temporal_frequency``/``time_bin`` rather than being encoded in
    feature names.
    """
    app_categories = app_categories or {}
    app_rows = select_application_usage_rows(rows)
    if not app_rows:
        return []

    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in app_rows:
        subject = str(row.get("username") or "")
        app_name = str(row.get("appName") or "Unknown")
        grouped[(subject, app_name)].append(row)

    bins: dict[tuple[str, object, str], dict] = {}
    category_names = {
        _category_label(str(category).strip())
        for category in app_categories.values()
        if str(category).strip()
    } | {"Unknown"}

    for (subject, app_name), app_group in grouped.items():
        ordered = sorted(
            app_group,
            key=lambda item: (
                _app_usage_datetime_from_row(item) or datetime.min.replace(tzinfo=timezone.utc),
                _safe_float(item.get("lastTimeUsed")) or 0,
            ),
        )
        prev_total: float | None = None
        prev_dt: datetime | None = None
        app_category = _category_label(app_categories.get(app_name.casefold(), "Unknown") or "Unknown")
        category_names.add(_category_label(app_category))
        for row in ordered:
            total_ms = _safe_float(row.get("totalTimeinForeground"))
            last_used_dt = _datetime_from_epoch_ms(row.get("lastTimeUsed"))
            current_dt = last_used_dt or _app_usage_datetime_from_row(row)
            if total_ms is None or total_ms < 0 or total_ms > MAX_FOREGROUND_MS:
                if current_dt is not None:
                    prev_dt = current_dt
                continue

            begin_ms = _safe_float(row.get("beginTimeStamp"))
            end_ms = _safe_float(row.get("endTimeStamp"))
            begin_dt = _datetime_from_epoch_ms(begin_ms)
            end_dt = _datetime_from_epoch_ms(end_ms)
            begin_ms_norm = begin_ms * 1000 if begin_ms is not None and abs(begin_ms) < 1e11 else begin_ms
            end_ms_norm = end_ms * 1000 if end_ms is not None and abs(end_ms) < 1e11 else end_ms
            window_ms = (end_ms_norm - begin_ms_norm) if begin_ms_norm is not None and end_ms_norm is not None else None
            freq = (temporal_frequency or "daily").lower()
            row_is_hour_window = freq == "hourly" and window_ms is not None and 0 < window_ms <= 2 * 3_600_000

            if row_is_hour_window:
                # Rows that already represent an hourly usage window should be
                # treated as window totals, not cumulative counters.
                inc_ms = max(total_ms, 0.0)
                interval_start = None
            elif prev_total is None:
                inc_ms = max(total_ms, 0.0)
                interval_start = None
            elif total_ms >= prev_total:
                inc_ms = max(total_ms - prev_total, 0.0)
                interval_start = prev_dt
            else:
                # Android UsageStats counters can reset at day/window boundaries.
                inc_ms = max(total_ms, 0.0)
                interval_start = None

            # Match the previous JTrack/R behavior: when lastTimeUsed exists,
            # the derived increment belongs to the hour/date of that event.
            # This prevents daily begin/end windows from spreading or collapsing
            # category totals into midnight bins.
            if freq == "hourly" and last_used_dt is not None and not row_is_hour_window:
                if prev_dt is not None and last_used_dt > prev_dt:
                    elapsed_ms = (last_used_dt - prev_dt).total_seconds() * 1000
                    if elapsed_ms >= 0 and inc_ms > elapsed_ms:
                        inc_ms = elapsed_ms
                if inc_ms > 0:
                    _add_category_hours_to_bins(
                        bins,
                        category_names,
                        subject=subject,
                        row=row,
                        app_name=app_name,
                        app_category=app_category,
                        temporal_frequency=temporal_frequency,
                        hours=inc_ms / 3_600_000,
                        bin_dt=last_used_dt,
                    )
            elif inc_ms > 0:
                # If lastTimeUsed is missing, use a true row window when possible;
                # otherwise fall back to the available event timestamp.
                pass_begin = begin_dt if row_is_hour_window or last_used_dt is None else None
                pass_end = end_dt if row_is_hour_window or last_used_dt is None else None
                _add_hourly_increment_to_bins(
                    bins,
                    category_names,
                    subject=subject,
                    row=row,
                    app_name=app_name,
                    app_category=app_category,
                    temporal_frequency=temporal_frequency,
                    inc_ms=inc_ms,
                    previous_dt=interval_start,
                    current_dt=current_dt,
                    begin_dt=pass_begin,
                    end_dt=pass_end,
                )

            if not row_is_hour_window:
                prev_total = total_ms
                if current_dt is not None:
                    prev_dt = current_dt

    ordered_categories = sorted(category_names)
    out: list[dict] = []
    for item in bins.values():
        row = {
            "Subject_ID": item["Subject_ID"],
            "Study_day": item["Study_day"],
            "Date": item["Date"],
            "time_bin": item["time_bin"],
            "temporal_frequency": item["temporal_frequency"],
            "total_foreground_hours": round(item["total_foreground_hours"], 3),
            "unique_apps": len(item["unique_apps"]),
            "unique_categories": len(item["unique_categories"]),
            "top_category": item["top_category"],
            "top_category_hours": round(item["top_category_hours"], 3),
            "records": item["records"],
            "_sort_key": item["_sort_key"],
        }
        for category in ordered_categories:
            row[category] = round(float(item.get(category, 0.0)), 3)
        out.append(row)

    out.sort(key=lambda item: (str(item["Subject_ID"]), str(item.get("_sort_key", ""))))
    for item in out:
        item.pop("_sort_key", None)
    return out


def review_application_usage(
    rows: list[dict],
    app_categories: dict[str, str] | None = None,
) -> tuple[ApplicationUsageReview | None, list[dict], list[dict], list[dict]]:
    """Return a compact review summary and the daily feature table."""
    daily, category_daily_long, category_daily_wide = extract_application_usage_daily_features(
        rows,
        app_categories=app_categories,
    )
    if not daily:
        return None, [], [], []

    app_hours: dict[str, float] = defaultdict(float)
    category_hours: dict[str, float] = defaultdict(float)
    subjects = {row["Subject_ID"] for row in daily if row["Subject_ID"]}
    study_days = {row["Study_day"] for row in daily if row["Study_day"] is not None}
    total_hours = 0.0
    unique_apps = set()
    unique_categories = set()
    records = 0

    raw_app_rows = select_application_usage_rows(rows)
    for row in raw_app_rows:
        app_name = str(row.get("appName") or "Unknown")
        unique_apps.add(app_name)
        category = (app_categories or {}).get(app_name.casefold(), "Unknown") or "Unknown"
        unique_categories.add(category)
        records += 1

    for day_row in daily:
        total_hours += float(day_row["total_foreground_hours"])
        if day_row["top_app"]:
            app_hours[str(day_row["top_app"])] += float(day_row["top_app_hours"])
        if day_row["top_category"]:
            category_hours[str(day_row["top_category"])] += float(day_row["top_category_hours"])

    top_app = None
    top_app_hours = 0.0
    if app_hours:
        top_app, top_app_hours = max(app_hours.items(), key=lambda item: item[1])
    top_category = None
    top_category_hours = 0.0
    if category_hours:
        top_category, top_category_hours = max(category_hours.items(), key=lambda item: item[1])

    review = ApplicationUsageReview(
        records=records,
        subjects=len(subjects),
        distinct_days=len(study_days),
        unique_apps=len(unique_apps),
        total_foreground_hours=round(total_hours, 3),
        mean_daily_foreground_hours=round(total_hours / len(daily), 3) if daily else 0.0,
        top_app=top_app,
        top_app_hours=round(top_app_hours, 3),
        unique_categories=len(unique_categories),
        top_category=top_category,
        top_category_hours=round(top_category_hours, 3),
    )
    return review, daily, category_daily_long, category_daily_wide
