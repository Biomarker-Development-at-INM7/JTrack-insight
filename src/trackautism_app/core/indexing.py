"""Dataset indexing and folder parsing.

This module will be the first Python port from the R app because it drives the
entire workflow and is comparatively easy to validate.
"""

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Callable


SUBJECT_PATTERN = re.compile(r"^TrackAutism_[0-9]+_[0-9]+$")
KNOWN_SENSORS = {
    "activity",
    "application_usage",
    "location",
    "gps",
    "geolocation",
    "bbi",
    "hrv",
    "heart_rate",
    "heartrate",
    "pedometer",
    "steps",
    "calories",
    "sleep",
    "ema",
    "lockunlock",
    "lock_unlock",
}
EXCLUDED_PATH_PATTERNS = (
    "__MACOSX",
    "_excluded_files",
    "_clean_dataset",
    "_original_dataset_backup",
    "_qc_removed",
)


@dataclass(slots=True)
class IndexedFile:
    source_file: Path
    study_id: str | None = None
    username: str | None = None
    device_id: str | None = None
    sensor_name: str | None = None
    wearable_sensor: str | None = None


def _clean_choice(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value if value else None


def _normalize_dir_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")


KNOWN_SENSOR_KEY_MAP = { _normalize_dir_key(sensor): sensor for sensor in KNOWN_SENSORS }


def _clean_study_guess(username: str | None) -> str | None:
    if not username:
        return None
    guess = re.sub(r"_?[0-9]+_?.*$", "", username)
    return guess if guess and guess != username else None


def _should_keep_json(root_dir: Path, file_path: Path) -> bool:
    rel = file_path.relative_to(root_dir).as_posix()
    if "/__MACOSX/" in f"/{rel}/":
        return False
    if any(part.startswith("._") for part in file_path.parts):
        return False
    lower_rel = rel.lower()
    return not any(pattern.lower() in lower_rel for pattern in EXCLUDED_PATH_PATTERNS)


def extract_metadata_from_path(file_path: Path, root_dir: Path) -> IndexedFile:
    """Port the path-based metadata inference from the R app."""
    file_abs = file_path.resolve()
    root_abs = root_dir.resolve()
    rel_parts = list(file_abs.relative_to(root_abs).parts)
    filename = rel_parts[-1]
    dirs = rel_parts[:-1]
    root_leaf = root_abs.name

    study_file: str | None = None
    user_file: str | None = None
    device_file: str | None = None
    sensor_folder: str | None = None

    dir_keys = [_normalize_dir_key(part) for part in dirs]
    sensor_indices = [idx for idx, key in enumerate(dir_keys) if key in KNOWN_SENSOR_KEY_MAP]
    sensor_idx = sensor_indices[-1] if sensor_indices else (len(dirs) - 1 if dirs else None)

    if sensor_idx is not None and sensor_idx >= 0:
        sensor_folder = dirs[sensor_idx]
        before_sensor = dirs[:sensor_idx]
        if len(before_sensor) >= 3:
            study_file = before_sensor[-3]
            user_file = before_sensor[-2]
            device_file = before_sensor[-1]
        elif len(before_sensor) == 2:
            if SUBJECT_PATTERN.match(before_sensor[0]):
                user_file = before_sensor[0]
                device_file = before_sensor[1]
            elif SUBJECT_PATTERN.match(root_leaf):
                user_file = root_leaf
                device_file = before_sensor[0]
            else:
                user_file = before_sensor[0]
                device_file = before_sensor[1]
            study_file = _clean_study_guess(user_file)
        elif len(before_sensor) == 1:
            device_file = before_sensor[0]
            if root_leaf:
                user_file = root_leaf
                study_file = _clean_study_guess(user_file)

    sensor_name = sensor_folder
    wearable_sensor: str | None = None
    sensor_key = _normalize_dir_key(sensor_folder or "")
    if sensor_folder and "_" in sensor_folder:
        matching_known_prefixes = [
            known_key
            for known_key in KNOWN_SENSOR_KEY_MAP
            if sensor_key == known_key or sensor_key.startswith(f"{known_key}_")
        ]
        if matching_known_prefixes:
            best_key = max(matching_known_prefixes, key=len)
            sensor_name = KNOWN_SENSOR_KEY_MAP[best_key]
            wearable_suffix = sensor_key[len(best_key):].lstrip("_")
            wearable_sensor = wearable_suffix.upper() if wearable_suffix else None
        elif sensor_key not in KNOWN_SENSOR_KEY_MAP:
            sensor_name = sensor_folder.split("_", 1)[0]
            wearable_sensor = sensor_folder.split("_", 1)[1]

    return IndexedFile(
        source_file=file_abs,
        study_id=study_file,
        username=user_file,
        device_id=device_file,
        sensor_name=sensor_name,
        wearable_sensor=wearable_sensor,
    )


def scan_dataset_metadata(
    root_dir: Path,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> list[IndexedFile]:
    """Scan a dataset root and infer study/user/device/sensor metadata."""
    root_dir = Path(root_dir).expanduser().resolve()
    if not root_dir.exists() or not root_dir.is_dir():
        raise FileNotFoundError(f"Dataset root not found: {root_dir}")

    if progress_callback:
        progress_callback(0, 0, "Finding JSON files in the selected folder...")
    files = sorted(path for path in root_dir.rglob("*.json") if _should_keep_json(root_dir, path))
    total = len(files)
    indexed: list[IndexedFile] = []
    for position, path in enumerate(files, start=1):
        indexed.append(extract_metadata_from_path(path, root_dir))
        if progress_callback and (position == total or position == 1 or position % 25 == 0):
            progress_callback(position, total, f"Indexing file metadata ({position:,} of {total:,})...")
    return indexed


def summarize_indexed_files(indexed_files: list[IndexedFile]) -> dict[str, int]:
    """Return quick summary counts for the current metadata index."""
    return {
        "json_files": len(indexed_files),
        "subjects": len({row.username for row in indexed_files if row.username}),
        "devices": len({row.device_id for row in indexed_files if row.device_id}),
        "sensors": len(
            {
                (row.sensor_name or "", row.wearable_sensor or "")
                for row in indexed_files
                if row.sensor_name or row.wearable_sensor
            }
        ),
    }


def available_filter_choices(indexed_files: list[IndexedFile]) -> dict[str, list[str]]:
    """Return sorted distinct values for metadata-based filtering."""
    return {
        "username": sorted({_clean_choice(row.username) for row in indexed_files if _clean_choice(row.username)}),
        "device_id": sorted({_clean_choice(row.device_id) for row in indexed_files if _clean_choice(row.device_id)}),
        "sensor_name": sorted({_clean_choice(row.sensor_name) for row in indexed_files if _clean_choice(row.sensor_name)}),
        "wearable_sensor": sorted({_clean_choice(row.wearable_sensor) for row in indexed_files if _clean_choice(row.wearable_sensor)}),
    }


def filter_indexed_files(
    indexed_files: list[IndexedFile],
    username: str | None = None,
    device_id: str | None = None,
    sensor_name: str | None = None,
    wearable_sensor: str | None = None,
) -> list[IndexedFile]:
    """Apply metadata-based scope filters to indexed files."""
    username = _clean_choice(username)
    device_id = _clean_choice(device_id)
    sensor_name = _clean_choice(sensor_name)
    wearable_sensor = _clean_choice(wearable_sensor)

    out = indexed_files
    if username and username != "All":
        out = [row for row in out if _clean_choice(row.username) == username]
    if device_id and device_id != "All":
        out = [row for row in out if _clean_choice(row.device_id) == device_id]
    if sensor_name and sensor_name != "All":
        out = [row for row in out if _clean_choice(row.sensor_name) == sensor_name]
    if wearable_sensor and wearable_sensor != "All":
        out = [row for row in out if _clean_choice(row.wearable_sensor) == wearable_sensor]
    return out
