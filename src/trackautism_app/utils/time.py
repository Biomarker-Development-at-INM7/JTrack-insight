"""Time parsing and normalization helpers."""
import numpy as np
import pandas as pd

from datetime import datetime


def iso_now() -> str:
    """Return a compact ISO timestamp string."""
    return datetime.now().astimezone().isoformat(timespec="seconds")




def safe_num(x):
    return pd.to_numeric(x, errors="coerce")


def stream_with_time(df):
    out = df.copy()

    if out.empty:
        out["analysis_time"] = pd.Series(dtype="float")
        out["analysis_time_dt"] = pd.to_datetime(pd.Series(dtype="float"), utc=True)
        out["window_sec"] = pd.Series(dtype="float")
        out["timing_mode"] = pd.Series(dtype="object")
        return out

    for col in ["timestamp", "timestamp_start", "timestamp_end", "startTime", "endTime"]:
        if col in out.columns:
            out[col] = safe_num(out[col])

    if "timestamp_start" not in out.columns and "startTime" in out.columns:
        out["timestamp_start"] = out["startTime"]

    if "timestamp_end" not in out.columns and "endTime" in out.columns:
        out["timestamp_end"] = out["endTime"]

    if "timestamp" not in out.columns:
        out["timestamp"] = np.nan

    has_windows = (
        "timestamp_start" in out.columns
        and "timestamp_end" in out.columns
        and np.isfinite(out["timestamp_start"]).any()
        and np.isfinite(out["timestamp_end"]).any()
    )

    if has_windows:
        valid_window = np.isfinite(out["timestamp_start"]) & np.isfinite(out["timestamp_end"])
        out["analysis_time"] = np.where(
            valid_window,
            (out["timestamp_start"] + out["timestamp_end"]) / 2,
            out["timestamp"],
        )
        out["window_sec"] = np.where(
            valid_window,
            np.maximum(0, (out["timestamp_end"] - out["timestamp_start"]) / 1000),
            np.nan,
        )
        out["timing_mode"] = np.where(valid_window, "window", "point")
    else:
        out["analysis_time"] = out["timestamp"]
        out["window_sec"] = np.nan
        out["timing_mode"] = "point"

    out["analysis_time_dt"] = pd.to_datetime(
        out["analysis_time"],
        unit="ms",
        origin="unix",
        utc=True,
        errors="coerce",
    )

    return out.sort_values("analysis_time")