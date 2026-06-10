"""Local web UI for JTrack Insight."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import csv
import html
import io
import json
import math
import random
import re
from pathlib import Path
import platform
import subprocess
from typing import Iterable
from urllib.parse import urlencode

from fastapi import FastAPI, Query, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, Response
import plotly.graph_objects as go
from plotly.offline import plot
from plotly.subplots import make_subplots
import uvicorn

from trackautism_app.core.indexing import (
    IndexedFile,
    available_filter_choices,
    filter_indexed_files,
    scan_dataset_metadata,
    summarize_indexed_files,
)
from trackautism_app.core.application_usage import review_application_usage, extract_application_usage_category_features
from trackautism_app.core.app_categories import (
    DEFAULT_APP_CATEGORY_PATH,
    load_app_category_mapping_from_text,
    load_app_category_mapping,
)
from trackautism_app.core.location import review_location
from trackautism_app.core.pedometer import review_pedometer
from trackautism_app.core.loading import (
    filter_loaded_rows_by_study_day,
    load_indexed_json_rows,
    summarize_loaded_rows,
)
from trackautism_app.core.qc import QCScanItem, scan_file_qc, summarize_qc_results
from trackautism_app.utils.time import iso_now


@dataclass
class PrototypeState:
    data_root: str | None = None
    indexed_rows: list[IndexedFile] = field(default_factory=list)
    qc_rows: list[QCScanItem] = field(default_factory=list)
    filtered_rows: list[IndexedFile] = field(default_factory=list)
    loaded_rows: list[dict] = field(default_factory=list)
    loaded_filtered_rows: list[dict] = field(default_factory=list)
    selected_sensor_name: str | None = None
    selected_wearable_sensor: str | None = None
    app_usage_daily: list[dict] = field(default_factory=list)
    app_usage_category_daily: list[dict] = field(default_factory=list)
    app_usage_category_daily_wide: list[dict] = field(default_factory=list)
    app_usage_review: dict | None = None
    location_daily: list[dict] = field(default_factory=list)
    location_trajectory: list[dict] = field(default_factory=list)
    location_review: dict | None = None
    pedometer_daily: list[dict] = field(default_factory=list)
    pedometer_review: dict | None = None
    generic_sensor_daily: list[dict] = field(default_factory=list)
    activity_features: list[dict] = field(default_factory=list)
    custom_feature_rows: list[dict] = field(default_factory=list)
    custom_feature_script_name: str | None = None
    custom_feature_script_text: str | None = None
    custom_feature_last_error: str | None = None
    generated_feature_key: str | None = None
    generated_feature_name: str | None = None
    generated_temporal_frequency: str = "daily"
    generated_feature_transform: str = "none"
    feature_qc_rows: list[dict] = field(default_factory=list)
    feature_qc_audit_rows: list[dict] = field(default_factory=list)
    feature_qc_description: str | None = None
    app_category_map: dict[str, str] = field(default_factory=dict)
    app_category_source: str | None = None
    group_label_rows: list[dict] = field(default_factory=list)
    group_label_source: str | None = None
    group_label_folder: str | None = None
    group_source_rows: list[dict] = field(default_factory=list)
    group_selected_column: str | None = None
    group_comorbidity_columns: list[str] = field(default_factory=list)
    status: str = "Welcome. Load a dataset to begin."


APP_STATE = PrototypeState()

FEATURE_TRANSFORM_CHOICES = [
    ("none", "No transformation"),
    ("log1p", "Log(x + 1)"),
    ("log10p", "Log10(x + 1)"),
    ("sqrt", "Square root"),
    ("zscore", "Z-score"),
    ("center", "Mean-center"),
]
WORKFLOW_STEPS = [
    ("home", "Home"),
    ("step1", "Step 1  Load / Index"),
    ("step2", "Step 2  File QC"),
    ("step3", "Step 3  Feature Computation"),
    ("step4", "Step 4  Quality Control"),
    ("step5", "Step 5  Review / Export"),
    ("step6", "Step 6  Reports"),
    ("step7", "Step 7  Group Analysis"),
    ("roadmap", "Roadmap"),
]


def _workflow_status_summary() -> str:
    """Compact global status: useful context without adding repeated tables."""
    indexed_subjects = len({getattr(row, "username", "") for row in APP_STATE.indexed_rows if getattr(row, "username", "")})
    indexed_sensors = len({getattr(row, "sensorname", "") for row in APP_STATE.indexed_rows if getattr(row, "sensorname", "")})
    generated_rows = len(_generated_feature_rows(APP_STATE.generated_feature_key)) if APP_STATE.generated_feature_key else 0
    qc_rows = len(APP_STATE.feature_qc_rows or [])
    labels = len(APP_STATE.group_label_rows or [])
    items = [
        ("Subjects", indexed_subjects),
        ("Sensors", indexed_sensors),
        ("Feature rows", generated_rows),
        ("QC rows", qc_rows),
        ("Group labels", labels),
    ]
    return "<div class='workflow-status'>" + "".join(
        f"<div class='workflow-mini'><div class='k'>{html.escape(str(label))}</div><div class='v'>{html.escape(str(value))}</div></div>"
        for label, value in items
    ) + "</div>"

def _page_shell(current_step: str, title: str, body: str) -> HTMLResponse:
    def _tab_item(key: str, label: str, idx: int) -> str:
        active = "active" if key == current_step else ""
        if key.startswith("step"):
            step_no = key.replace("step", "")
            short_label = label.replace(f"Step {step_no}  ", "")
            return (
                f"<a class='workflow-tab {active}' href='/?step={key}'>"
                f"<span class='tab-index'>{html.escape(step_no)}</span>"
                f"<span class='tab-title'>{html.escape(short_label)}</span>"
                f"</a>"
            )
        short_label = "Start" if key == "home" else label
        icon = "⌂" if key == "home" else "?"
        return (
            f"<a class='workflow-tab utility-tab {active}' href='/?step={key}'>"
            f"<span class='tab-index'>{html.escape(icon)}</span>"
            f"<span class='tab-title'>{html.escape(short_label)}</span>"
            f"</a>"
        )

    nav = "\n".join(_tab_item(key, label, idx) for idx, (key, label) in enumerate(WORKFLOW_STEPS))
    root_text = (
        f"<div class='root-path'>Dataset root: {html.escape(APP_STATE.data_root)}</div>"
        if APP_STATE.data_root
        else "<div class='root-path'>No dataset loaded yet.</div>"
    )
    pager_script = """
        <script>
          document.addEventListener('DOMContentLoaded', function () {
            document.querySelectorAll('.table-scroll').forEach(function (wrap) {
              const table = wrap.querySelector('table');
              if (!table || table.dataset.paged === '1') return;
              const tbody = table.querySelector('tbody');
              if (!tbody) return;
              const rows = Array.from(tbody.querySelectorAll('tr'));
              const initialSize = parseInt(table.dataset.pageSize || wrap.dataset.pageSize || '20', 10) || 20;
              if (rows.length <= initialSize) return;
              table.dataset.paged = '1';
              let pageSize = initialSize;
              let page = 1;
              const pager = document.createElement('div');
              pager.className = 'table-pager';
              pager.innerHTML = `
                <span>Rows per page</span>
                <select aria-label="Rows per page">
                  <option value="10">10</option>
                  <option value="20">20</option>
                  <option value="50">50</option>
                  <option value="100">100</option>
                </select>
                <button type="button" data-action="prev">Previous</button>
                <button type="button" data-action="next">Next</button>
                <span class="table-page-info"></span>
              `;
              wrap.insertAdjacentElement('afterend', pager);
              const sizeSelect = pager.querySelector('select');
              const prevBtn = pager.querySelector('[data-action="prev"]');
              const nextBtn = pager.querySelector('[data-action="next"]');
              const info = pager.querySelector('.table-page-info');
              if (!Array.from(sizeSelect.options).some(function (opt) { return parseInt(opt.value, 10) === pageSize; })) {
                const opt = document.createElement('option');
                opt.value = String(pageSize);
                opt.textContent = String(pageSize);
                sizeSelect.appendChild(opt);
              }
              sizeSelect.value = String(pageSize);
              function renderPage() {
                const totalPages = Math.max(1, Math.ceil(rows.length / pageSize));
                page = Math.min(Math.max(page, 1), totalPages);
                const start = (page - 1) * pageSize;
                const end = start + pageSize;
                rows.forEach(function (row, idx) { row.style.display = idx >= start && idx < end ? '' : 'none'; });
                prevBtn.disabled = page <= 1;
                nextBtn.disabled = page >= totalPages;
                info.textContent = `Page ${page} of ${totalPages} · ${rows.length} rows`;
              }
              prevBtn.addEventListener('click', function () { page -= 1; renderPage(); });
              nextBtn.addEventListener('click', function () { page += 1; renderPage(); });
              sizeSelect.addEventListener('change', function () { pageSize = parseInt(sizeSelect.value, 10) || initialSize; page = 1; renderPage(); });
              renderPage();
            });
          });
        </script>
    """
    document = f"""
    <!doctype html>
    <html>
      <head>
        <meta charset="utf-8" />
        <title>JTrack Insight</title>
        <style>
          body {{
            margin: 0;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            background: #f4f7fa;
            color: #213547;
          }}
          .app {{
            min-height: 100vh;
          }}
          .topbar {{
            position: sticky;
            top: 0;
            z-index: 1000;
            background: #f4f7fa;
            border-bottom: 1px solid #dbe6ef;
            box-shadow: 0 8px 22px rgba(16, 35, 52, 0.08);
          }}
          .topbar-inner {{
            display: grid;
            grid-template-columns: minmax(190px, 260px) minmax(0, 1fr);
            gap: 16px;
            align-items: center;
            padding: 10px 18px 0 18px;
            background: #153650;
            color: white;
          }}
          .brand-block {{
            min-width: 0;
            padding-bottom: 10px;
          }}
          .brand {{
            font-size: 21px;
            font-weight: 800;
            line-height: 1.05;
            white-space: nowrap;
          }}
          .sub {{
            color: #c6d5e3;
            font-size: 12px;
            margin-top: 2px;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
          }}
          .topbar-meta {{
            justify-self: end;
            color: #c6d5e3;
            font-size: 12px;
            max-width: 420px;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            padding-bottom: 10px;
          }}
          .root-path {{
            color: #c6d5e3;
            font-size: 12px;
          }}
          .workflow-tabs-wrap {{
            background: linear-gradient(#ffffff, #f7fafc);
            padding: 0 18px;
            border-bottom: 1px solid #dbe6ef;
          }}
          .workflow-tabs {{
            display: flex;
            gap: 0;
            overflow-x: auto;
            scrollbar-width: thin;
            align-items: flex-end;
            max-width: 100%;
          }}
          .workflow-tab {{
            position: relative;
            display: inline-flex;
            align-items: center;
            gap: 7px;
            flex: 0 0 auto;
            color: #496173;
            text-decoration: none;
            padding: 10px 14px 11px 14px;
            border: 1px solid transparent;
            border-bottom: 0;
            border-radius: 12px 12px 0 0;
            font-size: 13px;
            font-weight: 700;
            white-space: nowrap;
            margin-top: 8px;
          }}
          .workflow-tab:hover {{
            background: #eef6fb;
            color: #153650;
          }}
          .workflow-tab.active {{
            background: white;
            color: #153650;
            border-color: #dbe6ef;
            box-shadow: 0 -2px 0 #0f8b8d inset;
          }}
          .workflow-tab.active::after {{
            content: "";
            position: absolute;
            left: 0;
            right: 0;
            bottom: -1px;
            height: 1px;
            background: white;
          }}
          .tab-index {{
            display: inline-grid;
            place-items: center;
            min-width: 22px;
            height: 22px;
            border-radius: 999px;
            background: #e8f1f7;
            color: #31506a;
            font-size: 12px;
            font-weight: 800;
          }}
          .workflow-tab.active .tab-index {{
            background: #0f8b8d;
            color: white;
          }}
          .utility-tab .tab-index {{
            font-size: 14px;
          }}
          .content {{
            padding: 20px 24px 24px 24px;
            min-width: 0;
            overflow-x: hidden;
          }}
          .workflow-status {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 8px;
            margin: 12px 0 18px 0;
          }}
          .workflow-mini {{
            background: white;
            border: 1px solid #dce8f2;
            border-radius: 12px;
            padding: 10px 12px;
            min-height: 54px;
          }}
          .workflow-mini .k {{
            color: #5d7387;
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: .035em;
          }}
          .workflow-mini .v {{
            color: #153650;
            font-size: 18px;
            font-weight: 750;
            margin-top: 2px;
          }}
          .workbench {{
            display: grid;
            grid-template-columns: minmax(280px, 360px) minmax(0, 1fr);
            gap: 16px;
            align-items: start;
            height: calc(100vh - 175px);
            min-height: 520px;
            overflow: hidden;
          }}
          .control-card {{
            position: sticky;
            top: 16px;
            max-height: calc(100vh - 195px);
            overflow-y: auto;
            overflow-x: hidden;
          }}
          .preview-card {{
            min-width: 0;
            max-height: calc(100vh - 195px);
            overflow-y: auto;
            overflow-x: hidden;
          }}
          .workbench > .card,
          .step-grid > .card {{
            max-height: calc(100vh - 195px);
            overflow-y: auto;
            overflow-x: hidden;
          }}
          .preview-card > .card {{
            max-height: none;
            overflow: visible;
          }}
          .section-title {{
            color: #17324d;
            font-weight: 750;
            margin: 14px 0 8px 0;
          }}
          .button-row {{
            display: flex;
            gap: 10px;
            flex-wrap: wrap;
            align-items: center;
          }}
          .button-secondary {{
            background: #eef6fb;
            color: #17324d;
            border: 1px solid #cfe0ec;
          }}
          .card-section {{
            border: 1px solid #e2ebf3;
            border-radius: 14px;
            padding: 12px 12px;
            margin: 12px 0;
            background: #fbfdff;
          }}
          .card-section .section-title {{
            margin-top: 0;
          }}
          .feature-grid {{
            display: grid;
            grid-template-columns: 1fr;
            gap: 8px;
            max-height: 260px;
            overflow: auto;
            padding-right: 2px;
          }}
          .feature-option {{
            display: grid;
            grid-template-columns: 22px 1fr;
            gap: 8px;
            align-items: start;
            padding: 8px 9px;
            border: 1px solid #dbe7f2;
            border-radius: 10px;
            background: white;
          }}
          .feature-option input {{
            width: auto;
            margin: 3px 0 0 0;
          }}
          .feature-option span {{
            line-height: 1.25;
          }}
          .feature-option small {{
            display: block;
            margin-top: 3px;
            color: #6b7d8f;
          }}
          .summary-chip-row {{
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            margin: 10px 0 12px 0;
          }}
          .summary-chip {{
            background: #eef6fb;
            color: #17324d;
            border: 1px solid #cfe0ec;
            border-radius: 999px;
            padding: 7px 10px;
            font-size: 13px;
            font-weight: 650;
          }}
          .compact-note {{
            color: #617485;
            font-size: 13px;
            line-height: 1.35;
          }}
          details.clean-details {{
            background: white;
            border: 1px solid #dde7f0;
            border-radius: 14px;
            padding: 12px 14px;
            margin-bottom: 16px;
          }}
          details.clean-details > summary {{
            cursor: pointer;
            font-weight: 750;
            color: #17324d;
          }}

          .code-preview {{
            max-height: 420px;
            overflow: auto;
            background: #0b1220;
            color: #e5edf7;
            border-radius: 12px;
            padding: 14px;
            font-size: 12px;
            line-height: 1.45;
            white-space: pre;
          }}
          @media (max-width: 980px) {{
            .app {{ grid-template-columns: 1fr; }}
            .sidebar {{ position: relative; }}
            .workbench {{ grid-template-columns: 1fr; height: auto; min-height: 0; overflow: visible; }}
            .control-card, .preview-card {{ position: relative; top: 0; max-height: none; overflow: visible; }}
          }}
          .hero {{
            margin-bottom: 16px;
          }}
          .hero h1 {{
            margin: 0 0 8px 0;
            color: #0f5b8d;
          }}
          .hero p {{
            margin: 0;
            color: #617485;
          }}
          .root-path {{
            margin-top: 18px;
            color: #d8e6f2;
            font-size: 13px;
            word-break: break-word;
          }}
          .card {{
            background: white;
            border: 1px solid #dde7f0;
            border-radius: 14px;
            padding: 18px 18px;
            margin-bottom: 16px;
            box-shadow: 0 6px 18px rgba(16, 35, 52, 0.05);
            max-width: 100%;
            overflow: hidden;
          }}
          .card h2 {{
            margin: 0 0 8px 0;
            font-size: 18px;
            color: #17324d;
          }}
          .muted {{
            color: #617485;
          }}
          .status {{
            background: #eef6fb;
            border: 1px solid #d7e7f3;
            border-radius: 12px;
            padding: 12px 14px;
            margin-top: 14px;
            color: #28445d;
          }}
          .grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 12px;
          }}
          .metric {{
            background: #f8fbfe;
            border: 1px solid #dbe7f2;
            border-radius: 12px;
            padding: 12px 14px;
          }}
          .metric-label {{
            color: #5e7386;
            font-size: 13px;
            margin-bottom: 6px;
          }}
          .metric-value {{
            font-size: 22px;
            font-weight: 700;
            color: #153650;
          }}
          form {{
            display: flex;
            flex-direction: column;
            gap: 12px;
          }}
          input, select, button, textarea {{
            font: inherit;
          }}
          input, select {{
            padding: 10px 12px;
            border-radius: 10px;
            border: 1px solid #c8d6e2;
            background: white;
            width: 100%;
            box-sizing: border-box;
          }}
          select[multiple] {{
            min-height: 180px;
          }}
          button {{
            width: fit-content;
            background: #0f8b8d;
            color: white;
            border: none;
            border-radius: 10px;
            padding: 10px 16px;
            cursor: pointer;
          }}
          .cols {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 12px;
          }}
          pre {{
            white-space: pre-wrap;
            background: #f7fafc;
            border: 1px solid #d9e4ee;
            border-radius: 10px;
            padding: 12px;
            max-height: 220px;
            overflow: auto;
          }}
          ul {{
            margin-top: 8px;
          }}
          .plot-wrap {{
            background: #f8fbfe;
            border: 1px solid #dbe7f2;
            border-radius: 12px;
            padding: 12px;
            overflow-x: auto;
          }}
          .viz-note {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 10px;
            margin: 10px 0 14px 0;
          }}
          .viz-chip {{
            background: #f8fbfe;
            border: 1px solid #dbe7f2;
            border-radius: 12px;
            padding: 10px 12px;
            color: #28445d;
          }}
          .viz-chip strong {{
            display: block;
            color: #153650;
            margin-bottom: 3px;
          }}
          .table-scroll {{
            max-width: 100%;
            max-height: 430px;
            overflow: auto;
            border: 1px solid #d9e4ee;
            border-radius: 10px;
            background: white;
          }}
          .table-scroll table {{
            border-collapse: collapse;
            min-width: 100%;
            width: max-content;
          }}
          .table-scroll th, .table-scroll td {{
            border: 1px solid #d9e4ee;
            padding: 8px 10px;
            text-align: left;
            white-space: nowrap;
          }}
          .table-scroll thead th {{
            background: #edf5fb;
            color: #17324d;
            position: sticky;
            top: 0;
            z-index: 1;
          }}
          .table-pager {{
            display: flex;
            gap: 8px;
            align-items: center;
            justify-content: flex-end;
            flex-wrap: wrap;
            margin: 8px 0 12px 0;
            color: #4f6578;
            font-size: 13px;
          }}
          .table-pager button {{
            padding: 6px 10px;
            border-radius: 8px;
            background: #eef6fb;
            color: #17324d;
            border: 1px solid #cfe0ec;
          }}
          .table-pager button:disabled {{ opacity: .45; cursor: not-allowed; }}
          .table-pager select {{ width: auto; padding: 6px 8px; border-radius: 8px; }}
          .table-page-info {{ min-width: 170px; text-align: right; }}
          .control-card::-webkit-scrollbar,
          .preview-card::-webkit-scrollbar,
          .side-card::-webkit-scrollbar,
          .step-grid > .card::-webkit-scrollbar,
          .workbench > .card::-webkit-scrollbar {{
            width: 10px;
            height: 10px;
          }}
          .control-card::-webkit-scrollbar-thumb,
          .preview-card::-webkit-scrollbar-thumb,
          .side-card::-webkit-scrollbar-thumb,
          .step-grid > .card::-webkit-scrollbar-thumb,
          .workbench > .card::-webkit-scrollbar-thumb {{
            background: #c8d8e5;
            border-radius: 999px;
          }}

          .report-stage-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 12px;
            margin: 12px 0 18px 0;
          }}
          .report-stage-card {{
            background: #f8fbff;
            border: 1px solid #d9e6f2;
            border-radius: 12px;
            padding: 12px 14px;
          }}
          .report-stage-label {{
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 0.04em;
            color: #5f6f82;
            margin-bottom: 6px;
          }}
          .report-stage-value {{
            font-size: 24px;
            font-weight: 750;
            color: #153650;
            line-height: 1.1;
          }}
          .report-stage-note {{
            margin-top: 6px;
            color: #5b6775;
            font-size: 12px;
          }}
          .report-section-title {{
            font-weight: 750;
            color: #17324d;
            margin-top: 18px;
            margin-bottom: 8px;
          }}
          .report-key-list {{
            margin: 0 0 12px 18px;
            color: #3f4f61;
          }}
          .report-key-list li {{
            margin-bottom: 4px;
          }}
          .report-note {{
            background: #f5f9fc;
            border-left: 4px solid #2c7fb8;
            padding: 10px 12px;
            margin: 10px 0 14px 0;
            color: #24425c;
            border-radius: 6px;
          }}

          .step-grid {{
            display: grid;
            grid-template-columns: minmax(280px, 360px) minmax(0, 1fr);
            gap: 16px;
            align-items: start;
            height: calc(100vh - 175px);
            min-height: 520px;
            overflow: hidden;
          }}
          .side-card {{
            position: sticky;
            top: 16px;
            max-height: calc(100vh - 195px);
            overflow-y: auto;
            overflow-x: hidden;
          }}
          .mini-actions {{
            display: flex;
            gap: 10px;
            flex-wrap: wrap;
            margin-top: 10px;
          }}
          .form-section {{
            border-top: 1px solid #e5edf5;
            padding-top: 12px;
            margin-top: 12px;
          }}
          .inline-label {{
            font-size: 13px;
            font-weight: 650;
            color: #435b70;
            margin-bottom: 4px;
            display: block;
          }}
          .empty-state {{
            border: 1px dashed #cbd8e4;
            border-radius: 12px;
            padding: 16px;
            color: #617485;
            background: #fbfdff;
          }}
          @media (max-width: 980px) {{
            .topbar-inner {{ grid-template-columns: 1fr; gap: 8px; }}
            .topbar-meta {{ display: none; }}
            .workflow-tabs-wrap {{ padding: 0 12px; }}
            .workflow-tab {{ padding: 9px 11px 10px 11px; }}
            .content {{ padding: 16px; }}
            .step-grid {{ grid-template-columns: 1fr; height: auto; min-height: 0; overflow: visible; }}
            .side-card, .step-grid > .card {{ position: relative; top: 0; max-height: none; overflow: visible; }}
          }}

        </style>
      </head>
      <body>
        <div class="app" id="app-shell">
          <header class="topbar">
            <div class="topbar-inner">
              <div class="brand-block">
                <div class="brand">JTrack Insight</div>
                <div class="sub">Digital phenotyping QC and analysis</div>
              </div>
              <div class="topbar-meta">{root_text}</div>
            </div>
            <div class="workflow-tabs-wrap">
              <nav class="workflow-tabs" aria-label="Workflow tabs">{nav}</nav>
            </div>
          </header>
          <main class="content">
            <section class="hero">
              <h1>{html.escape(title)}</h1>
            </section>
            {body}
            <div class="status">{html.escape(APP_STATE.status)}</div>
          </main>
        </div>
        {pager_script}
      </body>
    </html>
    """
    return HTMLResponse(document)


def _select_options(values: Iterable[str], selected: str = "All") -> str:
    """Render safe select options for scalar values.

    Some pages pass (value, label) pairs; support them here as a guard so a
    tuple cannot reach html.escape() and crash group-level analysis.
    """
    options: list[tuple[str, str]] = [("All", "All")]
    for item in values:
        if isinstance(item, (tuple, list)) and len(item) >= 2:
            value, label = item[0], item[1]
        else:
            value, label = item, item
        value_s = str(value)
        label_s = str(label)
        if value_s == "All" and options and options[0][0] == "All":
            continue
        options.append((value_s, label_s))
    selected_s = str(selected)
    return "\n".join(
        f"<option value='{html.escape(value)}' {'selected' if value == selected_s else ''}>{html.escape(label)}</option>"
        for value, label in options
    )


def _summary_metrics(metrics: dict[str, int]) -> str:
    return """
    <div class="grid">
      {}
    </div>
    """.format(
        "\n".join(
            f"<div class='metric'><div class='metric-label'>{html.escape(label)}</div><div class='metric-value'>{value}</div></div>"
            for label, value in metrics.items()
        )
    )


def _home_page() -> HTMLResponse:
    body = """
    <div class="card">
      <h2>Home</h2>
      <p class="muted">This local browser UI provides the JTrack Insight standalone workflow while keeping the same Python backend modules.</p>
      <ul>
        <li>Step 1 metadata indexing</li>
        <li>Step 2 QC summary</li>
        <li>Step 3 feature computation with subject, sensor, and feature selection</li>
        <li>Step 4 study-level and feature-level QC</li>
        <li>Step 5 review, filtering, and export of generated features</li>
        <li>Step 6 reporting from the reviewed and QC-filtered feature table</li>
        <li>Step 7 group-level analysis for cohort comparisons</li>
      </ul>
    </div>
    """
    return _page_shell("home", "Home", body)



def _qc_flagged_preview_rows(limit: int = 100) -> list[dict]:
    rows = []
    for item in APP_STATE.qc_rows:
        status = getattr(item, "qc_status", "")
        if status == "clean":
            continue
        rows.append({
            "status": status,
            "file": str(getattr(item, "source_file", "")),
            "valid_json": getattr(item, "json_valid", ""),
            "duplicate": getattr(item, "duplicate_file", ""),
            "error": getattr(item, "json_error", "") or "",
        })
        if len(rows) >= limit:
            break
    return rows


def _compact_kv(items: list[tuple[str, object]]) -> str:
    if not items:
        return "<p class='muted'>No summary available.</p>"
    return "<ul class='report-key-list'>" + "".join(
        f"<li><strong>{html.escape(str(k))}:</strong> {html.escape(str(v))}</li>" for k, v in items
    ) + "</ul>"


def _step1_page(dataset_root: str | None = None) -> HTMLResponse:
    counts = summarize_indexed_files(APP_STATE.indexed_rows) if APP_STATE.indexed_rows else {"json_files": 0, "subjects": 0, "devices": 0, "sensors": 0}
    summary_cards = _summary_metrics({
        "JSON files": counts["json_files"],
        "Subjects": counts["subjects"],
        "Devices": counts["devices"],
        "Sensors": counts["sensors"],
    })
    dataset_value = dataset_root if dataset_root not in (None, "") else (APP_STATE.data_root or "")
    body = f"""
    <div class="step-grid">
      <div class="card side-card">
        <h2>Load dataset</h2>
        <p class="compact-note">Choose the study root folder or paste an absolute path. Indexing also runs the file-level QC scan.</p>
        <form action="/action/pick_dataset" method="get">
          <button type="submit">Choose Folder</button>
        </form>
        <form action="/action/load_dataset" method="get" class="form-section">
          <label class="inline-label">Dataset root</label>
          <input type="text" name="dataset_root" placeholder="/absolute/path/to/dataset" value="{html.escape(dataset_value)}" />
          <button type="submit">Load Dataset</button>
        </form>
      </div>
      <div class="card preview-card">
        <h2>Indexed dataset</h2>
        {summary_cards if APP_STATE.indexed_rows else '<div class="empty-state">No dataset indexed yet.</div>'}
        <details class="clean-details">
          <summary>Indexing note</summary>
          <p class="compact-note">The index summarizes available JSON files, participants, devices, and sensor streams without loading all raw records into memory.</p>
        </details>
      </div>
    </div>
    """
    return _page_shell("step1", "Step 1  Load / Index", body)


def _step2_page() -> HTMLResponse:
    qc_rows = APP_STATE.qc_rows
    if qc_rows:
        summary = summarize_qc_results(qc_rows)
        metrics = _summary_metrics({
            "Total files": summary.total_files,
            "Clean files": summary.clean_files,
            "Invalid JSON": summary.invalid_json_files,
            "Duplicates": summary.duplicate_files,
        })
        flagged = _qc_flagged_preview_rows()
        flagged_preview = _table_preview(flagged, limit=20) if flagged else "<div class='empty-state'>No flagged files detected.</div>"
    else:
        metrics = "<div class='empty-state'>No QC scan has been run yet. Load a dataset in Step 1.</div>"
        flagged_preview = "<div class='empty-state'>No flagged-file preview available.</div>"
    body = f"""
    <div class="step-grid">
      <div class="card side-card">
        <h2>File QC</h2>
        <p class="compact-note">Review JSON validity and duplicate-file checks before computing features.</p>
        {metrics}
      </div>
      <div class="card preview-card">
        <h2>Flagged files</h2>
        {flagged_preview}
      </div>
    </div>
    """
    return _page_shell("step2", "Step 2  File QC", body)



FEATURE_LABELS = {
    "raw_sensor_data": "Raw sensor data",
    "application_usage_daily": "Application usage daily features",
    "application_usage_category_daily": "Application usage category features",
    "location_daily": "Location daily features",
    "pedometer_daily": "Pedometer daily features",
    "sensor_daily_summary": "Sensor summary features",
    "activity_features": "Android activity features",
    "custom_feature": "Custom uploaded feature",
}

SENSOR_FEATURES = {
    "APPLICATION_USAGE": ["application_usage_daily", "application_usage_category_daily"],
    "USAGE": ["application_usage_daily", "application_usage_category_daily"],
    "LOCATION": ["location_daily"],
    "GPS": ["location_daily"],
    "GEOLOCATION": ["location_daily"],
    "PEDOMETER": ["pedometer_daily"],
    "STEPS": ["pedometer_daily"],
    "STEP": ["pedometer_daily"],
    "ACTIVITY": ["activity_features"],
    "BBI": ["sensor_daily_summary"],
    "ENHANCED_BBI": ["sensor_daily_summary"],
    "HEART_RATE": ["sensor_daily_summary"],
    "HEARTRATE": ["sensor_daily_summary"],
    "HRV": ["sensor_daily_summary"],
    "CALORIES": ["sensor_daily_summary"],
    "RESPIRATION": ["sensor_daily_summary"],
    "SPO2": ["sensor_daily_summary"],
    "STRESS": ["sensor_daily_summary"],
    "WRIST_STATUS": ["sensor_daily_summary"],
    "ZERO_CROSSING": ["sensor_daily_summary"],
    "ACTIGRAPHY_1": ["sensor_daily_summary"],
    "ACTIGRAPHY_2": ["sensor_daily_summary"],
    "ACTIGRAPHY_3": ["sensor_daily_summary"],
}

FEATURE_MODE_CHOICES = [
    ("core", "Core clinical features"),
    ("qc", "QC / data completeness features"),
    ("advanced", "Advanced / exploratory features"),
    ("custom", "Custom uploaded script"),
]

COMMON_FEATURE_METRICS = [
    ("all", "All available features"),
    ("records", "Record count / coverage"),
    ("temporal_coverage", "Temporal coverage"),
    ("sampling", "Sampling interval / continuity"),
]

QC_FEATURE_METRICS = [
    ("records", "Record count"),
    ("temporal_coverage", "Temporal coverage / valid days"),
    ("data_frequency", "Computed data frequency"),
    ("sampling", "Sampling interval / gaps"),
    ("outliers", "Out-of-range / artifact burden"),
]

CORE_SENSOR_FEATURE_METRICS = {
    "APPLICATION_USAGE": [
        ("total_foreground", "Total app-use time"),
        ("category", "App-category usage"),
        ("unique_apps", "Unique apps"),
    ],
    "USAGE": [
        ("total_foreground", "Total app-use time"),
        ("category", "App-category usage"),
        ("unique_apps", "Unique apps"),
    ],
    "LOCATION": [
        ("daily_distance", "Distance"),
        ("mobility", "Mobility radius / location variability"),
    ],
    "GPS": [
        ("daily_distance", "Distance"),
        ("mobility", "Mobility radius / location variability"),
    ],
    "GEOLOCATION": [
        ("daily_distance", "Distance"),
        ("mobility", "Mobility radius / location variability"),
    ],
    "PEDOMETER": [
        ("daily_steps", "Steps"),
        ("active_hours", "Active hours"),
    ],
    "STEPS": [
        ("daily_steps", "Steps"),
        ("active_hours", "Active hours"),
    ],
    "STEP": [
        ("daily_steps", "Steps"),
        ("active_hours", "Active hours"),
    ],
    "ACTIVITY": [
        ("activity_time", "Time per Android activity label"),
        ("activity_share", "Activity-label percentage"),
    ],
    "BBI": [
        ("bbi_hrv", "SDNN / RMSSD / pNN metrics"),
        ("outliers", "Artifact / out-of-range BBI"),
    ],
    "ENHANCED_BBI": [
        ("bbi_hrv", "SDNN / RMSSD / pNN metrics"),
        ("outliers", "Artifact / out-of-range BBI"),
    ],
    "HEART_RATE": [
        ("resting_hr", "Resting heart-rate proxy"),
        ("peak_hr", "Peak heart-rate proxy"),
        ("hr_variability", "Heart-rate variability / range"),
    ],
    "HEARTRATE": [
        ("resting_hr", "Resting heart-rate proxy"),
        ("peak_hr", "Peak heart-rate proxy"),
        ("hr_variability", "Heart-rate variability / range"),
    ],
    "HRV": [
        ("hrv_distribution", "HRV distribution"),
    ],
    "STRESS": [
        ("stress_burden", "Stress burden"),
        ("high_stress", "High-stress percentage"),
        ("recovery", "Low-stress / recovery percentage"),
    ],
    "SPO2": [
        ("spo2_summary", "SpO2 summary"),
        ("low_spo2", "Low-SpO2 burden"),
    ],
    "RESPIRATION": [
        ("respiration_summary", "Respiration summary"),
    ],
    "CALORIES": [
        ("energy_expenditure", "Energy expenditure"),
    ],
    "ACTIGRAPHY_1": [
        ("activity_energy", "Activity energy"),
    ],
    "ACTIGRAPHY_2": [
        ("zero_crossing", "Zero-crossing activity"),
    ],
    "ACTIGRAPHY_3": [
        ("threshold_activity", "Time-above-threshold activity"),
    ],
    "ZERO_CROSSING": [
        ("zero_crossing", "Zero-crossing activity"),
    ],
}

SENSOR_FEATURE_METRICS = {
    "APPLICATION_USAGE": [
        ("total_foreground", "Total foreground time"),
        ("mean_foreground", "Mean foreground time"),
        ("unique_apps", "Unique apps"),
        ("app_diversity", "App-use diversity / concentration"),
        ("category", "App-category totals"),
    ],
    "USAGE": [
        ("total_foreground", "Total foreground time"),
        ("mean_foreground", "Mean foreground time"),
        ("unique_apps", "Unique apps"),
        ("app_diversity", "App-use diversity / concentration"),
        ("category", "App-category totals"),
    ],
    "LOCATION": [
        ("daily_distance", "Distance"),
        ("mobility", "Mobility radius / location variability"),
        ("speed", "Speed / movement bursts"),
        ("accuracy", "Location accuracy"),
        ("location_points", "Location record count"),
    ],
    "GPS": [
        ("daily_distance", "Distance"),
        ("mobility", "Mobility radius / location variability"),
        ("speed", "Speed / movement bursts"),
        ("accuracy", "Location accuracy"),
        ("location_points", "Location record count"),
    ],
    "GEOLOCATION": [
        ("daily_distance", "Distance"),
        ("mobility", "Mobility radius / location variability"),
        ("speed", "Speed / movement bursts"),
        ("accuracy", "Location accuracy"),
        ("location_points", "Location record count"),
    ],
    "PEDOMETER": [
        ("daily_steps", "Steps"),
        ("active_hours", "Active hours"),
        ("sedentary_hours", "Sedentary / zero-step hours"),
        ("peak_hourly_steps", "Peak hourly steps"),
    ],
    "STEPS": [
        ("daily_steps", "Steps"),
        ("active_hours", "Active hours"),
        ("sedentary_hours", "Sedentary / zero-step hours"),
        ("peak_hourly_steps", "Peak hourly steps"),
    ],
    "STEP": [
        ("daily_steps", "Steps"),
        ("active_hours", "Active hours"),
        ("sedentary_hours", "Sedentary / zero-step hours"),
        ("peak_hourly_steps", "Peak hourly steps"),
    ],
    "ACTIVITY": [
        ("activity_time", "Time per Android activity label"),
        ("activity_share", "Activity-label percentage"),
        ("activity_confidence", "Activity recognition confidence"),
        ("activity_transitions", "Activity transitions"),
        ("activity_records", "Records per activity label"),
    ],
    "BBI": [
        ("bbi_hrv", "BBI/HRV time-domain metrics"),
        ("autonomic_variability", "Autonomic variability"),
        ("outliers", "Physiological outliers"),
    ],
    "ENHANCED_BBI": [
        ("bbi_hrv", "BBI/HRV time-domain metrics"),
        ("autonomic_variability", "Autonomic variability"),
        ("outliers", "Physiological outliers"),
    ],
    "HEART_RATE": [
        ("resting_hr", "Resting heart-rate proxy"),
        ("peak_hr", "Peak heart-rate proxy"),
        ("hr_variability", "Heart-rate variability/range"),
        ("outliers", "Brady/tachy range flags"),
    ],
    "HEARTRATE": [
        ("resting_hr", "Resting heart-rate proxy"),
        ("peak_hr", "Peak heart-rate proxy"),
        ("hr_variability", "Heart-rate variability/range"),
        ("outliers", "Brady/tachy range flags"),
    ],
    "HRV": [
        ("hrv_distribution", "HRV distribution"),
        ("autonomic_variability", "Autonomic variability"),
        ("outliers", "Physiological outliers"),
    ],
    "STRESS": [
        ("stress_burden", "Stress burden"),
        ("high_stress", "High-stress percentage"),
        ("recovery", "Low-stress/recovery percentage"),
    ],
    "SPO2": [
        ("spo2_summary", "SpO2 summary"),
        ("low_spo2", "Low-SpO2 burden"),
    ],
    "RESPIRATION": [
        ("respiration_summary", "Respiration summary"),
        ("night_mean", "Night-time mean"),
    ],
    "CALORIES": [
        ("energy_expenditure", "Energy expenditure"),
        ("active_calories", "Active/resting calories"),
    ],
    "ACTIGRAPHY_1": [
        ("activity_energy", "Activity energy"),
        ("activity_variability", "Activity variability"),
    ],
    "ACTIGRAPHY_2": [
        ("zero_crossing", "Zero-crossing activity"),
        ("activity_variability", "Activity variability"),
    ],
    "ACTIGRAPHY_3": [
        ("threshold_activity", "Time-above-threshold activity"),
        ("activity_variability", "Activity variability"),
    ],
    "ZERO_CROSSING": [
        ("zero_crossing", "Zero-crossing activity"),
        ("activity_variability", "Activity variability"),
    ],
}

GENERIC_SENSOR_FEATURE_METRICS = [
    ("mean", "Mean"),
    ("median", "Median"),
    ("std", "Standard deviation"),
    ("iqr", "Interquartile range"),
    ("percentiles", "5th/25th/75th/95th percentiles"),
    ("min", "Minimum"),
    ("max", "Maximum"),
    ("sum", "Sum"),
    ("cv", "Coefficient of variation"),
    ("trend", "Linear trend over time"),
]


def _norm(value: str | None) -> str:
    return (value or "").strip().upper().replace("-", "_").replace(" ", "_")


def _label_key(value: object) -> str:
    """Normalize labels for robust stream matching across folder names/columns."""
    return re.sub(r"[^A-Z0-9]+", "", str(value or "").upper())


def _row_get_ci(row: dict, keys: Iterable[str]) -> object:
    """Case-insensitive row lookup for messy JSON column names."""
    if not row:
        return None
    direct = {str(k).lower(): k for k in row.keys()}
    for key in keys:
        hit = direct.get(str(key).lower())
        if hit is not None:
            value = row.get(hit)
            if value not in (None, ""):
                return value
    return None


def _row_get_float_ci(row: dict, keys: Iterable[str]) -> float | None:
    value = _row_get_ci(row, keys)
    return _safe_float(value)


def _row_text_blob(row: dict) -> str:
    """Text used only for robust sensor matching when metadata are incomplete."""
    parts = []
    for key in (
        "sensorname", "sensor_name", "sensor", "wearable_sensor",
        "sensor_file", "wearable_file", "sensor_folder", "file_name", "source_file"
    ):
        value = row.get(key)
        if value not in (None, ""):
            parts.append(str(value))
    return " ".join(parts)


def _display_sensor_choice(value: str | None) -> str | None:
    text = (value or "").strip()
    if not text or text == "None":
        return None
    # Garmin folders are stored as garmin_<sensor>. In Step 3 we expose only
    # the actual sensor stream (BBI, HEART_RATE, ACTIGRAPHY_1, etc.), not a
    # separate Garmin grouping.
    lower = text.lower()
    if lower.startswith("garmin_"):
        text = text.split("_", 1)[1]
    if lower == "garmin":
        return None
    return text


def _combined_sensor_choices() -> list[str]:
    choices = available_filter_choices(APP_STATE.indexed_rows)
    values: set[str] = set()
    for raw in list(choices.get("sensor_name", [])) + list(choices.get("wearable_sensor", [])):
        cleaned = _display_sensor_choice(raw)
        if cleaned:
            values.add(cleaned)
    return sorted(values, key=lambda x: x.upper())



CUSTOM_FEATURE_TEMPLATE = r'''
"""
JTrack Insight custom feature template

This script computes custom features from the selected raw data.
Upload this .py file in Step 3 > Custom feature script.

Required function:
    compute_feature(rows, context)

Inputs:
    rows:
        A list of dictionaries. Each dictionary is one raw data row after
        the user-selected filters such as participant, sensor, and temporal
        frequency.

    context:
        A dictionary with useful metadata, for example:
        {
            "sensor_name": "HEART_RATE",
            "participant": "TrackAutism_001",
            "temporal_frequency": "daily",
            "selected_features": ["custom_script"]
        }

Output:
    A list of dictionaries. Each dictionary becomes one row in the generated
    feature table.

Important:
    Always include:
        Subject_ID
        time_bin
        temporal_frequency

    Then add your custom feature columns.

Example below:
    Computes min, max, mean, and valid-record count from the raw column "value"
    for each subject and selected temporal bin.
"""

from collections import defaultdict
from datetime import datetime, timezone
import math
import re


def _to_float(value):
    """Safely convert values to float."""
    try:
        if value is None or value == "":
            return None
        x = float(value)
        if math.isnan(x):
            return None
        return x
    except Exception:
        return None


def _get_subject(row, context):
    """Try common subject column names."""
    return (
        row.get("Subject_ID")
        or row.get("username")
        or row.get("subject")
        or row.get("participant")
        or context.get("participant")
        or "unknown_subject"
    )


def _get_timestamp(row):
    """
    Try common timestamp fields.

    Supported formats:
    - Unix milliseconds, e.g. 1716372000000
    - Unix seconds, e.g. 1716372000
    - ISO/string datetime, e.g. 2024-05-22 14:30:00
    """
    value = (
        row.get("analysis_time_ms")
        or row.get("analysis_time")
        or row.get("timestamp")
        or row.get("timestamp_start")
        or row.get("startTime")
        or row.get("lastTimeUsed")
        or row.get("time")
        or row.get("datetime")
    )

    if value is None or value == "":
        return None

    try:
        value_float = float(value)
        if abs(value_float) > 1e11:  # milliseconds
            return datetime.fromtimestamp(value_float / 1000, tz=timezone.utc)
        return datetime.fromtimestamp(value_float, tz=timezone.utc)  # seconds
    except Exception:
        pass

    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(str(value), fmt).replace(tzinfo=timezone.utc)
        except Exception:
            continue

    return None


def _make_time_bin(dt, temporal_frequency):
    """Create output time_bin based on the selected temporal frequency."""
    if dt is None:
        return "unknown_time"

    frequency = str(temporal_frequency or "daily").lower()

    if frequency == "hourly":
        return dt.replace(minute=0, second=0, microsecond=0).isoformat()

    if frequency == "monthly":
        return dt.strftime("%Y-%m")

    if frequency in ("full", "full_study", "study", "study_duration"):
        return "full_study"

    # default: daily
    return dt.date().isoformat()


def compute_feature(rows, context):
    """
    Example custom feature:
    Compute min, max, mean, and number of valid records per subject/time bin.

    Change value_column if your raw data use a different numeric column.
    Common examples: "value", "steps", "stepCount", "heartRate", "bbi".
    """
    temporal_frequency = context.get("temporal_frequency", "daily")
    value_column = "value"

    grouped_values = defaultdict(list)

    for row in rows:
        subject = _get_subject(row, context)
        dt = _get_timestamp(row)
        time_bin = _make_time_bin(dt, temporal_frequency)

        value = _to_float(row.get(value_column))
        if value is None:
            continue

        grouped_values[(subject, time_bin)].append(value)

    output_rows = []

    for (subject, time_bin), values in sorted(grouped_values.items()):
        if not values:
            continue

        output_rows.append({
            "Subject_ID": subject,
            "time_bin": time_bin,
            "temporal_frequency": temporal_frequency,
            "custom_min": min(values),
            "custom_max": max(values),
            "custom_mean": sum(values) / len(values),
            "custom_valid_records": len(values),
        })

    return output_rows
'''


def _custom_feature_metric_pair() -> tuple[str, str]:
    name = APP_STATE.custom_feature_script_name or "custom feature script"
    return ("custom_script", f"Custom script: {name}")


def _custom_feature_tools_html() -> str:
    script_name = APP_STATE.custom_feature_script_name or "No script uploaded"
    error_html = (
        f"<p class='warning'>Last custom-feature error: {html.escape(APP_STATE.custom_feature_last_error)}</p>"
        if APP_STATE.custom_feature_last_error else ""
    )
    template_preview = html.escape(CUSTOM_FEATURE_TEMPLATE)
    return f"""
    <details class="clean-details">
      <summary>Custom feature script</summary>
      <p class="compact-note">Upload a trusted Python script to add a custom feature option. The script must define <code>compute_feature(rows, context)</code> and return a list of dictionaries.</p>
      <form action="/action/upload_custom_feature_script" method="post" enctype="multipart/form-data">
        <label>Python feature script</label>
        <input type="file" name="script_file" accept=".py,text/x-python,text/plain" />
        <div class="button-row" style="margin-top:10px;">
          <button type="submit">Upload Custom Feature Script</button>
          <a class="button secondary" href="/download/custom_feature_template.py">Download Example Template</a>
        </div>
      </form>
      <details class="clean-details" style="margin-top:12px;">
        <summary>View example template: min / max / mean feature</summary>
        <pre class="code-preview"><code>{template_preview}</code></pre>
      </details>
      <p class="compact-note">Current script: <strong>{html.escape(script_name)}</strong>. After upload, choose <strong>Custom uploaded script</strong> as the feature mode and select the custom feature.</p>
      <p class="compact-note">Run only scripts you trust. Custom scripts execute locally on this computer.</p>
      {error_html}
    </details>
    """


def _run_custom_feature_script(rows: list[dict], sensor_name: str, temporal_frequency: str, selected_features: list[str]) -> list[dict]:
    if not APP_STATE.custom_feature_script_text:
        raise RuntimeError("No custom feature script has been uploaded.")
    namespace: dict = {"__name__": "jtrack_custom_feature"}
    exec(APP_STATE.custom_feature_script_text, namespace)
    func = namespace.get("compute_feature")
    if not callable(func):
        raise RuntimeError("Custom script must define a callable compute_feature(rows, context) function.")
    context = {
        "sensor_name": sensor_name,
        "temporal_frequency": temporal_frequency,
        "selected_features": selected_features,
        "script_name": APP_STATE.custom_feature_script_name or "custom_feature.py",
    }
    result = func(list(rows), context)
    if hasattr(result, "to_dict"):
        result = result.to_dict(orient="records")
    if not isinstance(result, list):
        raise RuntimeError("Custom compute_feature must return a list of dictionaries or a pandas DataFrame.")
    cleaned: list[dict] = []
    for idx, row in enumerate(result):
        if not isinstance(row, dict):
            raise RuntimeError(f"Custom feature row {idx + 1} is not a dictionary.")
        item = {str(k): v for k, v in row.items()}
        item.setdefault("temporal_frequency", temporal_frequency)
        item.setdefault("custom_feature_script", APP_STATE.custom_feature_script_name or "custom_feature.py")
        cleaned.append(item)
    return cleaned


def _feature_choices_for_sensor(sensor_name: str | None) -> list[str]:
    key = _norm(sensor_name)
    if key in ("", "ALL"):
        return list(FEATURE_LABELS.keys())
    if key in SENSOR_FEATURES:
        return SENSOR_FEATURES[key]
    if "LOCATION" in key or "GPS" in key:
        return ["location_daily"]
    if "PEDOMETER" in key or "STEP" in key:
        return ["pedometer_daily"]
    if key == "ACTIVITY" or "ACTIVITY" in key:
        return ["activity_features"]
    if "APP" in key or "USAGE" in key:
        return ["application_usage_daily", "application_usage_category_daily"]
    return ["sensor_daily_summary"]


def _feature_metric_choices_for_sensor(sensor_name: str | None, feature_mode: str = "core") -> list[tuple[str, str]]:
    """Return feature metrics shown in Step 3 for the selected sensor.

    Feature modes keep the UI clinically interpretable:
    - core: concise default clinical features
    - qc: coverage/completeness/artifact checks
    - advanced: full exploratory set including generic statistics
    """
    key = _norm(sensor_name)
    mode = (feature_mode or "core").lower()

    if mode == "custom":
        pairs = [_custom_feature_metric_pair()]
    elif mode == "qc":
        pairs = list(QC_FEATURE_METRICS)
    elif mode == "advanced":
        pairs = list(COMMON_FEATURE_METRICS)
        if key in SENSOR_FEATURE_METRICS:
            pairs.extend(SENSOR_FEATURE_METRICS[key])
        elif "LOCATION" in key or "GPS" in key:
            pairs.extend(SENSOR_FEATURE_METRICS["LOCATION"])
        elif "PEDOMETER" in key or "STEP" in key:
            pairs.extend(SENSOR_FEATURE_METRICS["PEDOMETER"])
        elif key == "ACTIVITY" or "ACTIVITY" in key:
            pairs.extend(SENSOR_FEATURE_METRICS["ACTIVITY"])
        elif "APP" in key or "USAGE" in key:
            pairs.extend(SENSOR_FEATURE_METRICS["APPLICATION_USAGE"])
        else:
            pairs.extend(GENERIC_SENSOR_FEATURE_METRICS)
    else:
        pairs = []
        if key in CORE_SENSOR_FEATURE_METRICS:
            pairs.extend(CORE_SENSOR_FEATURE_METRICS[key])
        elif "LOCATION" in key or "GPS" in key:
            pairs.extend(CORE_SENSOR_FEATURE_METRICS["LOCATION"])
        elif "PEDOMETER" in key or "STEP" in key:
            pairs.extend(CORE_SENSOR_FEATURE_METRICS["PEDOMETER"])
        elif key == "ACTIVITY" or "ACTIVITY" in key:
            pairs.extend(CORE_SENSOR_FEATURE_METRICS["ACTIVITY"])
        elif "APP" in key or "USAGE" in key:
            pairs.extend(CORE_SENSOR_FEATURE_METRICS["APPLICATION_USAGE"])
        else:
            # Generic wearable streams: keep a small, interpretable default.
            pairs.extend([
                ("mean", "Mean"),
                ("median", "Median"),
                ("std", "Standard deviation"),
                ("percentiles", "5th/25th/75th/95th percentiles"),
                ("outliers", "Out-of-range / artifact burden"),
            ])

    if mode != "custom" and APP_STATE.custom_feature_script_text:
        pairs.append(_custom_feature_metric_pair())

    # Preserve order while removing duplicates.
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for value, label in pairs:
        if value not in seen:
            seen.add(value)
            out.append((value, label))
    return out


def _filter_feature_columns(rows: list[dict], sensor_name: str = "All", feature_name: str | None = "all") -> list[dict]:
    """Keep identifiers plus columns relevant to the selected feature metric."""
    if not rows or not feature_name or feature_name in {"all", "All"}:
        return rows

    features = [item.strip() for item in str(feature_name).split(",") if item.strip()]
    if not features or "all" in {item.lower() for item in features}:
        return rows
    id_cols = {
        "Subject_ID", "username", "Study_day", "study_day", "Date", "date", "time_bin", "temporal_frequency",
        "sensor", "cohort", "group", "group_label", "app_category", "category",
    }

    keyword_map = {
        "records": ["records", "count", "points"],
        "temporal_coverage": ["coverage", "observed", "active_hours", "duration_hours", "temporal"],
        "data_frequency": ["computed_frequency"],
        "sampling": ["sampling", "interval", "gap", "continuity"],
        "total_foreground": ["total_foreground"],
        "mean_foreground": ["mean_foreground"],
        "unique_apps": ["unique_apps"],
        "app_diversity": ["app_entropy", "app_hhi", "top_app_share"],
        "category": ["app_category", "category", "foreground_hours", "foreground_min"],
        "daily_distance": ["daily_distance", "distance_km"],
        "mobility": ["radius", "lat_span", "lon_span", "location_entropy", "unique_locations"],
        "speed": ["speed", "movement"],
        "accuracy": ["accuracy"],
        "location_points": ["records", "location_points", "points"],
        "daily_steps": ["daily_steps", "steps"],
        "active_hours": ["active_hours"],
        "sedentary_hours": ["sedentary", "zero_step"],
        "peak_hourly_steps": ["peak_hourly_steps"],
        "bbi_hrv": ["sdnn", "rmssd", "pnn", "bbi", "hrv"],
        "autonomic_variability": ["sdnn", "rmssd", "cv", "variability"],
        "resting_hr": ["resting", "p10", "heart_rate"],
        "peak_hr": ["peak", "p95", "heart_rate"],
        "hr_variability": ["hr_range", "heart_rate", "std", "cv"],
        "outliers": ["out_of_range", "brady", "tachy", "low", "high"],
        "stress_burden": ["stress", "burden", "mean", "p95"],
        "high_stress": ["high_stress"],
        "recovery": ["low_stress", "recovery"],
        "spo2_summary": ["spo2", "oxygen", "p05", "min"],
        "low_spo2": ["low_spo2"],
        "respiration_summary": ["respiration", "respiratory"],
        "night_mean": ["night"],
        "energy_expenditure": ["calories", "energy", "sum"],
        "active_calories": ["active_calories", "resting_calories"],
        "activity_time": ["time_", "duration", "estimated_duration", "dominant_activity"],
        "activity_share": ["pct_", "share_", "dominant_activity"],
        "activity_confidence": ["confidence", "low_confidence"],
        "activity_transitions": ["transition"],
        "activity_records": ["records_", "records", "unique_activity_labels"],
        "activity_energy": ["energy", "activity", "sum"],
        "zero_crossing": ["zero_crossing"],
        "threshold_activity": ["threshold"],
        "activity_variability": ["std", "iqr", "cv", "activity"],
        "mean": ["_mean"],
        "median": ["_median"],
        "std": ["_std"],
        "iqr": ["_iqr"],
        "percentiles": ["_p05", "_p25", "_p75", "_p95"],
        "min": ["_min"],
        "max": ["_max"],
        "sum": ["_sum"],
        "cv": ["_cv"],
        "trend": ["trend", "slope"],
    }
    keywords: list[str] = []
    for feature in features:
        feature_key = _norm(feature).lower()
        keywords.extend(keyword_map.get(feature_key, [feature_key]))

    all_cols = list(dict.fromkeys(col for row in rows for col in row.keys()))
    keep_cols = [col for col in all_cols if col in id_cols or any(keyword in col.lower() for keyword in keywords)]
    if not any(col not in id_cols for col in keep_cols):
        return rows

    filtered: list[dict] = []
    for row in rows:
        filtered.append({col: row.get(col) for col in keep_cols if col in row})
    return filtered



TEMPORAL_FREQUENCY_CHOICES = [
    ("daily", "Daily"),
    ("hourly", "Hourly"),
    ("monthly", "Monthly"),
    ("study_duration", "Full study duration"),
]


def _multi_select_options_from_pairs(pairs: Iterable[tuple[str, str]], selected_values: Iterable[str] | None = None) -> str:
    selected_set = set(selected_values or [])
    return "\n".join(
        f"<option value='{html.escape(value)}' {'selected' if value in selected_set else ''}>{html.escape(label)}</option>"
        for value, label in pairs
    )


def _default_feature_names_for_sensor(sensor_name: str | None, feature_mode: str = "core") -> list[str]:
    """Small, sensor-aware defaults that keep feature computation simple."""
    key = _norm(sensor_name)
    mode = (feature_mode or "core").lower()
    if mode == "custom":
        return ["custom_script"]
    if mode == "qc":
        return ["records", "temporal_coverage", "data_frequency"]
    if mode == "advanced":
        return ["mean", "median", "std", "percentiles"]
    if "APP" in key or "USAGE" in key:
        return ["total_foreground", "category", "unique_apps"]
    if "LOCATION" in key or "GPS" in key:
        return ["daily_distance", "mobility"]
    if "PEDOMETER" in key or "STEP" in key:
        return ["daily_steps", "active_hours"]
    if key == "ACTIVITY" or "ACTIVITY" in key:
        return ["activity_time", "activity_share"]
    if key in {"BBI", "ENHANCED_BBI", "HRV"}:
        return ["bbi_hrv", "outliers"]
    if key in {"HEART_RATE", "HEARTRATE", "HR"}:
        return ["resting_hr", "peak_hr", "hr_variability"]
    if "SPO2" in key or "PULSE" in key:
        return ["spo2_summary", "low_spo2"]
    if "STRESS" in key:
        return ["stress_burden", "high_stress", "recovery"]
    if "RESP" in key:
        return ["respiration_summary"]
    if "CALOR" in key:
        return ["energy_expenditure"]
    if "ACTIGRAPHY" in key or "ZERO_CROSSING" in key:
        if "2" in key or "ZERO_CROSSING" in key:
            return ["zero_crossing"]
        if "3" in key:
            return ["threshold_activity"]
        return ["activity_energy"]
    return ["mean", "median", "percentiles"]


def _checkbox_feature_options_from_pairs(pairs: Iterable[tuple[str, str]], selected_values: Iterable[str] | None = None) -> str:
    selected_set = set(selected_values or [])
    rows: list[str] = []
    for value, label in pairs:
        checked = "checked" if value in selected_set else ""
        value_esc = html.escape(str(value))
        label_esc = html.escape(str(label))
        hint = "" if value != "all" else "<small>Use only when you want every available metric for this sensor.</small>"
        rows.append(
            f"<label class='feature-option'><input type='checkbox' name='feature_names' value='{value_esc}' form='feature_compute_form' {checked} />"
            f"<span>{label_esc}{hint}</span></label>"
        )
    return "<div class='feature-grid' id='step3_features'>" + "\n".join(rows) + "</div>"


def _normalise_feature_names(feature_names: list[str] | str | None) -> list[str]:
    if feature_names is None:
        return ["all"]
    if isinstance(feature_names, str):
        values = [feature_names]
    else:
        values = list(feature_names)
    values = [str(v).strip() for v in values if str(v).strip()]
    if not values or "all" in values or "All" in values:
        return ["all"]
    return values


def _has_selected_metric(selected_features: Iterable[str] | None, metric_name: str) -> bool:
    selected = {str(item).strip().lower() for item in (selected_features or []) if str(item).strip()}
    if not selected or "all" in selected:
        return True
    return str(metric_name).strip().lower() in selected


def _feature_key_for_selection(sensor_name: str, feature_names: list[str]) -> str:
    if "custom_script" in feature_names:
        return "custom_feature"
    choices = _feature_choices_for_sensor(sensor_name)
    if not choices:
        return "sensor_daily_summary"
    if "application_usage_category_daily" in choices and "category" in feature_names:
        return "application_usage_category_daily"
    if "activity_features" in choices:
        return "activity_features"
    return choices[0]


def _row_datetime(row: dict) -> datetime | None:
    for key in ("analysis_time_iso", "timestamp_iso", "datetime", "dateTime", "Date", "date", "measurementTime", "recordedTime"):
        raw = _row_get_ci(row, [key])
        if raw:
            text = str(raw).strip().replace("Z", "+00:00")
            try:
                dt = datetime.fromisoformat(text)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except ValueError:
                pass
    for key in (
        "analysis_time_ms", "timestamp", "time", "timeStamp", "Timestamp", "timestampMs",
        "timestamp_ms", "measurementTimestamp", "measurementTimeStamp", "measurement_time",
        "sampleTimestamp", "sampleTimeStamp", "recordedTimestamp", "recordedTimeStamp",
        "eventTimestamp", "eventTime", "createdAt", "createTime", "serverTimestamp",
        "timestamp_start", "timestamp_end", "startTime", "endTime", "beginTimeStamp",
        "endTimeStamp", "lastTimeUsed", "location.timestamp", "locationTimestamp"
    ):
        value = _row_get_float_ci(row, [key])
        if value is None:
            continue
        if abs(value) < 1e11:
            value *= 1000
        try:
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            pass
    return None

def _temporal_key(row: dict, temporal_frequency: str) -> tuple[object, str, str]:
    freq = (temporal_frequency or "daily").lower()
    subject = _subject_label(row)
    if freq == "study_duration":
        return subject, "study_duration", "Full study duration"
    dt = _row_datetime(row)
    if dt is not None:
        if freq == "hourly":
            return subject, dt.strftime("%Y-%m-%d %H:00"), dt.strftime("%Y-%m-%d %H:00")
        if freq == "monthly":
            return subject, dt.strftime("%Y-%m"), dt.strftime("%Y-%m")
        return subject, dt.strftime("%Y-%m-%d"), dt.strftime("%Y-%m-%d")
    study_day = row.get("Study_day", row.get("study_day"))
    if freq == "monthly":
        return subject, "month_unknown", "Unknown month"
    if freq == "hourly":
        return subject, f"study_day_{study_day}_hour_unknown", f"Study day {study_day}, hour unknown"
    return subject, study_day, str(row.get("Date") or row.get("date") or f"Study day {study_day}")


def _ensure_temporal_frequency_column(rows: list[dict], temporal_frequency: str) -> list[dict]:
    """Add one explicit temporal-frequency column without changing feature names."""
    freq = (temporal_frequency or "daily").lower()
    out: list[dict] = []
    for row in rows or []:
        item = dict(row)
        item["temporal_frequency"] = freq
        if "time_bin" not in item or item.get("time_bin") in (None, ""):
            if freq == "daily":
                item["time_bin"] = item.get("Date") or item.get("date") or item.get("Study_day") or item.get("study_day") or "daily"
            else:
                item["time_bin"] = freq
        out.append(item)
    return out


def _aggregate_feature_rows_by_temporal(rows: list[dict], temporal_frequency: str) -> list[dict]:
    """Aggregate an already computed feature table to the requested temporal frequency.

    Temporal frequency is stored as its own column; metric/category names are
    left unchanged so exports have stable feature names across daily, hourly,
    monthly, and full-study computations.
    """
    freq = (temporal_frequency or "daily").lower()
    if not rows:
        return []
    if freq == "daily":
        return _ensure_temporal_frequency_column(rows, freq)
    id_like = {"Subject_ID", "username", "Study_day", "study_day", "Date", "date", "temporal_frequency", "time_bin", "sensor", "cohort", "group", "group_label", "app_category", "category"}
    all_cols = list(dict.fromkeys(col for row in rows for col in row.keys()))
    numeric_cols = [col for col in all_cols if col not in id_like and any(_safe_float(row.get(col)) is not None for row in rows)]
    has_category_dimension = any((row.get("app_category") or row.get("category")) for row in rows)
    groups: dict[tuple[object, str, str, str | None], list[dict]] = {}
    for row in rows:
        subject, sort_key, label = _temporal_key(row, freq)
        category_value = None
        if has_category_dimension:
            category_value = str(row.get("app_category") or row.get("category") or "Unknown")
        groups.setdefault((subject, sort_key, label, category_value), []).append(row)
    out: list[dict] = []
    for (subject, _, label, category_value), group_rows in sorted(groups.items(), key=lambda item: (str(item[0][0]), str(item[0][1]), str(item[0][3]))):
        item = {"Subject_ID": subject, "time_bin": label, "temporal_frequency": freq}
        if category_value is not None:
            item["app_category"] = category_value
        if any(row.get("sensor") for row in group_rows):
            item["sensor"] = next((row.get("sensor") for row in group_rows if row.get("sensor")), None)
        if any(row.get("group_label") for row in group_rows):
            item["group_label"] = next((row.get("group_label") for row in group_rows if row.get("group_label")), None)
        for col in numeric_cols:
            vals = [_safe_float(row.get(col)) for row in group_rows]
            vals = [v for v in vals if v is not None]
            if not vals:
                continue
            lc = col.lower()
            if any(token in lc for token in ("record", "count", "sum", "total", "distance", "steps", "foreground", "calorie", "duration", "hours")):
                item[col] = round(sum(vals), 4)
            elif any(token in lc for token in ("min", "lowest")):
                item[col] = round(min(vals), 4)
            elif any(token in lc for token in ("max", "peak", "highest")):
                item[col] = round(max(vals), 4)
            else:
                item[col] = round(sum(vals) / len(vals), 4)
        out.append(item)
    return out

def _select_options_from_pairs(pairs: Iterable[tuple[str, str]], selected: str = "All") -> str:
    selected_s = str(selected)
    return "\n".join(
        f"<option value='{html.escape(str(value))}' {'selected' if str(value) == selected_s else ''}>{html.escape(str(label))}</option>"
        for value, label in pairs
    )


def _select_options_plain(values: Iterable[str], selected: str | None = None, selected_many: Iterable[str] | None = None) -> str:
    selected_set = {str(x) for x in (selected_many or [])}
    if selected is not None:
        selected_set.add(str(selected))
    return "\n".join(
        f"<option value='{html.escape(str(value))}' {'selected' if str(value) in selected_set else ''}>{html.escape(str(value))}</option>"
        for value in values
    )


def _filter_index_for_feature(username: str = "All", sensor_name: str = "All") -> list[IndexedFile]:
    rows = APP_STATE.indexed_rows
    if username != "All":
        rows = [row for row in rows if row.username == username]
    if sensor_name != "All":
        sensor_key = _norm(sensor_name)
        rows = [
            row for row in rows
            if _norm(row.sensor_name) == sensor_key or _norm(row.wearable_sensor) == sensor_key
        ]
    return rows




def _safe_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        if isinstance(value, (list, tuple)):
            value = value[0] if value else None
        text = str(value).strip()
        if text == "":
            return None
        # Accept European-style decimal commas when no thousands separator is present.
        if "," in text and "." not in text and text.count(",") == 1:
            text = text.replace(",", ".")
        out = float(text)
    except (TypeError, ValueError):
        return None
    return out if out == out and out not in (float("inf"), float("-inf")) else None


def _subject_label(row: dict) -> str:
    return str(_row_get_ci(row, ["Subject_ID", "username", "subject", "participant", "participant_id", "ID", "id"]) or "Unknown")


def _date_label(row: dict) -> str:
    iso = row.get("analysis_time_iso")
    if iso:
        return str(iso)[:10]
    day = row.get("study_day")
    return f"study_day_{day}" if day is not None else "unknown_date"


def _is_identifier_or_metadata_column(col: str) -> bool:
    """Return True for ID / metadata columns that must not become sensor features."""
    raw = (col or "").strip()
    key = raw.lower().replace("-", "_").replace(" ", "_")
    if key in {
        "id", "uid", "uuid", "guid", "row_id", "record_id", "file_id", "source_file",
        "file_name", "path", "sensor", "sensor_name", "sensorname", "wearable_sensor",
        "deviceid", "device_id", "username", "subject", "subject_id", "participant",
        "studyid", "study_id", "study_day", "date", "datetime", "time_bin",
        "temporal_frequency", "timestamp", "timestamp_start", "timestamp_end",
        "analysis_time_ms", "analysis_time_iso", "starttime", "endtime", "start_time",
        "end_time", "lasttimeused", "begintimestamp", "endtimestamp",
    }:
        return True
    if key.endswith("_id") or key.endswith("id"):
        return True
    if key.startswith(("id_", "uid_", "uuid_", "guid_")):
        return True
    return False


def _safe_stats(values: list[float]) -> dict[str, float | None]:
    vals = sorted(v for v in values if v is not None and math.isfinite(v))
    n = len(vals)
    if n == 0:
        return {"mean": None, "median": None, "std": None, "min": None, "max": None, "sum": None, "p05": None, "p25": None, "p75": None, "p95": None, "iqr": None, "cv": None}
    mean = sum(vals) / n
    median = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2
    var = sum((v - mean) ** 2 for v in vals) / (n - 1) if n >= 2 else 0.0
    std = math.sqrt(var) if var >= 0 else None
    def q(p: float) -> float:
        if n == 1:
            return vals[0]
        pos = (n - 1) * p
        lo = int(math.floor(pos))
        hi = int(math.ceil(pos))
        if lo == hi:
            return vals[lo]
        return vals[lo] + (vals[hi] - vals[lo]) * (pos - lo)
    p25 = q(0.25)
    p75 = q(0.75)
    cv = (std / abs(mean)) if std is not None and mean not in (0, None) else None
    return {
        "mean": mean, "median": median, "std": std, "min": vals[0], "max": vals[-1], "sum": sum(vals),
        "p05": q(0.05), "p25": p25, "p75": p75, "p95": q(0.95), "iqr": p75 - p25, "cv": cv,
    }


def _format_sampling_frequency_from_interval_sec(interval_sec: float | None) -> str | None:
    """Format the typical sampling interval as one compact frequency label.

    Examples: 60 seconds -> ``1 m``, 600 seconds -> ``10 m``.
    The app uses this single column instead of exposing multiple interval/rate columns.
    """
    if interval_sec is None or not math.isfinite(interval_sec) or interval_sec <= 0:
        return None
    minutes = interval_sec / 60.0
    if abs(minutes - round(minutes)) < 1e-6:
        return f"{int(round(minutes))} m"
    return f"{minutes:.2f}".rstrip("0").rstrip(".") + " m"


def _linear_slope_per_hour(times_ms: list[float], values: list[float]) -> float | None:
    pairs = [(t, v) for t, v in zip(times_ms, values) if t is not None and v is not None and math.isfinite(t) and math.isfinite(v)]
    if len(pairs) < 2:
        return None
    t0 = min(t for t, _ in pairs)
    xs = [(t - t0) / 3600000.0 for t, _ in pairs]
    ys = [v for _, v in pairs]
    xbar = sum(xs) / len(xs)
    ybar = sum(ys) / len(ys)
    denom = sum((x - xbar) ** 2 for x in xs)
    if denom == 0:
        return None
    return sum((x - xbar) * (y - ybar) for x, y in zip(xs, ys)) / denom


def _time_ms_for_row(row: dict) -> float | None:
    dt = _row_datetime(row)
    if dt is not None:
        return dt.timestamp() * 1000.0
    for key in ("analysis_time_ms", "timestamp", "timestamp_start", "startTime", "lastTimeUsed"):
        value = _safe_float(row.get(key))
        if value is not None:
            return value * 1000.0 if abs(value) < 1e11 else value
    return None


def _sensor_numeric_value(row: dict, numeric_cols: list[str]) -> tuple[str | None, float | None]:
    preferred = [
        "value", "heartRate", "heart_rate", "bbi", "hrv", "stress", "spo2", "oxygenSaturation",
        "respiration", "respirationRate", "calories", "steps", "stepCount", "zeroCrossingCount",
        "totalEnergy", "timeAboveThreshold",
    ]
    for col in preferred + numeric_cols:
        if col not in numeric_cols or _is_identifier_or_metadata_column(col):
            continue
        val = _safe_float(row.get(col))
        if val is not None:
            return col, val
    return None, None


def _add_sensor_specific_features(
    item: dict,
    sensor_key: str,
    vals: list[float],
    times_ms: list[float],
    selected_features: Iterable[str] | None = None,
) -> None:
    vals = [v for v in vals if v is not None and math.isfinite(v)]
    if not vals:
        return
    stats = _safe_stats(vals)
    def put(name: str, value: float | None, digits: int = 4):
        if value is not None and math.isfinite(value):
            item[name] = round(value, digits)
    key = sensor_key.upper()
    n = len(vals)
    if key in {"BBI", "ENHANCED_BBI"}:
        diffs = [vals[i] - vals[i - 1] for i in range(1, n)]
        abs_diffs = [abs(d) for d in diffs]
        squared_diffs = [d * d for d in diffs]
        if _has_selected_metric(selected_features, "bbi_hrv"):
            put("bbi_mean_ms", stats["mean"])
            put("bbi_median_ms", stats["median"])
            put("bbi_sdnn_ms", stats["std"])
            if squared_diffs:
                put("bbi_rmssd_ms", math.sqrt(sum(squared_diffs) / len(squared_diffs)))
                put("bbi_pnn20", 100.0 * sum(d > 20 for d in abs_diffs) / len(abs_diffs))
                put("bbi_pnn50", 100.0 * sum(d > 50 for d in abs_diffs) / len(abs_diffs))
        if _has_selected_metric(selected_features, "autonomic_variability"):
            put("bbi_cv", stats["cv"])
        if _has_selected_metric(selected_features, "outliers"):
            item["bbi_out_of_range_count"] = sum(v < 300 or v > 2000 for v in vals)
    elif key == "HRV":
        if _has_selected_metric(selected_features, "hrv_distribution"):
            put("hrv_mean_ms", stats["mean"])
            put("hrv_median_ms", stats["median"])
            put("hrv_sd_ms", stats["std"])
            put("hrv_iqr_ms", stats["iqr"])
            put("hrv_p05_ms", stats["p05"])
            put("hrv_p95_ms", stats["p95"])
        if _has_selected_metric(selected_features, "autonomic_variability"):
            put("hrv_cv", stats["cv"])
    elif key in {"HEART_RATE", "HEARTRATE", "HR"}:
        if _has_selected_metric(selected_features, "resting_hr"):
            put("heart_rate_mean_bpm", stats["mean"])
            put("heart_rate_resting_proxy_bpm", stats["p10"] if "p10" in stats else stats["p05"])
        if _has_selected_metric(selected_features, "peak_hr"):
            put("heart_rate_peak_proxy_bpm", stats["p95"])
        if _has_selected_metric(selected_features, "hr_variability"):
            put("heart_rate_sd_bpm", stats["std"])
            put("heart_rate_range_bpm", (stats["max"] - stats["min"]) if stats["max"] is not None and stats["min"] is not None else None)
        if _has_selected_metric(selected_features, "outliers"):
            item["bradycardia_count_lt50"] = sum(v < 50 for v in vals)
            item["tachycardia_count_gt100"] = sum(v > 100 for v in vals)
            item["heart_rate_out_of_range_count"] = sum(v < 30 or v > 220 for v in vals)
    elif key in {"SPO2", "PULSEOX", "PULSE_OX", "OXYGENSATURATION"}:
        if _has_selected_metric(selected_features, "spo2_summary"):
            put("spo2_mean_pct", stats["mean"])
            put("spo2_min_pct", stats["min"])
            put("spo2_p05_pct", stats["p05"])
        if _has_selected_metric(selected_features, "low_spo2"):
            item["low_spo2_count_lt90"] = sum(v < 90 for v in vals)
            item["low_spo2_pct_lt90"] = round(100.0 * sum(v < 90 for v in vals) / n, 4)
    elif key in {"STRESS", "STRESSLEVEL"}:
        if _has_selected_metric(selected_features, "stress_burden"):
            put("stress_mean_score", stats["mean"])
            put("stress_median_score", stats["median"])
            put("stress_peak_p95_score", stats["p95"])
        if _has_selected_metric(selected_features, "high_stress"):
            item["high_stress_pct_ge50"] = round(100.0 * sum(v >= 50 for v in vals) / n, 4)
        if _has_selected_metric(selected_features, "recovery"):
            item["low_stress_pct_le25"] = round(100.0 * sum(v <= 25 for v in vals) / n, 4)
    elif key in {"RESPIRATION", "RESPIRATORYRATE", "RESPRATE"}:
        if _has_selected_metric(selected_features, "respiration_summary"):
            put("respiration_mean_bpm", stats["mean"])
            put("respiration_median_bpm", stats["median"])
            put("respiration_sd_bpm", stats["std"])
            put("respiration_p95_bpm", stats["p95"])
    elif key in {"CALORIES", "TOTALCALORIES", "ACTIVECALORIES"}:
        if _has_selected_metric(selected_features, "energy_expenditure"):
            put("calories_sum_kcal", stats["sum"])
            put("calories_mean_kcal", stats["mean"])
        if _has_selected_metric(selected_features, "active_calories"):
            item["calories_decrease_count"] = sum(vals[i] < vals[i - 1] for i in range(1, n))
    elif key in {"ACTIGRAPHY_1", "ACTIGRAPHY1"}:
        if _has_selected_metric(selected_features, "activity_energy"):
            put("activity_energy_sum", stats["sum"])
            put("activity_energy_mean", stats["mean"])
        if _has_selected_metric(selected_features, "activity_variability"):
            put("activity_energy_cv", stats["cv"])
            put("activity_energy_p95", stats["p95"])
    elif key in {"ACTIGRAPHY_2", "ACTIGRAPHY2", "ZERO_CROSSING", "ZEROCROSSING"}:
        if _has_selected_metric(selected_features, "zero_crossing"):
            put("zero_crossing_sum", stats["sum"])
            put("zero_crossing_mean", stats["mean"])
        if _has_selected_metric(selected_features, "activity_variability"):
            put("zero_crossing_cv", stats["cv"])
            put("zero_crossing_p95", stats["p95"])
    elif key in {"ACTIGRAPHY_3", "ACTIGRAPHY3"}:
        if _has_selected_metric(selected_features, "threshold_activity"):
            put("time_above_threshold_sum", stats["sum"])
            put("time_above_threshold_mean", stats["mean"])
        if _has_selected_metric(selected_features, "activity_variability"):
            put("time_above_threshold_cv", stats["cv"])
            put("time_above_threshold_p95", stats["p95"])




def _android_activity_label(value: object) -> str:
    """Map Android Activity Recognition numeric codes to labels.

    This follows Android DetectedActivity constants and the existing R app:
    0=IN_VEHICLE, 1=ON_BICYCLE, 2=ON_FOOT, 3=STILL, 4=UNKNOWN,
    5=TILTING, 7=WALKING, 8=RUNNING.
    """
    try:
        code = int(float(str(value).strip()))
    except Exception:
        return "UNKNOWN"
    mapping = {
        0: "IN_VEHICLE",
        1: "ON_BICYCLE",
        2: "ON_FOOT",
        3: "STILL",
        4: "UNKNOWN",
        5: "TILTING",
        7: "WALKING",
        8: "RUNNING",
    }
    return mapping.get(code, f"TYPE_{code}")


def _activity_value_from_row(row: dict) -> object:
    return _row_get_ci(row, ["activityType", "activity_type", "type", "detectedActivity", "activity", "activity_code", "ActivityType"])


def _activity_confidence_from_row(row: dict, max_confidence: float | None = None) -> float | None:
    raw = _row_get_float_ci(row, ["confidence", "activityConfidence", "confidenceScore", "probability", "Confidence"])
    if raw is None:
        return None
    # Android confidence is commonly 0-100, but some exports store 0-2 or 0-3.
    if max_confidence is not None and max_confidence <= 2:
        return (raw / 2.0) * 100.0
    if max_confidence is not None and max_confidence <= 3:
        return (raw / 3.0) * 100.0
    if raw <= 1:
        return raw * 100.0
    return raw


def _activity_features(
    rows: list[dict],
    temporal_frequency: str = "daily",
    selected_features: Iterable[str] | None = None,
) -> list[dict]:
    """Compute Android activity-recognition features.

    Features are based on the R workflow: assign Android activity labels,
    estimate label durations from consecutive timestamps, summarize transitions
    and recognition confidence, and output wide label-specific features for the
    requested temporal frequency.
    """
    if not rows:
        return []
    raw_conf = []
    for row in rows:
        val = None
        for key in ("confidence", "activityConfidence", "confidenceScore", "probability"):
            val = _safe_float(row.get(key))
            if val is not None:
                raw_conf.append(val)
                break
    max_conf = max(raw_conf) if raw_conf else None

    prepared = []
    for row in rows:
        t = _time_ms_for_row(row)
        if t is None:
            continue
        label = _android_activity_label(_activity_value_from_row(row))
        prepared.append({
            "row": row,
            "subject": _subject_label(row),
            "time_ms": t,
            "label": label,
            "confidence_score": _activity_confidence_from_row(row, max_conf),
        })
    if not prepared:
        return []

    prepared.sort(key=lambda x: (str(x["subject"]), float(x["time_ms"])))
    # Estimate duration until the next record within subject. Cap very long gaps
    # so isolated records do not dominate time-use estimates.
    by_subject: dict[str, list[dict]] = {}
    for item in prepared:
        by_subject.setdefault(str(item["subject"]), []).append(item)
    all_items = []
    for subject, items in by_subject.items():
        for i, item in enumerate(items):
            if i + 1 < len(items):
                delta_sec = (items[i + 1]["time_ms"] - item["time_ms"]) / 1000.0
                item["segment_sec"] = max(0.0, min(delta_sec, 300.0)) if math.isfinite(delta_sec) else 0.0
                item["transition_flag"] = 1 if items[i + 1]["label"] != item["label"] else 0
            else:
                item["segment_sec"] = 0.0
                item["transition_flag"] = 0
            all_items.append(item)

    groups: dict[tuple[object, str, str], list[dict]] = {}
    for item in all_items:
        groups.setdefault(_temporal_key(item["row"], temporal_frequency), []).append(item)

    labels_order = ["IN_VEHICLE", "ON_BICYCLE", "ON_FOOT", "WALKING", "RUNNING", "STILL", "TILTING", "UNKNOWN"]
    observed = sorted({item["label"] for item in all_items if item["label"] not in labels_order})
    labels_order = labels_order + observed

    out: list[dict] = []
    freq = (temporal_frequency or "daily").lower()
    for (subject, sort_label, time_label), items in sorted(groups.items(), key=lambda x: (str(x[0][0]), str(x[0][1]))):
        total_duration_min = sum(float(x.get("segment_sec") or 0.0) for x in items) / 60.0
        conf_vals = [x["confidence_score"] for x in items if x.get("confidence_score") is not None]
        label_counts: dict[str, int] = {}
        label_minutes: dict[str, float] = {}
        for x in items:
            label = x["label"]
            label_counts[label] = label_counts.get(label, 0) + 1
            label_minutes[label] = label_minutes.get(label, 0.0) + (float(x.get("segment_sec") or 0.0) / 60.0)
        dominant = max(label_counts.items(), key=lambda kv: (kv[1], label_minutes.get(kv[0], 0.0)))[0] if label_counts else "UNKNOWN"
        item = {
            "Subject_ID": subject,
            "time_bin": time_label,
            "temporal_frequency": freq,
            "dominant_activity": dominant,
        }
        if freq == "daily":
            item["Date"] = time_label
        if _has_selected_metric(selected_features, "activity_records"):
            item["records"] = len(items)
            item["unique_activity_labels"] = len(label_counts)
        if _has_selected_metric(selected_features, "activity_time"):
            item["estimated_duration_min"] = round(total_duration_min, 4)
        if _has_selected_metric(selected_features, "activity_transitions"):
            item["transitions"] = int(sum(x.get("transition_flag", 0) for x in items))
        if _has_selected_metric(selected_features, "activity_confidence") and conf_vals:
            st = _safe_stats(conf_vals)
            item["mean_confidence_score"] = round(st["mean"], 4) if st["mean"] is not None else None
            item["median_confidence_score"] = round(st["median"], 4) if st["median"] is not None else None
            item["low_confidence_rows_lt50"] = sum(v < 50 for v in conf_vals)
        for label in labels_order:
            minutes = label_minutes.get(label, 0.0)
            count = label_counts.get(label, 0)
            clean = label.lower()
            if _has_selected_metric(selected_features, "activity_time"):
                item[f"time_{clean}_min"] = round(minutes, 4)
            if _has_selected_metric(selected_features, "activity_records"):
                item[f"records_{clean}"] = count
            if _has_selected_metric(selected_features, "activity_share"):
                item[f"pct_{clean}"] = round((100.0 * minutes / total_duration_min), 4) if total_duration_min > 0 else 0.0
        out.append(item)
    return out

def _generic_sensor_daily_features(
    rows: list[dict],
    sensor_name: str = "All",
    temporal_frequency: str = "daily",
    selected_features: Iterable[str] | None = None,
) -> list[dict]:
    if not rows:
        return []
    preferred = [
        "value", "value_text", "heartRate", "heart_rate", "bbi", "hrv", "stress",
        "spo2", "oxygenSaturation", "respiration", "respirationRate", "calories",
        "steps", "stepCount", "zeroCrossingCount", "totalEnergy", "timeAboveThreshold",
    ]
    numeric_cols: list[str] = []
    all_cols = sorted({key for row in rows for key in row.keys()})
    for col in preferred + all_cols:
        if col in numeric_cols or _is_identifier_or_metadata_column(col):
            continue
        vals = [_safe_float(row.get(col)) for row in rows[:500]]
        if any(v is not None for v in vals):
            numeric_cols.append(col)
    if not numeric_cols:
        return []

    groups: dict[tuple[object, str, str], dict[str, object]] = {}
    temporal_frequency = (temporal_frequency or "daily").lower()
    for row in rows:
        key = _temporal_key(row, temporal_frequency)
        g = groups.setdefault(key, {"cols": {col: [] for col in numeric_cols}, "times": [], "sensor_vals": [], "row_count": 0})
        g["row_count"] += 1
        t = _time_ms_for_row(row)
        if t is not None:
            g["times"].append(t)
        for col in numeric_cols:
            value = _safe_float(row.get(col))
            if value is not None:
                g["cols"][col].append(value)
        _, sensor_value = _sensor_numeric_value(row, numeric_cols)
        if sensor_value is not None:
            g["sensor_vals"].append(sensor_value)

    sensor_prefix = _norm(sensor_name).lower() if sensor_name and sensor_name != "All" else "sensor"
    out: list[dict] = []
    for (subject, sort_label, time_label), group in sorted(groups.items(), key=lambda item: (str(item[0][0]), str(item[0][1]))):
        times = sorted(group.get("times", []))
        item = {
            "Subject_ID": subject,
            "time_bin": time_label,
            "temporal_frequency": temporal_frequency,
            "sensor": sensor_name,
        }
        if temporal_frequency == "daily":
            item["Date"] = time_label
        if _has_selected_metric(selected_features, "records"):
            item["records"] = int(group.get("row_count", 0))
        if times:
            if _has_selected_metric(selected_features, "temporal_coverage"):
                item["temporal_coverage_hours"] = round((max(times) - min(times)) / 3600000.0, 4) if len(times) > 1 else 0
                item["active_day_hours"] = len({datetime.fromtimestamp(t / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H") for t in times})
            if len(times) > 1:
                intervals = [(times[i] - times[i - 1]) / 1000.0 for i in range(1, len(times)) if times[i] > times[i - 1]]
                if intervals:
                    st = _safe_stats(intervals)
                    med = st["median"] or 0
                    if med and med > 0 and _has_selected_metric(selected_features, "data_frequency"):
                        frequency_label = _format_sampling_frequency_from_interval_sec(med)
                        if frequency_label:
                            item["computed_frequency"] = frequency_label
                    if _has_selected_metric(selected_features, "sampling"):
                        item["max_gap_sec"] = round(max(intervals), 4)
                        item["long_gap_count"] = sum(x > max(300, 3 * med) for x in intervals) if med else 0
        primary_vals = group.get("sensor_vals", [])
        _add_sensor_specific_features(item, _norm(sensor_name), primary_vals, times, selected_features=selected_features)
        for col, vals in group["cols"].items():
            if not vals:
                continue
            stats = _safe_stats(vals)
            safe_col = _norm(col).lower()
            base = f"{sensor_prefix}_{safe_col}"
            metric_to_selected = {
                "mean": "mean",
                "median": "median",
                "std": "std",
                "min": "min",
                "max": "max",
                "sum": "sum",
                "iqr": "iqr",
                "cv": "cv",
                "p05": "percentiles",
                "p25": "percentiles",
                "p75": "percentiles",
                "p95": "percentiles",
            }
            for metric in ("mean", "median", "std", "min", "max", "sum", "p05", "p25", "p75", "p95", "iqr", "cv"):
                if not _has_selected_metric(selected_features, metric_to_selected.get(metric, metric)):
                    continue
                val = stats.get(metric)
                if val is not None and math.isfinite(val):
                    item[f"{base}_{metric}"] = round(val, 4)
            if _has_selected_metric(selected_features, "trend"):
                col_times = []
                col_values = []
                for row in rows:
                    if _temporal_key(row, temporal_frequency) == (subject, sort_label, time_label):
                        val = _safe_float(row.get(col))
                        t = _time_ms_for_row(row)
                        if val is not None and t is not None:
                            col_times.append(t)
                            col_values.append(val)
                slope = _linear_slope_per_hour(col_times, col_values)
                if slope is not None and math.isfinite(slope):
                    item[f"{base}_trend_slope_per_hour"] = round(slope, 6)
        out.append(item)
    return out


def _standardize_generated_feature_names(rows: list[dict]) -> list[dict]:
    """Remove temporal words such as daily from generated feature columns.

    Temporal resolution is represented by temporal_frequency/time_bin, so
    metric columns should stay stable across daily, hourly, monthly, and
    full-study calculations. Internal review summaries may still use their
    original names, but generated/exported feature tables do not.
    """
    rename_map = {
        "daily_steps": "steps",
        "daily_distance": "distance",
        "daily_distance_km": "distance_km",
        "mean_daily_steps": "mean_steps",
        "median_daily_steps": "median_steps",
        "peak_daily_steps": "peak_steps",
        "mean_daily_distance_km": "mean_distance_km",
        "max_daily_distance_km": "max_distance_km",
        "mean_daily_foreground_hours": "mean_foreground_hours",
    }
    out: list[dict] = []
    for row in rows or []:
        item: dict = {}
        for key, value in row.items():
            new_key = rename_map.get(key, key)
            if new_key == key and isinstance(key, str):
                if key.startswith("category_") and key.endswith("_hours"):
                    label = key[len("category_"):-len("_hours")].replace("_", " ").strip()
                    new_key = "".join(part[:1].upper() + part[1:] for part in label.split()) or key
                else:
                    new_key = key.replace("daily_", "").replace("_daily_", "_")
            # Avoid accidental overwrite; preserve first populated value.
            if new_key in item and item[new_key] not in (None, ""):
                continue
            item[new_key] = value
        out.append(item)
    return out



def _normalize_subject_key(value: object) -> str:
    text = str(value or "").strip()
    # JTrack device suffixes sometimes appear as Subject_001_1; group labels
    # are usually stored at the participant level, so remove one trailing
    # numeric device/session suffix for matching while keeping the original ID.
    import re
    text = re.sub(r"^(.+_[0-9]{3,})_[0-9]+$", r"\1", text)
    return "".join(ch for ch in text.upper() if ch.isalnum())


def _group_label_map() -> dict[str, dict]:
    mapping: dict[str, dict] = {}
    for row in APP_STATE.group_label_rows or []:
        subject = None
        for col in ("Subject_ID", "subject", "subject_id", "username", "participant", "participant_id", "ID", "id"):
            if row.get(col) not in (None, ""):
                subject = row.get(col)
                break
        if subject in (None, ""):
            continue
        group_value = None
        for col in ("group_label", "cohort", "group", "condition", "arm", "label"):
            if row.get(col) not in (None, ""):
                group_value = row.get(col)
                break
        if group_value in (None, ""):
            continue
        key = _normalize_subject_key(subject)
        if not key:
            continue
        extra = dict(row)
        extra["group_label"] = str(group_value)
        mapping[key] = extra
    return mapping


def _attach_group_labels(rows: list[dict]) -> list[dict]:
    if not rows:
        return rows
    label_map = _group_label_map()
    if not label_map:
        return rows
    out: list[dict] = []
    for row in rows:
        item = dict(row)
        subject = None
        for col in ("Subject_ID", "username", "subject", "participant"):
            if item.get(col) not in (None, ""):
                subject = item.get(col)
                break
        label = label_map.get(_normalize_subject_key(subject)) if subject is not None else None
        if label:
            # Preserve existing labels if the feature table already contains them.
            if item.get("group_label") in (None, ""):
                item["group_label"] = label.get("group_label")
            if item.get("cohort") in (None, "") and label.get("cohort") not in (None, ""):
                item["cohort"] = label.get("cohort")
            # Keep selected metadata columns for reports/group analysis.
            for col in ("condition", "arm", "diagnosis", "sex", "gender", "age"):
                if item.get(col) in (None, "") and label.get(col) not in (None, ""):
                    item[col] = label.get(col)
        out.append(item)
    return out


def _first_existing_col_in_rows(rows: list[dict], candidates: Iterable[str]) -> str | None:
    if not rows:
        return None
    names: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row.keys():
            key_s = str(key)
            if key_s not in seen:
                seen.add(key_s)
                names.append(key_s)
    lower_map = {name.lower(): name for name in names}
    for cand in candidates:
        hit = lower_map.get(str(cand).lower())
        if hit is not None:
            return hit
    key_map = {_label_key(name): name for name in names}
    for cand in candidates:
        hit = key_map.get(_label_key(cand))
        if hit is not None:
            return hit
    return None


def _label_key(value: object) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "", str(value or "")).upper()


def _read_delimited_rows(path: Path, delimiter: str) -> list[dict]:
    content = path.read_text(encoding="utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(content), delimiter=delimiter)
    rows: list[dict] = []
    for row in reader:
        clean = {str(k).strip(): ("" if v is None else str(v).strip()) for k, v in row.items() if k is not None}
        if any(v for v in clean.values()):
            rows.append(clean)
    return rows


def _read_cohort_file_like_r(path_text: str) -> list[dict]:
    """Read cohort/group metadata like the R app.

    Tries semicolon CSV first, then comma CSV, then tab-separated text. Adds
    cohort_subject and cohort_subject_key and keeps one row per normalized
    subject key.
    """
    path = Path(path_text).expanduser()
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"Group/cohort file not found: {path}")
    rows: list[dict] = []
    for delim in (";", ",", "\t"):
        try:
            candidate = _read_delimited_rows(path, delim)
        except Exception:
            candidate = []
        if candidate and len(candidate[0].keys()) >= 2:
            rows = candidate
            break
    if not rows:
        return []

    subject_col = _first_existing_col_in_rows(rows, (
        "subject_name", "username", "subject", "subject_id",
        "participant", "participant_id", "ID", "id"
    ))
    if not subject_col:
        return []

    out: list[dict] = []
    seen_keys: set[str] = set()
    for row in rows:
        subject = row.get(subject_col, "")
        subject_key = _normalize_subject_key(subject)
        if not subject_key or subject_key in seen_keys:
            continue
        item = dict(row)
        item["cohort_subject"] = str(subject)
        item["cohort_subject_key"] = subject_key
        out.append(item)
        seen_keys.add(subject_key)
    return out


def _resolve_group_column(rows: list[dict], requested_col: str | None) -> str | None:
    requested_col = str(requested_col or "").strip()
    if rows and requested_col:
        names = list(rows[0].keys())
        for name in names:
            if name.lower() == requested_col.lower():
                return name
        requested_key = _label_key(requested_col)
        for name in names:
            if _label_key(name) == requested_key:
                return name
        likely = _first_existing_col_in_rows(rows, ("Kohorte", "kohorte", "cohort", "group", "group_label", "label", "arm", "condition"))
        if likely and requested_key in {"COHORT", "GROUP", "GROUPLABEL", "LABEL", "ARM", "CONDITION", "KOHORTE"}:
            return likely
    return None


def _default_group_column(rows: list[dict]) -> str | None:
    return _first_existing_col_in_rows(rows, ("Kohorte", "kohorte", "cohort", "group", "group_label", "label", "arm", "condition"))


def _default_comorbidity_columns(rows: list[dict]) -> list[str]:
    cols: list[str] = []
    for candidates in (
        ("ND1", "nd1", "comorbidity_1", "comorbidity1"),
        ("ND2", "nd2", "comorbidity_2", "comorbidity2"),
        ("komorbiditaet", "komorbidität", "comorbidity", "comorbidities"),
    ):
        col = _first_existing_col_in_rows(rows, candidates)
        if col and col not in cols:
            cols.append(col)
    return cols


def _is_nullish_group_value(value: object) -> bool:
    text = str(value or "").strip().lower()
    return text in {"", "0", "none", "na", "n/a", "no", "null", "nan"}


def _build_group_labels_from_source(rows: list[dict], requested_col: str | None, comorbidity_cols: Iterable[str] | None = None) -> list[dict]:
    if not rows:
        return []
    group_col = _resolve_group_column(rows, requested_col) or _default_group_column(rows)
    if not group_col:
        return []
    subject_col = _first_existing_col_in_rows(rows, (
        "cohort_subject", "subject_name", "username", "subject",
        "subject_id", "participant", "participant_id", "ID", "id"
    ))
    if not subject_col:
        return []
    names = set(rows[0].keys())
    requested = [str(c) for c in (comorbidity_cols or []) if str(c) in names]
    if not requested:
        requested = _default_comorbidity_columns(rows)

    out: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        subject_key = row.get("cohort_subject_key") or _normalize_subject_key(row.get(subject_col))
        group_value = row.get(group_col, "")
        if not subject_key or _is_nullish_group_value(group_value) or subject_key in seen:
            continue
        comorb_vals: list[str] = []
        for col in requested:
            val = str(row.get(col, "")).strip()
            if not _is_nullish_group_value(val) and val not in comorb_vals:
                comorb_vals.append(val)
        item = dict(row)
        item["username"] = str(row.get(subject_col, ""))
        item["subject_key"] = str(subject_key)
        item["group_label"] = str(group_value).strip()
        item["comorbidity_1"] = str(row.get(requested[0], "")).strip() if len(requested) >= 1 else ""
        item["comorbidity_2"] = str(row.get(requested[1], "")).strip() if len(requested) >= 2 else ""
        item["comorbidity_other"] = "; ".join(comorb_vals[2:]) if len(comorb_vals) > 2 else ""
        item["comorbidity_summary"] = "; ".join(comorb_vals)
        item["comorbidity_any"] = "TRUE" if comorb_vals else "FALSE"
        out.append(item)
        seen.add(str(subject_key))
    return out


def _load_group_cohort_file(path_text: str) -> tuple[list[dict], list[dict], str | None, list[str]]:
    source = _read_cohort_file_like_r(path_text)
    group_col = _default_group_column(source)
    comorbidity_cols = _default_comorbidity_columns(source)
    labels = _build_group_labels_from_source(source, group_col, comorbidity_cols)
    return source, labels, group_col, comorbidity_cols


def _read_group_label_file(path_text: str) -> list[dict]:
    """Backward-compatible wrapper returning built group-label rows."""
    source, labels, _, _ = _load_group_cohort_file(path_text)
    return labels


def _group_label_folder_files() -> list[tuple[str, str]]:
    """Return CSV/TSV-like label files available in the selected cohort folder."""
    folder_text = (APP_STATE.group_label_folder or "").strip()
    if not folder_text:
        return []
    folder = Path(folder_text).expanduser()
    if not folder.exists() or not folder.is_dir():
        return []
    allowed = {".csv", ".tsv", ".txt"}
    files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in allowed]
    files = sorted(files, key=lambda x: x.name.casefold())
    return [(str(p), p.name) for p in files]

def _generated_feature_rows(feature_key: str | None = None, use_qc: bool = False) -> list[dict]:
    key = feature_key or APP_STATE.generated_feature_key
    rows: list[dict] = []
    if use_qc and APP_STATE.feature_qc_rows and (key == APP_STATE.generated_feature_key or key in (None, "All")):
        rows = APP_STATE.feature_qc_rows
    elif key == "application_usage_daily":
        rows = APP_STATE.app_usage_daily
    elif key == "application_usage_category_daily":
        rows = APP_STATE.app_usage_category_daily_wide
    elif key == "location_daily":
        rows = APP_STATE.location_daily
    elif key == "pedometer_daily":
        rows = APP_STATE.pedometer_daily
    elif key == "sensor_daily_summary":
        rows = APP_STATE.generic_sensor_daily
    elif key == "activity_features":
        rows = APP_STATE.activity_features
    elif key == "custom_feature":
        rows = APP_STATE.custom_feature_rows
    elif APP_STATE.feature_qc_rows:
        rows = APP_STATE.feature_qc_rows
    else:
        for candidate in (APP_STATE.app_usage_daily, APP_STATE.app_usage_category_daily_wide, APP_STATE.location_daily, APP_STATE.pedometer_daily, APP_STATE.generic_sensor_daily, APP_STATE.activity_features, APP_STATE.custom_feature_rows):
            if candidate:
                rows = candidate
                break
    return _attach_group_labels(list(rows))


def _all_generated_feature_keys() -> list[str]:
    keys = []
    if APP_STATE.loaded_filtered_rows or APP_STATE.loaded_rows:
        keys.append("raw_sensor_data")
    for key in FEATURE_LABELS:
        if key == "raw_sensor_data":
            continue
        if _generated_feature_rows(key):
            keys.append(key)
    return keys


def _table_preview(rows: list[dict], limit: int = 20) -> str:
    """Render a scrollable, paged table preview.

    `limit` is used as the default page size. To keep the page responsive,
    very large previews are capped but still paginated client-side.
    """
    if not rows:
        return "<p>No rows available.</p>"
    page_size = max(1, int(limit or 20))
    max_preview_rows = 5000
    preview_rows = rows[:max_preview_rows]
    columns: list[str] = []
    seen: set[str] = set()
    for row in preview_rows:
        for col in row.keys():
            if col not in seen:
                seen.add(col)
                columns.append(col)
    header = "".join(f"<th>{html.escape(str(col))}</th>" for col in columns)
    body = "\n".join(
        "<tr>" + "".join(f"<td>{html.escape(str(row.get(col, '')))}</td>" for col in columns) + "</tr>"
        for row in preview_rows
    )
    if len(rows) > max_preview_rows:
        note = f"<p class='muted'>Paginated preview of first {max_preview_rows} of {len(rows)} rows.</p>"
    else:
        note = f"<p class='muted'>Paginated preview: {len(rows)} rows.</p>"
    return f"""
    {note}
    <div class="table-scroll" data-page-size="{page_size}">
      <table data-page-size="{page_size}">
        <thead><tr>{header}</tr></thead>
        <tbody>{body}</tbody>
      </table>
    </div>

    """
def _numeric_columns(rows: list[dict]) -> list[str]:
    cols: list[str] = []
    if not rows:
        return cols
    for col in rows[0].keys():
        for row in rows[:100]:
            try:
                value = row.get(col)
                if value not in (None, ""):
                    float(value)
                    cols.append(col)
                    break
            except (TypeError, ValueError):
                continue
    return cols


def _subject_values_from_rows(rows: list[dict]) -> list[str]:
    values = set()
    for row in rows:
        for col in ("Subject_ID", "username", "subject", "participant"):
            value = row.get(col)
            if value not in (None, ""):
                values.add(str(value))
                break
    return sorted(values)


def _cohort_values_from_rows(rows: list[dict]) -> list[str]:
    values = set()
    for row in rows:
        for col in ("cohort", "group", "group_label", "condition"):
            value = row.get(col)
            if value not in (None, ""):
                values.add(str(value))
                break
    return sorted(values)


def _normalize_group_filter(value: object) -> list[str]:
    """Normalize group/cohort filter selections from GET forms.

    The UI can submit one or several cohort values. Returning an empty list
    means no restriction (All groups).
    """
    if value is None:
        return []
    if isinstance(value, str):
        values = [value]
    else:
        try:
            values = list(value)
        except TypeError:
            values = [value]
    out = [str(v).strip() for v in values if str(v).strip()]
    if not out or "All" in out:
        return []
    return sorted(set(out))


def _group_filter_label(value: object) -> str:
    selected = _normalize_group_filter(value)
    return "All" if not selected else ", ".join(selected)


def _row_group_values(row: dict) -> set[str]:
    return {str(row.get(c, "")) for c in ("cohort", "group", "group_label", "condition") if row.get(c) not in (None, "")}


def _filtered_feature_rows(feature_key: str | None = None, subject: str = "All", feature_column: str = "All", cohort: object = "All") -> list[dict]:
    rows = list(_generated_feature_rows(feature_key, use_qc=True))
    if subject != "All":
        rows = [row for row in rows if subject in {str(row.get(c, "")) for c in ("Subject_ID", "username", "subject", "participant")}]
    selected_groups = _normalize_group_filter(cohort)
    if selected_groups:
        selected_set = set(selected_groups)
        rows = [row for row in rows if _row_group_values(row) & selected_set]
    if feature_column != "All":
        keep = ["Subject_ID", "username", "Study_day", "Date", "time_bin", "temporal_frequency", "cohort", "group", "group_label", feature_column]
        rows = [{k: row.get(k) for k in keep if k in row} for row in rows]
    return rows


def _transform_feature_rows(
    rows: list[dict],
    feature_column: str,
    transform: str = "none",
) -> tuple[list[dict], str, dict[str, object]]:
    transform = str(transform or "none").strip().lower()
    if not rows or feature_column in {"", "All"} or transform == "none":
        return rows, feature_column, {"applied": False, "label": "No transformation", "valid_rows": 0, "skipped_rows": 0}

    vals: list[float] = []
    for row in rows:
        value = _safe_float(row.get(feature_column))
        if value is not None and math.isfinite(value):
            vals.append(value)
    if not vals:
        return rows, feature_column, {"applied": False, "label": "No transformation", "valid_rows": 0, "skipped_rows": len(rows)}

    mean_val = sum(vals) / len(vals)
    sd_val = (sum((v - mean_val) ** 2 for v in vals) / (len(vals) - 1)) ** 0.5 if len(vals) > 1 else 0.0

    transform_specs = {
        "log1p": ("log(x+1)", f"{feature_column}__log1p"),
        "log10p": ("log10(x+1)", f"{feature_column}__log10p"),
        "sqrt": ("sqrt(x)", f"{feature_column}__sqrt"),
        "zscore": ("z-score", f"{feature_column}__zscore"),
        "center": ("mean-centered", f"{feature_column}__centered"),
    }
    label, new_column = transform_specs.get(transform, ("No transformation", feature_column))
    if transform not in transform_specs:
        return rows, feature_column, {"applied": False, "label": "No transformation", "valid_rows": 0, "skipped_rows": 0}

    out_rows: list[dict] = []
    valid_rows = 0
    skipped_rows = 0
    for row in rows:
        out = dict(row)
        value = _safe_float(row.get(feature_column))
        transformed: float | None = None
        if value is not None and math.isfinite(value):
            if transform == "log1p":
                transformed = math.log1p(value) if value > -1 else None
            elif transform == "log10p":
                transformed = math.log10(value + 1.0) if value > -1 else None
            elif transform == "sqrt":
                transformed = math.sqrt(value) if value >= 0 else None
            elif transform == "zscore":
                transformed = 0.0 if sd_val == 0 else (value - mean_val) / sd_val
            elif transform == "center":
                transformed = value - mean_val
        if transformed is None or not math.isfinite(transformed):
            out[new_column] = ""
            skipped_rows += 1
        else:
            out[new_column] = round(transformed, 6)
            valid_rows += 1
        out_rows.append(out)

    return out_rows, new_column, {
        "applied": True,
        "label": label,
        "valid_rows": valid_rows,
        "skipped_rows": skipped_rows,
        "source_column": feature_column,
        "transformed_column": new_column,
    }


def _analysis_rows_with_transform(
    rows: list[dict],
    feature_column: str = "All",
    feature_transform: str = "none",
) -> tuple[list[dict], str, dict[str, object]]:
    if feature_column in {"", "All", None}:
        return rows, str(feature_column or "All"), {"applied": False, "label": "No transformation", "valid_rows": 0, "skipped_rows": 0}
    return _transform_feature_rows(rows, feature_column, feature_transform)




def _raw_base_rows() -> list[dict]:
    """Return currently loaded raw rows after study-level QC, with group labels attached."""
    return _attach_group_labels(list(APP_STATE.loaded_filtered_rows or APP_STATE.loaded_rows))


def _row_sensor_label(row: dict) -> str:
    # Prefer wearable stream labels, then generic sensor metadata, then folder/file hints.
    wearable = _display_sensor_choice(str(_row_get_ci(row, ["wearable_sensor", "wearable_file"]) or ""))
    sensor = _display_sensor_choice(str(_row_get_ci(row, ["sensorname", "sensor_name", "sensor", "sensor_file"]) or ""))
    folder = _display_sensor_choice(str(_row_get_ci(row, ["sensor_folder"]) or ""))
    if wearable or sensor or folder:
        return wearable or sensor or folder or "Unknown"
    blob_key = _label_key(_row_text_blob(row))
    aliases = [
        ("ENHANCEDBBI", "ENHANCED_BBI"),
        ("HEARTRATE", "HEART_RATE"),
        ("LOCKUNLOCK", "LOCKUNLOCK"),
        ("APPLICATIONUSAGE", "APPLICATION_USAGE"),
        ("ACTIGRAPHY1", "ACTIGRAPHY_1"),
        ("ACTIGRAPHY2", "ACTIGRAPHY_2"),
        ("ACTIGRAPHY3", "ACTIGRAPHY_3"),
        ("ZEROCROSSING", "ZERO_CROSSING"),
        ("LOCATION", "LOCATION"),
        ("GEOLOCATION", "GEOLOCATION"),
        ("ACTIVITY", "ACTIVITY"),
        ("CALORIES", "CALORIES"),
        ("STRESS", "STRESS"),
        ("SPO2", "SPO2"),
        ("PULSEOX", "SPO2"),
        ("RESPIR", "RESPIRATION"),
        ("HRV", "HRV"),
        ("BBI", "BBI"),
        ("GPS", "GPS"),
    ]
    for token, label in aliases:
        if token in blob_key:
            return label
    return "Unknown"


def _raw_sensor_values(rows: list[dict] | None = None) -> list[str]:
    values = {_row_sensor_label(row) for row in (rows if rows is not None else _raw_base_rows())}
    return sorted((v for v in values if v and v != "Unknown"), key=lambda x: x.upper())


def _raw_rows_filtered(subject: str = "All", sensor_name: str = "All", cohort: object = "All") -> list[dict]:
    rows = _raw_base_rows()
    if subject != "All":
        rows = [row for row in rows if subject in {str(row.get(c, "")) for c in ("Subject_ID", "username", "subject", "participant")}]
    selected_groups = _normalize_group_filter(cohort)
    if selected_groups:
        selected_set = set(selected_groups)
        rows = [row for row in rows if _row_group_values(row) & selected_set]
    sensor_key = _norm(sensor_name)
    if sensor_key not in {"", "ALL"}:
        filtered = []
        for row in rows:
            labels = {
                _norm(row.get("sensorname") or row.get("sensor_name") or row.get("sensor") or ""),
                _norm(row.get("wearable_sensor") or ""),
                _norm(_row_sensor_label(row)),
            }
            labels = {x for x in labels if x}
            if sensor_key in labels or any(sensor_key in x or x in sensor_key for x in labels):
                filtered.append(row)
        rows = filtered
    return rows


def _raw_numeric_columns(rows: list[dict]) -> list[str]:
    excluded = {"analysis_time_ms", "timestamp", "timestamp_start", "timestamp_end", "beginTimeStamp", "endTimeStamp", "lastTimeUsed", "startTime", "endTime", "study_day"}
    cols = []
    for col in _numeric_columns(rows):
        if col in excluded or _is_identifier_or_metadata_column(col):
            continue
        cols.append(col)
    return cols


def _raw_sensor_points(rows: list[dict], feature_column: str = "All") -> list[dict]:
    preferred = [
        "value", "heartRate", "heart_rate", "bbi", "hrv", "stress", "spo2",
        "oxygenSaturation", "respiration", "respirationRate", "calories", "steps",
        "stepCount", "zeroCrossingCount", "totalEnergy", "timeAboveThreshold",
        "confidence", "activityType", "category_hours", "totalTimeinForeground",
    ]
    out: list[dict] = []
    for row in rows:
        dt = _row_datetime(row)
        if dt is None:
            continue
        value_col = None
        val = None
        if feature_column not in {"", "All", None}:
            val = _safe_float(row.get(feature_column))
            value_col = feature_column if val is not None else None
        else:
            for col in preferred + sorted(row.keys()):
                if _is_identifier_or_metadata_column(col):
                    continue
                if col in {"analysis_time_ms", "timestamp", "timestamp_start", "timestamp_end", "beginTimeStamp", "endTimeStamp", "lastTimeUsed", "startTime", "endTime", "study_day"}:
                    continue
                val = _safe_float(row.get(col))
                if val is not None:
                    value_col = col
                    break
        if val is None:
            continue
        out.append({
            "Subject_ID": _subject_label(row),
            "time": dt.isoformat(),
            "value": val,
            "value_col": value_col or "value",
            "sensor": _row_sensor_label(row),
            "group": row.get("group_label") or row.get("cohort") or row.get("group") or "",
            "source_file": row.get("source_file") or "",
        })
    return sorted(out, key=lambda item: (item["Subject_ID"], item["sensor"], item["time"]))



def _plotly_export_config(filename: str, height: int = 900, width: int = 1500) -> dict:
    return {
        "displayModeBar": True,
        "displaylogo": False,
        "responsive": True,
        "toImageButtonOptions": {
            "format": "png",
            "filename": filename,
            "height": height,
            "width": width,
            "scale": 2,
        },
    }


def _apply_scientific_layout(fig: go.Figure, title: str, x_title: str = "Time", y_title: str = "Value", height: int = 640) -> go.Figure:
    fig.update_layout(
        title={"text": title, "x": 0.02, "xanchor": "left"},
        xaxis_title=x_title,
        yaxis_title=y_title,
        template="plotly_white",
        height=height,
        hovermode="closest",
        legend_title="Trace",
        margin=dict(l=72, r=32, t=86, b=72),
        font=dict(size=13),
        paper_bgcolor="white",
        plot_bgcolor="white",
    )
    fig.update_xaxes(showgrid=True, gridcolor="rgba(31, 41, 55, 0.10)", zeroline=False, showline=True, linewidth=1, linecolor="rgba(31,41,55,0.40)")
    fig.update_yaxes(showgrid=True, gridcolor="rgba(31, 41, 55, 0.10)", zeroline=False, showline=True, linewidth=1, linecolor="rgba(31,41,55,0.40)")
    return fig


def _raw_series_groups(points: list[dict], feature_column: str = "All") -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for point in points:
        trace_name = f"{point['Subject_ID']} — {point['sensor']}"
        if feature_column in {"", "All", None}:
            trace_name = f"{trace_name} — {point['value_col']}"
        grouped.setdefault(trace_name, []).append(point)
    return grouped


def _raw_time_series_plot_div(rows: list[dict], feature_column: str = "All", plot_type: str = "raw_line") -> str:
    points = _raw_sensor_points(rows, feature_column)
    if not points:
        return "<p>No raw sensor values with valid timestamps are available for the current filters.</p>"
    fig = go.Figure()
    grouped = _raw_series_groups(points, feature_column)
    for name, pts in sorted(grouped.items()):
        pts = sorted(pts, key=lambda p: p["time"])
        x_vals = [p["time"] for p in pts]
        y_vals = [p["value"] for p in pts]
        custom = [[p["Subject_ID"], p["sensor"], p["value_col"], p["group"], p.get("source_file", "")] for p in pts]
        hover = (
            "Subject: %{customdata[0]}<br>Sensor: %{customdata[1]}<br>Time: %{x}<br>"
            "Field: %{customdata[2]}<br>Value: %{y:.4g}<br>Group: %{customdata[3]}<extra></extra>"
        )
        if plot_type in {"raw_scatter", "scatter"}:
            fig.add_trace(go.Scattergl(x=x_vals, y=y_vals, mode="markers", marker=dict(size=5, opacity=0.70), name=name, customdata=custom, hovertemplate=hover))
        elif plot_type in {"raw_bar", "bar"}:
            fig.add_trace(go.Bar(x=x_vals, y=y_vals, name=name, customdata=custom, hovertemplate=hover))
        else:
            mode = "lines" if len(pts) > 250 else "lines+markers"
            marker = dict(size=4, opacity=0.75)
            fig.add_trace(go.Scattergl(x=x_vals, y=y_vals, mode=mode, marker=marker, line=dict(width=1.8), name=name, customdata=custom, hovertemplate=hover))
    y_title = _clean_feature_label(feature_column if feature_column not in {"", "All", None} else "Raw sensor value")
    _apply_scientific_layout(fig, "Raw sensor time series", "Time", y_title, height=660)
    fig.update_layout(legend_title="Participant — sensor", hovermode="x unified" if plot_type not in {"raw_scatter", "scatter"} else "closest")
    fig.update_xaxes(rangeslider=dict(visible=True), rangeselector=dict(buttons=list([
        dict(count=1, label="1d", step="day", stepmode="backward"),
        dict(count=7, label="7d", step="day", stepmode="backward"),
        dict(step="all", label="All"),
    ])))
    return plot(fig, output_type="div", include_plotlyjs="cdn", config=_plotly_export_config("jtrack_raw_sensor_timeseries", 900, 1600))


def _raw_panel_plot_div(rows: list[dict], feature_column: str = "All") -> str:
    points = _raw_sensor_points(rows, feature_column)
    if not points:
        return "<p>No raw sensor values with valid timestamps are available for the current filters.</p>"
    by_sensor: dict[str, list[dict]] = {}
    for pnt in points:
        panel = pnt["sensor"]
        if feature_column in {"", "All", None}:
            panel = f"{panel} — {pnt['value_col']}"
        by_sensor.setdefault(panel, []).append(pnt)
    # Keep the layout readable for very dense multimodal datasets.
    ordered_panels = sorted(by_sensor, key=lambda k: (-len(by_sensor[k]), k))[:12]
    fig = make_subplots(
        rows=len(ordered_panels),
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.035,
        subplot_titles=ordered_panels,
    )
    for idx, panel in enumerate(ordered_panels, start=1):
        subject_groups: dict[str, list[dict]] = {}
        for pnt in by_sensor[panel]:
            subject_groups.setdefault(pnt["Subject_ID"], []).append(pnt)
        for subject, pts in sorted(subject_groups.items()):
            pts = sorted(pts, key=lambda p: p["time"])
            fig.add_trace(
                go.Scattergl(
                    x=[p["time"] for p in pts],
                    y=[p["value"] for p in pts],
                    mode="lines" if len(pts) > 250 else "lines+markers",
                    marker=dict(size=4, opacity=0.70),
                    line=dict(width=1.6),
                    name=subject,
                    legendgroup=subject,
                    showlegend=idx == 1,
                    customdata=[[p["sensor"], p["value_col"]] for p in pts],
                    hovertemplate="Subject: %{fullData.name}<br>Sensor: %{customdata[0]}<br>Field: %{customdata[1]}<br>Time: %{x}<br>Value: %{y:.4g}<extra></extra>",
                ),
                row=idx,
                col=1,
            )
        fig.update_yaxes(title_text="Value", row=idx, col=1, showgrid=True, gridcolor="rgba(31,41,55,0.10)")
    title = "Multisensor raw time-series panels"
    if len(by_sensor) > len(ordered_panels):
        title += f" (top {len(ordered_panels)} of {len(by_sensor)} streams by record count)"
    fig.update_layout(
        title={"text": title, "x": 0.02, "xanchor": "left"},
        template="plotly_white",
        height=max(520, min(1300, 230 * len(ordered_panels))),
        hovermode="x unified",
        legend_title="Participant",
        margin=dict(l=76, r=32, t=90, b=70),
        font=dict(size=12),
    )
    fig.update_xaxes(title_text="Time", row=len(ordered_panels), col=1, showgrid=True, gridcolor="rgba(31,41,55,0.10)")
    return plot(fig, output_type="div", include_plotlyjs="cdn", config=_plotly_export_config("jtrack_raw_sensor_panels", 1200, 1600))


def _raw_day_hour_heatmap_plot_div(rows: list[dict], feature_column: str = "All") -> str:
    points = _raw_sensor_points(rows, feature_column)
    if not points:
        return "<p>No raw sensor values with valid timestamps are available for the current filters.</p>"
    from collections import defaultdict
    buckets: dict[tuple[str, int], list[float]] = defaultdict(list)
    for pnt in points:
        try:
            dt = datetime.fromisoformat(str(pnt["time"]).replace("Z", "+00:00"))
        except Exception:
            continue
        day = dt.date().isoformat()
        hour = int(dt.hour)
        buckets[(day, hour)].append(float(pnt["value"]))
    days = sorted({k[0] for k in buckets})
    hours = list(range(24))
    use_counts = feature_column in {"", "All", None}
    z = []
    for day in days:
        row_vals = []
        for hour in hours:
            vals = buckets.get((day, hour), [])
            if not vals:
                row_vals.append(None)
            elif use_counts:
                row_vals.append(len(vals))
            else:
                row_vals.append(sum(vals) / len(vals))
        z.append(row_vals)
    color_title = "Records" if use_counts else _clean_feature_label(feature_column)
    fig = go.Figure(data=go.Heatmap(
        x=[f"{h:02d}:00" for h in hours],
        y=days,
        z=z,
        colorscale="Viridis",
        colorbar=dict(title=color_title),
        hovertemplate="Date: %{y}<br>Hour: %{x}<br>Value: %{z}<extra></extra>",
    ))
    title = "Raw data day × hour coverage heatmap" if use_counts else f"Raw data day × hour mean: {_clean_feature_label(feature_column)}"
    _apply_scientific_layout(fig, title, "Hour of day", "Date", height=max(460, min(900, 80 + 28 * len(days))))
    return plot(fig, output_type="div", include_plotlyjs="cdn", config=_plotly_export_config("jtrack_raw_day_hour_heatmap", 900, 1400))


def _raw_distribution_plot_div(rows: list[dict], feature_column: str = "All") -> str:
    points = _raw_sensor_points(rows, feature_column)
    if not points:
        return "<p>No raw sensor values are available for the current filters.</p>"
    grouped: dict[str, list[float]] = {}
    for pnt in points:
        label = pnt["sensor"] if feature_column not in {"", "All", None} else f"{pnt['sensor']} — {pnt['value_col']}"
        grouped.setdefault(label, []).append(float(pnt["value"]))
    fig = go.Figure()
    for label, vals in sorted(grouped.items()):
        fig.add_trace(go.Box(y=vals, name=label, boxmean=True, points="outliers", hovertemplate="Stream: %{fullData.name}<br>Value: %{y:.4g}<extra></extra>"))
    _apply_scientific_layout(fig, "Raw sensor value distribution", "Sensor / field", _clean_feature_label(feature_column if feature_column not in {"", "All", None} else "Value"), height=620)
    fig.update_xaxes(tickangle=-35)
    return plot(fig, output_type="div", include_plotlyjs="cdn", config=_plotly_export_config("jtrack_raw_distribution", 900, 1600))



def _sensor_label_key(row: dict) -> str:
    return _label_key(_row_sensor_label(row) or _row_text_blob(row))


def _row_matches_sensor_tokens(row: dict, sensor_tokens: set[str]) -> bool:
    tokens = {_label_key(t) for t in sensor_tokens if t}
    if not tokens:
        return True
    labels = {
        _label_key(_row_sensor_label(row)),
        _label_key(_row_get_ci(row, ["sensorname", "sensor_name", "sensor", "sensor_file"])),
        _label_key(_row_get_ci(row, ["wearable_sensor", "wearable_file"])),
        _label_key(_row_get_ci(row, ["sensor_folder", "file_name", "source_file"])),
    }
    labels = {x for x in labels if x}
    # Alias groups used by Garmin/JTrack exports.
    alias_groups = {
        "BBI": {"BBI", "INTERBEATINTERVAL", "IBI"},
        "ENHANCEDBBI": {"ENHANCEDBBI", "ENHANCEDBBI", "ENHANCEDINTERBEATINTERVAL"},
        "HRV": {"HRV", "RMSSD"},
        "HEARTRATE": {"HEARTRATE", "HEARTRATE", "HR"},
        "ACTIVITY": {"ACTIVITY", "ACTIVITYRECOGNITION", "DETECTEDACTIVITY"},
        "LOCATION": {"LOCATION", "GPS", "GEOLOCATION"},
        "GPS": {"LOCATION", "GPS", "GEOLOCATION"},
        "GEOLOCATION": {"LOCATION", "GPS", "GEOLOCATION"},
    }
    expanded_tokens = set(tokens)
    for token in list(tokens):
        expanded_tokens |= alias_groups.get(token, set())
    return any(t in lab or lab in t for t in expanded_tokens for lab in labels)


def _raw_points_for_sensor(rows: list[dict], sensor_tokens: set[str], feature_column: str = "All") -> list[dict]:
    """Extract timestamped numeric values for raw rows matching sensor tokens."""
    tokens = {_label_key(t) for t in sensor_tokens if t}
    preferred_by_token = {
        "BBI": ["value", "Value", "bbi", "BBI", "interbeatInterval", "ibi"],
        "ENHANCEDBBI": ["value", "Value", "enhanced_bbi", "enhancedBBI", "EnhancedBBI", "bbi", "interbeatInterval"],
        "HEARTRATE": ["value", "Value", "heartRate", "heart_rate", "hr", "HR"],
        "HRV": ["value", "Value", "hrv", "HRV", "rmssd", "RMSSD"],
    }
    preferred: list[str] = []
    for token in tokens:
        preferred.extend(preferred_by_token.get(token, []))
    preferred.extend(["value", "Value", "bbi", "BBI", "heartRate", "heart_rate", "hr", "HR", "hrv", "HRV", "rmssd", "RMSSD"])
    skip_cols = {"analysis_time_ms", "timestamp", "timestamp_start", "timestamp_end", "beginTimeStamp", "endTimeStamp", "lastTimeUsed", "startTime", "endTime", "study_day"}
    out: list[dict] = []
    for row in rows:
        if not _row_matches_sensor_tokens(row, sensor_tokens):
            continue
        dt = _row_datetime(row)
        if dt is None:
            continue
        value_col = None
        val = None
        if feature_column not in {"", "All", None}:
            val = _row_get_float_ci(row, [feature_column])
            value_col = feature_column if val is not None else None
        else:
            # Prefer canonical value columns, then fall back to any numeric non-metadata column.
            candidate_cols = list(dict.fromkeys(preferred + list(row.keys())))
            for col in candidate_cols:
                if _is_identifier_or_metadata_column(str(col)) or str(col) in skip_cols:
                    continue
                val = _row_get_float_ci(row, [str(col)])
                if val is not None:
                    value_col = str(col)
                    break
        if val is None:
            continue
        out.append({
            "Subject_ID": _subject_label(row),
            "time_dt": dt,
            "time": dt.isoformat(),
            "value": val,
            "value_col": value_col or "value",
            "sensor": _row_sensor_label(row),
        })
    return sorted(out, key=lambda item: (item["Subject_ID"], item["time"]))

def _nearest_pairs(a: list[dict], b: list[dict], max_delta_sec: float = 300.0) -> list[dict]:
    """Pair time series by nearest timestamp within subject."""
    from bisect import bisect_left
    b_by_subject: dict[str, list[dict]] = {}
    for item in b:
        b_by_subject.setdefault(item["Subject_ID"], []).append(item)
    for subj in list(b_by_subject):
        b_by_subject[subj] = sorted(b_by_subject[subj], key=lambda x: x["time_dt"])
    pairs: list[dict] = []
    for item in a:
        candidates = b_by_subject.get(item["Subject_ID"], [])
        if not candidates:
            continue
        times = [c["time_dt"] for c in candidates]
        idx = bisect_left(times, item["time_dt"])
        best = None
        best_delta = None
        for j in (idx - 1, idx):
            if 0 <= j < len(candidates):
                delta = abs((candidates[j]["time_dt"] - item["time_dt"]).total_seconds())
                if best_delta is None or delta < best_delta:
                    best_delta = delta
                    best = candidates[j]
        if best is not None and best_delta is not None and best_delta <= max_delta_sec:
            pairs.append({
                "Subject_ID": item["Subject_ID"],
                "time": item["time"],
                "x": item["value"],
                "y": best["value"],
                "delta_sec": best_delta,
            })
    return pairs


def _pearson_r(x: list[float], y: list[float]) -> float | None:
    if len(x) < 3 or len(y) < 3 or len(x) != len(y):
        return None
    mx = sum(x) / len(x)
    my = sum(y) / len(y)
    sx = math.sqrt(sum((v - mx) ** 2 for v in x))
    sy = math.sqrt(sum((v - my) ** 2 for v in y))
    if sx == 0 or sy == 0:
        return None
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / (sx * sy)


def _raw_bbi_enhanced_comparison_plot_div(rows: list[dict]) -> str:
    """Scientific comparison plot for BBI vs enhanced BBI/HRV-like streams."""
    bbi = _raw_points_for_sensor(rows, {"BBI"})
    enhanced = _raw_points_for_sensor(rows, {"ENHANCED_BBI", "ENHANCEDBBI"})
    comparison_label = "Enhanced BBI"
    if not enhanced:
        enhanced = _raw_points_for_sensor(rows, {"HRV"})
        comparison_label = "HRV"
    if not bbi or not enhanced:
        available = sorted({_row_sensor_label(row) for row in rows})
        return (
            "<p>BBI comparison needs BBI and Enhanced BBI (or HRV) raw streams for the selected subject/filter.</p>"
            f"<p class='muted'>Detected BBI points: {len(bbi)}; comparison points: {len(enhanced)}. "
            f"Available raw streams in this selection: {html.escape(', '.join(available[:20]) or 'none')}.</p>"
        )
    pairs = _nearest_pairs(bbi, enhanced, max_delta_sec=300)
    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=False,
        vertical_spacing=0.16,
        subplot_titles=("Raw time-series overlay", f"Nearest-neighbor paired values: BBI vs {comparison_label}"),
    )
    for label, pts in [("BBI", bbi), (comparison_label, enhanced)]:
        by_subject: dict[str, list[dict]] = {}
        for pnt in pts:
            by_subject.setdefault(pnt["Subject_ID"], []).append(pnt)
        for subject, sub_pts in sorted(by_subject.items()):
            sub_pts = sorted(sub_pts, key=lambda p: p["time"])
            fig.add_trace(go.Scattergl(
                x=[p["time"] for p in sub_pts],
                y=[p["value"] for p in sub_pts],
                mode="lines" if len(sub_pts) > 250 else "lines+markers",
                name=f"{subject} — {label}",
                line=dict(width=1.7),
                marker=dict(size=4),
                hovertemplate="%{fullData.name}<br>Time: %{x}<br>Value: %{y:.4g}<extra></extra>",
            ), row=1, col=1)
    if pairs:
        xs = [p["x"] for p in pairs]
        ys = [p["y"] for p in pairs]
        rr = _pearson_r(xs, ys)
        median_delta = sorted([p["delta_sec"] for p in pairs])[len(pairs)//2]
        fig.add_trace(go.Scattergl(
            x=xs,
            y=ys,
            mode="markers",
            name="Paired points",
            marker=dict(size=7, opacity=0.68),
            customdata=[[p["Subject_ID"], p["delta_sec"]] for p in pairs],
            hovertemplate="Subject: %{customdata[0]}<br>BBI: %{x:.4g}<br>Comparison: %{y:.4g}<br>Δt: %{customdata[1]:.1f}s<extra></extra>",
        ), row=2, col=1)
        lo = min(xs + ys)
        hi = max(xs + ys)
        fig.add_trace(go.Scatter(x=[lo, hi], y=[lo, hi], mode="lines", name="Identity line", line=dict(dash="dash", width=1.5)), row=2, col=1)
        annotation = f"N paired = {len(pairs)}"
        if rr is not None:
            annotation += f"<br>Pearson r = {rr:.3f}"
        annotation += f"<br>Median Δt = {median_delta:.1f}s"
        fig.add_annotation(text=annotation, xref="paper", yref="paper", x=0.02, y=0.42, showarrow=False, align="left", bgcolor="rgba(255,255,255,0.85)", bordercolor="rgba(31,41,55,0.2)")
    fig.update_layout(
        title={"text": f"BBI comparison with {comparison_label}", "x": 0.02, "xanchor": "left"},
        template="plotly_white",
        height=820,
        margin=dict(l=76, r=32, t=90, b=70),
        legend_title="Stream",
        hovermode="closest",
    )
    fig.update_xaxes(title_text="Time", row=1, col=1, showgrid=True, gridcolor="rgba(31,41,55,0.10)")
    fig.update_yaxes(title_text="Raw value", row=1, col=1, showgrid=True, gridcolor="rgba(31,41,55,0.10)")
    fig.update_xaxes(title_text="BBI", row=2, col=1, showgrid=True, gridcolor="rgba(31,41,55,0.10)")
    fig.update_yaxes(title_text=comparison_label, row=2, col=1, showgrid=True, gridcolor="rgba(31,41,55,0.10)")
    return plot(fig, output_type="div", include_plotlyjs="cdn", config=_plotly_export_config("jtrack_bbi_comparison", 1000, 1500))


def _lat_lon_from_row(row: dict) -> tuple[float | None, float | None]:
    """Return latitude/longitude from messy JTrack/Android/Garmin exports.

    The raw exports are not always consistent: coordinates can appear as explicit
    latitude/longitude columns, abbreviated names (lat/lon/lng/lun), nested-like
    names (location.latitude), E7 integer coordinates, or as a delimited value
    column with a matching unit column.  This helper intentionally searches more
    broadly than the feature-computation code so raw location visualizations can
    work across older JTrack datasets.
    """

    def _normalize_coord(value: float | None, limit: float) -> float | None:
        if value is None:
            return None
        # Android/Google exports sometimes store coordinates as E7 integers.
        if abs(value) > limit and abs(value) <= 1800000000:
            value = value / 1e7
        return value if -limit <= value <= limit else None

    def _value_from_known_names(kind: str) -> float | None:
        if kind == "lat":
            keys = [
                "latitude", "lat", "lattitude", "location_latitude", "gps_latitude",
                "Latitude", "LAT", "LATTITUDE", "location.latitude", "location.lat",
                "coords.latitude", "coordinate_latitude", "latitudeE7", "latE7",
                "gps_latitude_e7", "lat_e7", "y", "Y",
            ]
        else:
            keys = [
                "longitude", "long", "lon", "lng", "lun", "location_longitude",
                "gps_longitude", "Longitude", "LONG", "LON", "LNG", "LUN",
                "location.longitude", "location.long", "location.lon", "location.lng",
                "location.lun", "coords.longitude", "coordinate_longitude", "longitudeE7",
                "longE7", "lonE7", "lngE7", "gps_longitude_e7", "long_e7", "x", "X",
            ]
        return _row_get_float_ci(row, keys)

    def _value_from_fuzzy_keys(kind: str) -> float | None:
        # Search all column names for coordinate-like labels while avoiding
        # unrelated terms such as latest/lasting or longitude-like metadata text.
        candidates: list[tuple[int, str]] = []
        for key in row.keys():
            raw_key = str(key)
            key_norm = re.sub(r"[^a-z0-9]+", "", raw_key.lower())
            if kind == "lat":
                if any(token in key_norm for token in ("latitude", "lattitude", "gpslat", "locationlat")) or key_norm in {"lat", "late7", "latitudee7", "y"}:
                    candidates.append((0 if key_norm in {"lat", "latitude", "latitudee7", "late7"} else 1, raw_key))
            else:
                if any(token in key_norm for token in ("longitude", "gpslon", "gpslng", "locationlon", "locationlng", "locationlun")) or key_norm in {"lon", "lng", "long", "lun", "lone7", "lnge7", "longitudee7", "x"}:
                    candidates.append((0 if key_norm in {"lon", "lng", "long", "lun", "longitude", "longitudee7"} else 1, raw_key))
        for _, key in sorted(candidates):
            value = _safe_float(row.get(key))
            if value is not None:
                return value
        return None

    def _value_from_value_unit() -> tuple[float | None, float | None]:
        # Some streams encode coordinates as value="52.1, 13.2" with
        # unit="latitude, longitude" or similar.  Also support JSON-like value
        # strings containing lat/lon keys.
        raw_value = _row_get_ci(row, ["value_text", "valueText", "raw_value", "rawValue", "value", "Value", "values", "location", "coordinates", "coords"])
        if raw_value in (None, ""):
            return None, None

        # Dict/list values can appear before CSV flattening.
        if isinstance(raw_value, dict):
            return _lat_lon_from_row(raw_value)
        if isinstance(raw_value, (list, tuple)) and len(raw_value) >= 2:
            return _safe_float(raw_value[0]), _safe_float(raw_value[1])

        text = str(raw_value).strip()
        if not text:
            return None, None

        # JSON-ish string with coordinate fields.
        if (text.startswith("{") and text.endswith("}")) or (text.startswith("[") and text.endswith("]")):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    return _lat_lon_from_row(parsed)
                if isinstance(parsed, list) and len(parsed) >= 2:
                    return _safe_float(parsed[0]), _safe_float(parsed[1])
            except Exception:
                pass

        # Key-value coordinate text, for example "lat=52.1; lon=13.2" or
        # "latitude: 52.1, longitude: 13.2".
        lat_match = re.search(r"(?:lat|latitude|lattitude)\s*[:=]\s*(-?\d+(?:[\.,]\d+)?)", text, flags=re.IGNORECASE)
        lon_match = re.search(r"(?:lon|lng|long|lun|longitude)\s*[:=]\s*(-?\d+(?:[\.,]\d+)?)", text, flags=re.IGNORECASE)
        if lat_match and lon_match:
            return _safe_float(lat_match.group(1)), _safe_float(lon_match.group(1))

        # Delimited coordinate pair.  Prefer unit labels when available, otherwise
        # infer common latitude,longitude ordering if both numbers are plausible.
        pieces = [part.strip() for part in re.split(r"[;,|]", text) if part.strip()]
        nums = [_safe_float(part) for part in pieces]
        # If pieces were labelled ("lat=52"), extract the numeric part.
        if sum(v is not None for v in nums) < 2:
            nums = [_safe_float(m.group(0)) for m in re.finditer(r"-?\d+(?:[\.,]\d+)?", text)]
        nums = [v for v in nums if v is not None]
        if len(nums) >= 2:
            unit_text = str(_row_get_ci(row, ["unit", "Unit", "units", "Units"]) or "")
            unit_parts = [re.sub(r"[^a-z0-9]+", "", part.lower()) for part in re.split(r"[;,|]", unit_text)]
            lat_idx = next((i for i, u in enumerate(unit_parts) if "lat" in u and i < len(nums)), None)
            lon_idx = next((i for i, u in enumerate(unit_parts) if any(tok in u for tok in ("lon", "lng", "long", "lun")) and i < len(nums)), None)
            if lat_idx is not None and lon_idx is not None:
                return nums[lat_idx], nums[lon_idx]
            # Common raw coordinate pair order: latitude, longitude.
            if -90 <= nums[0] <= 90 and -180 <= nums[1] <= 180:
                return nums[0], nums[1]
            if -90 <= nums[1] <= 90 and -180 <= nums[0] <= 180:
                return nums[1], nums[0]
        return None, None

    lat = _value_from_known_names("lat")
    lon = _value_from_known_names("lon")
    if lat is None:
        lat = _value_from_fuzzy_keys("lat")
    if lon is None:
        lon = _value_from_fuzzy_keys("lon")
    if lat is None or lon is None:
        v_lat, v_lon = _value_from_value_unit()
        if lat is None:
            lat = v_lat
        if lon is None:
            lon = v_lon

    lat = _normalize_coord(lat, 90)
    lon = _normalize_coord(lon, 180)
    if lat is None or lon is None:
        return None, None
    return lat, lon



def _coordinate_component_from_row(row: dict) -> tuple[str | None, float | None]:
    """Return a single latitude/longitude component when coordinates are stored row-wise.

    Some JTrack location exports store one coordinate per row, for example
    unit='latitude', value='52.1' and a matching unit='longitude', value='13.2'
    row at the same timestamp.  The R app works after normalization to wide
    latitude/longitude columns; this helper reconstructs that wide point for
    raw visualizations.
    """
    descriptor_parts = []
    for key in (
        "unit", "Unit", "units", "Units", "name", "Name", "label", "Label",
        "variable", "Variable", "field", "Field", "measure", "Measure", "type", "Type",
        "dataType", "data_type", "key", "Key", "property", "Property",
    ):
        value = _row_get_ci(row, [key])
        if value not in (None, ""):
            descriptor_parts.append(str(value))
    descriptor = _label_key(" ".join(descriptor_parts))
    if not descriptor:
        return None, None
    kind = None
    if any(token in descriptor for token in ("LATITUDE", "LATTITUDE", "GPSLAT")) or descriptor in {"LAT", "Y"}:
        kind = "lat"
    elif any(token in descriptor for token in ("LONGITUDE", "GPSLON", "GPSLNG")) or descriptor in {"LON", "LNG", "LONG", "LUN", "X"}:
        kind = "lon"
    if kind is None:
        return None, None
    value = _row_get_float_ci(row, [
        "value", "Value", "value_text", "valueText", "raw_value", "rawValue",
        "numeric_value", "numericValue", "measurement", "Measurement",
    ])
    if value is None:
        return None, None
    # E7 support.
    if kind == "lat" and abs(value) > 90 and abs(value) <= 900000000:
        value = value / 1e7
    if kind == "lon" and abs(value) > 180 and abs(value) <= 1800000000:
        value = value / 1e7
    if kind == "lat" and not (-90 <= value <= 90):
        return None, None
    if kind == "lon" and not (-180 <= value <= 180):
        return None, None
    return kind, value


def _location_group_key(row: dict, idx: int) -> tuple:
    """Stable grouping key for row-wise coordinate components."""
    subject = _subject_label(row)
    # Prefer exact raw timing fields to avoid rounding away matching pairs.
    raw_time = None
    for key in (
        "timestamp", "time", "timeStamp", "Timestamp", "timestampMs", "timestamp_ms",
        "measurementTimestamp", "measurementTimeStamp", "sampleTimestamp", "recordedTimestamp",
        "timestamp_start", "startTime", "beginTimeStamp", "lastTimeUsed",
    ):
        raw = _row_get_ci(row, [key])
        if raw not in (None, ""):
            raw_time = str(raw)
            break
    if raw_time is None:
        dt = _row_datetime(row)
        raw_time = dt.isoformat() if dt is not None else f"rowblock-{idx // 2}"
    source = str(_row_get_ci(row, ["source_file", "file_name", "sensor_folder"]) or "")
    return subject, source, raw_time



def _iter_nested_dicts(value: object, depth: int = 0, max_depth: int = 5):
    """Yield nested dictionaries from JSON-like row values.

    Some JTrack exports store coordinates inside nested payload fields rather
    than as top-level latitude/longitude columns.  The R loader flattens many
    of these structures; the web visualizer therefore needs to inspect nested
    dict/list values as well.
    """
    if depth > max_depth:
        return
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _iter_nested_dicts(child, depth + 1, max_depth)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_nested_dicts(child, depth + 1, max_depth)
    elif isinstance(value, str):
        text = value.strip()
        if not text or len(text) > 20000:
            return
        if (text.startswith('{') and text.endswith('}')) or (text.startswith('[') and text.endswith(']')):
            try:
                parsed = json.loads(text)
            except Exception:
                return
            yield from _iter_nested_dicts(parsed, depth + 1, max_depth)


def _nested_coordinate_key_hints(rows: list[dict], limit: int = 200) -> list[str]:
    hints: set[str] = set()
    for row in rows[:limit]:
        for obj in _iter_nested_dicts(row, max_depth=4):
            for key in obj.keys():
                key_s = str(key)
                if any(x in key_s.lower() for x in ["lat", "lon", "lng", "lun", "long", "coord", "location", "geo", "gps", "unit", "value"]):
                    hints.add(key_s)
    return sorted(hints)


def _nested_location_points_from_row(row: dict, parent_idx: int) -> list[dict]:
    """Extract location points from nested JSON structures inside one loaded row."""
    points: list[dict] = []
    parent_dt = _row_datetime(row)
    parent_subject = _subject_label(row)
    parent_sensor = _row_sensor_label(row) or "Location"
    seen: set[tuple] = set()

    # Inspect nested children but not the parent dict itself first; the parent
    # is already handled by direct top-level coordinate extraction.
    for obj in _iter_nested_dicts(row, max_depth=5):
        if obj is row:
            continue
        lat, lon = _lat_lon_from_row(obj)
        if lat is None or lon is None:
            continue
        dt = _row_datetime(obj) or parent_dt
        sort_key = dt.timestamp() if dt is not None else float(parent_idx)
        key = (round(float(lat), 7), round(float(lon), 7), dt.isoformat() if dt else parent_idx)
        if key in seen:
            continue
        seen.add(key)
        points.append({
            "Subject_ID": parent_subject,
            "sensor": parent_sensor,
            "time_dt": dt,
            "sort_key": sort_key,
            "time": dt.isoformat() if dt is not None else f"row {parent_idx + 1}",
            "lat": float(lat),
            "lon": float(lon),
        })
    return points

def _location_points_from_rows(rows: list[dict]) -> tuple[list[dict], dict]:
    """Extract location points from explicit columns or row-wise lat/lon components.

    This mirrors the R workflow more closely: direct latitude/longitude columns
    are used first.  If coordinates are stored as separate latitude and
    longitude rows, the rows are reconstructed into points by exact timestamp
    and then, as a fallback, by nearest timestamp within each subject/source.
    """
    points: list[dict] = []
    component_groups: dict[tuple, dict] = {}
    component_records: list[dict] = []
    direct_count = 0
    component_count = 0

    for idx, row in enumerate(rows):
        dt = _row_datetime(row)
        lat, lon = _lat_lon_from_row(row)
        if lat is not None and lon is not None:
            direct_count += 1
            points.append({
                "Subject_ID": _subject_label(row),
                "sensor": _row_sensor_label(row) or "Location",
                "time_dt": dt,
                "sort_key": dt.timestamp() if dt is not None else float(idx),
                "time": dt.isoformat() if dt is not None else f"row {idx + 1}",
                "lat": lat,
                "lon": lon,
            })
            continue

        nested_points = _nested_location_points_from_row(row, idx)
        if nested_points:
            points.extend(nested_points)
            direct_count += len(nested_points)
            continue

        kind, value = _coordinate_component_from_row(row)
        if kind is None or value is None:
            continue
        component_count += 1
        source = str(_row_get_ci(row, ["source_file", "file_name", "sensor_folder"]) or "")
        comp = {
            "kind": kind,
            "value": value,
            "Subject_ID": _subject_label(row),
            "sensor": _row_sensor_label(row) or "Location",
            "time_dt": dt,
            "sort_key": dt.timestamp() if dt is not None else float(idx),
            "time": dt.isoformat() if dt is not None else f"row {idx + 1}",
            "source": source,
            "idx": idx,
        }
        component_records.append(comp)
        key = _location_group_key(row, idx)
        grp = component_groups.setdefault(key, {
            "Subject_ID": comp["Subject_ID"],
            "sensor": comp["sensor"],
            "time_dt": dt,
            "sort_key": comp["sort_key"],
            "time": comp["time"],
            "lat": None,
            "lon": None,
        })
        grp[kind] = value
        if grp.get("time_dt") is None and dt is not None:
            grp["time_dt"] = dt
            grp["time"] = dt.isoformat()
            grp["sort_key"] = dt.timestamp()

    seen = set()
    for point in points:
        key = (point["Subject_ID"], point.get("time"), round(point["lat"], 7), round(point["lon"], 7))
        seen.add(key)

    exact_paired_count = 0
    for grp in component_groups.values():
        if grp.get("lat") is None or grp.get("lon") is None:
            continue
        key = (grp["Subject_ID"], grp.get("time"), round(grp["lat"], 7), round(grp["lon"], 7))
        if key in seen:
            continue
        seen.add(key)
        exact_paired_count += 1
        points.append(grp)

    # Fallback for datasets where latitude and longitude components are stored
    # in adjacent rows or near-identical timestamps rather than exactly the same
    # raw timestamp.  Pair the nearest unmatched lat/lon component within each
    # subject/source. This is deliberately only used for component rows, so it
    # does not affect normal wide location rows.
    nearest_paired_count = 0
    by_context: dict[tuple[str, str], list[dict]] = {}
    for comp in component_records:
        by_context.setdefault((comp["Subject_ID"], comp["source"]), []).append(comp)

    def _pair_delta(a: dict, b: dict) -> float:
        if a.get("time_dt") is not None and b.get("time_dt") is not None:
            return abs((a["time_dt"] - b["time_dt"]).total_seconds())
        return abs(float(a.get("idx", 0)) - float(b.get("idx", 0)))

    used_component_idx: set[int] = set()
    for (_subject, _source), comps in by_context.items():
        lats = sorted([c for c in comps if c["kind"] == "lat"], key=lambda c: (c.get("sort_key", 0), c.get("idx", 0)))
        lons = sorted([c for c in comps if c["kind"] == "lon"], key=lambda c: (c.get("sort_key", 0), c.get("idx", 0)))
        for lat_comp in lats:
            if lat_comp["idx"] in used_component_idx:
                continue
            candidates = [lon_comp for lon_comp in lons if lon_comp["idx"] not in used_component_idx]
            if not candidates:
                continue
            best = min(candidates, key=lambda lon_comp: _pair_delta(lat_comp, lon_comp))
            delta = _pair_delta(lat_comp, best)
            # If timestamped, require within 5 minutes like the activity-location
            # matching. If not timestamped, require adjacent/nearby rows.
            if lat_comp.get("time_dt") is not None and best.get("time_dt") is not None:
                if delta > 300:
                    continue
            elif delta > 3:
                continue
            time_dt = lat_comp.get("time_dt") or best.get("time_dt")
            sort_key = lat_comp.get("sort_key", best.get("sort_key", 0))
            point = {
                "Subject_ID": lat_comp["Subject_ID"],
                "sensor": lat_comp.get("sensor") or best.get("sensor") or "Location",
                "time_dt": time_dt,
                "sort_key": sort_key,
                "time": time_dt.isoformat() if time_dt is not None else lat_comp.get("time", best.get("time", "row")),
                "lat": lat_comp["value"],
                "lon": best["value"],
            }
            key = (point["Subject_ID"], point.get("time"), round(point["lat"], 7), round(point["lon"], 7))
            if key not in seen:
                seen.add(key)
                points.append(point)
                nearest_paired_count += 1
            used_component_idx.add(lat_comp["idx"])
            used_component_idx.add(best["idx"])

    points = sorted(points, key=lambda x: (x.get("Subject_ID", ""), x.get("sort_key", 0)))
    diagnostics = {
        "direct_coordinate_rows": direct_count,
        "coordinate_component_rows": component_count,
        "paired_component_points": exact_paired_count,
        "nearest_component_pairs": nearest_paired_count,
        "location_points": len(points),
    }
    return points, diagnostics


def _activity_rows_from_rows(rows: list[dict]) -> list[dict]:
    """Extract Android activity rows with R-code-compatible labels/confidence."""
    raw_conf = []
    candidate_rows = []
    for row in rows:
        dt = _row_datetime(row)
        if dt is None:
            continue
        sensor_key = _sensor_label_key(row)
        activity_value = _activity_value_from_row(row)
        if "ACTIVITY" not in sensor_key and activity_value in (None, ""):
            continue
        candidate_rows.append((row, dt, activity_value))
        raw = _row_get_float_ci(row, ["confidence", "activityConfidence", "confidenceScore", "probability", "Confidence"])
        if raw is not None:
            raw_conf.append(raw)
    max_conf = max(raw_conf) if raw_conf else None
    act_rows = []
    for row, dt, activity_value in candidate_rows:
        act_rows.append({
            "Subject_ID": _subject_label(row),
            "time_dt": dt,
            "time": dt.isoformat(),
            "label": _android_activity_label(activity_value),
            "confidence": _activity_confidence_from_row(row, max_conf),
        })
    return sorted(act_rows, key=lambda x: (x["Subject_ID"], x["time_dt"]))


def _subject_context_rows(rows: list[dict]) -> list[dict]:
    """Return all raw rows for the selected subject context.

    This intentionally mirrors the R app's ``comparison_context_data()`` behavior:
    compound plots such as BBI-vs-HRV and activity-vs-location must not be limited
    to the currently selected sensor.  Step 3 feature computation often loads only
    the selected sensor into ``loaded_rows``; for activity-location plotting that
    means the location stream is absent unless we go back to the indexed files and
    reload every sensor for the same subject.
    """
    subjects = sorted({_subject_label(r) for r in rows if _subject_label(r) and _subject_label(r) != "Unknown"})
    if not subjects:
        # Fall back to the globally selected feature subject when Step 5 was opened
        # from an empty/filtered table.  Otherwise keep the input rows.
        return rows

    # First use any already-loaded all-sensor context.  This is fast and preserves
    # study-day filters when the user loaded a broad scope.
    out: list[dict] = []
    for subj in subjects:
        out.extend(_raw_rows_filtered(subject=subj, sensor_name="All", cohort="All"))

    # If the loaded context is still sensor-limited, reload from the metadata index
    # with sensor_name="All".  This is the critical difference from previous
    # versions: it gives activity plots access to LOCATION/GPS files even after the
    # user computed ACTIVITY features in Step 3.
    def _has_location_like(candidate_rows: list[dict]) -> bool:
        points, _diag = _location_points_from_rows(candidate_rows)
        if points:
            return True
        return any(_label_key(_row_sensor_label(r)) in {"LOCATION", "GPS", "GEOLOCATION"} for r in candidate_rows)

    if not _has_location_like(out):
        reloaded: list[dict] = []
        for subj in subjects:
            indexed = filter_indexed_files(APP_STATE.indexed_rows, username=subj, sensor_name="All")
            if indexed:
                reloaded.extend(load_indexed_json_rows(indexed))
        if reloaded:
            # Preserve group labels and current study-day range when available.
            out = _attach_group_labels(reloaded)

    return out or rows



def _is_location_stream_row(row: dict) -> bool:
    """Return True for rows that belong to a LOCATION/GPS/GEOLOCATION stream.

    This intentionally uses stream metadata/file-path hints only.  It does not
    inspect all raw values, because activity rows can contain numeric fields that
    accidentally look like coordinates and would distort the activity-location
    plot.  This mirrors the R workflow, where prepare_location_stream_data()
    first selects the LOCATION/GPS/GEOLOCATION stream and only then extracts
    latitude/longitude columns.
    """
    parts = []
    for key in (
        "sensorname", "sensor_name", "sensor", "sensor_file",
        "wearable_sensor", "wearable_file", "sensor_folder",
        "file_name", "source_file",
    ):
        value = _row_get_ci(row, [key])
        if value not in (None, ""):
            parts.append(str(value))
    key = _label_key(" ".join(parts))
    return any(token in key for token in ("LOCATION", "GPS", "GEOLOCATION"))


def _location_stream_rows(rows: list[dict]) -> list[dict]:
    """Keep only location-like rows when available.

    Compound activity-location visualization should use the same trajectory as
    the plain location plot, but from the location stream only.  Returning an
    empty list when no location stream is detected is safer than scanning
    activity rows for arbitrary numeric coordinate-like fields.
    """
    return [row for row in rows if _is_location_stream_row(row)]


def _raw_location_trajectory_plot_div(rows: list[dict]) -> str:
    """Plot raw latitude/longitude trajectory for location-like rows."""
    loc_rows, diagnostics = _location_points_from_rows(rows)
    if not loc_rows:
        available_cols = sorted({str(k) for row in rows[:300] for k in row.keys()})
        coord_hints = [c for c in available_cols if any(x in c.lower() for x in ["lat", "lon", "lng", "lun", "long", "coord", "unit", "value", "location", "gps", "geo"])]
        coord_hints = sorted(set(coord_hints) | set(_nested_coordinate_key_hints(rows)))
        return (
            "<p>Location trajectory needs raw rows with valid latitude and longitude information.</p>"
            f"<p class='muted'>Coordinate-like columns detected: {html.escape(', '.join(coord_hints[:40]) or 'none')}.</p>"
            f"<p class='muted'>Direct coordinate rows: {diagnostics.get('direct_coordinate_rows', 0)}; "
            f"coordinate component rows: {diagnostics.get('coordinate_component_rows', 0)}; "
            f"paired component points: {diagnostics.get('paired_component_points', 0)}; "
            f"nearest component pairs: {diagnostics.get('nearest_component_pairs', 0)}.</p>"
        )
    fig = go.Figure()
    by_subject: dict[str, list[dict]] = {}
    for row in loc_rows:
        by_subject.setdefault(row["Subject_ID"], []).append(row)
    for subject, pts in sorted(by_subject.items()):
        pts = sorted(pts, key=lambda x: x.get("sort_key", 0))
        fig.add_trace(go.Scatter(
            x=[p["lon"] for p in pts],
            y=[p["lat"] for p in pts],
            mode="markers+lines" if len(pts) < 800 else "markers",
            name=subject,
            marker=dict(size=7, opacity=0.72),
            line=dict(width=1.4),
            customdata=[[p["time"], p.get("sensor", "Location"), p["lat"], p["lon"]] for p in pts],
            hovertemplate="Subject: %{fullData.name}<br>Time: %{customdata[0]}<br>Sensor: %{customdata[1]}<br>Lat: %{customdata[2]:.6f}<br>Lon: %{customdata[3]:.6f}<extra></extra>",
        ))
    fig.update_layout(
        title={"text": f"Raw location trajectory ({len(loc_rows)} points)", "x": 0.02, "xanchor": "left"},
        template="plotly_white",
        height=720,
        margin=dict(l=70, r=30, t=80, b=65),
        legend_title="Participant",
        xaxis_title="Longitude",
        yaxis_title="Latitude",
        yaxis=dict(scaleanchor="x", scaleratio=1),
    )
    return plot(fig, output_type="div", include_plotlyjs="cdn", config=_plotly_export_config("jtrack_raw_location", 1000, 1500))


def _raw_activity_location_plot_div(rows: list[dict]) -> str:
    """Show the exact location trajectory used by the raw location plot, with nearest Android activity labels overlaid."""
    # Important: the base trajectory must come from the same rows used by
    # ``Location trajectory from lat/lon``.  Earlier versions reloaded a broad
    # subject context first, which could change the trajectory compared with the
    # plain location plot.  We now use the selected raw rows as the primary
    # source for the location path, and only fall back to a broader same-subject
    # context when one of the two streams is missing.
    # R-code-compatible approach: prepare activity and location as separate
    # streams.  For location, only use LOCATION/GPS/GEOLOCATION rows before
    # extracting coordinates.  This prevents activity confidence/code columns
    # from being misread as latitude/longitude and creating impossible jumps.
    loc_source_rows = _location_stream_rows(rows)
    loc_rows, diagnostics = _location_points_from_rows(loc_source_rows) if loc_source_rows else ([], {
        "direct_coordinate_rows": 0,
        "coordinate_component_rows": 0,
        "paired_component_points": 0,
        "nearest_component_pairs": 0,
        "location_points": 0,
    })
    act_rows = _activity_rows_from_rows(rows)
    context_rows = loc_source_rows or rows

    if not loc_rows or not act_rows:
        broader_rows = _subject_context_rows(rows)
        broader_loc_source_rows = _location_stream_rows(broader_rows)
        if not loc_rows and broader_loc_source_rows:
            loc_rows, diagnostics = _location_points_from_rows(broader_loc_source_rows)
            context_rows = broader_loc_source_rows
        if not act_rows:
            act_rows = _activity_rows_from_rows(broader_rows)

    # Last-resort fallback: search all loaded raw rows for location-stream rows
    # belonging to the activity subject(s).  Do not run coordinate extraction on
    # non-location streams.
    if not loc_rows and act_rows:
        activity_subjects = {_label_key(a.get("Subject_ID")) for a in act_rows if a.get("Subject_ID")}
        candidate_rows = [r for r in _location_stream_rows(_raw_base_rows()) if (not activity_subjects or _label_key(_subject_label(r)) in activity_subjects)]
        all_points, all_diag = _location_points_from_rows(candidate_rows) if candidate_rows else ([], {})
        loc_rows = all_points
        diagnostics = {**diagnostics, **{f"all_{k}": v for k, v in all_diag.items()}}
        context_rows = candidate_rows or context_rows

    if not loc_rows and not act_rows:
        return "<p>Activity-location visualization needs both location rows with latitude/longitude and Android activity rows for the selected subject/filter.</p>"
    if not loc_rows:
        available_cols = sorted({str(k) for row in context_rows[:500] for k in row.keys()})
        coord_hints = [c for c in available_cols if any(x in c.lower() for x in ["lat", "lon", "lng", "lun", "long", "coord", "unit", "value", "location", "gps", "geo"])]
        coord_hints = sorted(set(coord_hints) | set(_nested_coordinate_key_hints(context_rows)))
        return (
            f"<p>Found {len(act_rows)} activity rows, but no valid latitude/longitude rows for the selected subject context.</p>"
            f"<p class='muted'>Location-stream rows searched: {len(context_rows)}. Coordinate-like columns detected: {html.escape(', '.join(coord_hints[:50]) or 'none')}.</p>"
            f"<p class='muted'>Direct coordinate rows: {diagnostics.get('direct_coordinate_rows', 0)}; "
            f"coordinate component rows: {diagnostics.get('coordinate_component_rows', 0)}; "
            f"paired component points: {diagnostics.get('paired_component_points', 0)}; "
            f"nearest component pairs: {diagnostics.get('nearest_component_pairs', 0)}.</p>"
        )

    # Match each location point to nearest activity state within 5 minutes.
    matched = []
    by_subject: dict[str, list[dict]] = {}
    for a in act_rows:
        by_subject.setdefault(a["Subject_ID"], []).append(a)
    for subj in by_subject:
        by_subject[subj] = sorted(by_subject[subj], key=lambda x: x["time_dt"])
    from bisect import bisect_left
    for loc in sorted(loc_rows, key=lambda x: (x["Subject_ID"], x.get("sort_key", 0))):
        acts = by_subject.get(loc["Subject_ID"], [])
        best = None
        best_delta = None
        if acts and loc.get("time_dt") is not None:
            times = [a["time_dt"] for a in acts]
            idx = bisect_left(times, loc["time_dt"])
            for j in (idx - 1, idx):
                if 0 <= j < len(acts):
                    delta = abs((acts[j]["time_dt"] - loc["time_dt"]).total_seconds())
                    if best_delta is None or delta < best_delta:
                        best_delta = delta
                        best = acts[j]
        if best is not None and best_delta is not None and best_delta <= 300:
            label = best["label"]
            conf = best.get("confidence")
        else:
            label = "No activity match" if acts else "No activity stream"
            conf = None
        matched.append({**loc, "label": label, "confidence": conf, "delta_sec": best_delta})

    fig = go.Figure()

    # Use the same local lon/lat trajectory method as the raw location plot:
    # first draw the chronological path per subject, then overlay activity
    # labels as markers.  Do not connect points within activity-label groups,
    # because that creates artificial zig-zag trajectories when the same label
    # appears in non-consecutive locations.
    by_subject: dict[str, list[dict]] = {}
    for point in matched:
        by_subject.setdefault(point["Subject_ID"], []).append(point)

    for subject, pts in sorted(by_subject.items()):
        pts = sorted(pts, key=lambda x: x.get("sort_key", 0))
        fig.add_trace(go.Scatter(
            x=[p["lon"] for p in pts],
            y=[p["lat"] for p in pts],
            mode="markers+lines" if len(pts) < 800 else "markers",
            name=f"{subject} trajectory",
            marker=dict(size=5, opacity=0.35, color="rgba(80,80,80,0.45)"),
            line=dict(width=1.4, color="rgba(80,80,80,0.35)"),
            customdata=[[p["time"], p.get("sensor", "Location"), p["lat"], p["lon"]] for p in pts],
            hovertemplate="Subject: %{fullData.name}<br>Time: %{customdata[0]}<br>Sensor: %{customdata[1]}<br>Lat: %{customdata[2]:.6f}<br>Lon: %{customdata[3]:.6f}<extra></extra>",
            showlegend=False,
        ))

    labels = sorted({m["label"] for m in matched})
    for label in labels:
        pts = sorted([m for m in matched if m["label"] == label], key=lambda x: (x["Subject_ID"], x.get("sort_key", 0)))
        fig.add_trace(go.Scatter(
            x=[p["lon"] for p in pts],
            y=[p["lat"] for p in pts],
            mode="markers",
            name=label,
            marker=dict(size=7, opacity=0.78),
            customdata=[[p["Subject_ID"], p["time"], p.get("confidence"), (f"{p.get('delta_sec'):.1f}s" if p.get("delta_sec") is not None else "not matched"), p["lat"], p["lon"]] for p in pts],
            hovertemplate="Subject: %{customdata[0]}<br>Time: %{customdata[1]}<br>Activity: %{fullData.name}<br>Confidence: %{customdata[2]}<br>Δt: %{customdata[3]}<br>Lat: %{customdata[4]:.6f}<br>Lon: %{customdata[5]:.6f}<extra></extra>",
        ))

    fig.update_layout(
        title={"text": f"Activity recognition over local location trajectory ({len(loc_rows)} location rows, {len(act_rows)} activity rows)", "x": 0.02, "xanchor": "left"},
        template="plotly_white",
        height=720,
        margin=dict(l=70, r=30, t=80, b=65),
        legend_title="Nearest activity state",
        xaxis_title="Longitude",
        yaxis_title="Latitude",
        yaxis=dict(scaleanchor="x", scaleratio=1),
    )
    return plot(fig, output_type="div", include_plotlyjs="cdn", config=_plotly_export_config("jtrack_activity_location", 1000, 1500))

def _raw_plot_div(rows: list[dict], feature_column: str = "All", plot_type: str = "raw_panel") -> str:
    if plot_type == "raw_panel":
        return _raw_panel_plot_div(rows, feature_column)
    if plot_type == "raw_bbi_compare":
        return _raw_bbi_enhanced_comparison_plot_div(rows)
    if plot_type == "raw_activity_location":
        return _raw_activity_location_plot_div(rows)
    if plot_type == "raw_location":
        return _raw_location_trajectory_plot_div(rows)
    if plot_type == "raw_heatmap":
        return _raw_day_hour_heatmap_plot_div(rows, feature_column)
    if plot_type == "raw_distribution":
        return _raw_distribution_plot_div(rows, feature_column)
    return _raw_time_series_plot_div(rows, feature_column, plot_type)

def _clean_feature_label(name: str | None) -> str:
    text = str(name or "").replace("_", " ").strip()
    return text[:1].upper() + text[1:] if text else "Feature"


def _feature_plot_title(feature_column: str, x_col: str | None, plot_type: str) -> str:
    feature = _clean_feature_label(feature_column)
    x_label = _clean_feature_label(x_col or "row")
    plot_label = _clean_feature_label(plot_type)
    return f"{feature} — {plot_label} by {x_label}"


def _first_subject_col(rows: list[dict]) -> str | None:
    if not rows:
        return None
    for col in ("Subject_ID", "username", "subject", "participant"):
        if col in rows[0]:
            return col
    return None


def _first_group_col(rows: list[dict]) -> str | None:
    if not rows:
        return None
    for col in ("group_label", "cohort", "group", "condition"):
        if col in rows[0]:
            return col
    return None


def _first_time_col(rows: list[dict]) -> str | None:
    if not rows:
        return None
    for col in ("time_bin", "Date", "date", "Study_day", "study_day"):
        if col in rows[0]:
            return col
    return None


def _temporal_frequency_values(rows: list[dict]) -> list[str]:
    """Return available temporal frequencies in a stable UI order."""
    order = ["daily", "hourly", "monthly", "full_study", "full study duration"]
    seen = []
    for row in rows:
        raw = row.get("temporal_frequency")
        if raw in (None, ""):
            continue
        value = str(raw).strip()
        if value and value not in seen:
            seen.append(value)
    if not seen:
        return []
    def key(value: str) -> tuple[int, str]:
        norm = value.lower().replace("-", "_").replace(" ", "_")
        for i, item in enumerate(order):
            if norm == item.replace(" ", "_"):
                return (i, value)
        return (99, value)
    return sorted(seen, key=key)


def _filter_rows_by_temporal_frequency(rows: list[dict], temporal_frequency: str = "All") -> list[dict]:
    if temporal_frequency in {None, "", "All"}:
        return rows
    wanted = str(temporal_frequency).strip().lower().replace("-", "_").replace(" ", "_")
    return [
        row for row in rows
        if str(row.get("temporal_frequency", "")).strip().lower().replace("-", "_").replace(" ", "_") == wanted
    ]


def _infer_temporal_frequency(rows: list[dict], fallback: str = "daily") -> str:
    vals = _temporal_frequency_values(rows)
    if len(vals) == 1:
        return vals[0]
    if vals:
        return vals[0]
    return fallback


def _parse_feature_datetime(raw) -> datetime | None:
    if raw in (None, ""):
        return None
    text = str(raw).strip()
    if not text:
        return None
    # Numeric epoch seconds or milliseconds.
    num = _safe_float(text)
    if num is not None:
        try:
            if abs(num) > 1e11:
                return datetime.fromtimestamp(num / 1000, tz=timezone.utc).replace(tzinfo=None)
            if abs(num) > 1e8:
                return datetime.fromtimestamp(num, tz=timezone.utc).replace(tzinfo=None)
        except Exception:
            pass
    for fmt in (
        "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M",
        "%Y-%m-%d", "%Y-%m", "%d-%b-%Y", "%d-%B-%Y", "%d/%m/%Y", "%m/%d/%Y",
    ):
        try:
            return datetime.strptime(text[:len(datetime.now().strftime(fmt))] if "%S" in fmt and len(text) >= 19 else text, fmt)
        except Exception:
            continue
    return None


def _feature_row_datetime(row: dict) -> datetime | None:
    for col in ("time_bin", "Date", "date", "datetime", "time", "timestamp"):
        dt = _parse_feature_datetime(row.get(col))
        if dt is not None:
            return dt
    return None


def _temporal_axis_values(rows: list[dict], temporal_frequency: str | None = None) -> tuple[list[float | None], str, str]:
    """Return numeric x-values aligned to rows plus axis title and detected frequency.

    Daily rows use Study_day when available; otherwise dates become relative study days.
    Hourly rows become relative study hours. Monthly rows become relative study months.
    Full-study rows use a single full-study bin.
    """
    freq = (temporal_frequency or _infer_temporal_frequency(rows)).strip().lower().replace("-", "_").replace(" ", "_")
    if freq in {"full", "full_study", "study", "study_duration", "full_study_duration"}:
        return [0.0 for _ in rows], "Full study", "full_study"

    if freq == "hourly":
        dts = [_feature_row_datetime(row) for row in rows]
        valid = [dt for dt in dts if dt is not None]
        if valid:
            start = min(valid)
            return [((dt - start).total_seconds() / 3600.0) if dt is not None else None for dt in dts], "Study hour", "hourly"
        # Fallback to Study_day if no time-bin datetime is available.
        return [_study_day_numeric(row) for row in rows], "Study hour", "hourly"

    if freq == "monthly":
        dts = [_feature_row_datetime(row) for row in rows]
        valid = [dt for dt in dts if dt is not None]
        if valid:
            start_idx = min(dt.year * 12 + dt.month for dt in valid)
            return [((dt.year * 12 + dt.month) - start_idx) if dt is not None else None for dt in dts], "Study month", "monthly"
        return [_study_day_numeric(row) for row in rows], "Study month", "monthly"

    # Default daily behavior.
    study_days = [_safe_float(row.get("Study_day", row.get("study_day"))) for row in rows]
    if any(v is not None for v in study_days):
        return study_days, "Study day", "daily"
    dts = [_feature_row_datetime(row) for row in rows]
    valid = [dt for dt in dts if dt is not None]
    if valid:
        start_date = min(dt.date() for dt in valid)
        return [float((dt.date() - start_date).days) if dt is not None else None for dt in dts], "Study day", "daily"
    return [_study_day_numeric(row) for row in rows], "Study day", "daily"


def _temporal_axis_dtick(axis_title: str) -> int | None:
    if axis_title in {"Study day", "Study hour", "Study month"}:
        return 1
    return None



def _category_wide_columns(rows: list[dict]) -> list[str]:
    if not rows:
        return []
    id_cols = {
        "Subject_ID", "username", "Study_day", "study_day", "Date", "date", "time_bin", "temporal_frequency",
        "total_foreground_hours", "unique_apps", "unique_categories", "top_category", "top_category_hours", "records",
        "sensor", "cohort", "group", "group_label",
    }
    cols = list(dict.fromkeys(col for row in rows for col in row.keys()))
    return [col for col in cols if col not in id_cols and any(_safe_float(row.get(col)) is not None for row in rows)]


def _is_app_category_feature_table(rows: list[dict], feature_column: str | None = None) -> bool:
    if not rows:
        return False
    cols = set(rows[0].keys())
    if "app_category" in cols and ("category_hours" in cols or feature_column == "category_hours"):
        return True
    return APP_STATE.generated_feature_key == "application_usage_category_daily" and bool(_category_wide_columns(rows))


def _app_category_feature_plot_div(rows: list[dict], feature_column: str = "category_hours", plot_type: str = "bar") -> str:
    """Specialized visualization for application-usage category features.

    Supports both the previous wide format, with one column per category, and
    the newer long format, with app_category/category_hours columns.
    """
    if not rows:
        return "<p>No application-category rows are available for visualization.</p>"
    subject_col = _first_subject_col(rows)
    x_col = _first_time_col(rows) or "time_bin"
    subjects = sorted({str(row.get(subject_col, "Unknown")) for row in rows}) if subject_col else ["All subjects"]
    one_subject = len(subjects) == 1

    fig = go.Figure()
    if "app_category" in rows[0] and "category_hours" in rows[0]:
        grouped: dict[str, list[dict]] = {}
        for row in rows:
            category = str(row.get("app_category") or "Unknown")
            grouped.setdefault(category, []).append(row)
        series_items = grouped.items()
        for category, category_rows in sorted(series_items):
            ordered = sorted(category_rows, key=lambda r: (str(r.get(subject_col, "")) if subject_col else "", str(r.get(x_col, ""))))
            x_vals, y_vals, custom = [], [], []
            for row in ordered:
                x_raw = str(row.get(x_col, ""))
                subject = str(row.get(subject_col, "All subjects")) if subject_col else "All subjects"
                x_vals.append(x_raw if one_subject else f"{subject}<br>{x_raw}")
                y_vals.append(_safe_float(row.get("category_hours")) or 0)
                custom.append([subject, row.get("temporal_frequency", ""), row.get("records", "")])
            if plot_type == "line":
                fig.add_trace(go.Scatter(x=x_vals, y=y_vals, mode="lines+markers", name=category, customdata=custom, hovertemplate="Subject: %{customdata[0]}<br>Time: %{x}<br>Category: %{fullData.name}<br>Hours: %{y}<br>Records: %{customdata[2]}<extra></extra>"))
            else:
                fig.add_trace(go.Bar(x=x_vals, y=y_vals, name=category, customdata=custom, hovertemplate="Subject: %{customdata[0]}<br>Time: %{x}<br>Category: %{fullData.name}<br>Hours: %{y}<br>Records: %{customdata[2]}<extra></extra>"))
    else:
        category_cols = _category_wide_columns(rows)
        ordered_rows = sorted(rows, key=lambda r: (str(r.get(subject_col, "")) if subject_col else "", str(r.get(x_col, ""))))
        for category in category_cols:
            x_vals, y_vals, custom = [], [], []
            for row in ordered_rows:
                x_raw = str(row.get(x_col, ""))
                subject = str(row.get(subject_col, "All subjects")) if subject_col else "All subjects"
                x_vals.append(x_raw if one_subject else f"{subject}<br>{x_raw}")
                y_vals.append(_safe_float(row.get(category)) or 0)
                custom.append([subject, row.get("temporal_frequency", ""), row.get("records", "")])
            if plot_type == "line":
                fig.add_trace(go.Scatter(x=x_vals, y=y_vals, mode="lines+markers", name=category, customdata=custom, hovertemplate="Subject: %{customdata[0]}<br>Time: %{x}<br>Category: %{fullData.name}<br>Hours: %{y}<br>Records: %{customdata[2]}<extra></extra>"))
            else:
                fig.add_trace(go.Bar(x=x_vals, y=y_vals, name=category, customdata=custom, hovertemplate="Subject: %{customdata[0]}<br>Time: %{x}<br>Category: %{fullData.name}<br>Hours: %{y}<br>Records: %{customdata[2]}<extra></extra>"))

    title = "Application usage by category"
    freq = next((str(row.get("temporal_frequency")) for row in rows if row.get("temporal_frequency")), "")
    if freq:
        title += f" ({freq})"
    fig.update_layout(
        title=title,
        xaxis_title=_clean_feature_label(x_col),
        yaxis_title="Foreground time (hours)",
        template="plotly_white",
        barmode="stack" if plot_type != "line" else "group",
        legend_title="App category",
        height=620,
        margin=dict(l=70, r=30, t=80, b=120),
        hovermode="x unified" if plot_type != "line" else "closest",
    )
    fig.update_xaxes(showgrid=True, gridcolor="rgba(0,0,0,0.08)", tickangle=-35)
    fig.update_yaxes(showgrid=True, gridcolor="rgba(0,0,0,0.08)")
    return plot(fig, output_type="div", include_plotlyjs="cdn", config={"displayModeBar": True, "toImageButtonOptions": {"format": "png", "filename": "jtrack_app_category_usage", "height": 900, "width": 1500, "scale": 2}})




def _study_day_numeric(row: dict) -> float | None:
    """Return a numeric study-day value for longitudinal scientific plots."""
    for col in ("Study_day", "study_day"):
        value = _safe_float(row.get(col))
        if value is not None:
            return value
    for col in ("time_bin", "Date", "date"):
        raw = row.get(col)
        if raw in (None, ""):
            continue
        text = str(raw)
        # ISO-like hourly/daily bins are common in the Python app.
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d-%b-%Y", "%d-%B-%Y"):
            try:
                dt = datetime.strptime(text[:19] if "T" in text or len(text) >= 19 else text, fmt)
                return float(dt.toordinal())
            except Exception:
                continue
        value = _safe_float(text)
        if value is not None:
            return value
    return None


def _study_day_axis_values(rows: list[dict]) -> tuple[dict[int | float, str], bool]:
    """Map raw x-values to display labels. Returns (label_lookup, calendar_based)."""
    vals: list[float] = []
    calendar_based = False
    for row in rows:
        value = _study_day_numeric(row)
        if value is not None:
            vals.append(value)
            if value > 700000:  # ordinal date values
                calendar_based = True
    if not vals:
        return {}, False
    if calendar_based:
        ordered = sorted(set(vals))
        start = min(ordered)
        lookup = {v: f"{int(v - start)}" for v in ordered}
        return lookup, True
    return {v: str(int(v)) if float(v).is_integer() else f"{v:g}" for v in sorted(set(vals))}, False


def _mean_ci_trajectory_plot_div(rows: list[dict], feature_column: str, include_individual: bool = False) -> str:
    """Plot feature mean over the selected temporal scale with 95% confidence intervals."""
    if not rows:
        return "<p>No rows are available for mean trajectory visualization.</p>"
    if feature_column == "All" or not feature_column:
        numeric_cols = [c for c in _numeric_columns(rows) if not _is_identifier_or_metadata_column(c)]
        feature_column = numeric_cols[0] if numeric_cols else ""
    if not feature_column:
        return "<p>Please select a numeric feature for the longitudinal mean plot.</p>"

    subject_col = _first_subject_col(rows)
    group_col = _first_group_col(rows)
    x_values, x_axis_title, detected_freq = _temporal_axis_values(rows)

    prepared = []
    for row, x in zip(rows, x_values):
        y = _safe_float(row.get(feature_column))
        if y is None or x is None:
            continue
        prepared.append({
            "x": float(x),
            "y": y,
            "subject": str(row.get(subject_col, "Unknown")) if subject_col else "Unknown",
            "group": str(row.get(group_col, "All")) if group_col and row.get(group_col) not in (None, "") else "All",
        })
    if not prepared:
        return "<p>No numeric feature values with compatible temporal information are available for this plot.</p>"

    groups = sorted({p["group"] for p in prepared})
    fig = go.Figure()

    if include_individual:
        subject_groups: dict[tuple[str, str], list[dict]] = {}
        for pnt in prepared:
            subject_groups.setdefault((pnt["group"], pnt["subject"]), []).append(pnt)
        for (grp, subj), pts in sorted(subject_groups.items()):
            pts = sorted(pts, key=lambda p: p["x"])
            fig.add_trace(go.Scatter(
                x=[p["x"] for p in pts],
                y=[p["y"] for p in pts],
                mode="lines+markers",
                name=f"{subj}" if grp == "All" else f"{grp}: {subj}",
                line=dict(width=1),
                marker=dict(size=4),
                opacity=0.28,
                showlegend=False,
                hovertemplate=f"Subject: %{{fullData.name}}<br>{x_axis_title}: %{{x}}<br>Value: %{{y:.4g}}<extra></extra>",
            ))

    for grp in groups:
        pts = [pnt for pnt in prepared if pnt["group"] == grp]
        x_unique = sorted({pnt["x"] for pnt in pts})
        x_plot, mean_vals, lower_vals, upper_vals, n_vals = [], [], [], [], []
        for x in x_unique:
            vals = [pnt["y"] for pnt in pts if pnt["x"] == x and math.isfinite(pnt["y"])]
            if not vals:
                continue
            n = len(vals)
            mean = sum(vals) / n
            if n >= 2:
                sd = math.sqrt(sum((v - mean) ** 2 for v in vals) / (n - 1))
                ci = 1.96 * sd / math.sqrt(n)
            else:
                ci = 0.0
            x_plot.append(x)
            mean_vals.append(mean)
            lower_vals.append(mean - ci)
            upper_vals.append(mean + ci)
            n_vals.append(n)
        if not x_plot:
            continue
        trace_name = "Mean" if grp == "All" else f"{grp} mean"
        fig.add_trace(go.Scatter(x=x_plot, y=upper_vals, mode="lines", line=dict(width=0), showlegend=False, hoverinfo="skip", legendgroup=grp))
        fig.add_trace(go.Scatter(x=x_plot, y=lower_vals, mode="lines", fill="tonexty", line=dict(width=0), name=f"{trace_name} 95% CI", legendgroup=grp, hoverinfo="skip", opacity=0.22))
        fig.add_trace(go.Scatter(
            x=x_plot,
            y=mean_vals,
            mode="lines+markers",
            name=trace_name,
            legendgroup=grp,
            customdata=[[n] for n in n_vals],
            line=dict(width=3),
            marker=dict(size=7),
            hovertemplate=f"{x_axis_title}: %{{x}}<br>Mean: %{{y:.4g}}<br>N: %{{customdata[0]}}<extra></extra>",
        ))

    title_prefix = "Individual trajectories with mean ± 95% CI" if include_individual else "Mean trajectory with 95% CI"
    _apply_scientific_layout(fig, f"{title_prefix}: {_clean_feature_label(feature_column)}", x_axis_title, _clean_feature_label(feature_column), height=640)
    fig.update_layout(hovermode="x unified", legend_title="Summary")
    dtick = _temporal_axis_dtick(x_axis_title)
    if dtick:
        fig.update_xaxes(dtick=dtick)
    if detected_freq == "full_study":
        fig.update_xaxes(tickvals=[0], ticktext=["Full study"])
    return plot(fig, output_type="div", include_plotlyjs="cdn", config=_plotly_export_config("jtrack_mean_ci_trajectory", 950, 1500))


def _individual_trajectory_plot_div(rows: list[dict], feature_column: str) -> str:
    """Cleaner individual subject trajectories over study day."""
    return _feature_plot_div(rows, feature_column, "line")


def _linear_trend_plot_div(rows: list[dict], feature_column: str) -> str:
    """Temporal feature plot with ordinary least squares trend annotation."""
    if feature_column == "All" or not feature_column:
        numeric_cols = [c for c in _numeric_columns(rows) if not _is_identifier_or_metadata_column(c)]
        feature_column = numeric_cols[0] if numeric_cols else ""
    x_values, x_axis_title, detected_freq = _temporal_axis_values(rows)
    if detected_freq == "full_study":
        return "<p>Trend visualization is not meaningful for full-study summaries. Use a distribution or bar summary instead.</p>"
    prepared = []
    subject_col = _first_subject_col(rows)
    for row, x in zip(rows, x_values):
        y = _safe_float(row.get(feature_column))
        if x is None or y is None:
            continue
        prepared.append((float(x), float(y), str(row.get(subject_col, "Unknown")) if subject_col else "Unknown"))
    if len(prepared) < 3:
        return "<p>At least three numeric observations with compatible temporal information are required for trend visualization.</p>"
    xs = [p[0] for p in prepared]
    ys = [p[1] for p in prepared]
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    denom = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom if denom else 0.0
    intercept = my - slope * mx
    pred = [intercept + slope * x for x in xs]
    ss_res = sum((y - yh) ** 2 for y, yh in zip(ys, pred))
    ss_tot = sum((y - my) ** 2 for y in ys)
    r2 = 1 - ss_res / ss_tot if ss_tot else None
    fig = go.Figure()
    fig.add_trace(go.Scattergl(
        x=xs,
        y=ys,
        mode="markers",
        name="Observations",
        marker=dict(size=7, opacity=0.68),
        customdata=[[p[2]] for p in prepared],
        hovertemplate=f"Subject: %{{customdata[0]}}<br>{x_axis_title}: %{{x}}<br>Value: %{{y:.4g}}<extra></extra>",
    ))
    x_line = [min(xs), max(xs)]
    y_line = [intercept + slope * x for x in x_line]
    fig.add_trace(go.Scatter(x=x_line, y=y_line, mode="lines", name="Linear trend", line=dict(width=3)))
    note = f"Slope = {slope:.4g} per {x_axis_title.lower()}"
    if r2 is not None:
        note += f"<br>R² = {r2:.3f}"
    fig.add_annotation(text=note, xref="paper", yref="paper", x=0.02, y=0.98, showarrow=False, align="left", bgcolor="rgba(255,255,255,0.88)", bordercolor="rgba(31,41,55,0.22)")
    _apply_scientific_layout(fig, f"Longitudinal trend: {_clean_feature_label(feature_column)}", x_axis_title, _clean_feature_label(feature_column), height=640)
    dtick = _temporal_axis_dtick(x_axis_title)
    if dtick:
        fig.update_xaxes(dtick=dtick)
    return plot(fig, output_type="div", include_plotlyjs="cdn", config=_plotly_export_config("jtrack_feature_trend", 950, 1500))


def _feature_plot_div(rows: list[dict], feature_column: str, plot_type: str = "line", subject: str = "All", sensor_name: str | None = None) -> str:
    if not rows:
        return "<p>No rows are available for visualization.</p>"
    if feature_column == "All" or not feature_column:
        numeric_cols = [c for c in _numeric_columns(rows) if not _is_identifier_or_metadata_column(c)]
        feature_column = numeric_cols[0] if numeric_cols else ""
    if not feature_column:
        return "<p>No numeric feature column is available for visualization.</p>"

    if plot_type == "mean_ci":
        return _mean_ci_trajectory_plot_div(rows, feature_column, include_individual=False)
    if plot_type == "spaghetti_ci":
        return _mean_ci_trajectory_plot_div(rows, feature_column, include_individual=True)
    if plot_type == "trend":
        return _linear_trend_plot_div(rows, feature_column)
    if _is_app_category_feature_table(rows, feature_column):
        return _app_category_feature_plot_div(rows, feature_column, plot_type)
    if plot_type == "sensor_dashboard":
        return _sensor_dashboard_plot_div(rows, feature_column, subject=subject, sensor_name=sensor_name)
    if plot_type == "heatmap":
        return _heatmap_plot_div(rows, feature_column)
    if plot_type in {"box", "violin", "histogram", "distribution"}:
        mapped_plot_type = "box" if plot_type == "distribution" else plot_type
        return _distribution_plot_div(rows, feature_column, mapped_plot_type)

    x_col = _first_time_col(rows)
    subject_col = _first_subject_col(rows)
    group_col = _first_group_col(rows)
    fig = go.Figure()
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        subject = str(row.get(subject_col, "All subjects")) if subject_col else "All subjects"
        grouped.setdefault(subject, []).append(row)
    for subject, subject_rows in sorted(grouped.items()):
        ordered = sorted(subject_rows, key=lambda r: str(r.get(x_col, "")) if x_col else "")
        x_vals = [row.get(x_col, i) if x_col else i for i, row in enumerate(ordered)]
        y_vals = []
        custom = []
        for row in ordered:
            y_vals.append(_safe_float(row.get(feature_column)))
            custom.append([row.get("temporal_frequency", ""), row.get(group_col, "") if group_col else ""])
        hover = (
            "Subject: %{fullData.name}<br>"
            + (f"{html.escape(x_col or 'Row')}: %{{x}}<br>")
            + f"{html.escape(feature_column)}: %{{y}}<br>"
            + "Temporal frequency: %{customdata[0]}<br>"
            + "Group: %{customdata[1]}<extra></extra>"
        )
        if plot_type == "bar":
            fig.add_trace(go.Bar(x=x_vals, y=y_vals, name=subject, customdata=custom, hovertemplate=hover))
        elif plot_type == "scatter":
            fig.add_trace(go.Scatter(x=x_vals, y=y_vals, mode="markers", name=subject, customdata=custom, hovertemplate=hover))
        else:
            fig.add_trace(go.Scatter(x=x_vals, y=y_vals, mode="lines+markers", name=subject, customdata=custom, hovertemplate=hover))

    fig.update_layout(
        title=_feature_plot_title(feature_column, x_col, plot_type),
        xaxis_title=_clean_feature_label(x_col or "Row"),
        yaxis_title=_clean_feature_label(feature_column),
        template="plotly_white",
        legend_title="Participant",
        height=560,
        hovermode="x unified" if plot_type == "line" else "closest",
        margin=dict(l=60, r=30, t=80, b=60),
    )
    fig.update_xaxes(showgrid=True, gridcolor="rgba(0,0,0,0.08)")
    fig.update_yaxes(showgrid=True, gridcolor="rgba(0,0,0,0.08)")
    return plot(fig, output_type="div", include_plotlyjs="cdn", config={"displayModeBar": True, "toImageButtonOptions": {"format": "png", "filename": "jtrack_insight_plot", "height": 900, "width": 1400, "scale": 2}})


def _distribution_plot_div(rows: list[dict], feature_column: str, plot_type: str) -> str:
    subject_col = _first_subject_col(rows)
    group_col = _first_group_col(rows)
    category_col = group_col or subject_col
    fig = go.Figure()
    grouped: dict[str, list[float]] = {}
    for row in rows:
        value = _safe_float(row.get(feature_column))
        if value is None:
            continue
        category = str(row.get(category_col, "All data")) if category_col else "All data"
        grouped.setdefault(category, []).append(value)
    if not grouped:
        return "<p>No numeric values are available for this distribution plot.</p>"
    for category, values in sorted(grouped.items()):
        if plot_type == "violin":
            fig.add_trace(go.Violin(y=values, name=category, box_visible=True, meanline_visible=True, points="all", jitter=0.25))
        elif plot_type == "histogram":
            fig.add_trace(go.Histogram(x=values, name=category, opacity=0.72))
        else:
            fig.add_trace(go.Box(y=values, name=category, boxmean=True, points="all", jitter=0.25))
    fig.update_layout(
        title=f"Distribution of {_clean_feature_label(feature_column)}",
        xaxis_title=_clean_feature_label(category_col or "Group"),
        yaxis_title=_clean_feature_label(feature_column),
        template="plotly_white",
        height=560,
        margin=dict(l=60, r=30, t=80, b=80),
        barmode="overlay",
    )
    return plot(fig, output_type="div", include_plotlyjs="cdn", config={"displayModeBar": True, "toImageButtonOptions": {"format": "png", "filename": "jtrack_insight_distribution", "height": 900, "width": 1400, "scale": 2}})


def _heatmap_plot_div(rows: list[dict], feature_column: str) -> str:
    subject_col = _first_subject_col(rows)
    x_col = _first_time_col(rows)
    if not subject_col or not x_col:
        return _feature_plot_div(rows, feature_column, "line")
    subjects = sorted({str(row.get(subject_col, "Unknown")) for row in rows})
    x_vals = sorted({str(row.get(x_col, "")) for row in rows})
    lookup: dict[tuple[str, str], float | None] = {}
    for row in rows:
        lookup[(str(row.get(subject_col, "Unknown")), str(row.get(x_col, "")))] = _safe_float(row.get(feature_column))
    z = [[lookup.get((subject, x)) for x in x_vals] for subject in subjects]
    fig = go.Figure(data=go.Heatmap(x=x_vals, y=subjects, z=z, colorscale="Viridis", colorbar=dict(title=_clean_feature_label(feature_column))))
    fig.update_layout(
        title=f"Participant × time heatmap: {_clean_feature_label(feature_column)}",
        xaxis_title=_clean_feature_label(x_col),
        yaxis_title="Participant",
        template="plotly_white",
        height=max(420, min(900, 120 + 24 * len(subjects))),
        margin=dict(l=100, r=30, t=80, b=90),
    )
    return plot(fig, output_type="div", include_plotlyjs="cdn", config={"displayModeBar": True, "toImageButtonOptions": {"format": "png", "filename": "jtrack_insight_heatmap", "height": 900, "width": 1400, "scale": 2}})


def _raw_sensor_points_for_dashboard(subject: str = "All", sensor_name: str | None = None, feature_column: str = "All") -> list[dict]:
    sensor_name = sensor_name or APP_STATE.selected_sensor_name or "All"
    sensor_key = _norm(sensor_name)
    rows = _raw_rows_filtered(subject=subject, sensor_name=sensor_name, cohort="All")
    if not rows:
        return []
    preferred = [
        "value", "heartRate", "heart_rate", "bbi", "hrv", "stress", "spo2",
        "oxygenSaturation", "respiration", "respirationRate", "calories", "steps",
        "stepCount", "zeroCrossingCount", "totalEnergy", "timeAboveThreshold",
    ]
    out: list[dict] = []
    for row in rows:
        if sensor_key not in {"", "ALL"}:
            row_sensor = _norm(row.get("sensorname") or row.get("sensor_name") or row.get("sensor") or "")
            row_wearable = _norm(row.get("wearable_sensor") or "")
            if sensor_key not in {row_sensor, row_wearable} and sensor_key not in row_sensor and sensor_key not in row_wearable:
                continue
        dt = _row_datetime(row)
        if dt is None:
            continue
        val = None
        value_col = None
        if feature_column not in {"", "All", None}:
            val = _safe_float(row.get(feature_column))
            value_col = feature_column if val is not None else None
        else:
            for col in preferred + sorted(row.keys()):
                if _is_identifier_or_metadata_column(col):
                    continue
                if col in {"analysis_time_ms", "timestamp", "timestamp_start", "timestamp_end", "beginTimeStamp", "endTimeStamp", "lastTimeUsed", "startTime", "endTime", "study_day"}:
                    continue
                val = _safe_float(row.get(col))
                if val is not None:
                    value_col = col
                    break
        if val is None:
            continue
        out.append({
            "Subject_ID": _subject_label(row),
            "time": dt.isoformat(),
            "value": val,
            "value_col": value_col or "value",
            "sensor": sensor_name,
        })
    return sorted(out, key=lambda item: (item["Subject_ID"], item["time"]))


def _sensor_dashboard_plot_div(rows: list[dict], feature_column: str, subject: str = "All", sensor_name: str | None = None) -> str:
    raw_points = _raw_sensor_points_for_dashboard(subject=subject, sensor_name=sensor_name, feature_column=feature_column)
    if not raw_points:
        return _feature_plot_div(rows, feature_column, "line")
    fig = make_subplots(
        rows=2,
        cols=2,
        specs=[[{"colspan": 2}, None], [{}, {}]],
        subplot_titles=("Raw sensor time series", "Feature distribution", "Records / temporal coverage"),
        vertical_spacing=0.16,
        horizontal_spacing=0.1,
    )
    by_subject: dict[str, list[dict]] = {}
    for point in raw_points:
        by_subject.setdefault(point["Subject_ID"], []).append(point)
    for subject, pts in sorted(by_subject.items()):
        fig.add_trace(
            go.Scatter(
                x=[p["time"] for p in pts],
                y=[p["value"] for p in pts],
                mode="lines+markers",
                name=subject,
                customdata=[[p["value_col"]] for p in pts],
                hovertemplate="Subject: %{fullData.name}<br>Time: %{x}<br>Value: %{y}<br>Source field: %{customdata[0]}<extra></extra>",
            ),
            row=1,
            col=1,
        )
    # Feature distribution from aggregated rows.
    grouped_values: dict[str, list[float]] = {}
    subject_col = _first_subject_col(rows)
    for row in rows:
        value = _safe_float(row.get(feature_column))
        if value is None:
            continue
        subject = str(row.get(subject_col, "All subjects")) if subject_col else "All subjects"
        grouped_values.setdefault(subject, []).append(value)
    for subject, vals in sorted(grouped_values.items()):
        fig.add_trace(go.Box(y=vals, name=f"{subject} feature", boxmean=True, showlegend=False), row=2, col=1)
    # Coverage/records from generated rows.
    x_col = _first_time_col(rows)
    record_col = "records" if rows and "records" in rows[0] else None
    coverage_by_subject: dict[str, list[dict]] = {}
    for row in rows:
        subject = str(row.get(subject_col, "All subjects")) if subject_col else "All subjects"
        coverage_by_subject.setdefault(subject, []).append(row)
    for subject, srows in sorted(coverage_by_subject.items()):
        ordered = sorted(srows, key=lambda r: str(r.get(x_col, "")) if x_col else "")
        fig.add_trace(
            go.Bar(
                x=[r.get(x_col, i) if x_col else i for i, r in enumerate(ordered)],
                y=[_safe_float(r.get(record_col)) if record_col else 1 for r in ordered],
                name=f"{subject} records",
                showlegend=False,
            ),
            row=2,
            col=2,
        )
    fig.update_layout(
        title=f"Sensor diagnostic dashboard — {html.escape(APP_STATE.selected_sensor_name or 'selected sensor')}",
        template="plotly_white",
        height=820,
        hovermode="closest",
        legend_title="Participant",
        margin=dict(l=60, r=30, t=90, b=60),
    )
    fig.update_yaxes(title_text="Raw value", row=1, col=1)
    fig.update_yaxes(title_text=_clean_feature_label(feature_column), row=2, col=1)
    fig.update_yaxes(title_text="Records", row=2, col=2)
    fig.update_xaxes(title_text="Time", row=1, col=1)
    fig.update_xaxes(title_text="Participant", row=2, col=1)
    fig.update_xaxes(title_text=_clean_feature_label(x_col or "Time bin"), row=2, col=2)
    return plot(fig, output_type="div", include_plotlyjs="cdn", config={"displayModeBar": True, "toImageButtonOptions": {"format": "png", "filename": "jtrack_insight_sensor_dashboard", "height": 1000, "width": 1500, "scale": 2}})



def _is_application_usage_sensor(sensor_name: str | None) -> bool:
    key = _norm(sensor_name)
    return key in {"APPLICATION_USAGE", "USAGE", "APP_USAGE", "APPLICATIONUSAGE"} or "APPLICATION_USAGE" in key or key.endswith("USAGE")


def _app_category_tools_html(
    sensor_name: str | None = None,
    username: str = "All",
    feature_name: str = "all",
    temporal_frequency: str = "daily",
    feature_mode: str = "core",
) -> str:
    """Controls for the application-usage category mapping used by Step 3."""
    source = APP_STATE.app_category_source or "No category file loaded"
    count = len(APP_STATE.app_category_map or {})
    preview = _application_usage_category_preview_html()
    scope_summary = _application_usage_category_scope_summary()
    mapping_ready = count > 0
    status_label = "Ready" if mapping_ready else "Not loaded"
    status_class = "good" if mapping_ready else "warning"
    unknown_note = ""
    if scope_summary["unknown_apps"]:
        unknown_note = (
            "<p class='compact-note warning'>"
            f"{scope_summary['unknown_apps']} app(s) in the current loaded scope are still uncategorized. "
            f"Examples: {html.escape(', '.join(scope_summary['unknown_preview']))}"
            "</p>"
        )
    display_style = "" if _is_application_usage_sensor(sensor_name) else "display:none;"
    hidden = (
        f"<input type='hidden' class='step3-mapping-state' data-field='username' name='username' value='{html.escape(str(username))}' />"
        f"<input type='hidden' class='step3-mapping-state' data-field='sensor_name' name='sensor_name' value='{html.escape(str(sensor_name or 'All'))}' />"
        f"<input type='hidden' class='step3-mapping-state' data-field='feature_name' name='feature_name' value='{html.escape(str(feature_name))}' />"
        f"<input type='hidden' class='step3-mapping-state' data-field='temporal_frequency' name='temporal_frequency' value='{html.escape(str(temporal_frequency))}' />"
        f"<input type='hidden' class='step3-mapping-state' data-field='feature_mode' name='feature_mode' value='{html.escape(str(feature_mode))}' />"
    )
    return f"""
    <details class="card" id="app_category_tools" style="{display_style}" open>
      <summary style="cursor:pointer;font-size:1.35rem;font-weight:700;color:#14324d;">App category mapping</summary>
      <p class="muted">Use this only when you want app-category features such as social, communication, or entertainment usage. The easiest option is the default mapping file.</p>
      <div class="grid">
        <div class="metric {status_class}"><div class="metric-label">Status</div><div class="metric-value">{html.escape(status_label)}</div></div>
        <div class="metric"><div class="metric-label">Active mappings</div><div class="metric-value">{count}</div></div>
        <div class="metric"><div class="metric-label">Loaded apps in scope</div><div class="metric-value">{scope_summary['loaded_apps']}</div></div>
        <div class="metric"><div class="metric-label">Matched apps</div><div class="metric-value">{scope_summary['matched_apps']}</div></div>
      </div>
      <div class="compact-kv" style="margin-top:12px;">
        <div><strong>Recommended workflow:</strong> 1. Click <em>Use Default Mapping</em>. 2. Check the match preview. 3. Compute <em>App-category usage</em>.</div>
        <div><strong>Current source:</strong> <span style="word-break:break-word;">{html.escape(source)}</span></div>
      </div>
      {unknown_note}
      <div class="button-row" style="margin-top:12px;">
        <form action="/action/use_default_app_categories" method="get">
          {hidden}
          <button type="submit">Use Default Mapping</button>
        </form>
        <form action="/action/pick_app_categories" method="get">
          {hidden}
          <button type="submit" class="secondary">Choose Mapping File</button>
        </form>
      </div>
      {preview}
    </details>
    """

def _step3_page(username: str = "All", sensor_name: str = "All", feature_name: str = "all", temporal_frequency: str = "daily", feature_mode: str = "core") -> HTMLResponse:
    choices = available_filter_choices(APP_STATE.indexed_rows)
    sensor_choices = _combined_sensor_choices()
    if feature_mode not in {value for value, _ in FEATURE_MODE_CHOICES}:
        feature_mode = "core"
    metric_pairs = _feature_metric_choices_for_sensor(sensor_name, feature_mode)
    selected_features = _normalise_feature_names(feature_name.split(",") if isinstance(feature_name, str) else feature_name)
    metric_values = {value for value, _ in metric_pairs}
    if selected_features == ["all"]:
        selected_features = _default_feature_names_for_sensor(sensor_name, feature_mode)
    selected_features = [f for f in selected_features if f == "all" or f in metric_values] or _default_feature_names_for_sensor(sensor_name, feature_mode)
    if temporal_frequency not in {value for value, _ in TEMPORAL_FREQUENCY_CHOICES}:
        temporal_frequency = "daily"

    # Client-side map used to update Feature checkboxes immediately when the
    # user changes the sensor or feature mode. Each sensor has a small default
    # set so the first view is clinically useful rather than exhaustive.
    feature_choice_map = {
        mode: {
            sensor: {
                "choices": [{"value": value, "label": label} for value, label in _feature_metric_choices_for_sensor(sensor, mode)],
                "defaults": _default_feature_names_for_sensor(sensor, mode),
            }
            for sensor in ["All"] + sensor_choices
        }
        for mode, _ in FEATURE_MODE_CHOICES
    }
    feature_choice_json = json.dumps(feature_choice_map)

    current_rows = _generated_feature_rows(APP_STATE.generated_feature_key)
    computed_features = APP_STATE.generated_feature_name or "all"
    computed_frequency = APP_STATE.generated_temporal_frequency or "daily"
    computed_labels = []
    metric_lookup = dict(_feature_metric_choices_for_sensor(APP_STATE.selected_sensor_name or sensor_name, feature_mode))
    for item in _normalise_feature_names(computed_features.split(",") if isinstance(computed_features, str) else computed_features):
        computed_labels.append(metric_lookup.get(item, item))
    computed_text = (
        f"Current generated features: {', '.join(computed_labels)} / temporal frequency: {computed_frequency} ({len(current_rows)} rows)."
        if current_rows else "No feature set has been computed yet."
    )
    step3_matches = [
        row for row in APP_STATE.indexed_rows
        if (username == "All" or getattr(row, "username", "") == username)
        and (sensor_name == "All" or getattr(row, "sensorname", "") == sensor_name or getattr(row, "wearable_sensor", "") == sensor_name)
    ]
    selected_sensor_label = sensor_name if sensor_name != "All" else "All sensors"
    selected_subject_label = username if username != "All" else "All participants"
    feature_count_label = "All available" if selected_features == ["all"] else str(len(selected_features))
    body = f"""
    <div class="workbench">
      <div class="card control-card">
        <h2>Compute features</h2>
        <p class="compact-note">Choose only what is needed for the next analysis step. Feature names stay stable; temporal frequency is stored as a separate column.</p>
        <form action="/action/compute_feature" method="get" id="feature_compute_form"></form>
        <div class="card-section">
          <div class="section-title">1. Select data</div>
          <label>Participant</label>
          <select name="username" id="step3_username" form="feature_compute_form">{_select_options(choices["username"], username)}</select>
          <label>Sensor</label>
          <select name="sensor_name" id="step3_sensor" form="feature_compute_form">{_select_options(sensor_choices, sensor_name)}</select>
        </div>

        {_custom_feature_tools_html()}

        <div class="card-section">
          <div class="section-title">2. Select feature set</div>
          <label>Feature mode</label>
          <select name="feature_mode" id="step3_feature_mode" form="feature_compute_form">{_select_options_from_pairs(FEATURE_MODE_CHOICES, feature_mode)}</select>
          <p class="compact-note">Recommended features are selected automatically for each sensor. Switch mode only when you need QC or exploratory metrics.</p>
          <label>Feature(s)</label>
          {_checkbox_feature_options_from_pairs(metric_pairs, selected_features)}
        </div>

        <div class="card-section">
          <div class="section-title">3. Temporal aggregation</div>
          <label>Temporal frequency</label>
          <select name="temporal_frequency" id="step3_temporal_frequency" form="feature_compute_form">{_select_options_from_pairs(TEMPORAL_FREQUENCY_CHOICES, temporal_frequency)}</select>
          <div class="button-row" style="margin-top:12px;">
            <button type="submit" form="feature_compute_form">Compute selected features</button>
          </div>
        </div>
      </div>

      <div class="preview-card">
        <div class="card">
          <h2>Selection summary</h2>
          <div class="summary-chip-row">
            <span class="summary-chip">Participant: {html.escape(selected_subject_label)}</span>
            <span class="summary-chip">Sensor: {html.escape(selected_sensor_label)}</span>
            <span class="summary-chip">Files: {len(step3_matches)}</span>
            <span class="summary-chip">Features: {html.escape(feature_count_label)}</span>
          </div>
          <p class="compact-note">{html.escape(computed_text)}</p>
        </div>

        {_app_category_tools_html(sensor_name, username, feature_name, temporal_frequency, feature_mode)}

        <details class="clean-details" open>
          <summary>Generated feature preview</summary>
          {_table_preview(current_rows, limit=10) if current_rows else '<p>No generated feature table yet.</p>'}
        </details>
      </div>
    </div>
    <script>
      (function() {{
        const featureChoices = {feature_choice_json};
        const sensorSelect = document.getElementById('step3_sensor');
        const usernameSelect = document.getElementById('step3_username');
        const featureSelect = document.getElementById('step3_features');
        const modeSelect = document.getElementById('step3_feature_mode');
        const temporalSelect = document.getElementById('step3_temporal_frequency');
        function selectedFeatureNames() {{
          if (!featureSelect) return 'all';
          const picked = Array.from(featureSelect.querySelectorAll('input[name=\"feature_names\"]:checked'))
            .map(node => node.value)
            .filter(Boolean);
          return picked.length ? picked.join(',') : 'all';
        }}
        function syncCategoryForms() {{
          const state = {{
            username: usernameSelect && usernameSelect.value ? usernameSelect.value : 'All',
            sensor_name: sensorSelect && sensorSelect.value ? sensorSelect.value : 'All',
            feature_name: selectedFeatureNames(),
            temporal_frequency: temporalSelect && temporalSelect.value ? temporalSelect.value : 'daily',
            feature_mode: modeSelect && modeSelect.value ? modeSelect.value : 'core',
          }};
          document.querySelectorAll('.step3-mapping-state').forEach(function(node) {{
            const field = node.getAttribute('data-field');
            if (field && Object.prototype.hasOwnProperty.call(state, field)) {{
              node.value = state[field];
            }}
          }});
        }}
        function isApplicationUsageSensor(value) {{
          const key = String(value || '').trim().toUpperCase().replace(/[-\\s]+/g, '_');
          return key === 'APPLICATION_USAGE' || key === 'USAGE' || key === 'APP_USAGE' || key === 'APPLICATIONUSAGE' || key.indexOf('APPLICATION_USAGE') >= 0 || key.endsWith('USAGE');
        }}
        function refreshAppCategoryTools() {{
          const selectedSensor = sensorSelect && sensorSelect.value ? sensorSelect.value : 'All';
          const tools = document.getElementById('app_category_tools');
          if (tools) tools.style.display = isApplicationUsageSensor(selectedSensor) ? '' : 'none';
          syncCategoryForms();
        }}
        function refreshFeatureList() {{
          const selectedSensor = sensorSelect && sensorSelect.value ? sensorSelect.value : 'All';
          const selectedMode = modeSelect && modeSelect.value ? modeSelect.value : 'core';
          const byMode = featureChoices[selectedMode] || featureChoices['core'] || featureChoices;
          const bundle = byMode[selectedSensor] || byMode['All'] || {{choices: [], defaults: []}};
          const choices = bundle.choices || [];
          const defaults = new Set(bundle.defaults || []);
          featureSelect.innerHTML = '';
          choices.forEach(function(item) {{
            const label = document.createElement('label');
            label.className = 'feature-option';
            const checkbox = document.createElement('input');
            checkbox.type = 'checkbox';
            checkbox.name = 'feature_names';
            checkbox.value = item.value;
            checkbox.setAttribute('form', 'feature_compute_form');
            checkbox.checked = defaults.has(item.value) || (defaults.size === 0 && item.value !== 'all');
            const text = document.createElement('span');
            text.textContent = item.label;
            if (item.value === 'all') {{
              const small = document.createElement('small');
              small.textContent = 'Use only when you want every available metric for this sensor.';
              text.appendChild(small);
            }}
            label.appendChild(checkbox);
            label.appendChild(text);
            featureSelect.appendChild(label);
          }});
          refreshAppCategoryTools();
        }}
        if (sensorSelect && featureSelect) {{
          sensorSelect.addEventListener('change', refreshFeatureList);
          if (modeSelect) modeSelect.addEventListener('change', refreshFeatureList);
          if (usernameSelect) usernameSelect.addEventListener('change', syncCategoryForms);
          if (temporalSelect) temporalSelect.addEventListener('change', syncCategoryForms);
          featureSelect.addEventListener('change', syncCategoryForms);
          refreshAppCategoryTools();
          syncCategoryForms();
        }}
      }})();
    </script>
    """
    return _page_shell("step3", "Step 3  Feature Computation", body)

def _step3a_page(selected: dict[str, str] | None = None) -> HTMLResponse:
    selected = selected or {}
    return _step3_page(
        username=selected.get("username", "All"),
        sensor_name=selected.get("sensor_name") or selected.get("wearable_sensor", "All"),
        feature_mode=selected.get("feature_mode", "core"),
    )


def _step3b_page() -> HTMLResponse:
    return _step3_page()


def _step3c_page(min_day: str | None = None, max_day: str | None = None) -> HTMLResponse:
    return _step4_page(min_day=min_day, max_day=max_day)


def _roadmap_page() -> HTMLResponse:
    body = """
    <div class="card">
      <h2>Roadmap</h2>
      <ul>
        <li>First sensor-specific feature extraction path</li>
        <li>Save / resume project state</li>
        <li>Group analysis foundations</li>
        <li>Publication-style reports and export summaries</li>
      </ul>
    </div>
    """
    return _page_shell("roadmap", "Roadmap", body)




def _subject_value_from_row(row: dict) -> str:
    for col in ("Subject_ID", "username", "subject", "subject_id", "participant", "participant_id", "ID", "id"):
        value = row.get(col)
        if value not in (None, ""):
            return str(value)
    return "Unknown"


def _time_bin_value_from_row(row: dict) -> str:
    for col in ("Study_day", "study_day", "time_bin", "Date", "date", "analysis_date"):
        value = row.get(col)
        if value not in (None, ""):
            return str(value)
    return "single_bin"


def _study_day_number(row: dict) -> int | None:
    for col in ("Study_day", "study_day"):
        value = row.get(col)
        if value not in (None, ""):
            try:
                return int(float(str(value)))
            except (TypeError, ValueError):
                return None
    return None


def _int_or_none(value: object) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return None


def _float_or_none(value: object) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _qc_preset_hint(column: str) -> str:
    key = (column or "").lower()
    if column in ("All", ""):
        return "Select a numeric feature column to enable feature-specific plausibility checks."
    if "frequency" in key or "sampling_rate" in key:
        return "Computed data frequency: typical sampling interval reported as one compact label, for example 1 m, 10 m, or 15 m."
    if "step" in key:
        return "Step features: inspect unrealistic zero-only days and extreme daily/hourly step counts."
    if "heart" in key or key.startswith("hr_") or "_hr" in key:
        return "Heart-rate features: typical plausibility limits are commonly set around 30-220 bpm, depending on study protocol."
    if "bbi" in key:
        return "BBI features: inspect artifact burden and values outside physiologically plausible beat-to-beat intervals."
    if "hrv" in key or "rmssd" in key or "sdnn" in key or "pnn" in key:
        return "HRV features: apply minimum valid-record thresholds and inspect extreme autonomic values."
    if "spo2" in key or "oxygen" in key:
        return "SpO2 features: low-burden, minimum, and p05 values are usually more informative than means alone."
    if "distance" in key or "speed" in key or "location" in key or "gps" in key:
        return "Location features: check suspicious speeds, invalid coordinates, and low GPS coverage before interpretation."
    if "screen" in key or "unlock" in key or "app" in key or "category" in key:
        return "Phone-use features: inspect impossible usage durations and category/app concentration."
    if "stress" in key:
        return "Stress features: inspect high-stress burden, variability, and outlying peaks."
    return "Generic numeric feature: use study-specific lower/upper bounds or leave blank to keep all plausible values."


def _quality_control_decisions(
    rows: list[dict],
    qc_column: str = "All",
    min_day: str | int | None = "",
    max_day: str | int | None = "",
    min_records_per_subject: str | int | None = "",
    min_active_bins_per_subject: str | int | None = "",
    min_value: str | float | None = "",
    max_value: str | float | None = "",
) -> tuple[list[dict], list[dict], dict[str, object]]:
    """Apply participant/study inclusion and feature plausibility QC.

    Returns kept rows, audit rows with qc_status/qc_reason, and a compact summary.
    """
    min_day_n = _int_or_none(min_day)
    max_day_n = _int_or_none(max_day)
    min_records_n = _int_or_none(min_records_per_subject) or 0
    min_bins_n = _int_or_none(min_active_bins_per_subject) or 0
    min_value_n = _float_or_none(min_value)
    max_value_n = _float_or_none(max_value)

    day_scoped: list[dict] = []
    day_excluded_subjects: set[str] = set()
    for row in rows:
        subject = _subject_value_from_row(row)
        day = _study_day_number(row)
        if min_day_n is not None and day is not None and day < min_day_n:
            day_excluded_subjects.add(subject)
            continue
        if max_day_n is not None and day is not None and day > max_day_n:
            day_excluded_subjects.add(subject)
            continue
        day_scoped.append(row)

    subject_counts: dict[str, int] = {}
    subject_bins: dict[str, set[str]] = {}
    for row in day_scoped:
        subject = _subject_value_from_row(row)
        subject_counts[subject] = subject_counts.get(subject, 0) + 1
        subject_bins.setdefault(subject, set()).add(_time_bin_value_from_row(row))

    excluded_subjects: set[str] = set()
    for subject, count in subject_counts.items():
        if min_records_n and count < min_records_n:
            excluded_subjects.add(subject)
            continue
        if min_bins_n and len(subject_bins.get(subject, set())) < min_bins_n:
            excluded_subjects.add(subject)

    value_cols: list[str] = []
    if qc_column and qc_column != "All":
        value_cols = [qc_column]
    elif min_value_n is not None or max_value_n is not None:
        value_cols = [col for col in _numeric_columns(rows) if not _is_identifier_or_metadata_column(col)]

    kept: list[dict] = []
    audit: list[dict] = []
    for row in rows:
        reasons: list[str] = []
        subject = _subject_value_from_row(row)
        day = _study_day_number(row)
        if min_day_n is not None and day is not None and day < min_day_n:
            reasons.append("Outside minimum study-day range")
        if max_day_n is not None and day is not None and day > max_day_n:
            reasons.append("Outside maximum study-day range")
        if subject in excluded_subjects:
            if min_records_n and subject_counts.get(subject, 0) < min_records_n:
                reasons.append("Too few rows for participant")
            if min_bins_n and len(subject_bins.get(subject, set())) < min_bins_n:
                reasons.append("Too few active time bins for participant")
        for col in value_cols:
            if col not in row:
                continue
            value = _float_or_none(row.get(col))
            if value is None:
                continue
            if min_value_n is not None and value < min_value_n:
                reasons.append(f"{col} below minimum")
            if max_value_n is not None and value > max_value_n:
                reasons.append(f"{col} above maximum")
        out = dict(row)
        out["qc_status"] = "Exclude" if reasons else "Pass"
        out["qc_reason"] = "; ".join(reasons) if reasons else ""
        audit.append(out)
        if not reasons:
            kept.append(out)

    subjects_before = len({_subject_value_from_row(row) for row in rows}) if rows else 0
    subjects_after = len({_subject_value_from_row(row) for row in kept}) if kept else 0
    summary = {
        "rows_before": len(rows),
        "rows_after": len(kept),
        "rows_removed": max(0, len(rows) - len(kept)),
        "subjects_before": subjects_before,
        "subjects_after": subjects_after,
        "subjects_removed": max(0, subjects_before - subjects_after),
        "value_column": qc_column if qc_column != "All" else "None",
    }
    return kept, audit, summary


def _qc_result_cards(summary: dict[str, object]) -> str:
    return _summary_metrics({
        "Rows before QC": int(summary.get("rows_before") or 0),
        "Rows after QC": int(summary.get("rows_after") or 0),
        "Rows removed": int(summary.get("rows_removed") or 0),
        "Subjects retained": int(summary.get("subjects_after") or 0),
    })



def _normalise_frequency_value(value: str | None) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _feature_domain(feature_key: str, rows: list[dict]) -> str:
    """Return a compact domain label used to keep Step 5 plots relevant."""
    key = str(feature_key or "").lower()
    if feature_key == "raw_sensor_data":
        return "raw"
    if key == "application_usage_category_daily" or _is_app_category_feature_table(rows):
        return "application_category"
    if key == "application_usage_daily":
        return "application_usage"
    if key == "activity_features":
        return "activity"
    if key == "location_daily":
        return "location"
    if key == "pedometer_daily":
        return "pedometer"
    if key == "sensor_daily_summary":
        sensors = {_label_key(row.get("sensor") or row.get("selected_sensor") or row.get("sensorname") or row.get("wearable_sensor") or "") for row in rows[:200]}
        if any(x in sensors for x in {"BBI", "ENHANCEDBBI", "HRV"}):
            return "bbi_hrv"
        if any("HEARTRATE" in x or x == "HR" for x in sensors):
            return "heart_rate"
        if any("SPO2" in x or "PULSEOX" in x for x in sensors):
            return "spo2"
        return "sensor"
    if key == "custom_feature":
        return "custom"
    return "feature"


def _raw_context_for_visual_checks(rows: list[dict]) -> list[dict]:
    """Use selected rows plus same-subject context for availability checks only."""
    context = list(rows or [])
    try:
        broader = _subject_context_rows(rows) if rows else _raw_base_rows()
        if broader:
            # Keep order and avoid duplicates by object id fallback/key.
            seen: set[tuple] = set()
            merged: list[dict] = []
            for row in context + broader:
                key = (
                    _subject_label(row),
                    str(row.get("source_file", "")),
                    str(row.get("timestamp", row.get("time", ""))),
                    str(row.get("sensor_folder", "")),
                    str(row.get("file_name", "")),
                    id(row),
                )
                if key in seen:
                    continue
                seen.add(key)
                merged.append(row)
            context = merged
    except Exception:
        pass
    return context


def _has_bbi_comparison_streams(rows: list[dict]) -> bool:
    labels = {_label_key(_row_sensor_label(row)) for row in rows}
    has_bbi = any(label == "BBI" or label.endswith("BBI") for label in labels)
    has_counterpart = any(label in {"ENHANCEDBBI", "ENHANCED_BBI", "HRV"} or "HRV" in label for label in labels)
    return has_bbi and has_counterpart


def _has_location_points(rows: list[dict]) -> bool:
    try:
        loc_rows = _location_stream_rows(rows)
        if not loc_rows:
            return False
        points, _diag = _location_points_from_rows(loc_rows)
        return bool(points)
    except Exception:
        return False


def _has_activity_rows(rows: list[dict]) -> bool:
    try:
        return bool(_activity_rows_from_rows(rows))
    except Exception:
        return False


def _raw_visual_focus(feature_column: str | None, rows: list[dict]) -> str:
    """Infer the raw-data domain selected in Step 5.

    This prevents raw-only plot templates from leaking across domains, e.g.
    BBI comparison while the user is inspecting Android activity columns.
    """
    key = _label_key(feature_column or "")
    if not key or key == "ALL":
        return "mixed"

    activity_keys = {
        "ACTIVITYTYPE", "DETECTEDACTIVITY", "ACTIVITY", "ACTIVITYLABEL",
        "ACTIVITYCONFIDENCE", "CONFIDENCE", "COUNTER"
    }
    if key in activity_keys or "ACTIVITY" in key:
        return "activity"

    location_keys = {
        "LAT", "LATITUDE", "LATTITUDE", "LATE7", "LATITUDEE7",
        "LON", "LNG", "LONG", "LUN", "LONGITUDE", "LONGITUDEE7", "LONE7", "LNGE7"
    }
    if key in location_keys or "LATITUDE" in key or "LONGITUDE" in key or key.startswith("LOCATION"):
        return "location"

    if key in {"BBI", "IBI", "INTERBEATINTERVAL", "ENHANCEDBBI", "ENHANCED_BBI", "HRV", "RMSSD"} or "BBI" in key or "HRV" in key:
        return "bbi_hrv"

    if key in {"HEARTRATE", "HEART_RATE", "HR", "PULSE"} or "HEARTRATE" in key:
        return "heart_rate"

    # If the field is generic (for example `value`), infer from the selected
    # rows only when they clearly belong to one stream. Do not use the broader
    # subject context here, because that would reintroduce irrelevant plot types.
    if key in {"VALUE", "VALUETEXT", "RAWVALUE"}:
        labels = {_label_key(_row_sensor_label(row)) for row in rows if _row_sensor_label(row)}
        if labels:
            if all(label in {"ACTIVITY"} or "ACTIVITY" in label for label in labels):
                return "activity"
            if all(label in {"LOCATION", "GPS", "GEOLOCATION"} or "LOCATION" in label or "GPS" in label for label in labels):
                return "location"
            if all(label in {"BBI", "ENHANCEDBBI", "HRV"} or "BBI" in label or "HRV" in label for label in labels):
                return "bbi_hrv"

    return "generic"


def _compatible_raw_plot_pairs(filtered_rows: list[dict], subject: str = "All", feature_column: str = "All") -> list[tuple[str, str]]:
    """Only show raw plots that can plausibly run for the selected raw domain."""
    focus = _raw_visual_focus(feature_column, filtered_rows)
    context_rows = _raw_context_for_visual_checks(filtered_rows)
    pairs: list[tuple[str, str]] = [
        ("raw_panel", "Multisensor time-series panels"),
        ("raw_line", "Raw time-series line"),
        ("raw_scatter", "Raw time-series points"),
        ("raw_heatmap", "Day × hour coverage heatmap"),
        ("raw_distribution", "Raw value distribution"),
    ]

    has_location = _has_location_points(context_rows)
    has_activity = _has_activity_rows(context_rows)

    if focus in {"mixed", "bbi_hrv"} and _has_bbi_comparison_streams(context_rows):
        pairs.append(("raw_bbi_compare", "BBI vs Enhanced BBI / HRV comparison"))

    if focus in {"mixed", "location"} and has_location:
        pairs.append(("raw_location", "Location trajectory from lat/lon"))

    if focus in {"mixed", "activity", "location"} and has_location and has_activity:
        pairs.append(("raw_activity_location", "Activity recognition + location trajectory"))

    return pairs


def _compatible_generated_plot_pairs(feature_key: str, rows: list[dict], temporal_frequency: str, feature_column: str = "All") -> list[tuple[str, str]]:
    """Return plot templates relevant to the generated feature domain and temporal scale."""
    freq = _normalise_frequency_value(temporal_frequency if temporal_frequency != "All" else _infer_temporal_frequency(rows))
    full_freq = freq in {"full", "full_study", "study", "study_duration", "full_study_duration"}
    domain = _feature_domain(feature_key, rows)
    freq_label = (temporal_frequency if temporal_frequency != "All" else _infer_temporal_frequency(rows)).replace("_", " ").title()

    if domain == "application_category":
        if full_freq:
            return [
                ("bar", "Category usage summary"),
                ("box", "Category usage distribution"),
            ]
        return [
            ("bar", f"Stacked category usage by {freq_label.lower()}"),
            ("line", f"Category usage trajectories by {freq_label.lower()}"),
        ]

    if domain == "activity":
        if full_freq:
            return [
                ("bar", "Activity composition summary"),
                ("box", "Activity feature distribution"),
            ]
        return [
            ("mean_ci", f"Mean activity feature over {freq_label.lower()} with 95% CI"),
            ("spaghetti_ci", f"Individual activity trajectories + mean CI"),
            ("heatmap", f"Subject × {freq_label.lower()} activity heatmap"),
            ("box", "Activity feature distribution"),
        ]

    if domain in {"location", "pedometer", "application_usage"}:
        if full_freq:
            return [
                ("box", "Full-study distribution by subject/group"),
                ("bar", "Full-study bar summary"),
            ]
        return [
            ("mean_ci", f"Mean over {freq_label.lower()} bins with 95% CI"),
            ("spaghetti_ci", f"Individual trajectories + mean CI ({freq_label.lower()})"),
            ("trend", f"Temporal trend with regression summary ({freq_label.lower()})"),
            ("heatmap", f"Subject × {freq_label.lower()} heatmap"),
            ("box", "Distribution by subject/group"),
        ]

    if domain in {"bbi_hrv", "heart_rate", "spo2", "sensor"}:
        if full_freq:
            return [
                ("box", "Full-study distribution by subject/group"),
                ("bar", "Full-study bar summary"),
                ("sensor_dashboard", "Sensor diagnostic dashboard"),
            ]
        return [
            ("mean_ci", f"Mean over {freq_label.lower()} bins with 95% CI"),
            ("spaghetti_ci", f"Individual trajectories + mean CI ({freq_label.lower()})"),
            ("trend", f"Temporal trend with regression summary ({freq_label.lower()})"),
            ("heatmap", f"Subject × {freq_label.lower()} heatmap"),
            ("box", "Distribution by subject/group"),
            ("sensor_dashboard", "Sensor diagnostic dashboard"),
        ]

    # Custom and generic feature tables: keep conservative scientific templates.
    if full_freq:
        return [("box", "Full-study distribution"), ("bar", "Full-study bar summary")]
    return [
        ("mean_ci", f"Mean over {freq_label.lower()} bins with 95% CI"),
        ("spaghetti_ci", f"Individual trajectories + mean CI ({freq_label.lower()})"),
        ("trend", f"Temporal trend with regression summary ({freq_label.lower()})"),
        ("heatmap", f"Subject × {freq_label.lower()} heatmap"),
        ("box", "Distribution by subject/group"),
    ]


def _compatible_feature_columns(feature_key: str, rows: list[dict]) -> list[str]:
    """Numeric columns for Step 5, excluding metadata and non-features."""
    cols = [col for col in _numeric_columns(rows) if not _is_identifier_or_metadata_column(col)]
    if feature_key == "application_usage_category_daily" or _is_app_category_feature_table(rows):
        category_cols = _category_wide_columns(rows)
        # Keep summary columns first, then category feature columns.
        summary_cols = [col for col in ("total_foreground_hours", "unique_apps", "unique_categories") if col in cols]
        ordered = summary_cols + [col for col in category_cols if col not in summary_cols]
        return ordered or cols
    return cols

def _step4_page(
    min_day: str | None = "",
    max_day: str | None = "",
    feature_key: str = "All",
    qc_column: str = "All",
    min_value: str = "",
    max_value: str = "",
    min_records_per_subject: str = "",
    min_active_bins_per_subject: str = "",
) -> HTMLResponse:
    feature_keys = _all_generated_feature_keys()
    if feature_key == "All" and APP_STATE.generated_feature_key:
        feature_key = APP_STATE.generated_feature_key
    rows = _generated_feature_rows(feature_key, use_qc=False)
    numeric_cols = [col for col in _numeric_columns(rows) if not _is_identifier_or_metadata_column(col)]
    qc_pairs = [("All", "No feature-value filter"), *[(col, _clean_feature_label(col)) for col in numeric_cols]]
    feature_pairs = [(key, FEATURE_LABELS.get(key, key)) for key in feature_keys if key != "raw_sensor_data"] or [("All", "No generated feature yet")]

    kept_preview_rows = APP_STATE.feature_qc_rows or rows
    audit_rows = APP_STATE.feature_qc_audit_rows or []
    if APP_STATE.feature_qc_rows:
        summary = {
            "rows_before": len(audit_rows) if audit_rows else len(rows),
            "rows_after": len(APP_STATE.feature_qc_rows),
            "rows_removed": max(0, (len(audit_rows) if audit_rows else len(rows)) - len(APP_STATE.feature_qc_rows)),
            "subjects_after": len({_subject_value_from_row(row) for row in APP_STATE.feature_qc_rows}),
        }
        description = APP_STATE.feature_qc_description or "Quality-control filters have been applied."
    else:
        summary = {
            "rows_before": len(rows),
            "rows_after": len(rows),
            "rows_removed": 0,
            "subjects_after": len({_subject_value_from_row(row) for row in rows}) if rows else 0,
        }
        description = "No Step 4 QC filters have been applied yet."

    excluded_preview = [row for row in audit_rows if str(row.get("qc_status")) == "Exclude"]
    if excluded_preview:
        preview_title = "Excluded-row audit preview"
        preview_html = _table_preview(excluded_preview, limit=12)
    else:
        preview_title = "QC-filtered output preview"
        preview_html = _table_preview(kept_preview_rows, limit=12) if kept_preview_rows else "<div class='empty-state'>No generated feature rows are available yet. Compute features in Step 3.</div>"

    preset_hint = _qc_preset_hint(qc_column)
    body = f"""
    <div class="step-grid">
      <div class="card side-card">
        <h2>Quality Control and Data Inclusion</h2>
        <p class="compact-note">Select a generated feature table, define participant/study inclusion rules, and apply feature plausibility bounds only when needed.</p>
        <form action="/action/apply_quality_control" method="get">
          <div class="section-title">Feature table</div>
          <label class="inline-label">Generated feature table</label>
          <select name="feature_key">{_select_options_from_pairs(feature_pairs, feature_key)}</select>

          <div class="section-title">4A Participant / study inclusion</div>
          <div class="cols">
            <div><label class="inline-label">Min study day</label><input type="number" name="min_day" value="{html.escape(str(min_day or ''))}" placeholder="optional" /></div>
            <div><label class="inline-label">Max study day</label><input type="number" name="max_day" value="{html.escape(str(max_day or ''))}" placeholder="optional" /></div>
          </div>
          <div class="cols">
            <div><label class="inline-label">Min rows / subject</label><input type="number" name="min_records_per_subject" value="{html.escape(str(min_records_per_subject or ''))}" placeholder="optional" /></div>
            <div><label class="inline-label">Min active bins / subject</label><input type="number" name="min_active_bins_per_subject" value="{html.escape(str(min_active_bins_per_subject or ''))}" placeholder="optional" /></div>
          </div>

          <div class="section-title">4B Feature plausibility filtering</div>
          <label class="inline-label">Feature column</label>
          <select name="qc_column">{_select_options_from_pairs(qc_pairs, qc_column)}</select>
          <p class="compact-note">{html.escape(preset_hint)}</p>
          <div class="cols">
            <div><label class="inline-label">Minimum value</label><input type="number" step="any" name="min_value" value="{html.escape(min_value)}" placeholder="optional" /></div>
            <div><label class="inline-label">Maximum value</label><input type="number" step="any" name="max_value" value="{html.escape(max_value)}" placeholder="optional" /></div>
          </div>
          <button type="submit">Apply QC and Save Filtered Table</button>
        </form>
        <form action="/action/reset_quality_control" method="get" class="form-section">
          <button type="submit" class="secondary">Reset Step 4 QC</button>
        </form>
      </div>
      <div class="card preview-card">
        <h2>QC decision summary</h2>
        {_qc_result_cards(summary)}
        {_compact_kv([
            ("Applied QC", description),
            ("Value filter", qc_column if qc_column != "All" else "None"),
            ("Rows used by Steps 5-7", len(APP_STATE.feature_qc_rows) if APP_STATE.feature_qc_rows else len(rows)),
        ])}
        <details open>
          <summary>{html.escape(preview_title)}</summary>
          {preview_html}
        </details>
      </div>
    </div>
    """
    return _page_shell("step4", "Step 4  Quality Control and Data Inclusion", body)



def _step5_page(
    feature_key: str = "All",
    subject: str = "All",
    feature_column: str = "All",
    plot_type: str = "mean_ci",
    cohort: str = "All",
    sensor_name: str = "All",
    temporal_frequency: str = "All",
    feature_transform: str = "none",
    render_plot: str = "0",
) -> HTMLResponse:
    # Step 5 intentionally exposes a compact review/visualization filter set.
    # Cohort and sensor-level filtering are handled in dedicated workflow steps;
    # this page filters by data source, participant, feature/value field, and plot type.
    cohort = "All"
    sensor_name = "All"
    APP_STATE.generated_feature_transform = feature_transform if feature_transform in {value for value, _label in FEATURE_TRANSFORM_CHOICES} else "none"
    feature_keys = _all_generated_feature_keys()
    if feature_key == "All" and APP_STATE.generated_feature_key:
        feature_key = APP_STATE.generated_feature_key
    if feature_key == "All" and "raw_sensor_data" in feature_keys and not APP_STATE.generated_feature_key:
        feature_key = "raw_sensor_data"

    is_raw = feature_key == "raw_sensor_data"
    if is_raw:
        feature_transform = "none"
        if plot_type in {"line", "scatter", "bar"}:
            plot_type = {"line": "raw_panel", "scatter": "raw_scatter", "bar": "raw_bar"}.get(plot_type, plot_type)
        base_rows = _raw_rows_filtered(subject="All", sensor_name="All", cohort="All")
        subjects = _subject_values_from_rows(base_rows)
        if subject not in subjects:
            subject = "All"
        filtered_rows = _raw_rows_filtered(subject=subject, sensor_name="All", cohort="All")
        numeric_cols = _raw_numeric_columns(filtered_rows or base_rows)
        if feature_column != "All" and feature_column not in numeric_cols:
            feature_column = "All"
        plot_pairs = _compatible_raw_plot_pairs(filtered_rows or base_rows, subject=subject, feature_column=feature_column)
        valid_raw_plots = {value for value, _label in plot_pairs}
        if plot_type not in valid_raw_plots:
            plot_type = plot_pairs[0][0] if plot_pairs else "raw_panel"
        plot_div = _raw_plot_div(filtered_rows, feature_column, plot_type) if filtered_rows else "<p>No raw rows match the current filters.</p>"
    else:
        base_rows_all = _generated_feature_rows(feature_key, use_qc=True)
        frequency_values = _temporal_frequency_values(base_rows_all)
        if temporal_frequency not in frequency_values:
            temporal_frequency = frequency_values[0] if len(frequency_values) == 1 else "All"
        base_rows = _filter_rows_by_temporal_frequency(base_rows_all, temporal_frequency)
        numeric_cols = _compatible_feature_columns(feature_key, base_rows)
        subjects = _subject_values_from_rows(base_rows)
        if subject not in subjects:
            subject = "All"
        if feature_column != "All" and feature_column not in numeric_cols:
            feature_column = "All"
        filtered_rows = _filtered_feature_rows(feature_key, subject=subject, feature_column=feature_column, cohort="All")
        filtered_rows = _filter_rows_by_temporal_frequency(filtered_rows, temporal_frequency)

        plot_pairs = _compatible_generated_plot_pairs(feature_key, base_rows, temporal_frequency, feature_column)
        valid_generated_plots = {value for value, _label in plot_pairs}
        if plot_type not in valid_generated_plots or plot_type.startswith("raw_"):
            plot_type = plot_pairs[0][0] if plot_pairs else "mean_ci"
        plot_rows = _filtered_feature_rows(feature_key, subject=subject, cohort="All")
        plot_rows = _filter_rows_by_temporal_frequency(plot_rows, temporal_frequency)
        transformed_filtered_rows, transformed_column, transform_info = _analysis_rows_with_transform(filtered_rows, feature_column, feature_transform)
        transformed_plot_rows, _, _ = _analysis_rows_with_transform(plot_rows, feature_column, feature_transform)
        filtered_rows = transformed_filtered_rows
        plot_rows = transformed_plot_rows
        plot_div = _feature_plot_div(plot_rows, transformed_column, plot_type, subject=subject, sensor_name="All") if filtered_rows else "<p>No plot available for the current filters.</p>"
    if is_raw:
        transform_info = {"applied": False, "label": "Not available for raw data", "valid_rows": 0, "skipped_rows": 0}
        transformed_column = feature_column

    should_render_plot = str(render_plot or "0") == "1"
    if not should_render_plot:
        plot_div = "<p class='muted'>Select a plot type, then click <strong>Visualize plot</strong> to render the figure.</p>"

    feature_pairs = [(key, FEATURE_LABELS.get(key, key)) for key in feature_keys] or [("All", "No generated feature yet")]
    frequency_values = [] if is_raw else _temporal_frequency_values(_generated_feature_rows(feature_key, use_qc=True))
    frequency_pairs = [("All", "All frequencies"), *[(value, value.replace("_", " ").title()) for value in frequency_values]]
    column_pairs = [("All", "Auto-select numeric feature"), *[(col, _clean_feature_label(col)) for col in numeric_cols if not _is_identifier_or_metadata_column(col)]]
    # `plot_pairs` is already computed above from the selected data source,
    # temporal frequency, and feature domain.  This avoids presenting templates
    # that cannot run for the selected table/stream.
    transform_pairs = FEATURE_TRANSFORM_CHOICES
    if feature_transform not in {value for value, _label in transform_pairs}:
        feature_transform = "none"
    export_query = urlencode({
        "feature_key": feature_key,
        "subject": subject,
        "feature_column": feature_column,
        "temporal_frequency": temporal_frequency,
        "feature_transform": feature_transform,
    })
    data_label = "Raw data" if is_raw else "Generated feature table"
    body = f"""
    <div class="workbench">
      <div class="card control-card">
        <h2>Visualization setup</h2>
        <p class="compact-note">Choose a table, feature, and scientific plot template. Use raw data only for signal inspection.</p>
        <form action="/" method="get" id="step5VizForm">
          <input type="hidden" name="step" value="step5" />
          <label>Data source</label>
          <select name="feature_key">{_select_options_from_pairs(feature_pairs, feature_key)}</select>
          <label>Temporal frequency</label>
          <select name="temporal_frequency" {'disabled' if is_raw or not frequency_values else ''}>{_select_options_from_pairs(frequency_pairs, temporal_frequency)}</select>
          <label>Subject</label>
          <select name="username">{_select_options(subjects, subject)}</select>
          <label>Feature / raw value field</label>
          <select name="feature_column">{_select_options_from_pairs(column_pairs, feature_column)}</select>
          <label>Transformation</label>
          <select name="feature_transform" {'disabled' if is_raw else ''}>{_select_options_from_pairs(transform_pairs, feature_transform)}</select>
          <label>Plot type</label>
          <select name="plot_type">{_select_options_from_pairs(plot_pairs, plot_type)}</select>
          <div class="button-row" style="margin-top:12px;">
            <button type="submit" name="render_plot" value="1">Visualize plot</button>
          </div>
        </form>
        <form action="/export/generated_features.csv?{html.escape(export_query)}" method="get" style="margin-top:10px;">
          <button type="submit" class="button-secondary">Export filtered CSV</button>
        </form>
        <p class="compact-note">Rows after filters: {len(filtered_rows)}. Drop-downs refresh automatically so available fields and plot templates stay compatible with the selected data source.</p>
      </div>

      <div class="preview-card">
        <div class="card">
          <h2>Scientific visualization</h2>
          <div class="viz-note">
            <div class="viz-chip"><strong>Plot</strong>{html.escape(dict(plot_pairs).get(plot_type, plot_type))}</div>
            <div class="viz-chip"><strong>Data</strong>{html.escape(data_label)}</div>
            <div class="viz-chip"><strong>Subject</strong>{html.escape(subject)}</div>
            <div class="viz-chip"><strong>Frequency</strong>{html.escape('Raw' if is_raw else (temporal_frequency if temporal_frequency != 'All' else _infer_temporal_frequency(filtered_rows or base_rows)))}</div>
            <div class="viz-chip"><strong>Transformation</strong>{html.escape(transform_info.get('label', 'No transformation'))}</div>
          </div>
          <p class="compact-note">Selected feature values are transformed only in Step 5. Valid transformed rows: {transform_info.get('valid_rows', 0)}. Skipped rows: {transform_info.get('skipped_rows', 0)}.</p>
          <div class="plot-wrap">{plot_div}</div>
        </div>

        <details class="clean-details">
          <summary>Filtered data preview</summary>
          {_table_preview(filtered_rows, limit=20) if filtered_rows else '<p>No rows match the current filters.</p>'}
        </details>
      </div>
    </div>
    """
    return _page_shell("step5", "Step 5  Review / Export", body)

def _application_usage_plot_div() -> str:
    daily = APP_STATE.app_usage_daily
    category_daily = APP_STATE.app_usage_category_daily
    if not daily:
        return "<p>No application-usage visualization is available yet.</p>"

    fig = go.Figure()
    if category_daily:
        if category_daily and "category" in category_daily[0] and "foreground_hours" in category_daily[0]:
            grouped: dict[str, list[dict]] = {}
            for row in category_daily:
                grouped.setdefault(str(row.get("category", "Unknown")), []).append(row)
            for category, rows in sorted(grouped.items()):
                ordered = sorted(
                    rows,
                    key=lambda item: (
                        item.get("Study_day") if item.get("Study_day") is not None else 10**9,
                        str(item.get("Date", "")),
                    ),
                )
                fig.add_trace(
                    go.Bar(
                        x=[row.get("Date") for row in ordered],
                        y=[row.get("foreground_hours", 0) for row in ordered],
                        name=category,
                        hovertemplate=(
                            "Category: %{fullData.name}<br>"
                            "Date: %{x}<br>"
                            "Foreground hours: %{y}<extra></extra>"
                        ),
                    )
                )
        else:
            category_cols = _category_wide_columns(category_daily)
            ordered = sorted(category_daily, key=lambda item: (str(item.get("Subject_ID", "")), str(item.get("time_bin", item.get("Date", "")))))
            for category in category_cols:
                fig.add_trace(
                    go.Bar(
                        x=[row.get("time_bin") or row.get("Date") for row in ordered],
                        y=[row.get(category, 0) for row in ordered],
                        name=category,
                        hovertemplate=(
                            "Category: %{fullData.name}<br>"
                            "Time: %{x}<br>"
                            "Foreground hours: %{y}<extra></extra>"
                        ),
                    )
                )
        fig.update_layout(barmode="stack")
        title = "Daily foreground time by app category"
    else:
        grouped = {}
        for row in daily:
            grouped.setdefault(str(row["Subject_ID"]), []).append(row)
        for subject, rows in sorted(grouped.items()):
            ordered = sorted(
                rows,
                key=lambda item: (
                    item["Study_day"] if item["Study_day"] is not None else 10**9,
                    item["Date"],
                ),
            )
            fig.add_trace(
                go.Scatter(
                    x=[row["Date"] for row in ordered],
                    y=[row["total_foreground_hours"] for row in ordered],
                    mode="lines+markers",
                    name=subject,
                    customdata=[[row["unique_apps"], row["top_app"] or "Unknown"] for row in ordered],
                    hovertemplate=(
                        "Subject: %{fullData.name}<br>"
                        "Date: %{x}<br>"
                        "Foreground hours: %{y}<br>"
                        "Unique apps: %{customdata[0]}<br>"
                        "Top app: %{customdata[1]}<extra></extra>"
                    ),
                )
            )
        title = "Daily foreground time by participant"

    fig.update_layout(
        title=title,
        xaxis_title="Date",
        yaxis_title="Foreground hours",
        template="plotly_white",
        legend_title="Subject",
        height=460,
        margin=dict(l=40, r=20, t=70, b=40),
    )
    return plot(fig, output_type="div", include_plotlyjs="cdn", config={"displayModeBar": True})


def _location_plot_div() -> str:
    rows = APP_STATE.location_trajectory
    if not rows:
        return "<p>No location visualization is available yet.</p>"

    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(str(row["Subject_ID"]), []).append(row)

    fig = go.Figure()
    for subject, subject_rows in sorted(grouped.items()):
        ordered = sorted(subject_rows, key=lambda item: item["analysis_time_ms"] or 0)
        fig.add_trace(
            go.Scatter(
                x=[row["lon"] for row in ordered],
                y=[row["lat"] for row in ordered],
                mode="lines+markers",
                name=subject,
                customdata=[[row["Date"], row.get("accuracy"), row.get("provider") or ""] for row in ordered],
                hovertemplate=(
                    "Subject: %{fullData.name}<br>"
                    "Lon: %{x}<br>"
                    "Lat: %{y}<br>"
                    "Date: %{customdata[0]}<br>"
                    "Accuracy: %{customdata[1]}<br>"
                    "Provider: %{customdata[2]}<extra></extra>"
                ),
            )
        )

    fig.update_layout(
        title="Location trajectory",
        xaxis_title="Longitude",
        yaxis_title="Latitude",
        template="plotly_white",
        legend_title="Subject",
        height=520,
        margin=dict(l=40, r=20, t=70, b=40),
    )
    return plot(fig, output_type="div", include_plotlyjs="cdn", config={"displayModeBar": True})


def _pedometer_plot_div() -> str:
    rows = APP_STATE.pedometer_daily
    if not rows:
        return "<p>No pedometer visualization is available yet.</p>"

    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(str(row["Subject_ID"]), []).append(row)

    fig = go.Figure()
    for subject, subject_rows in sorted(grouped.items()):
        ordered = sorted(
            subject_rows,
            key=lambda item: (
                item["Study_day"] if item["Study_day"] is not None else 10**9,
                item["Date"],
            ),
        )
        fig.add_trace(
            go.Scatter(
                x=[row["Date"] for row in ordered],
                y=[row.get("steps", row.get("daily_steps")) for row in ordered],
                mode="lines+markers",
                name=subject,
                customdata=[[row["active_hours"], row["peak_hourly_steps"], row["timing_mode"]] for row in ordered],
                hovertemplate=(
                    "Subject: %{fullData.name}<br>"
                    "Date: %{x}<br>"
                    "Steps: %{y}<br>"
                    "Active hours: %{customdata[0]}<br>"
                    "Peak hourly steps: %{customdata[1]}<br>"
                    "Timing mode: %{customdata[2]}<extra></extra>"
                ),
            )
        )

    fig.update_layout(
        title="Daily pedometer steps by participant",
        xaxis_title="Date",
        yaxis_title="Steps",
        template="plotly_white",
        legend_title="Subject",
        height=460,
        margin=dict(l=40, r=20, t=70, b=40),
    )
    return plot(fig, output_type="div", include_plotlyjs="cdn", config={"displayModeBar": True})



def _report_summary_cards(rows: list[dict], feature_key: str) -> str:
    subjects = _subject_values_from_rows(rows)
    cohorts = _cohort_values_from_rows(rows)
    numeric_cols = [col for col in _numeric_columns(rows) if not _is_identifier_or_metadata_column(col)]
    temporal_values = sorted({str(row.get("temporal_frequency")) for row in rows if row.get("temporal_frequency") not in (None, "")})
    metrics = {
        "Feature table": FEATURE_LABELS.get(feature_key, feature_key if feature_key != "All" else "Generated features"),
        "Rows": len(rows),
        "Subjects": len(subjects),
        "Cohorts": len(cohorts),
        "Numeric features": len(numeric_cols),
        "Temporal frequency": ", ".join(temporal_values) if temporal_values else "Not specified",
    }
    return _summary_metrics(metrics)


def _report_numeric_summary(rows: list[dict], max_cols: int = 12) -> list[dict]:
    out: list[dict] = []
    for col in _numeric_columns(rows):
        if _is_identifier_or_metadata_column(col):
            continue
        values: list[float] = []
        for row in rows:
            try:
                val = row.get(col)
                if val not in (None, ""):
                    values.append(float(val))
            except (TypeError, ValueError):
                continue
        if not values:
            continue
        values_sorted = sorted(values)
        n = len(values_sorted)
        median = values_sorted[n // 2] if n % 2 == 1 else (values_sorted[n // 2 - 1] + values_sorted[n // 2]) / 2
        out.append({
            "feature": col,
            "n": n,
            "mean": round(sum(values_sorted) / n, 4),
            "median": round(median, 4),
            "min": round(values_sorted[0], 4),
            "max": round(values_sorted[-1], 4),
        })
        if len(out) >= max_cols:
            break
    return out


def _report_cohort_summary(rows: list[dict]) -> list[dict]:
    cohorts: dict[str, list[dict]] = {}
    for row in rows:
        label = None
        for col in ("group_label", "cohort", "group", "condition"):
            if row.get(col) not in (None, ""):
                label = str(row.get(col))
                break
        if label:
            cohorts.setdefault(label, []).append(row)
    out = []
    for cohort, cohort_rows in sorted(cohorts.items()):
        out.append({
            "cohort/group": cohort,
            "rows": len(cohort_rows),
            "subjects": len(_subject_values_from_rows(cohort_rows)),
            "numeric_features": len([c for c in _numeric_columns(cohort_rows) if not _is_identifier_or_metadata_column(c)]),
        })
    return out


def _report_stage_card(label: str, value: object, note: str) -> str:
    return f"""
    <div class="report-stage-card">
      <div class="report-stage-label">{html.escape(str(label))}</div>
      <div class="report-stage-value">{html.escape(str(value))}</div>
      <div class="report-stage-note">{html.escape(str(note))}</div>
    </div>
    """


def _report_key_list(items: list[tuple[str, object]]) -> str:
    lis = "".join(
        f"<li><strong>{html.escape(str(label))}: </strong>{html.escape(str(value))}</li>"
        for label, value in items
    )
    return f"<ul class='report-key-list'>{lis}</ul>"


def _build_report_html(
    feature_key: str = "All",
    subject: str = "All",
    feature_column: str = "All",
    cohort: str = "All",
    feature_transform: str = "none",
    for_download: bool = False,
) -> str:
    if feature_key == "All" and APP_STATE.generated_feature_key:
        feature_key = APP_STATE.generated_feature_key
    rows = _filtered_feature_rows(feature_key, subject=subject, feature_column=feature_column, cohort=cohort)
    rows, feature_column_used, transform_info = _analysis_rows_with_transform(rows, feature_column, feature_transform)
    all_rows = _generated_feature_rows(feature_key, use_qc=True)
    created_text = html.escape(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"))
    feature_label = FEATURE_LABELS.get(feature_key, feature_key if feature_key != "All" else "Generated features")

    indexed_subjects = len({getattr(row, "username", "") for row in APP_STATE.indexed_rows if getattr(row, "username", "")})
    indexed_files = len({getattr(row, "source_file", "") for row in APP_STATE.indexed_rows if getattr(row, "source_file", "")})
    indexed_sensors = len({getattr(row, "sensorname", "") for row in APP_STATE.indexed_rows if getattr(row, "sensorname", "")})
    flagged_files = sum(1 for row in APP_STATE.qc_rows if getattr(row, "qc_status", "") == "flagged")
    qc_ready_files = max(indexed_files - flagged_files, 0)

    subjects = _subject_values_from_rows(rows)
    cohorts = _cohort_values_from_rows(rows)
    numeric_cols = [col for col in _numeric_columns(rows) if not _is_identifier_or_metadata_column(col)]
    temporal_values = sorted({str(row.get("temporal_frequency")) for row in rows if row.get("temporal_frequency") not in (None, "")})
    total_feature_rows = len(all_rows)

    stage_cards = f"""
    <div class="report-stage-grid">
      {_report_stage_card("Indexed sample", indexed_subjects, f"{indexed_files} JSON files across {indexed_sensors} sensor streams")}
      {_report_stage_card("After file QC", qc_ready_files, f"{flagged_files} files flagged by JSON/duplicate checks")}
      {_report_stage_card("Generated features", total_feature_rows, f"{feature_label}")}
      {_report_stage_card("Report scope", len(rows), f"{len(subjects)} subjects; {len(cohorts)} cohorts/groups")}
      {_report_stage_card("Numeric features", len(numeric_cols), ", ".join(temporal_values) if temporal_values else "Temporal level not specified")}
    </div>
    """

    filters_list = _report_key_list([
        ("Feature table", feature_label),
        ("Subject filter", subject),
        ("Feature column filter", feature_column_used),
        ("Transformation", transform_info.get("label", "No transformation")),
        ("Cohort/group filter", _group_filter_label(cohort)),
        ("Current status", APP_STATE.status or "NA"),
    ])
    qc_list = _report_key_list([
        ("File QC rows", len(APP_STATE.qc_rows)),
        ("Flagged files", flagged_files),
        ("QC-ready files", qc_ready_files),
        ("Feature-level QC", APP_STATE.feature_qc_description or "No feature-level QC filter has been applied yet"),
    ])
    feature_list = _report_key_list([
        ("Rows in selected scope", len(rows)),
        ("Subjects in selected scope", len(subjects)),
        ("Cohort/group labels", ", ".join(cohorts) if cohorts else "No cohort/group labels available"),
        ("Temporal frequency", ", ".join(temporal_values) if temporal_values else "Not specified"),
    ])

    numeric_summary = _report_numeric_summary(rows) if rows else []
    cohort_summary = _report_cohort_summary(rows) if rows else []
    numeric_table = _table_preview(numeric_summary, limit=20) if numeric_summary else "<p class='muted'>No numeric feature summary is available for this report.</p>"
    cohort_table = _table_preview(cohort_summary, limit=20) if cohort_summary else "<p class='muted'>No cohort/group labels are available for this report.</p>"
    preview_table = _table_preview(rows, limit=30) if rows else "<p>No rows available.</p>"

    style = ""
    if for_download:
        style = """
        <style>
          body { font-family: Arial, sans-serif; color: #213547; margin: 28px; }
          h1, h2 { color: #14324d; }
          .muted { color: #667085; }
          .report-stage-grid { display: grid; grid-template-columns: repeat(3, minmax(160px, 1fr)); gap: 12px; margin: 16px 0; }
          .report-stage-card { border: 1px solid #d9e5ef; border-radius: 10px; padding: 12px; background: #f8fbff; }
          .report-stage-label { color: #667085; font-size: 12px; text-transform: uppercase; }
          .report-stage-value { font-size: 22px; font-weight: 700; margin-top: 6px; color: #153650; }
          .report-stage-note { color: #5b6775; font-size: 12px; margin-top: 6px; }
          .report-section-title { font-weight: 700; color: #17324d; margin-top: 18px; margin-bottom: 8px; }
          .report-key-list { margin: 0 0 12px 18px; }
          .report-note { background: #f5f9fc; border-left: 4px solid #2c7fb8; padding: 10px 12px; margin: 10px 0 14px 0; border-radius: 6px; }
          .table-scroll { max-height: 520px; overflow: auto; border: 1px solid #d9e5ef; border-radius: 10px; }
          table { border-collapse: collapse; width: 100%; font-size: 13px; }
          th, td { border-bottom: 1px solid #e5edf5; padding: 8px; text-align: left; }
          th { background: #eaf1f8; }
        </style>
        """

    return f"""
    {style}
    <section class="report">
      <h1>JTrack Insight Analysis Report</h1>
      <p class="muted">Generated: {created_text}</p>
      <div class="report-note">This report summarizes the selected workflow scope, file-level QC, feature-level QC, generated feature table, and cohort/group availability. It is intended as an audit trail for review and export.</div>
      {stage_cards}
      <div class="report-section-title">Selected report filters</div>
      {filters_list}
      <div class="report-section-title">Quality-control summary</div>
      {qc_list}
      <div class="report-section-title">Generated feature summary</div>
      {feature_list}
      <div class="report-section-title">Numeric feature summary</div>
      {numeric_table}
      <div class="report-section-title">Cohort/group summary</div>
      {cohort_table}
      <div class="report-section-title">Data preview</div>
      {preview_table}
    </section>
    """


def _step6_page(
    feature_key: str = "All",
    subject: str = "All",
    feature_column: str = "All",
    plot_type: str = "line",
    cohort: str = "All",
    feature_transform: str = "none",
) -> HTMLResponse:
    feature_keys = _all_generated_feature_keys()
    if feature_key == "All" and APP_STATE.generated_feature_key:
        feature_key = APP_STATE.generated_feature_key
    if feature_transform == "none" and APP_STATE.generated_feature_transform != "none":
        feature_transform = APP_STATE.generated_feature_transform
    rows = _generated_feature_rows(feature_key, use_qc=True)
    subjects = _subject_values_from_rows(rows)
    cohorts = _cohort_values_from_rows(rows)
    numeric_cols = _numeric_columns(rows)
    feature_pairs = [(key, FEATURE_LABELS.get(key, key)) for key in feature_keys] or [("All", "No generated feature yet")]
    column_pairs = [("All", "All columns"), *[(col, _clean_feature_label(col)) for col in numeric_cols if not _is_identifier_or_metadata_column(col)]]
    transform_pairs = FEATURE_TRANSFORM_CHOICES
    report_query = urlencode({
        "feature_key": feature_key,
        "subject": subject,
        "feature_column": feature_column,
        "feature_transform": feature_transform,
        "cohort": _group_filter_label(cohort),
    })
    report_html = _build_report_html(
        feature_key=feature_key,
        subject=subject,
        feature_column=feature_column,
        cohort=cohort,
        feature_transform=feature_transform,
        for_download=False,
    )
    body = f"""
    <div class="step-grid">
      <div class="card side-card">
        <h2>Report scope</h2>
        <form action="/" method="get">
          <input type="hidden" name="step" value="step6" />
          <label class="inline-label">Feature table</label>
          <select name="feature_key">{_select_options_from_pairs(feature_pairs, feature_key)}</select>
          <label class="inline-label">Subject</label>
          <select name="username">{_select_options(subjects, subject)}</select>
          <label class="inline-label">Feature column</label>
          <select name="feature_column">{_select_options_from_pairs(column_pairs, feature_column)}</select>
          <label class="inline-label">Transformation</label>
          <select name="feature_transform">{_select_options_from_pairs(transform_pairs, feature_transform)}</select>
          <label class="inline-label">Cohort / group</label>
          <select name="cohort">{_select_options(cohorts, _group_filter_label(cohort))}</select>
          <button type="submit">Generate report</button>
        </form>
        <div class="mini-actions">
          <form action="/export/report.html?{html.escape(report_query)}" method="get">
            <button type="submit">Download HTML</button>
          </form>
        </div>
      </div>
      <div class="card preview-card">
        {report_html}
      </div>
    </div>
    """
    return _page_shell("step6", "Step 6  Reports", body)



def _choose_folder_native(prompt: str = "Choose dataset root") -> str | None:
    """Open a native folder picker on macOS and return the selected path."""
    if platform.system() != "Darwin":
        return None

    script = 'POSIX path of (choose folder with prompt "' + prompt.replace('"', '\\"') + '")'
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None

    selected = result.stdout.strip()
    return selected or None


def _choose_file_native(prompt: str) -> str | None:
    """Open a native file picker on macOS and return the selected path."""
    if platform.system() != "Darwin":
        return None

    script = (
        'POSIX path of (choose file with prompt "'
        + prompt.replace('"', '\\"')
        + '")'
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None

    selected = result.stdout.strip()
    return selected or None


def _indexed_summary_text() -> str:
    if not APP_STATE.indexed_rows:
        return "No dataset indexed yet."
    counts = summarize_indexed_files(APP_STATE.indexed_rows)
    return (
        f"Indexed {counts['json_files']} JSON files across {counts['subjects']} subjects, "
        f"{counts['devices']} devices, and {counts['sensors']} sensor streams."
    )


def _scope_summary_text() -> str:
    if not APP_STATE.filtered_rows:
        return "No metadata filters have been applied yet."
    counts = summarize_indexed_files(APP_STATE.filtered_rows)
    return (
        f"Scoped metadata preview: {counts['json_files']} JSON files, "
        f"{counts['subjects']} subjects, {counts['devices']} devices, and "
        f"{counts['sensors']} sensor streams remain after filtering."
    )


def _loaded_summary_text() -> str:
    if not APP_STATE.loaded_rows:
        return "No scoped JSON data have been loaded yet."
    summary = summarize_loaded_rows(APP_STATE.loaded_rows)
    return (
        f"Loaded scoped data preview: {summary.files_loaded} files, {summary.records_loaded} records, "
        f"{summary.subjects} subjects, {summary.devices} devices, and {summary.sensors} sensor streams."
    )


def _loaded_columns_preview() -> str:
    if not APP_STATE.loaded_rows:
        return "Loaded column preview will appear here."
    summary = summarize_loaded_rows(APP_STATE.loaded_rows)
    preview_cols = summary.columns[:30]
    more_note = "" if len(summary.columns) <= 30 else f"\n... and {len(summary.columns) - 30} more columns."
    return "Detected columns:\n" + "\n".join(preview_cols) + more_note


def _loaded_filter_summary_text() -> str:
    if not APP_STATE.loaded_rows:
        return "No loaded-data filters have been applied yet."
    summary = summarize_loaded_rows(APP_STATE.loaded_filtered_rows or APP_STATE.loaded_rows)
    return (
        f"Loaded-data filter preview: {summary.records_loaded} records, {summary.subjects} subjects, "
        f"{summary.devices} devices, and {summary.distinct_study_days} study days remain in the selected range."
    )


def _analysis_mode() -> str:
    sensor = (APP_STATE.selected_sensor_name or "").strip().lower()
    wearable = (APP_STATE.selected_wearable_sensor or "").strip().lower()
    if sensor in {"location", "gps", "geolocation"} or wearable in {"location", "gps", "geolocation"}:
        return "location"
    if sensor in {"pedometer", "steps"} or wearable in {"pedometer", "steps"}:
        return "pedometer"
    rows = APP_STATE.loaded_filtered_rows or APP_STATE.loaded_rows
    sensor_names = {
        str(row.get("sensorname") or "").strip().lower()
        for row in rows
        if str(row.get("sensorname") or "").strip()
    }
    if sensor_names and sensor_names <= {"location", "gps", "geolocation"}:
        return "location"
    if sensor_names and sensor_names <= {"pedometer", "steps"}:
        return "pedometer"
    return "application_usage"


def _application_usage_category_preview_html() -> str:
    rows = APP_STATE.loaded_filtered_rows or APP_STATE.loaded_rows
    if not rows or not APP_STATE.app_category_map:
        return "<p class='muted'>A category-match preview will appear here after data are loaded and a category file is active.</p>"

    seen = set()
    preview_rows = []
    for row in rows:
        sensor_name = str(row.get("sensorname") or "").strip().upper()
        wearable = str(row.get("wearable_sensor") or "").strip().upper()
        if sensor_name != "APPLICATION_USAGE" and wearable != "APPLICATION_USAGE":
            continue
        app_name = str(row.get("appName") or "").strip()
        if not app_name:
            continue
        key = app_name.casefold()
        if key in seen:
            continue
        seen.add(key)
        preview_rows.append(
            {
                "appName": app_name,
                "category": APP_STATE.app_category_map.get(key, "Unknown"),
            }
        )
        if len(preview_rows) >= 12:
            break

    if not preview_rows:
        return "<p class='muted'>No application-usage app names are currently loaded for preview.</p>"

    rows_html = "\n".join(
        f"<tr><td>{html.escape(item['appName'])}</td><td>{html.escape(item['category'])}</td></tr>"
        for item in preview_rows
    )
    return f"""
    <p class="muted">Preview of current app-to-category matches in the loaded scope:</p>
    <div class="table-scroll">
      <table>
        <thead><tr><th>App</th><th>Category</th></tr></thead>
        <tbody>{rows_html}</tbody>
      </table>
    </div>

    """


def _application_usage_category_scope_summary() -> dict[str, object]:
    rows = APP_STATE.loaded_filtered_rows or APP_STATE.loaded_rows
    app_names: list[str] = []
    seen: set[str] = set()
    for row in rows:
        sensor_name = str(row.get("sensorname") or "").strip().upper()
        wearable = str(row.get("wearable_sensor") or "").strip().upper()
        if sensor_name != "APPLICATION_USAGE" and wearable != "APPLICATION_USAGE":
            continue
        app_name = str(row.get("appName") or "").strip()
        if not app_name:
            continue
        key = app_name.casefold()
        if key in seen:
            continue
        seen.add(key)
        app_names.append(app_name)
    matched = [name for name in app_names if name.casefold() in APP_STATE.app_category_map]
    unknown = [name for name in app_names if name.casefold() not in APP_STATE.app_category_map]
    return {
        "loaded_apps": len(app_names),
        "matched_apps": len(matched),
        "unknown_apps": len(unknown),
        "unknown_preview": unknown[:8],
    }


def _step4_location_page() -> HTMLResponse:
    review = APP_STATE.location_review
    if review:
        metrics = _summary_metrics(
            {
                "Records": int(review["records"]),
                "Subjects": int(review["subjects"]),
                "Days": int(review["distinct_days"]),
                "Providers": int(review["providers"]),
                "Total km": review["total_distance_km"],
                "Mean daily km": review["mean_daily_distance_km"],
                "Median accuracy (m)": review["median_accuracy_m"],
            }
        )
        detail_text = f"Maximum daily distance: {review['max_daily_distance_km']} km."
    else:
        metrics = "<p>No location review has been run yet.</p>"
        detail_text = "Load filtered location rows and run the review first."
    body = f"""
    <div class="card">
      <h2>Location review</h2>
      <p class="muted">Second end-to-end Python analysis path, focused on daily movement distance and trajectory quality.</p>
      <form action="/action/run_location_review" method="get">
        <button type="submit">Run Location Review</button>
      </form>
      {metrics}
      <p>{html.escape(detail_text)}</p>
    </div>
    """
    return _page_shell("step4", "Step 4  Review", body)


def _step5_location_page() -> HTMLResponse:
    daily = APP_STATE.location_daily
    if not daily:
        table_html = "<p>No daily location feature table is available yet.</p>"
    else:
        table_html = _table_preview(daily, limit=20)
    body = f"""
    <div class="card">
      <h2>Daily location feature table</h2>
      <p class="muted">Python Step 5 preview for location, centered on daily movement distance and point quality.</p>
      <form action="/export/location_daily.csv" method="get">
        <button type="submit">Download Daily Location CSV</button>
      </form>
      {table_html}
    </div>
    """
    return _page_shell("step5", "Step 5  Features", body)


def _step4_pedometer_page() -> HTMLResponse:
    review = APP_STATE.pedometer_review
    if review:
        metrics = _summary_metrics(
            {
                "Records": int(review["records"]),
                "Subjects": int(review["subjects"]),
                "Days": int(review["distinct_days"]),
                "Total steps": int(review["total_steps"]),
                "Mean daily steps": review["mean_daily_steps"],
                "Median daily steps": review["median_daily_steps"],
                "Peak daily steps": int(review["peak_daily_steps"]),
            }
        )
        detail_text = f"Days meeting 10,000 steps: {review['goal_days_10000']}."
    else:
        metrics = "<p>No pedometer review has been run yet.</p>"
        detail_text = "Load filtered pedometer rows and run the review first."
    body = f"""
    <div class="card">
      <h2>Pedometer review</h2>
      <p class="muted">Third end-to-end Python analysis path, focused on daily step counts and activity volume.</p>
      <form action="/action/run_pedometer_review" method="get">
        <button type="submit">Run Pedometer Review</button>
      </form>
      {metrics}
      <p>{html.escape(detail_text)}</p>
    </div>
    """
    return _page_shell("step4", "Step 4  Review", body)


def _step5_pedometer_page() -> HTMLResponse:
    daily = APP_STATE.pedometer_daily
    if not daily:
        table_html = "<p>No daily pedometer feature table is available yet.</p>"
    else:
        table_html = _table_preview(daily, limit=20)
    body = f"""
    <div class="card">
      <h2>Daily pedometer feature table</h2>
      <p class="muted">Python Step 5 preview for pedometer, centered on daily steps and activity intensity summaries.</p>
      <form action="/export/pedometer_daily.csv" method="get">
        <button type="submit">Download Daily Pedometer CSV</button>
      </form>
      {table_html}
    </div>
    """
    return _page_shell("step5", "Step 5  Features", body)



def _group_analysis_tables(
    feature_key: str = "All",
    metric: str = "All",
    cohort: object = "All",
    feature_transform: str = "none",
) -> tuple[list[dict], list[dict], str]:
    rows = _filtered_feature_rows(feature_key=feature_key, cohort=cohort)
    numeric_cols = _numeric_columns(rows)
    id_cols = {"Study_day", "study_day", "records", "Date", "time_bin", "temporal_frequency"}
    numeric_cols = [c for c in numeric_cols if c not in id_cols]
    if metric == "All" or metric not in numeric_cols:
        metric = numeric_cols[0] if numeric_cols else "All"
    if metric == "All":
        return [], [], metric
    rows, metric, _transform_info = _analysis_rows_with_transform(rows, metric, feature_transform)

    subject_values: dict[tuple[str, str], list[float]] = {}
    for row in rows:
        subject = _subject_label(row)
        group = None
        for col in ("group_label", "cohort", "group", "condition"):
            if row.get(col) not in (None, ""):
                group = str(row.get(col))
                break
        if not group:
            continue
        val = _safe_float(row.get(metric))
        if val is None:
            continue
        subject_values.setdefault((subject, group), []).append(val)

    subject_summary: list[dict] = []
    for (subject, group), vals in sorted(subject_values.items(), key=lambda item: (item[0][1], item[0][0])):
        if not vals:
            continue
        vals_sorted = sorted(vals)
        n = len(vals_sorted)
        mean_val = sum(vals_sorted) / n
        median_val = vals_sorted[n // 2] if n % 2 else (vals_sorted[n // 2 - 1] + vals_sorted[n // 2]) / 2
        subject_summary.append({
            "Subject_ID": subject,
            "group_label": group,
            "metric": metric,
            "records": n,
            "mean": round(mean_val, 4),
            "median": round(median_val, 4),
            "min": round(vals_sorted[0], 4),
            "max": round(vals_sorted[-1], 4),
        })

    by_group: dict[str, list[float]] = {}
    for row in subject_summary:
        val = _safe_float(row.get("mean"))
        if val is not None:
            by_group.setdefault(str(row["group_label"]), []).append(val)

    group_summary: list[dict] = []
    for group, vals in sorted(by_group.items()):
        vals_sorted = sorted(vals)
        n = len(vals_sorted)
        mean_val = sum(vals_sorted) / n if n else 0
        median_val = vals_sorted[n // 2] if n % 2 else (vals_sorted[n // 2 - 1] + vals_sorted[n // 2]) / 2
        variance = sum((v - mean_val) ** 2 for v in vals_sorted) / (n - 1) if n > 1 else 0
        group_summary.append({
            "group_label": group,
            "subjects": n,
            "metric": metric,
            "mean": round(mean_val, 4),
            "median": round(median_val, 4),
            "sd": round(variance ** 0.5, 4),
            "min": round(vals_sorted[0], 4) if vals_sorted else "",
            "max": round(vals_sorted[-1], 4) if vals_sorted else "",
        })
    return subject_summary, group_summary, metric


def _mean_ci(vals: list[float]) -> tuple[float, float, float]:
    vals = [v for v in vals if v is not None and math.isfinite(v)]
    if not vals:
        return (math.nan, math.nan, math.nan)
    mean_val = sum(vals) / len(vals)
    if len(vals) <= 1:
        return (mean_val, mean_val, mean_val)
    sd = (sum((v - mean_val) ** 2 for v in vals) / (len(vals) - 1)) ** 0.5
    se = sd / (len(vals) ** 0.5)
    return (mean_val, mean_val - 1.96 * se, mean_val + 1.96 * se)


def _bootstrap_mean_ci(
    vals: list[float],
    iterations: int = 1500,
    confidence: float = 0.95,
    seed: int = 42,
) -> tuple[float, float, float]:
    vals = [v for v in vals if v is not None and math.isfinite(v)]
    if not vals:
        return (math.nan, math.nan, math.nan)
    mean_val = sum(vals) / len(vals)
    if len(vals) <= 1:
        return (mean_val, mean_val, mean_val)
    rng = random.Random(seed)
    n = len(vals)
    boot_means: list[float] = []
    for _ in range(iterations):
        sample = [vals[rng.randrange(n)] for _ in range(n)]
        boot_means.append(sum(sample) / n)
    boot_means.sort()
    alpha = max(0.0, min(1.0, 1.0 - confidence))
    lo_idx = max(0, min(len(boot_means) - 1, int((alpha / 2.0) * len(boot_means))))
    hi_idx = max(0, min(len(boot_means) - 1, int((1.0 - alpha / 2.0) * len(boot_means)) - 1))
    return (mean_val, boot_means[lo_idx], boot_means[hi_idx])


def _group_analysis_plot(subject_summary: list[dict], metric: str) -> str:
    """Publication-style group distribution: violin + box + subject points + mean CI."""
    if not subject_summary:
        return "<p class='muted'>No group-level plot is available. Load group labels or choose a numeric feature.</p>"
    groups: dict[str, list[float]] = {}
    for row in subject_summary:
        val = _safe_float(row.get("mean"))
        if val is not None:
            groups.setdefault(str(row.get("group_label", "Unknown")), []).append(val)
    if not groups:
        return "<p class='muted'>No numeric values were available for the selected metric.</p>"
    fig = go.Figure()
    for group, vals in sorted(groups.items()):
        vals = [v for v in vals if math.isfinite(v)]
        if not vals:
            continue
        fig.add_trace(go.Violin(
            y=vals,
            name=group,
            box_visible=True,
            meanline_visible=True,
            points="all",
            jitter=0.35,
            scalemode="width",
            marker=dict(size=6, opacity=0.75),
            hovertemplate=f"{html.escape(group)}<br>{html.escape(_clean_feature_label(metric))}: %{{y:.4f}}<extra></extra>",
        ))
        mean_val, lo, hi = _mean_ci(vals)
        fig.add_trace(go.Scatter(
            x=[group], y=[mean_val], mode="markers", showlegend=False,
            marker=dict(symbol="diamond", size=11, color="black"),
            error_y=dict(type="data", symmetric=False, array=[hi - mean_val], arrayminus=[mean_val - lo], thickness=1.5, width=8),
            hovertemplate=f"Mean: %{{y:.4f}}<br>95% CI: {lo:.4f} to {hi:.4f}<extra></extra>",
        ))
    fig.update_layout(
        title=f"Group distribution of {_clean_feature_label(metric)}",
        yaxis_title=_clean_feature_label(metric),
        xaxis_title="Group / cohort",
        template="plotly_white",
        violinmode="group",
        height=560,
        margin=dict(l=70, r=35, t=70, b=70),
        showlegend=False,
    )
    return plot(fig, include_plotlyjs="cdn", output_type="div", config={"displaylogo": False, "toImageButtonOptions": {"format": "png", "filename": "jtrack_group_distribution", "height": 900, "width": 1400, "scale": 2}})


def _pairwise_effect_plot(pairwise_stats: list[dict], metric: str) -> str:
    rows = []
    for row in pairwise_stats or []:
        d = _safe_float(row.get("cohens_d"))
        if d is None or not math.isfinite(d):
            continue
        label = f"{row.get('group_a', '')} vs {row.get('group_b', '')}"
        rows.append((label, d, row.get("welch_t_p") or row.get("mann_whitney_p") or ""))
    if not rows:
        return "<p class='muted'>No effect-size plot is available for the selected comparison.</p>"
    rows.sort(key=lambda x: abs(x[1]), reverse=True)
    labels = [r[0] for r in rows]
    vals = [r[1] for r in rows]
    pvals = [r[2] for r in rows]
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=vals, y=labels, orientation="h",
        customdata=pvals,
        hovertemplate="Comparison: %{y}<br>Cohen's d: %{x:.3f}<br>p: %{customdata}<extra></extra>",
    ))
    fig.add_vline(x=0, line_width=1, line_dash="dash")
    fig.update_layout(
        title=f"Pairwise effect sizes for {_clean_feature_label(metric)}",
        xaxis_title="Cohen's d",
        yaxis_title="Group comparison",
        template="plotly_white",
        height=max(420, 70 + 42 * len(labels)),
        margin=dict(l=150, r=40, t=70, b=55),
    )
    return plot(fig, include_plotlyjs="cdn", output_type="div", config={"displaylogo": False, "toImageButtonOptions": {"format": "png", "filename": "jtrack_pairwise_effects", "height": 900, "width": 1400, "scale": 2}})


def _longitudinal_group_trajectory_plot(
    feature_key: str,
    metric: str,
    cohort: object = "All",
    feature_transform: str = "none",
) -> str:
    rows = _filtered_feature_rows(feature_key=feature_key, cohort=cohort)
    if not rows or metric in (None, "", "All"):
        return "<p class='muted'>No longitudinal trajectory is available for the selected metric.</p>"
    rows, metric, _transform_info = _analysis_rows_with_transform(rows, metric, feature_transform)
    group_day: dict[tuple[str, int], list[float]] = {}
    has_day = False
    for row in rows:
        val = _safe_float(row.get(metric))
        day = _study_day_value(row)
        group = None
        for col in ("group_label", "cohort", "group", "condition"):
            if row.get(col) not in (None, ""):
                group = str(row.get(col))
                break
        if val is None or not math.isfinite(val) or day is None or not group:
            continue
        has_day = True
        group_day.setdefault((group, int(day)), []).append(val)
    if not group_day or not has_day:
        return "<p class='muted'>Longitudinal plotting requires study-day or time-bin information in the selected feature table.</p>"
    fig = go.Figure()
    for group in sorted({g for (g, _) in group_day}):
        xs, means, lo, hi, ns = [], [], [], [], []
        for day in sorted({d for (g, d) in group_day if g == group}):
            vals = group_day.get((group, day), [])
            if not vals:
                continue
            seed = (sum(ord(ch) for ch in f"{group}-{day}-{metric}") + len(vals) * 997) % (2**32)
            mean_val, ci_lo, ci_hi = _bootstrap_mean_ci(vals, iterations=1500, confidence=0.95, seed=seed)
            xs.append(day); means.append(mean_val); lo.append(ci_lo); hi.append(ci_hi); ns.append(len(vals))
        if not xs:
            continue
        fig.add_trace(go.Scatter(
            x=xs, y=means, mode="lines+markers", name=group,
            customdata=ns,
            hovertemplate="Study day: %{x}<br>Mean: %{y:.4f}<br>Participants: %{customdata}<extra></extra>",
        ))
        fig.add_trace(go.Scatter(
            x=xs + xs[::-1], y=hi + lo[::-1], fill="toself", mode="lines",
            line=dict(width=0), showlegend=False, hoverinfo="skip", name=f"95% CI {group}", opacity=0.16,
        ))
    fig.update_layout(
        title=f"Mean {_clean_feature_label(metric)} across study days",
        xaxis_title="Study day",
        yaxis_title=_clean_feature_label(metric),
        template="plotly_white",
        height=540,
        margin=dict(l=70, r=35, t=70, b=60),
        legend_title_text="Group",
    )
    return plot(fig, include_plotlyjs="cdn", output_type="div", config={"displaylogo": False, "toImageButtonOptions": {"format": "png", "filename": "jtrack_longitudinal_group_trajectory", "height": 900, "width": 1400, "scale": 2}})


def _model_coefficient_plot(model_stats: list[dict]) -> str:
    rows = []
    for row in model_stats or []:
        term = str(row.get("term", "")).strip()
        est = _safe_float(row.get("estimate"))
        se = _safe_float(row.get("std_error"))
        if not term or term.lower() == "intercept" or est is None or se is None or not math.isfinite(est) or not math.isfinite(se):
            continue
        rows.append((term, est, est - 1.96 * se, est + 1.96 * se, row.get("p_value", "")))
    if not rows:
        return "<p class='muted'>No coefficient forest plot is available for this model output.</p>"
    labels = [r[0] for r in rows]
    ests = [r[1] for r in rows]
    lo = [r[2] for r in rows]
    hi = [r[3] for r in rows]
    pvals = [r[4] for r in rows]
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=ests, y=labels, mode="markers", marker=dict(size=10),
        error_x=dict(type="data", symmetric=False, array=[h-e for h,e in zip(hi, ests)], arrayminus=[e-l for e,l in zip(ests, lo)], thickness=1.4),
        customdata=pvals,
        hovertemplate="Term: %{y}<br>Estimate: %{x:.4f}<br>p: %{customdata}<extra></extra>",
    ))
    fig.add_vline(x=0, line_width=1, line_dash="dash")
    fig.update_layout(
        title="Model coefficient estimates",
        xaxis_title="Estimate with 95% CI",
        yaxis_title="Model term",
        template="plotly_white",
        height=max(420, 80 + 45 * len(labels)),
        margin=dict(l=170, r=40, t=70, b=55),
    )
    return plot(fig, include_plotlyjs="cdn", output_type="div", config={"displaylogo": False, "toImageButtonOptions": {"format": "png", "filename": "jtrack_model_coefficients", "height": 900, "width": 1400, "scale": 2}})




def _format_p_value(value: object) -> str:
    p = _safe_float(value)
    if p is None or not math.isfinite(p):
        return ""
    if p < 0.001:
        return "<0.001"
    return f"{p:.4f}"


def _group_values_from_subject_summary(subject_summary: list[dict]) -> dict[str, list[float]]:
    groups: dict[str, list[float]] = {}
    for row in subject_summary:
        group = str(row.get("group_label", "")).strip()
        val = _safe_float(row.get("mean"))
        if group and val is not None and math.isfinite(val):
            groups.setdefault(group, []).append(val)
    return groups


def _overall_group_statistics(subject_summary: list[dict], metric: str) -> list[dict]:
    groups = _group_values_from_subject_summary(subject_summary)
    groups = {g: vals for g, vals in groups.items() if vals}
    if len(groups) < 2:
        return [{"analysis": "Overall group test", "result": "At least two groups with numeric subject-level values are required."}]

    out: list[dict] = []
    values_by_group = [vals for _, vals in sorted(groups.items())]
    try:
        from scipy import stats as scipy_stats  # type: ignore
        if all(len(vals) >= 2 for vals in values_by_group):
            f_stat, anova_p = scipy_stats.f_oneway(*values_by_group)
            out.append({
                "analysis": "One-way ANOVA",
                "metric": metric,
                "groups": len(values_by_group),
                "statistic": round(float(f_stat), 4) if math.isfinite(float(f_stat)) else "",
                "p_value": _format_p_value(anova_p),
                "note": "Group effect on subject-level mean values.",
            })
        h_stat, kw_p = scipy_stats.kruskal(*values_by_group)
        out.append({
            "analysis": "Kruskal-Wallis",
            "metric": metric,
            "groups": len(values_by_group),
            "statistic": round(float(h_stat), 4) if math.isfinite(float(h_stat)) else "",
            "p_value": _format_p_value(kw_p),
            "note": "Non-parametric group comparison.",
        })
    except Exception as exc:
        out.append({
            "analysis": "Overall group test",
            "metric": metric,
            "groups": len(values_by_group),
            "result": f"Statistical test unavailable: {exc}",
        })
    return out


def _pairwise_group_statistics(subject_summary: list[dict], metric: str) -> list[dict]:
    groups = _group_values_from_subject_summary(subject_summary)
    group_names = sorted([g for g, vals in groups.items() if vals])
    if len(group_names) < 2:
        return [{"comparison": "Pairwise tests", "result": "At least two groups are required."}]

    out: list[dict] = []
    try:
        from scipy import stats as scipy_stats  # type: ignore
    except Exception:
        scipy_stats = None

    for i, group_a in enumerate(group_names):
        for group_b in group_names[i + 1:]:
            a = groups[group_a]
            b = groups[group_b]
            mean_a = sum(a) / len(a)
            mean_b = sum(b) / len(b)
            median_a = sorted(a)[len(a)//2] if len(a) % 2 else (sorted(a)[len(a)//2 - 1] + sorted(a)[len(a)//2]) / 2
            median_b = sorted(b)[len(b)//2] if len(b) % 2 else (sorted(b)[len(b)//2 - 1] + sorted(b)[len(b)//2]) / 2
            sd_a = (sum((x - mean_a) ** 2 for x in a) / (len(a) - 1)) ** 0.5 if len(a) > 1 else 0.0
            sd_b = (sum((x - mean_b) ** 2 for x in b) / (len(b) - 1)) ** 0.5 if len(b) > 1 else 0.0
            pooled_var_num = (len(a) - 1) * sd_a ** 2 + (len(b) - 1) * sd_b ** 2
            pooled_den = max(len(a) + len(b) - 2, 1)
            pooled_sd = (pooled_var_num / pooled_den) ** 0.5 if pooled_den > 0 else 0.0
            cohens_d = (mean_a - mean_b) / pooled_sd if pooled_sd > 0 else None
            t_p = None
            w_p = None
            if scipy_stats is not None:
                try:
                    if len(a) >= 2 and len(b) >= 2:
                        _, t_p = scipy_stats.ttest_ind(a, b, equal_var=False, nan_policy="omit")
                except Exception:
                    t_p = None
                try:
                    if len(a) >= 1 and len(b) >= 1:
                        _, w_p = scipy_stats.mannwhitneyu(a, b, alternative="two-sided")
                except Exception:
                    w_p = None
            out.append({
                "metric": metric,
                "group_a": group_a,
                "group_b": group_b,
                "n_a": len(a),
                "n_b": len(b),
                "mean_a": round(mean_a, 4),
                "mean_b": round(mean_b, 4),
                "mean_diff_a_minus_b": round(mean_a - mean_b, 4),
                "median_diff_a_minus_b": round(median_a - median_b, 4),
                "cohens_d": round(cohens_d, 4) if cohens_d is not None and math.isfinite(cohens_d) else "",
                "welch_t_p": _format_p_value(t_p),
                "mann_whitney_p": _format_p_value(w_p),
            })
    return out


def _longitudinal_model_statistics(
    feature_key: str,
    metric: str,
    cohort: object = "All",
    feature_transform: str = "none",
) -> list[dict]:
    """Fit the R-style model idea: metric ~ group, with random subject intercept when repeated rows exist.

    If a mixed model cannot be fitted, falls back to a fixed-effects OLS model.
    """
    rows = _filtered_feature_rows(feature_key=feature_key, cohort=cohort)
    rows, metric, _transform_info = _analysis_rows_with_transform(rows, metric, feature_transform)
    model_rows: list[dict] = []
    for row in rows:
        val = _safe_float(row.get(metric))
        if val is None or not math.isfinite(val):
            continue
        subject = _subject_label(row)
        group = None
        for col in ("group_label", "cohort", "group", "condition"):
            if row.get(col) not in (None, ""):
                group = str(row.get(col))
                break
        if not subject or not group:
            continue
        model_rows.append({"metric_value": val, "group_label": group, "subject_id": subject})

    groups = sorted({r["group_label"] for r in model_rows})
    if len(model_rows) < 3 or len(groups) < 2:
        return [{"model": "Longitudinal model", "result": "At least two groups with enough numeric rows are required."}]

    try:
        import pandas as pd  # type: ignore
        import statsmodels.formula.api as smf  # type: ignore
        df = pd.DataFrame(model_rows)
        repeated_subjects = df["subject_id"].duplicated().any()
        if repeated_subjects and df["subject_id"].nunique() > 1:
            fit = smf.mixedlm("metric_value ~ C(group_label)", df, groups=df["subject_id"]).fit(reml=False, method="lbfgs", disp=False)
            model_type = "Mixed-effects model"
            params = fit.params
            bse = fit.bse
            pvals = fit.pvalues
            stats_vals = params / bse
            note = "Random intercept by subject, similar to the R mixed-effects workflow."
        else:
            fit = smf.ols("metric_value ~ C(group_label)", data=df).fit()
            model_type = "Linear model"
            params = fit.params
            bse = fit.bse
            pvals = fit.pvalues
            stats_vals = fit.tvalues
            note = "Fixed-effects linear model fitted because repeated subject rows were not available."
        out = []
        for term in params.index:
            out.append({
                "model": model_type,
                "term": str(term),
                "estimate": round(float(params[term]), 4) if math.isfinite(float(params[term])) else "",
                "std_error": round(float(bse[term]), 4) if term in bse and math.isfinite(float(bse[term])) else "",
                "statistic": round(float(stats_vals[term]), 4) if term in stats_vals and math.isfinite(float(stats_vals[term])) else "",
                "p_value": _format_p_value(pvals[term] if term in pvals else None),
                "note": note,
            })
        return out
    except Exception as exc:
        return [{"model": "Longitudinal model", "result": f"Model could not be fitted: {exc}"}]


def _group_label_status_text() -> str:
    source_rows = APP_STATE.group_source_rows or []
    labels = APP_STATE.group_label_rows or []
    if not source_rows:
        return "No cohort/group metadata file loaded."
    group_col = APP_STATE.group_selected_column or _default_group_column(source_rows) or ""
    group_count = len({str(row.get("group_label", "")) for row in labels if row.get("group_label") not in (None, "")})
    comorb_n = sum(1 for row in labels if str(row.get("comorbidity_any", "")).upper() == "TRUE")
    comorb_cols = APP_STATE.group_comorbidity_columns or []
    comorb_msg = f" Selected comorbidity columns: {', '.join(comorb_cols)}." if comorb_cols else " No explicit comorbidity columns selected."
    return (
        f"Using group column '{group_col}' with {group_count} groups across "
        f"{len(labels)} subjects. Comorbidity information found for {comorb_n} subjects."
        f"{comorb_msg}"
    )


def _group_scope_matching_rows(feature_key: str = "All") -> list[dict]:
    rows = _generated_feature_rows(feature_key, use_qc=True)
    scoped_subjects = {_normalize_subject_key(s) for s in _subject_values_from_rows(rows)}
    scoped_subjects = {s for s in scoped_subjects if s}
    label_subjects = {str(row.get("subject_key") or _normalize_subject_key(row.get("username"))) for row in APP_STATE.group_label_rows or []}
    label_subjects = {s for s in label_subjects if s}
    matched = scoped_subjects & label_subjects
    missing = scoped_subjects - label_subjects
    outside = label_subjects - scoped_subjects
    return [
        {"matching_metric": "Subjects in selected analysis table", "count": len(scoped_subjects)},
        {"matching_metric": "Subjects matched to cohort file", "count": len(matched)},
        {"matching_metric": "Analysis subjects without group label", "count": len(missing)},
        {"matching_metric": "Cohort labels outside selected analysis table", "count": len(outside)},
    ]


def _cohort_value_for_subject(subject: object, column_candidates: Iterable[str]) -> object:
    label = _group_label_map().get(_normalize_subject_key(subject))
    if not label:
        return ""
    for col in column_candidates:
        if label.get(col) not in (None, ""):
            return label.get(col)
    return ""


def _study_day_value(row: dict) -> int | None:
    for col in ("Study_day", "study_day", "study_day_index"):
        value = _safe_float(row.get(col))
        if value is not None and math.isfinite(value):
            return int(value)
    time_text = str(row.get("time_bin") or row.get("Date") or "").strip()
    if time_text:
        # Keep dates ordered by converting to an ordinal-like integer when possible.
        for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%Y-%m-%d %H:00", "%Y-%m"):
            try:
                dt = datetime.strptime(time_text[:len(fmt.replace('%H:00','00:00'))] if fmt == "%Y-%m-%d %H:00" else time_text, fmt)
                return dt.toordinal()
            except Exception:
                continue
    return None


def _looks_like_communication_category(col: str) -> bool:
    key = _label_key(col)
    return any(token in key for token in ("WRITTEN", "VERBAL", "MIXED", "COMMUNICATION", "TOTALCOMMUNICATION"))


def _communication_category_columns(rows: list[dict]) -> list[str]:
    if not rows:
        return []
    numeric = [c for c in _numeric_columns(rows) if not _is_identifier_or_metadata_column(c)]
    cols: list[str] = []
    for col in numeric:
        key = _label_key(col)
        if key in {"WRITTEN", "VERBAL", "MIXED", "TOTAL", "TOTALCOMMUNICATION", "WRITTENCOMMUNICATION", "VERBALCOMMUNICATION", "MIXEDCOMMUNICATION"} or _looks_like_communication_category(col):
            cols.append(col)
    # If the category table uses clean category names from the app-category file,
    # the three core paper categories should be enough. Total is computed below if absent.
    preferred = []
    for wanted in ("written", "verbal", "mixed", "total", "total_communication"):
        for col in cols:
            if _label_key(col) == _label_key(wanted) and col not in preferred:
                preferred.append(col)
    for col in cols:
        if col not in preferred:
            preferred.append(col)
    return preferred


def _communication_category_label(col: str) -> str:
    key = _label_key(col)
    if "WRITTEN" in key:
        return "written"
    if "VERBAL" in key:
        return "verbal"
    if "MIXED" in key:
        return "mixed"
    if key in {"TOTAL", "TOTALCOMMUNICATION"} or "TOTALCOMMUNICATION" in key:
        return "total"
    return str(col)


def _communication_analysis_rows(feature_key: str, cohort: object = "All") -> tuple[list[dict], list[str], list[dict]]:
    rows = _filtered_feature_rows(feature_key=feature_key, cohort=cohort)
    # The communication workflow is only meaningful for generated app/category tables.
    comm_cols = _communication_category_columns(rows)
    if not rows or not comm_cols:
        return [], [], []
    # Use one row per subject/day when available. Daily is the default paper-like granularity.
    daily_rows: list[dict] = []
    for row in rows:
        freq = str(row.get("temporal_frequency") or "").lower()
        if freq and "daily" not in freq and not row.get("Study_day") and not row.get("Date"):
            continue
        subject = _subject_label(row)
        group = None
        for col in ("group_label", "cohort", "group", "condition"):
            if row.get(col) not in (None, ""):
                group = str(row.get(col))
                break
        if not subject or not group:
            continue
        day = _study_day_value(row)
        if day is None:
            continue
        item = {
            "Subject_ID": subject,
            "subject_key": _normalize_subject_key(subject),
            "group_label": group,
            "Study_day": day,
            "Date": row.get("Date") or row.get("time_bin") or str(day),
            "age": _cohort_value_for_subject(subject, ("age", "Age", "Alter", "ALTER")),
            "sex": _cohort_value_for_subject(subject, ("sex", "Sex", "Geschlecht", "gender", "Gender")),
            "IQ": _cohort_value_for_subject(subject, ("IQ", "iq")),
            "AQ": _cohort_value_for_subject(subject, ("AQ", "aq")),
        }
        total = 0.0
        seen_core = False
        for col in comm_cols:
            label = _communication_category_label(col)
            val = _safe_float(row.get(col))
            if val is None or not math.isfinite(val):
                val = 0.0
            item[label] = val
            if label in {"written", "verbal", "mixed"} and val is not None:
                total += val
                seen_core = True
        if "total" not in item and seen_core:
            item["total"] = total
        daily_rows.append(item)
    coverage: list[dict] = []
    days_by_subject: dict[str, set[int]] = {}
    for row in daily_rows:
        days_by_subject.setdefault(row["Subject_ID"], set()).add(int(row["Study_day"]))
    for subject, days in sorted(days_by_subject.items()):
        group = next((r.get("group_label") for r in daily_rows if r.get("Subject_ID") == subject), "")
        coverage.append({
            "Subject_ID": subject,
            "group_label": group,
            "observed_days": len(days),
        })
    categories = [c for c in ("written", "verbal", "mixed", "total") if any(c in row and row.get(c) is not None for row in daily_rows)]
    return daily_rows, categories, coverage


def _communication_descriptive_summary(rows: list[dict], categories: list[str]) -> list[dict]:
    out: list[dict] = []
    by_group_cat: dict[tuple[str, str], list[float]] = {}
    for row in rows:
        group = str(row.get("group_label", ""))
        if not group:
            continue
        for cat in categories:
            val = _safe_float(row.get(cat))
            if val is not None and math.isfinite(val):
                by_group_cat.setdefault((group, cat), []).append(val)
    for (group, cat), vals in sorted(by_group_cat.items()):
        if not vals:
            continue
        vals_sorted = sorted(vals)
        mean = sum(vals_sorted) / len(vals_sorted)
        median = vals_sorted[len(vals_sorted)//2] if len(vals_sorted) % 2 else (vals_sorted[len(vals_sorted)//2 - 1] + vals_sorted[len(vals_sorted)//2]) / 2
        out.append({
            "group_label": group,
            "communication_category": cat,
            "daily_observations": len(vals_sorted),
            "subjects": len({r["Subject_ID"] for r in rows if r.get("group_label") == group and _safe_float(r.get(cat)) is not None}),
            "mean_daily_hours": round(mean, 4),
            "median_daily_hours": round(median, 4),
            "min": round(vals_sorted[0], 4),
            "max": round(vals_sorted[-1], 4),
        })
    return out


def _infer_asd_td_groups(rows: list[dict]) -> tuple[str | None, str | None]:
    groups = sorted({str(r.get("group_label", "")) for r in rows if r.get("group_label")})
    if len(groups) < 2:
        return None, None
    asd = next((g for g in groups if any(t in g.upper() for t in ("ASD", "AUTISM", "AUTISTIC"))), None)
    td = next((g for g in groups if any(t in g.upper() for t in ("TD", "CONTROL", "TYPICAL", "HC"))), None)
    if asd and td and asd != td:
        return asd, td
    return groups[0], groups[1]


def _communication_sign_tests(rows: list[dict], categories: list[str]) -> list[dict]:
    group_a, group_b = _infer_asd_td_groups(rows)
    if not group_a or not group_b:
        return [{"analysis": "Daily consistency sign test", "result": "At least two groups are required."}]
    out: list[dict] = []
    try:
        from scipy import stats as scipy_stats  # type: ignore
    except Exception:
        scipy_stats = None
    for cat in categories:
        day_group: dict[int, dict[str, list[float]]] = {}
        for row in rows:
            val = _safe_float(row.get(cat))
            if val is None or not math.isfinite(val):
                continue
            day_group.setdefault(int(row["Study_day"]), {}).setdefault(str(row["group_label"]), []).append(val)
        wins_a = wins_b = ties = 0
        for _, groups in day_group.items():
            if group_a not in groups or group_b not in groups:
                continue
            mean_a = sum(groups[group_a]) / len(groups[group_a])
            mean_b = sum(groups[group_b]) / len(groups[group_b])
            if mean_a > mean_b:
                wins_a += 1
            elif mean_b > mean_a:
                wins_b += 1
            else:
                ties += 1
        n = wins_a + wins_b
        p_val = None
        if scipy_stats is not None and n > 0:
            try:
                p_val = scipy_stats.binomtest(max(wins_a, wins_b), n, p=0.5, alternative="two-sided").pvalue
            except Exception:
                p_val = None
        out.append({
            "analysis": "Daily consistency sign test",
            "communication_category": cat,
            "group_a": group_a,
            "group_b": group_b,
            "days_group_a_higher": wins_a,
            "days_group_b_higher": wins_b,
            "ties": ties,
            "p_value": _format_p_value(p_val),
        })
    return out


def _communication_mixed_models(rows: list[dict], categories: list[str]) -> list[dict]:
    if not rows or len({r.get("group_label") for r in rows}) < 2:
        return [{"model": "Communication mixed model", "result": "At least two groups with communication rows are required."}]
    try:
        import pandas as pd  # type: ignore
        import statsmodels.formula.api as smf  # type: ignore
    except Exception as exc:
        return [{"model": "Communication mixed model", "result": f"statsmodels/pandas unavailable: {exc}"}]
    out: list[dict] = []
    for cat in categories:
        model_rows = []
        for row in rows:
            val = _safe_float(row.get(cat))
            if val is None or not math.isfinite(val):
                continue
            item = {
                "communication_time": math.log1p(max(val, 0.0)),
                "group_label": str(row.get("group_label")),
                "subject_id": str(row.get("Subject_ID")),
            }
            age = _safe_float(row.get("age"))
            iq = _safe_float(row.get("IQ"))
            sex = str(row.get("sex") or "").strip()
            if age is not None and math.isfinite(age):
                item["age"] = age
            if iq is not None and math.isfinite(iq):
                item["IQ"] = iq
            if sex:
                item["sex"] = sex
            model_rows.append(item)
        if len(model_rows) < 4 or len({r["group_label"] for r in model_rows}) < 2:
            out.append({"communication_category": cat, "model": "Communication mixed model", "result": "Not enough rows/groups."})
            continue
        df = pd.DataFrame(model_rows)
        terms = ["C(group_label)"]
        if "age" in df.columns and df["age"].nunique(dropna=True) > 1:
            terms.append("age")
        if "sex" in df.columns and df["sex"].nunique(dropna=True) > 1:
            terms.append("C(sex)")
        if "IQ" in df.columns and df["IQ"].nunique(dropna=True) > 1:
            terms.append("IQ")
        formula = "communication_time ~ " + " + ".join(terms)
        try:
            if df["subject_id"].duplicated().any() and df["subject_id"].nunique() > 1:
                fit = smf.mixedlm(formula, df, groups=df["subject_id"]).fit(reml=False, method="lbfgs", disp=False)
                model_type = "Mixed-effects model"
                note = "Log1p communication time; random intercept by subject; covariates included when available."
                params, bse, pvals = fit.params, fit.bse, fit.pvalues
                stats_vals = params / bse
            else:
                fit = smf.ols(formula, data=df).fit()
                model_type = "Linear model"
                note = "Log1p communication time; no repeated subject rows available."
                params, bse, pvals, stats_vals = fit.params, fit.bse, fit.pvalues, fit.tvalues
            for term in params.index:
                if term == "Intercept":
                    continue
                out.append({
                    "communication_category": cat,
                    "model": model_type,
                    "term": str(term),
                    "estimate": round(float(params[term]), 4) if math.isfinite(float(params[term])) else "",
                    "std_error": round(float(bse[term]), 4) if term in bse and math.isfinite(float(bse[term])) else "",
                    "statistic": round(float(stats_vals[term]), 4) if term in stats_vals and math.isfinite(float(stats_vals[term])) else "",
                    "p_value": _format_p_value(pvals[term] if term in pvals else None),
                    "formula": formula + " + (1|subject)",
                    "note": note,
                })
        except Exception as exc:
            out.append({"communication_category": cat, "model": "Communication mixed model", "result": f"Model failed: {exc}", "formula": formula})
    return out


def _communication_aq_correlations(rows: list[dict], categories: list[str]) -> list[dict]:
    subject_vals: dict[str, dict] = {}
    for row in rows:
        subject = str(row.get("Subject_ID"))
        aq = _safe_float(row.get("AQ"))
        item = subject_vals.setdefault(subject, {"AQ": aq, "cats": {c: [] for c in categories}})
        if aq is not None and item.get("AQ") is None:
            item["AQ"] = aq
        for cat in categories:
            val = _safe_float(row.get(cat))
            if val is not None and math.isfinite(val):
                item["cats"][cat].append(val)
    try:
        from scipy import stats as scipy_stats  # type: ignore
    except Exception:
        scipy_stats = None
    out: list[dict] = []
    for cat in categories:
        xs, ys = [], []
        for _, item in subject_vals.items():
            aq = item.get("AQ")
            vals = item["cats"].get(cat, [])
            if aq is not None and vals:
                xs.append(float(aq))
                ys.append(sum(vals) / len(vals))
        r = p_val = None
        if scipy_stats is not None and len(xs) >= 3:
            try:
                r, p_val = scipy_stats.pearsonr(xs, ys)
            except Exception:
                pass
        out.append({
            "analysis": "AQ correlation",
            "communication_category": cat,
            "subjects": len(xs),
            "pearson_r": round(float(r), 4) if r is not None and math.isfinite(float(r)) else "",
            "p_value": _format_p_value(p_val),
        })
    return out


def _communication_longitudinal_plot(rows: list[dict], categories: list[str]) -> str:
    if not rows or not categories:
        return "<p class='muted'>No communication-category trajectory can be drawn. Select an application-usage category feature table with written, verbal, mixed, or total communication columns.</p>"
    # Keep the figure readable: show written/verbal/mixed/total if present.
    plot_categories = [c for c in ("written", "verbal", "mixed", "total") if c in categories]
    if not plot_categories:
        plot_categories = categories[:4]
    group_day_cat: dict[tuple[str, int, str], list[float]] = {}
    for row in rows:
        group = str(row.get("group_label", ""))
        day = _study_day_value(row)
        if not group or day is None:
            continue
        for cat in plot_categories:
            val = _safe_float(row.get(cat))
            if val is not None and math.isfinite(val):
                group_day_cat.setdefault((group, int(day), cat), []).append(val)
    fig = go.Figure()
    for cat in plot_categories:
        for group in sorted({g for (g, _, c) in group_day_cat if c == cat}):
            xs, ys, lo, hi = [], [], [], []
            for day in sorted({d for (g, d, c) in group_day_cat if g == group and c == cat}):
                vals = group_day_cat.get((group, day, cat), [])
                if not vals:
                    continue
                mean = sum(vals) / len(vals)
                se = (sum((v - mean) ** 2 for v in vals) / (len(vals) - 1)) ** 0.5 / (len(vals) ** 0.5) if len(vals) > 1 else 0.0
                xs.append(day); ys.append(mean); lo.append(max(mean - 1.96 * se, 0)); hi.append(mean + 1.96 * se)
            if not xs:
                continue
            name = f"{group} - {cat}"
            fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines", name=name, hovertemplate="Study day: %{x}<br>Mean hours: %{y:.3f}<extra></extra>"))
            fig.add_trace(go.Scatter(x=xs + xs[::-1], y=hi + lo[::-1], fill="toself", mode="lines", line=dict(width=0), showlegend=False, hoverinfo="skip", name=f"95% CI {name}", opacity=0.15))
    fig.update_layout(
        title="Communication category trajectories",
        xaxis_title="Study day",
        yaxis_title="Mean daily communication time (hours)",
        template="plotly_white",
        height=520,
        margin=dict(l=60, r=30, t=70, b=60),
    )
    return plot(fig, include_plotlyjs="cdn", output_type="div", config={"displaylogo": False, "toImageButtonOptions": {"format": "png", "filename": "communication_preference_trajectory", "height": 900, "width": 1400, "scale": 2}})


def _communication_preference_section(feature_key: str, cohort: object = "All") -> str:
    rows, categories, inclusion = _communication_analysis_rows(feature_key, cohort=cohort)
    if not categories:
        return """
        <p class="muted">Communication analysis is available when the selected application-usage category table contains written, verbal, mixed, or total communication columns.</p>
        """
    desc = _communication_descriptive_summary(rows, categories)
    signs = _communication_sign_tests(rows, categories)
    models = _communication_mixed_models(rows, categories)
    corr = _communication_aq_correlations(rows, categories)
    plot_div = _communication_longitudinal_plot(rows, categories)
    retained = len({r.get("Subject_ID") for r in rows})
    return f"""
      <div class="summary-chip-row">
        <span class="summary-chip">{retained} subjects</span>
        <span class="summary-chip">Categories: {html.escape(', '.join(categories))}</span>
      </div>
      {plot_div}
      <details class="clean-details" open>
        <summary>Communication summary</summary>
        {_table_preview(desc, limit=40)}
      </details>
      <details class="clean-details" open>
        <summary>Mixed model results</summary>
        {_table_preview(models, limit=80)}
      </details>
      <details class="clean-details">
        <summary>Daily sign tests</summary>
        {_table_preview(signs, limit=40)}
      </details>
      <details class="clean-details">
        <summary>AQ correlations</summary>
        {_table_preview(corr, limit=40)}
      </details>
      <details class="clean-details">
        <summary>Observed days diagnostics</summary>
        {_table_preview(inclusion, limit=80)}
      </details>
    """


def _step7_page(
    feature_key: str = "All",
    feature_column: str = "All",
    cohort: object = "All",
    analysis_type: str = "descriptives",
    feature_transform: str = "none",
) -> HTMLResponse:
    feature_keys = _all_generated_feature_keys()
    if feature_key == "All" and APP_STATE.generated_feature_key:
        feature_key = APP_STATE.generated_feature_key
    if feature_transform == "none" and APP_STATE.generated_feature_transform != "none":
        feature_transform = APP_STATE.generated_feature_transform
    feature_pairs = [(key, FEATURE_LABELS.get(key, key)) for key in feature_keys] or [("All", "No generated feature yet")]

    rows = _filtered_feature_rows(feature_key=feature_key, cohort=cohort)
    metric_choices = _numeric_columns(rows)
    metric_choices = [c for c in metric_choices if c not in {"Study_day", "study_day", "records"} and not _is_identifier_or_metadata_column(c)]
    metric_pairs = [("All", "Auto-select metric")] + [(c, _clean_feature_label(c)) for c in metric_choices]
    transform_pairs = FEATURE_TRANSFORM_CHOICES
    cohorts = _cohort_values_from_rows(_generated_feature_rows(feature_key))

    subject_summary, group_summary, metric_used = _group_analysis_tables(
        feature_key=feature_key,
        metric=feature_column,
        cohort=cohort,
        feature_transform=feature_transform,
    )
    plot_div = _group_analysis_plot(subject_summary, metric_used)
    overall_stats = _overall_group_statistics(subject_summary, metric_used) if subject_summary else []
    pairwise_stats = _pairwise_group_statistics(subject_summary, metric_used) if subject_summary else []
    model_stats = _longitudinal_model_statistics(
        feature_key,
        metric_used,
        cohort=cohort,
        feature_transform=feature_transform,
    ) if subject_summary else []
    pairwise_plot = _pairwise_effect_plot(pairwise_stats, metric_used) if pairwise_stats else ""
    longitudinal_plot = _longitudinal_group_trajectory_plot(
        feature_key,
        metric_used,
        cohort=cohort,
        feature_transform=feature_transform,
    ) if subject_summary else ""
    coefficient_plot = _model_coefficient_plot(model_stats) if model_stats else ""
    communication_section = _communication_preference_section(feature_key, cohort=cohort)

    source_rows = APP_STATE.group_source_rows or []
    source_cols = list(source_rows[0].keys()) if source_rows else []
    group_col = APP_STATE.group_selected_column or _default_group_column(source_rows) or ""
    comorb_cols = APP_STATE.group_comorbidity_columns or _default_comorbidity_columns(source_rows)
    labels_loaded = bool(APP_STATE.group_label_rows)
    source_note = html.escape(Path(APP_STATE.group_label_source).name if APP_STATE.group_label_source else "No cohort file loaded")

    if labels_loaded:
        detected_groups = sorted({str(r.get("group_label", "")) for r in APP_STATE.group_label_rows if r.get("group_label") not in (None, "")})
        label_status = f"""
        <div class="summary-chip-row">
          <span class="summary-chip">{len(APP_STATE.group_label_rows)} labeled subjects</span>
          <span class="summary-chip">{len(detected_groups)} groups</span>
          <span class="summary-chip">{html.escape(group_col or 'Auto group column')}</span>
        </div>
        """
    else:
        label_status = "<p class='muted'>Load one cohort metadata CSV/TSV/TXT file to enable group comparisons.</p>"

    column_selector = ""
    if source_rows:
        column_selector = f"""
        <details class="clean-details">
          <summary>Detected columns</summary>
          <form action="/action/update_group_label_columns" method="get" class="form-section compact-form">
            <label class="inline-label">Group column</label>
            <select name="group_col">{_select_options_plain(source_cols, selected=group_col)}</select>
            <label class="inline-label">Optional comorbidity columns</label>
            <select name="comorbidity_cols" multiple size="4">{_select_options_plain(source_cols, selected_many=comorb_cols)}</select>
            <button type="submit">Update columns</button>
          </form>
          <p class="compact-note">{html.escape(_group_label_status_text())}</p>
        </details>
        """

    matching_rows = _group_scope_matching_rows(feature_key)
    selected_groups_label = _group_filter_label(cohort)
    analysis_options = [
        ("descriptives", "Descriptives"),
        ("tests", "Group tests"),
        ("model", "Longitudinal model"),
        ("communication", "Communication analysis"),
        ("diagnostics", "Matching diagnostics"),
    ]
    if analysis_type not in {x[0] for x in analysis_options}:
        analysis_type = "descriptives"

    if analysis_type == "tests":
        result_title = "Group tests"
        result_body = f"""
          <p class="compact-note">Overall tests and pairwise comparisons use subject-level mean values for the selected metric.</p>
          {pairwise_plot}
          <details class="clean-details" open><summary>Overall tests</summary>{_table_preview(overall_stats, limit=10) if overall_stats else "<p class='muted'>No overall test available.</p>"}</details>
          <details class="clean-details"><summary>Pairwise comparison table</summary>{_table_preview(pairwise_stats, limit=30) if pairwise_stats else "<p class='muted'>No pairwise comparison available.</p>"}</details>
        """
    elif analysis_type == "model":
        result_title = "Longitudinal model"
        result_body = f"""
          <p class="compact-note">Uses repeated rows when available. A subject random intercept is fitted when possible; otherwise the model falls back to fixed effects.</p>
          {longitudinal_plot}
          {coefficient_plot}
          <details class="clean-details" open><summary>Model table</summary>{_table_preview(model_stats, limit=30) if model_stats else "<p class='muted'>No model output available.</p>"}</details>
        """
    elif analysis_type == "communication":
        result_title = "Communication analysis"
        result_body = communication_section
    elif analysis_type == "diagnostics":
        result_title = "Matching diagnostics"
        result_body = f"""
          <details class="clean-details" open><summary>Subject matching</summary>{_table_preview(matching_rows, limit=20)}</details>
          <details class="clean-details"><summary>Cohort labels preview</summary>{_table_preview(APP_STATE.group_label_rows[:50], limit=50) if APP_STATE.group_label_rows else "<p class='muted'>No cohort rows loaded.</p>"}</details>
          <details class="clean-details"><summary>Subject-level values</summary>{_table_preview(subject_summary, limit=50) if subject_summary else "<p class='muted'>No subject-level values available.</p>"}</details>
        """
    else:
        result_title = "Descriptives"
        descriptive_pairwise = pairwise_stats if pairwise_stats and not any("result" in r for r in pairwise_stats) else []
        descriptive_overall = overall_stats if overall_stats and not any("result" in r for r in overall_stats) else []
        result_body = f"""
          {plot_div}
          <p class="compact-note">Longitudinal summary: displayed are the mean and the bootstrapped 95% confidence interval for each study day.</p>
          {longitudinal_plot}
          <details class="clean-details" open>
            <summary>Group summary</summary>
            {_table_preview(group_summary, limit=30) if group_summary else "<p class='muted'>No group summary available. Load a cohort file and select a numeric feature.</p>"}
          </details>
          <details class="clean-details" open>
            <summary>Mean-difference support tests</summary>
            <p class="compact-note">Descriptive group plots are supported by the same inferential tests used in Group tests: overall group tests and pairwise mean-difference tests on subject-level means.</p>
            {_table_preview(descriptive_overall, limit=10) if descriptive_overall else "<p class='muted'>Overall p-values require at least two groups with numeric values.</p>"}
            {_table_preview(descriptive_pairwise, limit=30) if descriptive_pairwise else "<p class='muted'>Pairwise p-values require at least two groups with numeric values.</p>"}
          </details>
        """

    body = f"""
    <div class="step-grid jamovi-grid">
      <div class="card side-card analysis-sidebar">
        <h2>Group analysis</h2>
        <p class="compact-note">1) Load a cohort file. 2) Select a feature and analysis. 3) Review the result on the right.</p>

        <section class="compact-section">
          <h3>Cohort file</h3>
          <div class="mini-actions">
            <form action="/action/pick_group_label_file" method="get">
              <button type="submit">Choose cohort file</button>
            </form>
          </div>
          <p class="compact-note">{source_note}</p>
          {label_status}
          {column_selector}
        </section>

        <section class="compact-section">
          <h3>Analysis</h3>
          <form method="get" action="/" class="form-section compact-form">
            <input type="hidden" name="step" value="step7" />
            <label class="inline-label">Feature table</label>
            <select name="feature_key">{_select_options_from_pairs(feature_pairs, feature_key)}</select>
            <label class="inline-label">Metric</label>
            <select name="feature_column">{_select_options_from_pairs(metric_pairs, feature_column)}</select>
            <label class="inline-label">Transformation</label>
            <select name="feature_transform">{_select_options_from_pairs(transform_pairs, feature_transform)}</select>
            <label class="inline-label">Groups</label>
            <select name="cohort" multiple size="5">{_select_options_plain(["All"] + cohorts, selected_many=(_normalize_group_filter(cohort) or ["All"]))}</select>
            <label class="inline-label">Analysis type</label>
            <select name="analysis_type">{_select_options_from_pairs(analysis_options, analysis_type)}</select>
            <button type="submit">Update analysis</button>
          </form>
          <div class="summary-chip-row">
            <span class="summary-chip">Metric: {html.escape(_clean_feature_label(metric_used) if metric_used else 'Auto')}</span>
            <span class="summary-chip">Transformation: {html.escape(dict(transform_pairs).get(feature_transform, feature_transform))}</span>
            <span class="summary-chip">Groups: {html.escape(selected_groups_label)}</span>
          </div>
        </section>
      </div>

      <div class="card preview-card analysis-results">
        <h2>{result_title}</h2>
        {result_body}
      </div>
    </div>
    """
    return _page_shell("step7", "Step 7  Group Analysis", body)


app = FastAPI(title="JTrack Insight", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "mode": "local-web"}


@app.get("/", response_class=HTMLResponse)
def root(
    step: str = "home",
    dataset_root: str | None = None,
    username: str | None = None,
    device_id: str | None = None,
    sensor_name: str | None = None,
    wearable_sensor: str | None = None,
    feature_key: str = "All",
    feature_name: str = "all",
    temporal_frequency: str = "daily",
    feature_mode: str = "core",
    feature_column: str = "All",
    feature_transform: str = "none",
    plot_type: str = "mean_ci",
    render_plot: str = "0",
    analysis_type: str = "descriptives",
    cohort: list[str] = Query(default=["All"]),
    min_day: str | None = None,
    max_day: str | None = None,
    qc_column: str = "All",
    min_value: str = "",
    max_value: str = "",
) -> HTMLResponse:
    if step == "step1":
        return _step1_page(dataset_root=dataset_root)
    if step == "step2":
        return _step2_page()
    if step in {"step3", "step3a", "step3b"}:
        return _step3_page(
            username=username or "All",
            sensor_name=sensor_name or wearable_sensor or "All",
            feature_name=feature_name,
            temporal_frequency=temporal_frequency,
            feature_mode=feature_mode,
        )
    if step == "step3c":
        return _step4_page(min_day=min_day, max_day=max_day)
    if step == "step4":
        return _step4_page(min_day=min_day, max_day=max_day, feature_key=feature_key, qc_column=qc_column, min_value=min_value, max_value=max_value)
    if step == "step5":
        return _step5_page(
            feature_key=feature_key,
            subject=username or "All",
            feature_column=feature_column,
            plot_type=plot_type,
            cohort=cohort,
            sensor_name=sensor_name or "All",
            temporal_frequency=temporal_frequency,
            feature_transform=feature_transform,
            render_plot=render_plot,
        )
    if step == "step6":
        return _step6_page(
            feature_key=feature_key,
            subject=username or "All",
            feature_column=feature_column,
            plot_type=plot_type,
            cohort=cohort,
            feature_transform=feature_transform,
        )
    if step == "step7":
        return _step7_page(
            feature_key=feature_key,
            feature_column=feature_column,
            cohort=cohort,
            analysis_type=analysis_type,
            feature_transform=feature_transform,
        )
    if step == "roadmap":
        return _roadmap_page()
    return _home_page()


@app.get("/action/load_dataset")
def load_dataset(dataset_root: str) -> RedirectResponse:
    root_path = Path(dataset_root).expanduser().resolve()
    indexed = scan_dataset_metadata(root_path)
    qc_rows = scan_file_qc(indexed)
    APP_STATE.data_root = str(root_path)
    APP_STATE.indexed_rows = indexed
    APP_STATE.qc_rows = qc_rows
    APP_STATE.filtered_rows = indexed
    APP_STATE.loaded_rows = []
    APP_STATE.loaded_filtered_rows = []
    APP_STATE.selected_sensor_name = None
    APP_STATE.selected_wearable_sensor = None
    APP_STATE.app_usage_daily = []
    APP_STATE.app_usage_category_daily = []
    APP_STATE.app_usage_category_daily_wide = []
    APP_STATE.app_usage_review = None
    APP_STATE.location_daily = []
    APP_STATE.location_trajectory = []
    APP_STATE.location_review = None
    APP_STATE.pedometer_daily = []
    APP_STATE.pedometer_review = None
    APP_STATE.generated_feature_key = None
    APP_STATE.feature_qc_rows = []
    APP_STATE.feature_qc_audit_rows = []
    APP_STATE.feature_qc_description = None
    APP_STATE.app_category_map = load_app_category_mapping()
    APP_STATE.app_category_source = str(DEFAULT_APP_CATEGORY_PATH) if APP_STATE.app_category_map else None
    APP_STATE.status = f"Indexing completed at {iso_now()}. Loaded metadata for {summarize_indexed_files(indexed)['subjects']} subjects."
    return RedirectResponse(url="/?step=step2", status_code=303)


@app.get("/action/pick_dataset")
def pick_dataset() -> RedirectResponse:
    selected = _choose_folder_native()
    if not selected:
        APP_STATE.status = "Folder picker was cancelled or is not available. You can still paste a dataset path manually."
        return RedirectResponse(url="/?step=step1", status_code=303)

    APP_STATE.status = "Folder selected. Review the dataset root in Step 1, then click Load Dataset to start indexing."
    query = urlencode({"step": "step1", "dataset_root": selected})
    return RedirectResponse(url=f"/?{query}", status_code=303)



@app.get("/action/pick_group_label_folder")
def pick_group_label_folder() -> RedirectResponse:
    selected = _choose_folder_native("Choose group / cohort label folder")
    if not selected:
        APP_STATE.status = "Group-label folder picker was cancelled or is not available. You can still paste a folder path manually."
        return RedirectResponse(url="/?step=step7", status_code=303)
    APP_STATE.group_label_folder = str(Path(selected).expanduser())
    file_count = len(_group_label_folder_files())
    APP_STATE.status = f"Selected group-label folder with {file_count} readable CSV/TSV/TXT files."
    return RedirectResponse(url="/?step=step7", status_code=303)


@app.get("/action/set_group_label_folder")
def set_group_label_folder(group_label_folder: str) -> RedirectResponse:
    folder = Path(group_label_folder).expanduser()
    if not folder.exists() or not folder.is_dir():
        APP_STATE.status = f"Group-label folder not found: {folder}"
    else:
        APP_STATE.group_label_folder = str(folder)
        file_count = len(_group_label_folder_files())
        APP_STATE.status = f"Selected group-label folder with {file_count} readable CSV/TSV/TXT files."
    return RedirectResponse(url="/?step=step7", status_code=303)


@app.get("/action/load_group_labels_from_folder")
def load_group_labels_from_folder(group_label_file: str) -> RedirectResponse:
    try:
        path = Path(group_label_file).expanduser()
        folder = Path(APP_STATE.group_label_folder).expanduser() if APP_STATE.group_label_folder else path.parent
        # Limit dropdown-based loading to the selected folder for safety/clarity.
        if APP_STATE.group_label_folder and path.parent.resolve() != folder.resolve():
            raise ValueError("Selected file is not inside the selected group-label folder.")
        source_rows, label_rows, group_col, comorb_cols = _load_group_cohort_file(str(path))
        if not source_rows:
            APP_STATE.status = "Group/cohort file was loaded but no valid subject metadata rows were detected."
        else:
            APP_STATE.group_source_rows = source_rows
            APP_STATE.group_label_rows = label_rows
            APP_STATE.group_selected_column = group_col
            APP_STATE.group_comorbidity_columns = comorb_cols
            APP_STATE.group_label_source = str(path)
            APP_STATE.group_label_folder = str(path.parent)
            APP_STATE.status = f"Loaded {len(source_rows)} cohort rows and {len(label_rows)} matched label rows for group-level analysis."
    except Exception as exc:
        APP_STATE.status = f"Could not load group labels: {exc}"
    return RedirectResponse("/?step=step7", status_code=303)


@app.get("/action/pick_group_label_file")
def pick_group_label_file() -> RedirectResponse:
    selected = _choose_file_native("Choose group / cohort labels CSV or TSV")
    if not selected:
        APP_STATE.status = "Group-label file picker was cancelled or is not available. You can still paste a file path manually."
        return RedirectResponse(url="/?step=step7", status_code=303)
    try:
        path = Path(selected).expanduser()
        source_rows, label_rows, group_col, comorb_cols = _load_group_cohort_file(str(path))
        if not source_rows:
            APP_STATE.status = "Group/cohort file was loaded but no valid subject metadata rows were detected."
        else:
            APP_STATE.group_source_rows = source_rows
            APP_STATE.group_label_rows = label_rows
            APP_STATE.group_selected_column = group_col
            APP_STATE.group_comorbidity_columns = comorb_cols
            APP_STATE.group_label_source = str(path)
            APP_STATE.group_label_folder = str(path.parent)
            APP_STATE.status = f"Loaded {len(source_rows)} cohort rows and {len(label_rows)} matched label rows for group-level analysis."
    except Exception as exc:
        APP_STATE.status = f"Could not load group labels: {exc}"
    return RedirectResponse(url="/?step=step7", status_code=303)


@app.get("/action/update_group_label_columns")
def update_group_label_columns(group_col: str = "", comorbidity_cols: list[str] = Query(default=[])) -> RedirectResponse:
    try:
        if not APP_STATE.group_source_rows:
            APP_STATE.status = "Load a cohort/group metadata file before selecting group columns."
        else:
            resolved = _resolve_group_column(APP_STATE.group_source_rows, group_col) or _default_group_column(APP_STATE.group_source_rows)
            if not resolved:
                APP_STATE.status = "No valid group label column was found in the cohort file."
            else:
                selected_comorbidity = [str(c) for c in comorbidity_cols if str(c) in APP_STATE.group_source_rows[0]]
                APP_STATE.group_selected_column = resolved
                APP_STATE.group_comorbidity_columns = selected_comorbidity
                APP_STATE.group_label_rows = _build_group_labels_from_source(APP_STATE.group_source_rows, resolved, selected_comorbidity)
                groups = len({str(row.get("group_label", "")) for row in APP_STATE.group_label_rows if row.get("group_label") not in (None, "")})
                APP_STATE.status = f"Updated group labels using column '{resolved}': {groups} groups across {len(APP_STATE.group_label_rows)} matched subjects."
    except Exception as exc:
        APP_STATE.status = f"Could not update group label columns: {exc}"
    return RedirectResponse(url="/?step=step7", status_code=303)


@app.get("/action/pick_app_categories")
def pick_app_categories(
    username: str = "All",
    sensor_name: str = "All",
    feature_name: str = "all",
    temporal_frequency: str = "daily",
    feature_mode: str = "core",
) -> RedirectResponse:
    step3_query = urlencode({
        "step": "step3",
        "username": username,
        "sensor_name": sensor_name,
        "feature_name": feature_name,
        "temporal_frequency": temporal_frequency,
        "feature_mode": feature_mode,
    })
    selected = _choose_file_native("Choose app category CSV")
    if not selected:
        APP_STATE.status = "Category-file picker was cancelled or is not available."
        return RedirectResponse(url=f"/?{step3_query}", status_code=303)

    query = urlencode({
        "mapping_path": selected,
        "username": username,
        "sensor_name": sensor_name,
        "feature_name": feature_name,
        "temporal_frequency": temporal_frequency,
        "feature_mode": feature_mode,
    })
    return RedirectResponse(url=f"/action/load_app_categories?{query}", status_code=303)


@app.get("/action/load_app_categories")
def load_app_categories(
    mapping_path: str,
    username: str = "All",
    sensor_name: str = "All",
    feature_name: str = "all",
    temporal_frequency: str = "daily",
    feature_mode: str = "core",
) -> RedirectResponse:
    step3_query = urlencode({
        "step": "step3",
        "username": username,
        "sensor_name": sensor_name,
        "feature_name": feature_name,
        "temporal_frequency": temporal_frequency,
        "feature_mode": feature_mode,
    })
    try:
        path = Path(mapping_path).expanduser().resolve()
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        mapping = load_app_category_mapping_from_text(text)
    except OSError:
        APP_STATE.status = "The selected app-category file could not be read."
        return RedirectResponse(url=f"/?{step3_query}", status_code=303)

    if not mapping:
        APP_STATE.status = "The selected app-category file was read, but no app mappings were detected."
        return RedirectResponse(url=f"/?{step3_query}", status_code=303)

    APP_STATE.app_category_map = mapping
    APP_STATE.app_category_source = str(path)
    APP_STATE.app_usage_daily = []
    APP_STATE.app_usage_category_daily = []
    APP_STATE.app_usage_category_daily_wide = []
    APP_STATE.app_usage_review = None
    APP_STATE.location_daily = []
    APP_STATE.location_trajectory = []
    APP_STATE.location_review = None
    APP_STATE.pedometer_daily = []
    APP_STATE.pedometer_review = None
    APP_STATE.generic_sensor_daily = []
    APP_STATE.activity_features = []
    APP_STATE.status = (
        f"App-category file loaded at {iso_now()}. "
        f"Loaded {len(mapping)} app mappings from {APP_STATE.app_category_source}."
    )
    return RedirectResponse(url=f"/?{step3_query}", status_code=303)


@app.get("/action/use_default_app_categories")
def use_default_app_categories(
    username: str = "All",
    sensor_name: str = "All",
    feature_name: str = "all",
    temporal_frequency: str = "daily",
    feature_mode: str = "core",
) -> RedirectResponse:
    step3_query = urlencode({
        "step": "step3",
        "username": username,
        "sensor_name": sensor_name,
        "feature_name": feature_name,
        "temporal_frequency": temporal_frequency,
        "feature_mode": feature_mode,
    })
    APP_STATE.app_category_map = load_app_category_mapping()
    APP_STATE.app_category_source = str(DEFAULT_APP_CATEGORY_PATH) if APP_STATE.app_category_map else None
    APP_STATE.app_usage_daily = []
    APP_STATE.app_usage_category_daily = []
    APP_STATE.app_usage_category_daily_wide = []
    APP_STATE.app_usage_review = None
    APP_STATE.location_daily = []
    APP_STATE.location_trajectory = []
    APP_STATE.location_review = None
    APP_STATE.pedometer_daily = []
    APP_STATE.pedometer_review = None
    if APP_STATE.app_category_map:
        APP_STATE.status = (
            f"Default app-category file restored at {iso_now()}. "
            f"Loaded {len(APP_STATE.app_category_map)} app mappings."
        )
    else:
        APP_STATE.status = "Default app-category file could not be loaded."
    return RedirectResponse(url=f"/?{step3_query}", status_code=303)


@app.get("/action/apply_scope")
def apply_scope(username: str = "All", device_id: str = "All", sensor_name: str = "All", wearable_sensor: str = "All") -> RedirectResponse:
    APP_STATE.filtered_rows = filter_indexed_files(
        APP_STATE.indexed_rows,
        username=username,
        device_id=device_id,
        sensor_name=sensor_name,
        wearable_sensor=wearable_sensor,
    )
    APP_STATE.selected_sensor_name = None if sensor_name == "All" else sensor_name
    APP_STATE.selected_wearable_sensor = None if wearable_sensor == "All" else wearable_sensor
    APP_STATE.loaded_rows = []
    APP_STATE.loaded_filtered_rows = []
    APP_STATE.app_usage_daily = []
    APP_STATE.app_usage_category_daily = []
    APP_STATE.app_usage_category_daily_wide = []
    APP_STATE.app_usage_review = None
    APP_STATE.location_daily = []
    APP_STATE.location_trajectory = []
    APP_STATE.location_review = None
    APP_STATE.pedometer_daily = []
    APP_STATE.pedometer_review = None
    APP_STATE.generic_sensor_daily = []
    APP_STATE.activity_features = []
    APP_STATE.status = f"Metadata filters applied at {iso_now()}. {summarize_indexed_files(APP_STATE.filtered_rows)['json_files']} JSON files remain in scope."
    query = urlencode(
        {
            "step": "step3b",
            "username": username,
            "device_id": device_id,
            "sensor_name": sensor_name,
            "wearable_sensor": wearable_sensor,
        }
    )
    return RedirectResponse(
        url=f"/?{query}",
        status_code=303,
    )


@app.get("/action/load_scoped_data")
def load_scoped_data() -> RedirectResponse:
    APP_STATE.loaded_rows = load_indexed_json_rows(APP_STATE.filtered_rows)
    APP_STATE.loaded_filtered_rows = APP_STATE.loaded_rows
    APP_STATE.app_usage_daily = []
    APP_STATE.app_usage_category_daily = []
    APP_STATE.app_usage_category_daily_wide = []
    APP_STATE.app_usage_review = None
    APP_STATE.location_daily = []
    APP_STATE.location_trajectory = []
    APP_STATE.location_review = None
    APP_STATE.pedometer_daily = []
    APP_STATE.pedometer_review = None
    summary = summarize_loaded_rows(APP_STATE.loaded_rows)
    APP_STATE.status = f"Scoped data loaded at {iso_now()}. {summary.records_loaded} records are ready for the next Python step."
    return RedirectResponse(url="/?step=step3c", status_code=303)


@app.get("/action/apply_loaded_filters")
def apply_loaded_filters(min_day: int = 0, max_day: int = 0) -> RedirectResponse:
    APP_STATE.loaded_filtered_rows = filter_loaded_rows_by_study_day(
        APP_STATE.loaded_rows,
        min_day=min_day,
        max_day=max_day,
    )
    summary = summarize_loaded_rows(APP_STATE.loaded_filtered_rows)
    APP_STATE.app_usage_daily = []
    APP_STATE.app_usage_category_daily = []
    APP_STATE.app_usage_category_daily_wide = []
    APP_STATE.app_usage_review = None
    APP_STATE.location_daily = []
    APP_STATE.location_trajectory = []
    APP_STATE.location_review = None
    APP_STATE.pedometer_daily = []
    APP_STATE.pedometer_review = None
    APP_STATE.generic_sensor_daily = []
    APP_STATE.activity_features = []
    APP_STATE.status = f"Loaded-data filters applied at {iso_now()}. {summary.records_loaded} records remain in range."
    return RedirectResponse(url=f"/?step=step4&min_day={min_day}&max_day={max_day}", status_code=303)



@app.get("/action/compute_feature")
def compute_feature(
    username: str = "All",
    sensor_name: str = "All",
    feature_names: list[str] | None = Query(default=None),
    temporal_frequency: str = "daily",
    feature_mode: str = "core",
) -> RedirectResponse:
    if feature_names is None or not [str(v).strip() for v in feature_names if str(v).strip()]:
        selected_features = _default_feature_names_for_sensor(sensor_name, feature_mode)
    else:
        selected_features = _normalise_feature_names(feature_names)
    if "custom_script" in selected_features:
        selected_features = ["custom_script"]
    feature_key = _feature_key_for_selection(sensor_name, selected_features)
    if temporal_frequency not in {value for value, _ in TEMPORAL_FREQUENCY_CHOICES}:
        temporal_frequency = "daily"

    APP_STATE.filtered_rows = _filter_index_for_feature(username=username, sensor_name=sensor_name)
    APP_STATE.loaded_rows = load_indexed_json_rows(APP_STATE.filtered_rows)
    APP_STATE.loaded_filtered_rows = APP_STATE.loaded_rows
    APP_STATE.selected_sensor_name = None if sensor_name == "All" else sensor_name
    APP_STATE.selected_wearable_sensor = None
    APP_STATE.app_usage_daily = []
    APP_STATE.app_usage_category_daily = []
    APP_STATE.app_usage_category_daily_wide = []
    APP_STATE.app_usage_review = None
    APP_STATE.location_daily = []
    APP_STATE.location_trajectory = []
    APP_STATE.location_review = None
    APP_STATE.pedometer_daily = []
    APP_STATE.pedometer_review = None
    APP_STATE.generic_sensor_daily = []
    APP_STATE.activity_features = []
    APP_STATE.custom_feature_rows = []
    APP_STATE.custom_feature_last_error = None
    APP_STATE.feature_qc_rows = []
    APP_STATE.feature_qc_audit_rows = []
    APP_STATE.feature_qc_description = None
    APP_STATE.generated_feature_key = feature_key
    APP_STATE.generated_feature_name = ",".join(selected_features)
    APP_STATE.generated_temporal_frequency = temporal_frequency

    rows = APP_STATE.loaded_filtered_rows or APP_STATE.loaded_rows
    if feature_key == "custom_feature":
        try:
            APP_STATE.custom_feature_rows = _standardize_generated_feature_names(
                _run_custom_feature_script(rows, sensor_name=sensor_name, temporal_frequency=temporal_frequency, selected_features=selected_features)
            )
        except Exception as exc:
            APP_STATE.custom_feature_rows = []
            APP_STATE.custom_feature_last_error = str(exc)
            APP_STATE.status = f"Custom feature script failed: {exc}"
            query = urlencode({"step": "step3", "username": username, "sensor_name": sensor_name, "feature_name": "custom_script", "temporal_frequency": temporal_frequency, "feature_mode": "custom"})
            return RedirectResponse(url=f"/?{query}", status_code=303)
    elif feature_key in {"application_usage_daily", "application_usage_category_daily"}:
        review, daily, category_daily, category_daily_wide = review_application_usage(
            rows,
            app_categories=APP_STATE.app_category_map,
        )
        APP_STATE.app_usage_daily = daily
        APP_STATE.app_usage_category_daily = category_daily
        APP_STATE.app_usage_category_daily_wide = category_daily_wide
        if review is not None:
            APP_STATE.app_usage_review = {
                "records": review.records,
                "subjects": review.subjects,
                "distinct_days": review.distinct_days,
                "unique_apps": review.unique_apps,
                "unique_categories": review.unique_categories,
                "total_foreground_hours": review.total_foreground_hours,
                "mean_daily_foreground_hours": review.mean_daily_foreground_hours,
                "top_app": review.top_app,
                "top_app_hours": review.top_app_hours,
                "top_category": review.top_category,
                "top_category_hours": review.top_category_hours,
            }
    elif feature_key == "location_daily":
        review, daily, trajectory = review_location(rows)
        APP_STATE.location_daily = daily
        APP_STATE.location_trajectory = trajectory
        if review is not None:
            APP_STATE.location_review = {
                "records": review.records,
                "subjects": review.subjects,
                "distinct_days": review.distinct_days,
                "providers": review.providers,
                "total_distance_km": review.total_distance_km,
                "mean_daily_distance_km": review.mean_daily_distance_km,
                "median_accuracy_m": review.median_accuracy_m,
                "max_daily_distance_km": review.max_daily_distance_km,
            }
    elif feature_key == "pedometer_daily":
        review, daily = review_pedometer(rows)
        APP_STATE.pedometer_daily = daily
        if review is not None:
            APP_STATE.pedometer_review = {
                "records": review.records,
                "subjects": review.subjects,
                "distinct_days": review.distinct_days,
                "total_steps": review.total_steps,
                "mean_daily_steps": review.mean_daily_steps,
                "median_daily_steps": review.median_daily_steps,
                "peak_daily_steps": review.peak_daily_steps,
                "goal_days_10000": review.goal_days_10000,
            }
    elif feature_key == "activity_features":
        APP_STATE.activity_features = _activity_features(
            rows,
            temporal_frequency=temporal_frequency,
            selected_features=selected_features,
        )
    elif feature_key == "sensor_daily_summary":
        APP_STATE.generic_sensor_daily = _generic_sensor_daily_features(
            rows,
            sensor_name=sensor_name,
            temporal_frequency=temporal_frequency,
            selected_features=selected_features,
        )

    # Keep identifiers plus the selected sensor-specific feature columns. Selecting
    # "all" keeps all computed columns.
    feature_name_for_filter = "all" if "all" in selected_features else ",".join(selected_features)
    if feature_key == "application_usage_daily":
        APP_STATE.app_usage_daily = _filter_feature_columns(APP_STATE.app_usage_daily, sensor_name, feature_name_for_filter)
        APP_STATE.app_usage_daily = _standardize_generated_feature_names(_aggregate_feature_rows_by_temporal(APP_STATE.app_usage_daily, temporal_frequency))
    elif feature_key == "application_usage_category_daily":
        # Compute category usage directly at the selected temporal resolution
        # (daily/hourly/monthly/full study). This restores the previous wide
        # per-category table: one row per subject/time bin and one column per
        # app category, while temporal_frequency/time_bin store the frequency.
        APP_STATE.app_usage_category_daily_wide = extract_application_usage_category_features(
            rows,
            app_categories=APP_STATE.app_category_map,
            temporal_frequency=temporal_frequency,
        )
        APP_STATE.app_usage_category_daily = APP_STATE.app_usage_category_daily_wide
        # Do not apply generic feature-column filtering here: category columns
        # are data-driven and named by category (as in the original workflow).
        APP_STATE.app_usage_category_daily_wide = _standardize_generated_feature_names(APP_STATE.app_usage_category_daily_wide)
    elif feature_key == "location_daily":
        APP_STATE.location_daily = _filter_feature_columns(APP_STATE.location_daily, sensor_name, feature_name_for_filter)
        APP_STATE.location_daily = _standardize_generated_feature_names(_aggregate_feature_rows_by_temporal(APP_STATE.location_daily, temporal_frequency))
    elif feature_key == "pedometer_daily":
        APP_STATE.pedometer_daily = _filter_feature_columns(APP_STATE.pedometer_daily, sensor_name, feature_name_for_filter)
        APP_STATE.pedometer_daily = _standardize_generated_feature_names(_aggregate_feature_rows_by_temporal(APP_STATE.pedometer_daily, temporal_frequency))
    elif feature_key == "activity_features":
        APP_STATE.activity_features = _standardize_generated_feature_names(_filter_feature_columns(APP_STATE.activity_features, sensor_name, feature_name_for_filter))
    elif feature_key == "sensor_daily_summary":
        APP_STATE.generic_sensor_daily = _standardize_generated_feature_names(_filter_feature_columns(APP_STATE.generic_sensor_daily, sensor_name, feature_name_for_filter))
    elif feature_key == "custom_feature":
        APP_STATE.custom_feature_rows = _standardize_generated_feature_names(APP_STATE.custom_feature_rows)

    generated_rows = _generated_feature_rows(feature_key)
    metric_lookup = dict(_feature_metric_choices_for_sensor(sensor_name, feature_mode))
    metric_labels = [metric_lookup.get(name, name) for name in selected_features]
    APP_STATE.status = (
        f"Feature computation completed at {iso_now()}. "
        f"Computed {', '.join(metric_labels)} at {temporal_frequency} frequency with {len(generated_rows)} rows."
    )
    query = urlencode({"step": "step3", "username": username, "sensor_name": sensor_name, "feature_name": ",".join(selected_features), "temporal_frequency": temporal_frequency, "feature_mode": feature_mode})
    return RedirectResponse(url=f"/?{query}", status_code=303)




@app.get("/download/custom_feature_template.py")
def download_custom_feature_template() -> Response:
    return Response(
        CUSTOM_FEATURE_TEMPLATE,
        media_type="text/x-python",
        headers={"Content-Disposition": "attachment; filename=jtrack_custom_feature_template.py"},
    )


@app.post("/action/upload_custom_feature_script")
async def upload_custom_feature_script(script_file: UploadFile = File(...)) -> RedirectResponse:
    filename = Path(script_file.filename or "custom_feature.py").name
    if not filename.lower().endswith(".py"):
        APP_STATE.custom_feature_last_error = "Please upload a Python .py file."
        return RedirectResponse(url="/?step=step3&feature_mode=custom", status_code=303)
    content = await script_file.read()
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        text = content.decode("utf-8-sig", errors="replace")
    if "def compute_feature" not in text:
        APP_STATE.custom_feature_last_error = "Script uploaded, but compute_feature(rows, context) was not found."
    else:
        APP_STATE.custom_feature_last_error = None
    APP_STATE.custom_feature_script_name = filename
    APP_STATE.custom_feature_script_text = text
    APP_STATE.custom_feature_rows = []
    APP_STATE.generated_feature_key = None
    APP_STATE.status = f"Custom feature script uploaded: {filename}. Select Custom uploaded script in Step 3 to compute it."
    return RedirectResponse(url="/?step=step3&feature_mode=custom&feature_name=custom_script", status_code=303)


@app.get("/action/apply_quality_control")
def apply_quality_control(
    feature_key: str = "All",
    qc_column: str = "All",
    min_day: str = "",
    max_day: str = "",
    min_records_per_subject: str = "",
    min_active_bins_per_subject: str = "",
    min_value: str = "",
    max_value: str = "",
) -> RedirectResponse:
    if feature_key == "All" and APP_STATE.generated_feature_key:
        feature_key = APP_STATE.generated_feature_key
    rows = list(_generated_feature_rows(feature_key, use_qc=False))
    if not rows:
        APP_STATE.feature_qc_rows = []
        APP_STATE.feature_qc_audit_rows = []
        APP_STATE.feature_qc_description = "No generated feature rows were available for Step 4 QC."
        return RedirectResponse(url="/?step=step4", status_code=303)

    kept, audit, summary = _quality_control_decisions(
        rows,
        qc_column=qc_column,
        min_day=min_day,
        max_day=max_day,
        min_records_per_subject=min_records_per_subject,
        min_active_bins_per_subject=min_active_bins_per_subject,
        min_value=min_value,
        max_value=max_value,
    )
    APP_STATE.generated_feature_key = feature_key
    APP_STATE.feature_qc_rows = kept
    APP_STATE.feature_qc_audit_rows = audit

    rules: list[str] = []
    if str(min_day).strip():
        rules.append(f"min study day >= {min_day}")
    if str(max_day).strip():
        rules.append(f"max study day <= {max_day}")
    if str(min_records_per_subject).strip():
        rules.append(f"min rows/subject >= {min_records_per_subject}")
    if str(min_active_bins_per_subject).strip():
        rules.append(f"min active bins/subject >= {min_active_bins_per_subject}")
    if qc_column != "All" and (str(min_value).strip() or str(max_value).strip()):
        lo = min_value if str(min_value).strip() else "-inf"
        hi = max_value if str(max_value).strip() else "+inf"
        rules.append(f"{qc_column} in [{lo}, {hi}]")
    if not rules:
        rules.append("no exclusion rules selected")

    APP_STATE.feature_qc_description = (
        f"Step 4 QC applied to {FEATURE_LABELS.get(feature_key, feature_key)}: "
        f"{'; '.join(rules)}. Rows kept: {summary['rows_after']} of {summary['rows_before']}; "
        f"subjects retained: {summary['subjects_after']} of {summary['subjects_before']}."
    )
    query = urlencode({
        "step": "step4",
        "feature_key": feature_key,
        "qc_column": qc_column,
        "min_day": min_day,
        "max_day": max_day,
        "min_records_per_subject": min_records_per_subject,
        "min_active_bins_per_subject": min_active_bins_per_subject,
        "min_value": min_value,
        "max_value": max_value,
    })
    return RedirectResponse(url=f"/?{query}", status_code=303)


@app.get("/action/reset_quality_control")
def reset_quality_control() -> RedirectResponse:
    APP_STATE.feature_qc_rows = []
    APP_STATE.feature_qc_audit_rows = []
    APP_STATE.feature_qc_description = None
    return RedirectResponse(url="/?step=step4", status_code=303)


@app.get("/action/apply_feature_qc")
def apply_feature_qc(feature_key: str = "All", qc_column: str = "All", min_value: str = "", max_value: str = "") -> RedirectResponse:
    if feature_key == "All" and APP_STATE.generated_feature_key:
        feature_key = APP_STATE.generated_feature_key
    rows = list(_generated_feature_rows(feature_key, use_qc=False))
    if not rows:
        APP_STATE.feature_qc_rows = []
        APP_STATE.feature_qc_audit_rows = []
        APP_STATE.feature_qc_description = "No generated feature rows were available for feature-level QC."
        return RedirectResponse(url="/?step=step4", status_code=303)

    columns = _numeric_columns(rows) if qc_column == "All" else [qc_column]
    min_num = float(min_value) if str(min_value).strip() else None
    max_num = float(max_value) if str(max_value).strip() else None
    filtered = []
    for row in rows:
        keep = True
        for col in columns:
            if col not in row:
                continue
            try:
                value = float(row.get(col))
            except (TypeError, ValueError):
                continue
            if min_num is not None and value < min_num:
                keep = False
            if max_num is not None and value > max_num:
                keep = False
        if keep:
            filtered.append(row)
    APP_STATE.generated_feature_key = feature_key
    APP_STATE.feature_qc_rows = filtered
    APP_STATE.feature_qc_audit_rows = [dict(row, qc_status=("Pass" if row in filtered else "Exclude"), qc_reason=("" if row in filtered else "Feature-level value filter")) for row in rows]
    column_text = qc_column if qc_column != "All" else "all numeric feature columns"
    APP_STATE.feature_qc_description = (
        f"Feature-level QC applied to {FEATURE_LABELS.get(feature_key, feature_key)} using {column_text}. "
        f"Rows kept: {len(filtered)} of {len(rows)}."
    )
    query = urlencode({"step": "step4", "feature_key": feature_key, "qc_column": qc_column, "min_value": min_value, "max_value": max_value})
    return RedirectResponse(url=f"/?{query}", status_code=303)


@app.get("/action/run_app_usage_review")
def run_app_usage_review() -> RedirectResponse:
    review, daily, category_daily, category_daily_wide = review_application_usage(
        APP_STATE.loaded_filtered_rows or APP_STATE.loaded_rows,
        app_categories=APP_STATE.app_category_map,
    )
    APP_STATE.app_usage_daily = daily
    APP_STATE.app_usage_category_daily = category_daily
    APP_STATE.app_usage_category_daily_wide = category_daily_wide
    APP_STATE.app_usage_review = None
    APP_STATE.location_daily = []
    APP_STATE.location_trajectory = []
    APP_STATE.location_review = None
    APP_STATE.pedometer_daily = []
    APP_STATE.pedometer_review = None
    if review is not None:
        APP_STATE.app_usage_review = {
            "records": review.records,
            "subjects": review.subjects,
            "distinct_days": review.distinct_days,
            "unique_apps": review.unique_apps,
            "unique_categories": review.unique_categories,
            "total_foreground_hours": review.total_foreground_hours,
            "mean_daily_foreground_hours": review.mean_daily_foreground_hours,
            "top_app": review.top_app,
            "top_app_hours": review.top_app_hours,
            "top_category": review.top_category,
            "top_category_hours": review.top_category_hours,
        }
        APP_STATE.status = f"Application-usage review completed at {iso_now()}."
    else:
        APP_STATE.status = "Application-usage review found no usable APPLICATION_USAGE rows in the current loaded scope."
    return RedirectResponse(url="/?step=step4", status_code=303)


@app.get("/action/run_location_review")
def run_location_review() -> RedirectResponse:
    review, daily, trajectory = review_location(APP_STATE.loaded_filtered_rows or APP_STATE.loaded_rows)
    APP_STATE.location_daily = daily
    APP_STATE.location_trajectory = trajectory
    APP_STATE.location_review = None
    APP_STATE.app_usage_daily = []
    APP_STATE.app_usage_category_daily = []
    APP_STATE.app_usage_category_daily_wide = []
    APP_STATE.app_usage_review = None
    APP_STATE.pedometer_daily = []
    APP_STATE.pedometer_review = None
    if review is not None:
        APP_STATE.location_review = {
            "records": review.records,
            "subjects": review.subjects,
            "distinct_days": review.distinct_days,
            "providers": review.providers,
            "total_distance_km": review.total_distance_km,
            "mean_daily_distance_km": review.mean_daily_distance_km,
            "median_accuracy_m": review.median_accuracy_m,
            "max_daily_distance_km": review.max_daily_distance_km,
        }
        APP_STATE.status = f"Location review completed at {iso_now()}."
    else:
        APP_STATE.status = "Location review found no usable LOCATION rows in the current loaded scope."
    return RedirectResponse(url="/?step=step4", status_code=303)


@app.get("/action/run_pedometer_review")
def run_pedometer_review() -> RedirectResponse:
    review, daily = review_pedometer(APP_STATE.loaded_filtered_rows or APP_STATE.loaded_rows)
    APP_STATE.pedometer_daily = daily
    APP_STATE.pedometer_review = None
    APP_STATE.app_usage_daily = []
    APP_STATE.app_usage_category_daily = []
    APP_STATE.app_usage_category_daily_wide = []
    APP_STATE.app_usage_review = None
    APP_STATE.location_daily = []
    APP_STATE.location_trajectory = []
    APP_STATE.location_review = None
    if review is not None:
        APP_STATE.pedometer_review = {
            "records": review.records,
            "subjects": review.subjects,
            "distinct_days": review.distinct_days,
            "total_steps": review.total_steps,
            "mean_daily_steps": review.mean_daily_steps,
            "median_daily_steps": review.median_daily_steps,
            "peak_daily_steps": review.peak_daily_steps,
            "goal_days_10000": review.goal_days_10000,
        }
        APP_STATE.status = f"Pedometer review completed at {iso_now()}."
    else:
        APP_STATE.status = "Pedometer review found no usable PEDOMETER rows in the current loaded scope."
    return RedirectResponse(url="/?step=step4", status_code=303)


@app.get("/export/app_usage_daily.csv")
def export_app_usage_daily_csv() -> Response:
    if not APP_STATE.app_usage_daily:
        return Response(
            content="No daily application-usage features are available yet.\n",
            media_type="text/plain; charset=utf-8",
        )

    output = io.StringIO()
    fieldnames = list(APP_STATE.app_usage_daily[0].keys())
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for row in APP_STATE.app_usage_daily:
        writer.writerow({key: row.get(key) for key in fieldnames})

    return Response(
        content=output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=application_usage_daily_features.csv"},
    )


@app.get("/export/location_daily.csv")
def export_location_daily_csv() -> Response:
    if not APP_STATE.location_daily:
        return Response(
            content="No daily location features are available yet.\n",
            media_type="text/plain; charset=utf-8",
        )

    output = io.StringIO()
    fieldnames = list(APP_STATE.location_daily[0].keys())
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for row in APP_STATE.location_daily:
        writer.writerow({key: row.get(key) for key in fieldnames})

    return Response(
        content=output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=location_daily_features.csv"},
    )


@app.get("/export/pedometer_daily.csv")
def export_pedometer_daily_csv() -> Response:
    if not APP_STATE.pedometer_daily:
        return Response(
            content="No daily pedometer features are available yet.\n",
            media_type="text/plain; charset=utf-8",
        )

    output = io.StringIO()
    fieldnames = list(APP_STATE.pedometer_daily[0].keys())
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for row in APP_STATE.pedometer_daily:
        writer.writerow({key: row.get(key) for key in fieldnames})

    return Response(
        content=output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=pedometer_daily_features.csv"},
    )


@app.get("/export/app_usage_categories_daily.csv")
def export_app_usage_category_daily_csv() -> Response:
    if not APP_STATE.app_usage_category_daily_wide:
        return Response(
            content="No daily application-usage category features are available yet.\n",
            media_type="text/plain; charset=utf-8",
        )

    output = io.StringIO()
    fieldnames = list(APP_STATE.app_usage_category_daily_wide[0].keys())
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for row in APP_STATE.app_usage_category_daily_wide:
        writer.writerow({key: row.get(key) for key in fieldnames})

    return Response(
        content=output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=application_usage_daily_categories.csv"},
    )




@app.get("/action/load_group_labels")
def load_group_labels(group_label_path: str) -> RedirectResponse:
    try:
        path = Path(group_label_path).expanduser()
        source_rows, label_rows, group_col, comorb_cols = _load_group_cohort_file(str(path))
        if not source_rows:
            APP_STATE.status = "Group/cohort file was loaded but no valid subject metadata rows were detected."
        else:
            APP_STATE.group_source_rows = source_rows
            APP_STATE.group_label_rows = label_rows
            APP_STATE.group_selected_column = group_col
            APP_STATE.group_comorbidity_columns = comorb_cols
            APP_STATE.group_label_source = str(path)
            APP_STATE.group_label_folder = str(path.parent)
            APP_STATE.status = f"Loaded {len(source_rows)} cohort rows and {len(label_rows)} matched label rows for group-level analysis."
    except Exception as exc:
        APP_STATE.status = f"Could not load group labels: {exc}"
    return RedirectResponse("/?step=step7", status_code=303)

@app.get("/export/generated_features.csv")
def export_generated_features_csv(
    feature_key: str = "All",
    subject: str = "All",
    feature_column: str = "All",
    cohort: str = "All",
    sensor_name: str = "All",
    temporal_frequency: str = "All",
    feature_transform: str = "none",
) -> Response:
    if feature_key == "raw_sensor_data":
        rows = _raw_rows_filtered(subject=subject, sensor_name=sensor_name, cohort=cohort)
        filename = "jtrack_insight_raw_sensor_data.csv"
    else:
        if feature_key == "All" and APP_STATE.generated_feature_key:
            feature_key = APP_STATE.generated_feature_key
        rows = _filtered_feature_rows(feature_key, subject=subject, feature_column=feature_column, cohort=cohort)
        rows = _filter_rows_by_temporal_frequency(rows, temporal_frequency)
        rows, _transformed_column, _transform_info = _transform_feature_rows(rows, feature_column, feature_transform)
        filename = "jtrack_insight_generated_features.csv"
    if not rows:
        return Response(
            content="No data are available for the current filters.\n",
            media_type="text/plain; charset=utf-8",
        )
    output = io.StringIO()
    fieldnames = list(rows[0].keys())
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: row.get(key) for key in fieldnames})
    return Response(
        content=output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )




@app.get("/export/report.html")
def export_report_html(
    feature_key: str = "All",
    subject: str = "All",
    feature_column: str = "All",
    cohort: str = "All",
    feature_transform: str = "none",
) -> Response:
    content = _build_report_html(
        feature_key=feature_key,
        subject=subject,
        feature_column=feature_column,
        cohort=cohort,
        feature_transform=feature_transform,
        for_download=True,
    )
    document = f"""<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>JTrack Insight Analysis Report</title>
  </head>
  <body>{content}</body>
</html>
"""
    return Response(
        content=document,
        media_type="text/html; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=jtrack_insight_report.html"},
    )


def main() -> None:
    uvicorn.run("trackautism_app.server.web:app", host="127.0.0.1", port=8000, reload=False)
