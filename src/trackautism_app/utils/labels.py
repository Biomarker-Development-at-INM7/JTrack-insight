import re
import numpy as np


def normalize_label(x):
    if x is None:
        return np.nan
    try:
        if np.isnan(x):
            return np.nan
    except Exception:
        pass

    y = str(x).strip().upper()

    aliases = {
        "APPLICATIONUSAGE": "APPLICATION_USAGE",
        "APPUSAGE": "APPLICATION_USAGE",
        "USAGE": "APPLICATION_USAGE",
        "LOCK_UNLOCK": "LOCKUNLOCK",
        "LOCKUNLOCK": "LOCKUNLOCK",
    }

    return aliases.get(y, y)


def label_key(x):
    if x is None:
        return ""
    try:
        if np.isnan(x):
            return ""
    except Exception:
        pass

    return re.sub(r"[^A-Za-z0-9]+", "", str(x).upper())


def activity_type_label(x):
    try:
        code = int(float(x))
    except Exception:
        return "TYPE_NA"

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