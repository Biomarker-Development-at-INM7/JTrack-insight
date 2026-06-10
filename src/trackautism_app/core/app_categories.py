"""App-category mapping helpers for application-usage analysis."""

from __future__ import annotations

import csv
from pathlib import Path
from io import StringIO


DEFAULT_APP_CATEGORY_PATH = Path("resources/app_category_mapping.csv")
DEFAULT_APP_CATEGORY_CODEBOOK_PATH = Path("resources/app_category_codebook.csv")

APP_COLUMN_CANDIDATES = (
    "App",
    "appName",
    "app_name",
    "application",
    "application_name",
    "app_title",
    "matched_play_title",
    "name",
    "package",
    "packageName",
)
CATEGORY_COLUMN_CANDIDATES = (
    "ios_category",
    "category",
    "app_category",
    "group",
    "label",
    "Categories",
)
CODE_COLUMN_CANDIDATES = (
    "ios_category_id",
    "category_code",
    "code",
    "Code",
)


def _detect_delimiter(file_path: Path) -> str:
    sample = file_path.read_text(encoding="utf-8", errors="replace")[:4096]
    try:
        return csv.Sniffer().sniff(sample, delimiters=";,").delimiter
    except csv.Error:
        return ";"


def _read_rows(file_path: Path) -> list[dict[str, str]]:
    if not file_path.exists():
        return []
    delimiter = _detect_delimiter(file_path)
    with file_path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=delimiter)
        return [{(key or "").strip(): (value or "").strip() for key, value in row.items()} for row in reader]


def _read_rows_from_text(text: str) -> list[dict[str, str]]:
    sample = text[:4096]
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=";,").delimiter
    except csv.Error:
        delimiter = ";"
    reader = csv.DictReader(StringIO(text), delimiter=delimiter)
    return [{(key or "").strip(): (value or "").strip() for key, value in row.items()} for row in reader]


def _find_first_existing_col(rows: list[dict[str, str]], candidates: tuple[str, ...]) -> str | None:
    if not rows:
        return None
    available = set(rows[0].keys())
    for name in candidates:
        if name in available:
            return name
    return None


def load_app_category_codebook(file_path: Path = DEFAULT_APP_CATEGORY_CODEBOOK_PATH) -> dict[str, str]:
    codebook: dict[str, str] = {}
    for row in _read_rows(file_path):
        code = row.get("Code", "").strip()
        category = row.get("Categories", "").strip()
        if code and category:
            codebook[code] = category
    return codebook


def load_app_category_mapping(
    mapping_path: Path = DEFAULT_APP_CATEGORY_PATH,
    codebook_path: Path = DEFAULT_APP_CATEGORY_CODEBOOK_PATH,
) -> dict[str, str]:
    """Load app-name to category mapping from the provided CSVs."""
    codebook = load_app_category_codebook(codebook_path)
    return build_app_category_mapping(_read_rows(mapping_path), codebook)


def build_app_category_mapping(
    rows: list[dict[str, str]],
    codebook: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build app-name to category mapping using the same column-selection strategy as the R app."""
    codebook = codebook or {}
    app_col = _find_first_existing_col(rows, APP_COLUMN_CANDIDATES)
    category_col = _find_first_existing_col(rows, CATEGORY_COLUMN_CANDIDATES)
    code_col = _find_first_existing_col(rows, CODE_COLUMN_CANDIDATES)

    if app_col is None or (category_col is None and code_col is None):
        return {}

    mapping: dict[str, str] = {}
    for row in rows:
        app_name = (row.get(app_col) or "").strip()
        if not app_name:
            continue
        code = (row.get(code_col) or "").strip() if code_col is not None else ""
        category = (row.get(category_col) or "").strip() if category_col is not None else ""
        if not category and code:
            category = codebook.get(code, "")
        if not category:
            continue
        mapping.setdefault(app_name.casefold(), category)
    return mapping


def load_app_category_mapping_from_text(
    mapping_text: str,
    codebook_path: Path = DEFAULT_APP_CATEGORY_CODEBOOK_PATH,
) -> dict[str, str]:
    """Load app-name to category mapping from uploaded CSV text."""
    codebook = load_app_category_codebook(codebook_path)
    return build_app_category_mapping(_read_rows_from_text(mapping_text), codebook)
