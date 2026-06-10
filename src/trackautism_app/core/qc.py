"""Quality-control logic for duplicate files, bad JSON, and exclusions."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import md5
import json
from pathlib import Path

from trackautism_app.core.indexing import IndexedFile


@dataclass(slots=True)
class QCScanItem:
    """Per-file QC result."""

    source_file: Path
    file_hash: str | None
    file_size_bytes: int | None
    json_valid: bool
    json_error: str | None
    duplicate_file: bool
    duplicate_group: str | None
    qc_status: str


@dataclass(slots=True)
class QCSummary:
    """Dataset-level QC summary."""

    total_files: int
    valid_json_files: int
    invalid_json_files: int
    duplicate_files: int
    clean_files: int


def _file_md5(file_path: Path) -> str:
    digest = md5()
    with file_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_json_text(file_path: Path) -> tuple[bool, str | None]:
    try:
        text = file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            text = file_path.read_text(encoding="latin-1")
        except Exception as exc:  # pragma: no cover - filesystem edge path
            return False, str(exc)
    except Exception as exc:  # pragma: no cover - filesystem edge path
        return False, str(exc)

    if not text.strip():
        return False, "Empty or unreadable JSON file"

    try:
        json.loads(text)
    except Exception as exc:
        return False, str(exc)
    return True, None


def scan_file_qc(indexed_files: list[IndexedFile]) -> list[QCScanItem]:
    """Run content-hash duplicate detection and JSON validity checks."""
    results: list[QCScanItem] = []
    hash_counts: dict[str, int] = {}
    interim_rows: list[tuple[IndexedFile, str | None, int | None, bool, str | None]] = []

    for row in indexed_files:
        file_path = row.source_file
        file_hash: str | None = None
        file_size: int | None = None
        json_valid = False
        json_error: str | None = None

        try:
            file_size = file_path.stat().st_size
        except Exception as exc:  # pragma: no cover - filesystem edge path
            json_error = str(exc)

        try:
            file_hash = _file_md5(file_path)
            if file_hash:
                hash_counts[file_hash] = hash_counts.get(file_hash, 0) + 1
        except Exception as exc:  # pragma: no cover - filesystem edge path
            json_error = str(exc)

        if json_error is None:
            json_valid, json_error = _validate_json_text(file_path)

        interim_rows.append((row, file_hash, file_size, json_valid, json_error))

    for row, file_hash, file_size, json_valid, json_error in interim_rows:
        duplicate = bool(file_hash and hash_counts.get(file_hash, 0) > 1)
        duplicate_group = file_hash[:12] if duplicate and file_hash else None
        qc_status = "clean"
        if not json_valid:
            qc_status = "invalid_json"
        elif duplicate:
            qc_status = "duplicate"

        results.append(
            QCScanItem(
                source_file=row.source_file,
                file_hash=file_hash,
                file_size_bytes=file_size,
                json_valid=json_valid,
                json_error=json_error,
                duplicate_file=duplicate,
                duplicate_group=duplicate_group,
                qc_status=qc_status,
            )
        )

    return results


def summarize_qc_results(qc_rows: list[QCScanItem]) -> QCSummary:
    """Summarize QC findings for a dataset."""
    total_files = len(qc_rows)
    valid_json_files = sum(1 for row in qc_rows if row.json_valid)
    invalid_json_files = sum(1 for row in qc_rows if not row.json_valid)
    duplicate_files = sum(1 for row in qc_rows if row.duplicate_file)
    clean_files = sum(1 for row in qc_rows if row.qc_status == "clean")
    return QCSummary(
        total_files=total_files,
        valid_json_files=valid_json_files,
        invalid_json_files=invalid_json_files,
        duplicate_files=duplicate_files,
        clean_files=clean_files,
    )
