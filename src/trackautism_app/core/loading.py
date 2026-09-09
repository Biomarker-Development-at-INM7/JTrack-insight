"""Scoped JSON loading for the Python prototype."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Callable

from trackautism_app.core.indexing import IndexedFile


@dataclass(slots=True)
class LoadedDatasetSummary:
    """Summary of the currently loaded scoped JSON files."""

    files_loaded: int
    records_loaded: int
    subjects: int
    devices: int
    sensors: int
    columns: list[str]
    distinct_study_days: int
    min_study_day: int | None
    max_study_day: int | None


def _rows_from_json_payload(payload: object) -> list[dict]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        return [payload]
    return []


def _coerce_epoch_millis(value: object) -> int | None:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num <= 0:
        return None
    if num < 10_000_000_000:
        num *= 1000
    return int(num)


def _parse_iso_datetime(value: object) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def derive_analysis_time_ms(row: dict) -> int | None:
    """Best-effort timestamp derivation similar to the R loader."""
    candidates = (
        "timestamp",
        "timestamp_start",
        "beginTimeStamp",
        "lastTimeUsed",
        "startTime",
        "time",
        "dateTime",
        "measurementTime",
        "sampleTime",
        "recordedTime",
        "calendarDate",
    )
    for key in candidates:
        if key not in row:
            continue
        epoch_ms = _coerce_epoch_millis(row.get(key))
        if epoch_ms is not None:
            return epoch_ms
        parsed_ms = _parse_iso_datetime(row.get(key))
        if parsed_ms is not None:
            return parsed_ms
    return None


def attach_loaded_time_fields(rows: list[dict]) -> list[dict]:
    """Attach derived timestamp and study-day fields to loaded rows."""
    if not rows:
        return rows

    subject_min_time: dict[str, int] = {}
    for row in rows:
        username = row.get("username")
        analysis_time_ms = derive_analysis_time_ms(row)
        if username and analysis_time_ms is not None:
            current = subject_min_time.get(username)
            if current is None or analysis_time_ms < current:
                subject_min_time[username] = analysis_time_ms

    enriched_rows: list[dict] = []
    for row in rows:
        out = dict(row)
        analysis_time_ms = derive_analysis_time_ms(out)
        out["analysis_time_ms"] = analysis_time_ms
        if analysis_time_ms is not None:
            out["analysis_time_iso"] = datetime.fromtimestamp(
                analysis_time_ms / 1000, tz=timezone.utc
            ).isoformat(timespec="seconds")
        username = out.get("username")
        if username and analysis_time_ms is not None and username in subject_min_time:
            delta_days = (analysis_time_ms - subject_min_time[username]) // 86_400_000
            out["study_day"] = int(delta_days)
        else:
            out["study_day"] = None
        enriched_rows.append(out)
    return enriched_rows


def load_indexed_json_rows(
    indexed_files: list[IndexedFile],
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> list[dict]:
    """Load scoped JSON payloads into row dictionaries with metadata attached."""
    loaded_rows: list[dict] = []

    total = len(indexed_files)
    for position, row in enumerate(indexed_files, start=1):
        try:
            payload = json.loads(row.source_file.read_text(encoding="utf-8"))
        except UnicodeDecodeError:
            payload = json.loads(row.source_file.read_text(encoding="latin-1"))
        except Exception:
            continue

        json_rows = _rows_from_json_payload(payload)
        for record in json_rows:
            enriched = dict(record)
            enriched.setdefault("source_file", str(row.source_file))
            enriched.setdefault("studyId", row.study_id)
            enriched.setdefault("username", row.username)
            enriched.setdefault("deviceid", row.device_id)
            enriched.setdefault("sensorname", row.sensor_name)
            enriched.setdefault("wearable_sensor", row.wearable_sensor)
            loaded_rows.append(enriched)

        if progress_callback and (position == total or position == 1 or position % 5 == 0):
            progress_callback(position, total, f"Reading selected JSON files ({position:,} of {total:,})...")

    if progress_callback:
        progress_callback(total, total, "Deriving analysis time and study-day fields...")
    return attach_loaded_time_fields(loaded_rows)


def filter_loaded_rows_by_study_day(
    rows: list[dict],
    min_day: int | None = None,
    max_day: int | None = None,
) -> list[dict]:
    """Filter loaded rows by derived study-day range."""
    if not rows:
        return rows
    out = rows
    if min_day is not None:
        out = [row for row in out if row.get("study_day") is not None and row.get("study_day") >= min_day]
    if max_day is not None:
        out = [row for row in out if row.get("study_day") is not None and row.get("study_day") <= max_day]
    return out


def summarize_loaded_rows(rows: list[dict]) -> LoadedDatasetSummary:
    """Create a lightweight preview summary for loaded scoped JSON rows."""
    columns = sorted({key for row in rows for key in row.keys()})
    study_days = sorted({row.get("study_day") for row in rows if row.get("study_day") is not None})
    return LoadedDatasetSummary(
        files_loaded=len({row.get("source_file") for row in rows if row.get("source_file")}),
        records_loaded=len(rows),
        subjects=len({row.get("username") for row in rows if row.get("username")}),
        devices=len({row.get("deviceid") for row in rows if row.get("deviceid")}),
        sensors=len(
            {
                (row.get("sensorname"), row.get("wearable_sensor"))
                for row in rows
                if row.get("sensorname") or row.get("wearable_sensor")
            }
        ),
        columns=columns,
        distinct_study_days=len(study_days),
        min_study_day=study_days[0] if study_days else None,
        max_study_day=study_days[-1] if study_days else None,
    )
