"""Desktop bootstrap using PySide6.

This version organizes the prototype as a step-by-step desktop workflow with a
left navigation, which feels closer to the current R app than a single long
scrolling page.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys

def _configure_qt_runtime() -> None:
    """Point Qt to the PySide6 plugin folders before Qt modules are imported."""
    spec = importlib.util.find_spec("PySide6")
    if spec is None or spec.origin is None:
        return

    pyside_root = Path(spec.origin).resolve().parent
    plugins_root = pyside_root / "Qt" / "plugins"
    platforms_dir = plugins_root / "platforms"
    frameworks_dir = pyside_root / "Qt" / "lib"

    if plugins_root.exists():
        os.environ.setdefault("QT_PLUGIN_PATH", str(plugins_root))
    if platforms_dir.exists():
        os.environ.setdefault("QT_QPA_PLATFORM_PLUGIN_PATH", str(platforms_dir))
    if frameworks_dir.exists():
        existing_framework_path = os.environ.get("DYLD_FRAMEWORK_PATH", "")
        existing_library_path = os.environ.get("DYLD_LIBRARY_PATH", "")
        framework_value = str(frameworks_dir)
        if framework_value not in existing_framework_path.split(":"):
            os.environ["DYLD_FRAMEWORK_PATH"] = (
                framework_value if not existing_framework_path else f"{framework_value}:{existing_framework_path}"
            )
        if framework_value not in existing_library_path.split(":"):
            os.environ["DYLD_LIBRARY_PATH"] = (
                framework_value if not existing_library_path else f"{framework_value}:{existing_library_path}"
            )


_configure_qt_runtime()

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from trackautism_app.core.indexing import (
    available_filter_choices,
    filter_indexed_files,
    scan_dataset_metadata,
    summarize_indexed_files,
)
from trackautism_app.core.loading import (
    filter_loaded_rows_by_study_day,
    load_indexed_json_rows,
    summarize_loaded_rows,
)
from trackautism_app.core.qc import scan_file_qc, summarize_qc_results
from trackautism_app.services.projects import new_project
from trackautism_app.utils.time import iso_now


class TrackAutismPrototypeWindow(QMainWindow):
    """Visible Python prototype with step-based navigation."""

    def __init__(self) -> None:
        super().__init__()
        self.project = new_project("JTrack Insight")
        self.indexed_rows = []
        self.filtered_rows = []
        self.loaded_rows = []
        self.loaded_filtered_rows = []
        self.page_index: dict[str, int] = {}

        self.setWindowTitle("JTrack Insight")
        self.resize(1180, 760)
        self.setMinimumSize(980, 640)

        self.data_root_label = QLabel("")
        self.summary_label = QLabel(
            "No dataset indexed yet. The R app remains the scientific reference."
        )
        self.summary_label.setWordWrap(True)
        self.qc_summary_label = QLabel("No QC scan has been run yet.")
        self.qc_summary_label.setWordWrap(True)
        self.scope_summary_label = QLabel("No metadata filters have been applied yet.")
        self.scope_summary_label.setWordWrap(True)
        self.loaded_summary_label = QLabel("No scoped JSON data have been loaded yet.")
        self.loaded_summary_label.setWordWrap(True)
        self.loaded_filter_summary_label = QLabel("No loaded-data filters have been applied yet.")
        self.loaded_filter_summary_label.setWordWrap(True)
        self.status_label = QLabel("Welcome. Start a new analysis or resume a saved project.")

        self._build_ui()

    def _build_ui(self) -> None:
        central = QWidget()
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(18, 18, 18, 18)
        root_layout.setSpacing(14)

        title = QLabel("JTrack Insight")
        title.setStyleSheet("font-size: 30px; font-weight: 700; color: #11324d;")
        root_layout.addWidget(title)

        subtitle = QLabel(
            "JTrack Insight standalone app. The R Shiny app remains the scientific reference while "
            "this interface is rebuilt step by step."
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet("color: #506070;")
        root_layout.addWidget(subtitle)

        content_row = QHBoxLayout()
        content_row.setSpacing(16)

        nav_box = QGroupBox("Workflow")
        nav_layout = QVBoxLayout(nav_box)
        nav_intro = QLabel("Move between steps here, similar to the staged R workflow.")
        nav_intro.setWordWrap(True)
        nav_intro.setStyleSheet("color: #58677a;")
        nav_layout.addWidget(nav_intro)

        self.nav_list = QListWidget()
        self.nav_list.setFixedWidth(240)
        self.nav_list.currentRowChanged.connect(self._on_nav_changed)
        nav_layout.addWidget(self.nav_list)
        content_row.addWidget(nav_box, 0)

        self.page_stack = QStackedWidget()
        self._register_pages()
        content_row.addWidget(self.page_stack, 1)

        root_layout.addLayout(content_row, 1)

        status_frame = QFrame()
        status_frame.setFrameShape(QFrame.Shape.StyledPanel)
        status_layout = QVBoxLayout(status_frame)
        status_layout.setContentsMargins(10, 8, 10, 8)
        status_layout.addWidget(self.status_label)
        root_layout.addWidget(status_frame)

        self.setCentralWidget(central)
        self._go_to_page("home")

    def _register_pages(self) -> None:
        self._add_page("home", "Home", self._build_home_page())
        self._add_page("step1", "Step 1  Load / Index", self._build_step1_page())
        self._add_page("step2", "Step 2  QC", self._build_step2_page())
        self._add_page("step3a", "Step 3A  Scope", self._build_step3a_page())
        self._add_page("step3b", "Step 3B  Load Data", self._build_step3b_page())
        self._add_page("step3c", "Step 3C  Loaded Filters", self._build_step3c_page())
        self._add_page("notes", "Roadmap", self._build_notes_page())

    def _add_page(self, key: str, title: str, widget: QWidget) -> None:
        index = self.page_stack.addWidget(widget)
        self.page_index[key] = index
        self.nav_list.addItem(QListWidgetItem(title))

    def _page_wrapper(self, title: str, intro: str) -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(14)

        title_label = QLabel(title)
        title_label.setStyleSheet("font-size: 22px; font-weight: 700; color: #0f5b8d;")
        layout.addWidget(title_label)

        intro_label = QLabel(intro)
        intro_label.setWordWrap(True)
        intro_label.setStyleSheet("color: #5a6a7d;")
        layout.addWidget(intro_label)

        return page, layout

    def _build_home_page(self) -> QWidget:
        page, layout = self._page_wrapper(
            "Home",
            "Choose whether to begin a new local analysis or later resume a saved one.",
        )

        button_row = QHBoxLayout()
        new_button = QPushButton("Start New Analysis")
        new_button.clicked.connect(self._select_dataset)
        button_row.addWidget(new_button)

        resume_button = QPushButton("Resume Saved Analysis")
        resume_button.clicked.connect(self._resume_placeholder)
        button_row.addWidget(resume_button)
        button_row.addStretch(1)
        layout.addLayout(button_row)

        overview = QGroupBox("Prototype overview")
        overview_layout = QVBoxLayout(overview)
        overview_text = QTextEdit()
        overview_text.setReadOnly(True)
        overview_text.setPlainText(
            "\n".join(
                [
                    "Current JTrack Insight capabilities:",
                    "",
                    "• Visible standalone desktop window",
                    "• Step 1 metadata indexing",
                    "• Step 2 QC summary for invalid JSON and duplicate files",
                    "• Step 3A metadata-based filters",
                    "• Step 3B scoped JSON loading",
                    "• Step 3C loaded-data study-day filtering",
                    "",
                    "Why this structure:",
                    "• keep the R app alive as the scientific reference",
                    "• rebuild the workflow as a standalone Python app",
                    "• allow optional backend support later without changing the core logic",
                ]
            )
        )
        overview_layout.addWidget(overview_text)
        layout.addWidget(overview, 1)

        return page

    def _build_step1_page(self) -> QWidget:
        page, layout = self._page_wrapper(
            "Step 1  Load and index dataset",
            "This mirrors the first stage of the R app: choose a dataset root and build metadata from the file structure.",
        )

        box = QGroupBox("Dataset indexing")
        box_layout = QVBoxLayout(box)
        button_row = QHBoxLayout()
        choose_button = QPushButton("Choose Dataset Root")
        choose_button.clicked.connect(self._select_dataset)
        button_row.addWidget(choose_button)
        button_row.addStretch(1)
        box_layout.addLayout(button_row)

        self.data_root_label.setStyleSheet("color: #375a7f;")
        self.data_root_label.setWordWrap(True)
        box_layout.addWidget(self.data_root_label)
        box_layout.addWidget(self.summary_label)
        layout.addWidget(box)
        layout.addStretch(1)
        return page

    def _build_step2_page(self) -> QWidget:
        page, layout = self._page_wrapper(
            "Step 2  QC control",
            "The prototype now runs a first QC pass immediately after indexing: JSON validity plus duplicate detection by content hash.",
        )

        box = QGroupBox("QC summary")
        box_layout = QVBoxLayout(box)
        box_layout.addWidget(self.qc_summary_label)
        layout.addWidget(box)
        layout.addStretch(1)
        return page

    def _build_step3a_page(self) -> QWidget:
        page, layout = self._page_wrapper(
            "Step 3A  Metadata-based scope",
            "Apply user, device, sensor, and wearable-stream filters before loading any JSON rows.",
        )

        box = QGroupBox("Metadata filters")
        box_layout = QVBoxLayout(box)

        self.user_combo = QComboBox()
        self.device_combo = QComboBox()
        self.sensor_combo = QComboBox()
        self.wearable_combo = QComboBox()
        for combo in (self.user_combo, self.device_combo, self.sensor_combo, self.wearable_combo):
            combo.addItem("All")
            combo.setEnabled(False)

        box_layout.addWidget(QLabel("User"))
        box_layout.addWidget(self.user_combo)
        box_layout.addWidget(QLabel("Device"))
        box_layout.addWidget(self.device_combo)
        box_layout.addWidget(QLabel("Sensor"))
        box_layout.addWidget(self.sensor_combo)
        box_layout.addWidget(QLabel("Wearable sensor"))
        box_layout.addWidget(self.wearable_combo)

        apply_scope_button = QPushButton("Apply Metadata Filters")
        apply_scope_button.clicked.connect(self._apply_scope_filters)
        box_layout.addWidget(apply_scope_button)
        box_layout.addWidget(self.scope_summary_label)
        layout.addWidget(box)
        layout.addStretch(1)
        return page

    def _build_step3b_page(self) -> QWidget:
        page, layout = self._page_wrapper(
            "Step 3B  Load scoped JSON data",
            "Load the JSON files that remain after metadata filtering and inspect a first loaded-data summary.",
        )

        box = QGroupBox("Scoped JSON loading")
        box_layout = QVBoxLayout(box)
        load_button = QPushButton("Load Scoped Data")
        load_button.clicked.connect(self._load_scoped_data)
        box_layout.addWidget(load_button)
        box_layout.addWidget(self.loaded_summary_label)

        self.loaded_columns_preview = QTextEdit()
        self.loaded_columns_preview.setReadOnly(True)
        self.loaded_columns_preview.setPlaceholderText("Loaded column preview will appear here.")
        self.loaded_columns_preview.setMaximumHeight(160)
        box_layout.addWidget(self.loaded_columns_preview)
        layout.addWidget(box)
        layout.addStretch(1)
        return page

    def _build_step3c_page(self) -> QWidget:
        page, layout = self._page_wrapper(
            "Step 3C  Loaded-data filters",
            "Apply post-load filters to the scoped JSON rows. The first ported example is a derived study-day range.",
        )

        box = QGroupBox("Loaded-data filters")
        box_layout = QVBoxLayout(box)

        day_row = QHBoxLayout()
        self.min_day_spin = QSpinBox()
        self.max_day_spin = QSpinBox()
        for spin in (self.min_day_spin, self.max_day_spin):
            spin.setEnabled(False)
            spin.setMinimum(0)
            spin.setMaximum(0)
        day_row.addWidget(QLabel("Min study day"))
        day_row.addWidget(self.min_day_spin)
        day_row.addWidget(QLabel("Max study day"))
        day_row.addWidget(self.max_day_spin)
        day_row.addStretch(1)
        box_layout.addLayout(day_row)

        apply_loaded_filter_button = QPushButton("Apply Loaded-Data Filters")
        apply_loaded_filter_button.clicked.connect(self._apply_loaded_filters)
        box_layout.addWidget(apply_loaded_filter_button)
        box_layout.addWidget(self.loaded_filter_summary_label)
        layout.addWidget(box)
        layout.addStretch(1)
        return page

    def _build_notes_page(self) -> QWidget:
        page, layout = self._page_wrapper(
            "Roadmap",
            "This page tracks what has been ported and what still comes next.",
        )

        notes_box = QGroupBox("Current scope of JTrack Insight")
        notes_layout = QVBoxLayout(notes_box)
        notes = QTextEdit()
        notes.setReadOnly(True)
        notes.setPlainText(
            "\n".join(
                [
                    "Ported so far:",
                    "• Step 1 metadata indexing",
                    "• Step 2 QC summary",
                    "• Step 3A metadata filters",
                    "• Step 3B scoped JSON loading",
                    "• Step 3C derived study-day filtering",
                    "",
                    "Recommended next ports:",
                    "1. First sensor-specific feature extraction path",
                    "2. Save/resume project state",
                    "3. Group analysis foundations",
                    "4. Publication-style visualization pages",
                ]
            )
        )
        notes_layout.addWidget(notes)
        layout.addWidget(notes_box, 1)
        return page

    def _go_to_page(self, key: str) -> None:
        index = self.page_index[key]
        self.page_stack.setCurrentIndex(index)
        self.nav_list.setCurrentRow(index)

    def _on_nav_changed(self, row: int) -> None:
        if row >= 0 and row != self.page_stack.currentIndex():
            self.page_stack.setCurrentIndex(row)

    def _select_dataset(self) -> None:
        chosen_dir = QFileDialog.getExistingDirectory(self, "Choose dataset root")
        if not chosen_dir:
            self.status_label.setText("Dataset selection cancelled.")
            return

        dataset_root = Path(chosen_dir)
        self.status_label.setText("Indexing dataset metadata and running QC...")
        QApplication.processEvents()

        try:
            indexed = scan_dataset_metadata(dataset_root)
            qc_rows = scan_file_qc(indexed)
            qc_summary = summarize_qc_results(qc_rows)
        except Exception as exc:  # pragma: no cover - UI path
            self.status_label.setText("Indexing failed.")
            QMessageBox.critical(self, "Indexing failed", str(exc))
            return

        self.project.data_root = str(dataset_root)
        self.project.current_step = "step_1_indexed"
        self.indexed_rows = indexed
        self.filtered_rows = indexed
        counts = summarize_indexed_files(indexed)
        self.data_root_label.setText(f"Dataset root: {dataset_root}")
        self.summary_label.setText(
            "Indexed "
            f"{counts['json_files']} JSON files across "
            f"{counts['subjects']} subjects, {counts['devices']} devices, and "
            f"{counts['sensors']} sensor streams."
        )
        self.qc_summary_label.setText(
            "QC scan completed. "
            f"Clean files: {qc_summary.clean_files}. "
            f"Valid JSON: {qc_summary.valid_json_files}. "
            f"Invalid JSON: {qc_summary.invalid_json_files}. "
            f"Duplicate files by content: {qc_summary.duplicate_files}."
        )
        self._populate_scope_filters(indexed)
        self.scope_summary_label.setText(
            "Metadata filters are ready. Review the dropdowns and apply the current scope."
        )
        self.loaded_rows = []
        self.loaded_filtered_rows = []
        self.loaded_summary_label.setText("No scoped JSON data have been loaded yet.")
        self.loaded_filter_summary_label.setText("No loaded-data filters have been applied yet.")
        self.loaded_columns_preview.clear()
        self._reset_loaded_day_controls()
        self.status_label.setText(
            f"Indexing completed at {iso_now()}. Loaded metadata for {counts['subjects']} subjects."
        )
        self._go_to_page("step2")

    def _resume_placeholder(self) -> None:
        self.status_label.setText("Resume workflow is not ported yet.")
        QMessageBox.information(
            self,
            "Resume saved analysis",
            (
                "The Python app does not restore saved analysis checkpoints yet.\n\n"
                "That workflow will be added after metadata indexing, QC, and project-state "
                "storage are ported from the R app."
            ),
        )

    def _populate_scope_filters(self, indexed_rows) -> None:
        choices = available_filter_choices(indexed_rows)
        mapping = {
            self.user_combo: choices["username"],
            self.device_combo: choices["device_id"],
            self.sensor_combo: choices["sensor_name"],
            self.wearable_combo: choices["wearable_sensor"],
        }
        for combo, values in mapping.items():
            combo.clear()
            combo.addItem("All")
            combo.addItems(values)
            combo.setEnabled(True)

    def _apply_scope_filters(self) -> None:
        if not self.indexed_rows:
            self.scope_summary_label.setText("Load and index a dataset first.")
            return

        filtered = filter_indexed_files(
            self.indexed_rows,
            username=self.user_combo.currentText(),
            device_id=self.device_combo.currentText(),
            sensor_name=self.sensor_combo.currentText(),
            wearable_sensor=self.wearable_combo.currentText(),
        )
        self.filtered_rows = filtered
        counts = summarize_indexed_files(filtered)
        self.scope_summary_label.setText(
            "Scoped metadata preview: "
            f"{counts['json_files']} JSON files, "
            f"{counts['subjects']} subjects, "
            f"{counts['devices']} devices, and "
            f"{counts['sensors']} sensor streams remain after filtering."
        )
        self.status_label.setText(
            f"Metadata filters applied at {iso_now()}. {counts['json_files']} JSON files remain in scope."
        )
        self.loaded_rows = []
        self.loaded_filtered_rows = []
        self.loaded_summary_label.setText(
            "Metadata scope changed. Load the scoped JSON data again to refresh the preview."
        )
        self.loaded_filter_summary_label.setText("No loaded-data filters have been applied yet.")
        self.loaded_columns_preview.clear()
        self._reset_loaded_day_controls()
        self._go_to_page("step3b")

    def _load_scoped_data(self) -> None:
        if not self.filtered_rows:
            self.loaded_summary_label.setText("Apply metadata filters first so there is a scoped dataset to load.")
            return

        self.status_label.setText("Loading scoped JSON data...")
        QApplication.processEvents()

        try:
            rows = load_indexed_json_rows(self.filtered_rows)
            summary = summarize_loaded_rows(rows)
        except Exception as exc:  # pragma: no cover - UI path
            self.status_label.setText("Scoped data load failed.")
            QMessageBox.critical(self, "Scoped data load failed", str(exc))
            return

        self.loaded_rows = rows
        self.loaded_filtered_rows = rows
        self.loaded_summary_label.setText(
            "Loaded scoped data preview: "
            f"{summary.files_loaded} files, "
            f"{summary.records_loaded} records, "
            f"{summary.subjects} subjects, "
            f"{summary.devices} devices, and "
            f"{summary.sensors} sensor streams."
        )
        preview_cols = summary.columns[:30]
        more_note = "" if len(summary.columns) <= 30 else f"\n... and {len(summary.columns) - 30} more columns."
        self.loaded_columns_preview.setPlainText(
            "Detected columns:\n" + "\n".join(preview_cols) + more_note
        )
        self._configure_loaded_day_controls(summary.min_study_day, summary.max_study_day)
        if summary.min_study_day is not None and summary.max_study_day is not None:
            self.loaded_filter_summary_label.setText(
                "Loaded-data filters are ready. "
                f"Derived study days currently span {summary.min_study_day} to {summary.max_study_day} "
                f"across {summary.distinct_study_days} distinct days."
            )
        else:
            self.loaded_filter_summary_label.setText(
                "Loaded rows do not yet expose a derived study-day range."
            )
        self.status_label.setText(
            f"Scoped data loaded at {iso_now()}. {summary.records_loaded} records are ready for the next Python step."
        )
        self._go_to_page("step3c")

    def _reset_loaded_day_controls(self) -> None:
        for spin in (self.min_day_spin, self.max_day_spin):
            spin.setEnabled(False)
            spin.setMinimum(0)
            spin.setMaximum(0)
            spin.setValue(0)

    def _configure_loaded_day_controls(self, min_day: int | None, max_day: int | None) -> None:
        if min_day is None or max_day is None:
            self._reset_loaded_day_controls()
            return
        for spin in (self.min_day_spin, self.max_day_spin):
            spin.setEnabled(True)
            spin.setMinimum(min_day)
            spin.setMaximum(max_day)
        self.min_day_spin.setValue(min_day)
        self.max_day_spin.setValue(max_day)

    def _apply_loaded_filters(self) -> None:
        if not self.loaded_rows:
            self.loaded_filter_summary_label.setText("Load scoped JSON data first.")
            return

        min_day = self.min_day_spin.value() if self.min_day_spin.isEnabled() else None
        max_day = self.max_day_spin.value() if self.max_day_spin.isEnabled() else None
        if min_day is not None and max_day is not None and min_day > max_day:
            QMessageBox.warning(
                self,
                "Invalid study-day range",
                "Minimum study day cannot be greater than maximum study day.",
            )
            return

        filtered = filter_loaded_rows_by_study_day(self.loaded_rows, min_day=min_day, max_day=max_day)
        self.loaded_filtered_rows = filtered
        summary = summarize_loaded_rows(filtered)
        self.loaded_filter_summary_label.setText(
            "Loaded-data filter preview: "
            f"{summary.records_loaded} records, "
            f"{summary.subjects} subjects, "
            f"{summary.devices} devices, and "
            f"{summary.distinct_study_days} study days remain in the selected range."
        )
        self.status_label.setText(
            f"Loaded-data filters applied at {iso_now()}. {summary.records_loaded} records remain in range."
        )


def main() -> None:
    app = QApplication.instance() or QApplication(sys.argv)
    window = TrackAutismPrototypeWindow()
    window.show()
    app.exec()


if __name__ == "__main__":
    main()
