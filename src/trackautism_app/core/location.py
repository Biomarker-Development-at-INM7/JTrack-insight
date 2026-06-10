"""Location analysis helpers for the Python prototype."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import math


@dataclass(slots=True)
class LocationReview:
    records: int
    subjects: int
    distinct_days: int
    providers: int
    total_distance_km: float
    mean_daily_distance_km: float
    median_accuracy_m: float
    max_daily_distance_km: float


def _is_location_row(row: dict) -> bool:
    sensor_name = str(row.get("sensorname") or "").strip().upper()
    wearable = str(row.get("wearable_sensor") or "").strip().upper()
    return sensor_name in {"LOCATION", "GPS", "GEOLOCATION"} or wearable in {"LOCATION", "GPS", "GEOLOCATION"}


def _safe_float(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _valid_lat_lon(row: dict) -> tuple[float, float] | None:
    lat = _safe_float(row.get("lat", row.get("latitude")))
    lon = _safe_float(row.get("lon", row.get("longitude")))
    if lat is None or lon is None:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def _day_label_from_row(row: dict) -> str:
    ts_ms = row.get("analysis_time_ms")
    if ts_ms is None:
        return "Unknown"
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_km = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius_km * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def select_location_rows(rows: list[dict]) -> list[dict]:
    out = []
    for row in rows:
        if not _is_location_row(row):
            continue
        coords = _valid_lat_lon(row)
        if coords is None:
            continue
        enriched = dict(row)
        enriched["_lat"], enriched["_lon"] = coords
        out.append(enriched)
    return out


def extract_location_daily_features(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    location_rows = select_location_rows(rows)
    if not location_rows:
        return [], []

    grouped: dict[tuple[str, int | None, str], list[dict]] = defaultdict(list)
    for row in location_rows:
        grouped[(str(row.get("username") or ""), row.get("study_day"), _day_label_from_row(row))].append(row)

    daily = []
    trajectory_rows = []
    for (subject, study_day, day_label), group_rows in grouped.items():
        ordered = sorted(group_rows, key=lambda item: item.get("analysis_time_ms") or 0)
        daily_distance_km = 0.0
        accuracies: list[float] = []
        speeds_kmh: list[float] = []
        step_distances_km: list[float] = []
        prev = None
        for row in ordered:
            trajectory_rows.append(
                {
                    "Subject_ID": subject,
                    "Study_day": study_day,
                    "Date": day_label,
                    "analysis_time_ms": row.get("analysis_time_ms"),
                    "lat": row["_lat"],
                    "lon": row["_lon"],
                    "accuracy": _safe_float(row.get("accuracy")),
                    "provider": str(row.get("provider") or ""),
                }
            )
            accuracy = _safe_float(row.get("accuracy"))
            if accuracy is not None:
                accuracies.append(accuracy)
            if prev is not None:
                distance_km = _haversine_km(prev["_lat"], prev["_lon"], row["_lat"], row["_lon"])
                dt_ms = (row.get("analysis_time_ms") or 0) - (prev.get("analysis_time_ms") or 0)
                if dt_ms > 0:
                    speed_kmh = distance_km / (dt_ms / 3_600_000)
                    if speed_kmh <= 250:
                        daily_distance_km += distance_km
                        speeds_kmh.append(speed_kmh)
                        step_distances_km.append(distance_km)
            prev = row

        lat_values = [row["_lat"] for row in ordered]
        lon_values = [row["_lon"] for row in ordered]
        centroid_lat = sum(lat_values) / len(lat_values) if lat_values else None
        centroid_lon = sum(lon_values) / len(lon_values) if lon_values else None
        radius_values = [
            _haversine_km(centroid_lat, centroid_lon, row["_lat"], row["_lon"])
            for row in ordered
        ] if centroid_lat is not None and centroid_lon is not None else []
        radius_gyration_km = math.sqrt(sum(r * r for r in radius_values) / len(radius_values)) if radius_values else 0.0
        unique_location_bins = len({(round(row["_lat"], 3), round(row["_lon"], 3)) for row in ordered})
        stationary_points = sum(1 for d in step_distances_km if d < 0.01)
        stationary_pct = (100 * stationary_points / len(step_distances_km)) if step_distances_km else None

        daily.append(
            {
                "Subject_ID": subject,
                "Study_day": study_day,
                "Date": day_label,
                "daily_distance_km": round(daily_distance_km, 3),
                "radius_gyration_km": round(radius_gyration_km, 3),
                "unique_location_bins": unique_location_bins,
                "lat_span": round(max(lat_values) - min(lat_values), 6) if lat_values else None,
                "lon_span": round(max(lon_values) - min(lon_values), 6) if lon_values else None,
                "median_speed_kmh": round(sorted(speeds_kmh)[len(speeds_kmh) // 2], 3) if speeds_kmh else None,
                "max_speed_kmh": round(max(speeds_kmh), 3) if speeds_kmh else None,
                "stationary_pct": round(stationary_pct, 3) if stationary_pct is not None else None,
                "records": len(ordered),
                "median_accuracy_m": round(sorted(accuracies)[len(accuracies) // 2], 3) if accuracies else None,
                "providers": len({str(row.get('provider') or '') for row in ordered if str(row.get('provider') or '')}),
            }
        )

    daily.sort(key=lambda item: (item["Subject_ID"], item["Study_day"] if item["Study_day"] is not None else 10**9))
    trajectory_rows.sort(
        key=lambda item: (
            item["Subject_ID"],
            item["Study_day"] if item["Study_day"] is not None else 10**9,
            item["analysis_time_ms"] if item["analysis_time_ms"] is not None else 0,
        )
    )
    return daily, trajectory_rows


def review_location(rows: list[dict]) -> tuple[LocationReview | None, list[dict], list[dict]]:
    daily, trajectory_rows = extract_location_daily_features(rows)
    if not daily:
        return None, [], []

    location_rows = select_location_rows(rows)
    accuracies = sorted(
        accuracy
        for accuracy in (_safe_float(row.get("accuracy")) for row in location_rows)
        if accuracy is not None
    )
    total_distance = sum(float(row["daily_distance_km"]) for row in daily)
    review = LocationReview(
        records=len(location_rows),
        subjects=len({row["Subject_ID"] for row in daily if row["Subject_ID"]}),
        distinct_days=len({row["Study_day"] for row in daily if row["Study_day"] is not None}),
        providers=len({str(row.get("provider") or "") for row in location_rows if str(row.get("provider") or "")}),
        total_distance_km=round(total_distance, 3),
        mean_daily_distance_km=round(total_distance / len(daily), 3) if daily else 0.0,
        median_accuracy_m=round(accuracies[len(accuracies) // 2], 3) if accuracies else 0.0,
        max_daily_distance_km=round(max(float(row["daily_distance_km"]) for row in daily), 3),
    )
    return review, daily, trajectory_rows
