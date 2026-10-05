"""Lab normalization and objective series metrics. No clinical-significance judgment lives here."""
from __future__ import annotations

import hashlib
import re

from .persistence import DB, now

# test_id -> (canonical unit, aliases). Small on purpose; unmapped names still form their own series.
TESTS = {
    "cr": ("μmol/L", ["肌酐", "血肌酐", "cr", "scr", "crea"]),
    "urea": ("mmol/L", ["尿素", "尿素氮", "urea", "bun"]),
    "k": ("mmol/L", ["钾", "血钾", "k", "k+"]),
    "na": ("mmol/L", ["钠", "血钠", "na", "na+"]),
    "glu": ("mmol/L", ["葡萄糖", "血糖", "glu"]),
    "hb": ("g/L", ["血红蛋白", "hb", "hgb"]),
    "wbc": ("10^9/L", ["白细胞", "白细胞计数", "wbc"]),
    "plt": ("10^9/L", ["血小板", "血小板计数", "plt"]),
    "alt": ("U/L", ["谷丙转氨酶", "丙氨酸氨基转移酶", "alt", "gpt"]),
    "ast": ("U/L", ["谷草转氨酶", "天门冬氨酸氨基转移酶", "天冬氨酸氨基转移酶", "ast", "got"]),
    "tbil": ("μmol/L", ["总胆红素", "tbil"]),
    "alb": ("g/L", ["白蛋白", "alb"]),
    "crp": ("mg/L", ["c反应蛋白", "c-反应蛋白", "crp"]),
    "pct": ("ng/mL", ["降钙素原"]),  # no "PCT" alias: in a blood count PCT is plateletcrit
    "neut_pct": ("%", ["中性粒细胞百分比", "中性粒细胞比例", "neut%", "ne%"]),
    "lac": ("mmol/L", ["乳酸", "lac"]),
    "ua": ("μmol/L", ["尿酸", "ua"]),
    "cl": ("mmol/L", ["氯", "血氯", "cl"]),
    "dbil": ("μmol/L", ["直接胆红素", "dbil"]),
    "tp": ("g/L", ["总蛋白", "tp"]),
    "ggt": ("U/L", ["γ-谷氨酰转肽酶", "谷氨酰转肽酶", "ggt"]),
    "alp": ("U/L", ["碱性磷酸酶", "alp"]),
    "rbc": ("10^12/L", ["红细胞", "红细胞计数", "rbc"]),
    "esr": ("mm/h", ["血沉", "红细胞沉降率", "esr"]),
    "hcy": ("μmol/L", ["同型半胱氨酸", "hcy"]),
}
_ALIAS = {a: tid for tid, (_, names) in TESTS.items() for a in names}

# Spelling variants only; never maps between different quantities.
# Note "G/L" is deliberately absent: ambiguous (giga-cells vs grams) -> stays unmapped, never merged.
_UNIT_SPELLINGS = {
    "μmol/l": "μmol/L", "umol/l": "μmol/L",
    "mmol/l": "mmol/L", "meq/l": "mEq/L",
    "mg/dl": "mg/dL", "g/dl": "g/dL", "g/l": "g/L", "mg/l": "mg/L",
    "u/l": "U/L", "iu/l": "U/L", "ng/ml": "ng/mL",
    "10^9/l": "10^9/L", "×10^9/l": "10^9/L", "x10^9/l": "10^9/L", "*10^9/l": "10^9/L", "10*9/l": "10^9/L",
    "10e9/l": "10^9/L", "/μl": "/μL", "/ul": "/μL", "cells/μl": "/μL",
}

# (test_id, from unit) -> factor to canonical. Only well-established, analyte-specific factors.
_FACTORS = {
    ("cr", "mg/dL"): 88.4,
    ("glu", "mg/dL"): 1 / 18.016,
    ("hb", "g/dL"): 10.0,
    ("alb", "g/dL"): 10.0,
    ("tbil", "mg/dL"): 17.1,
    ("crp", "mg/dL"): 10.0,
    ("k", "mEq/L"): 1.0,
    ("na", "mEq/L"): 1.0,
    ("wbc", "/μL"): 0.001,
    ("plt", "/μL"): 0.001,
}

# ponytail: no critical-value table yet; critical flags come only from reports. Add per-analyte
# thresholds from curated knowledge, never invented here.
CRITICAL: dict[str, tuple[float | None, float | None]] = {}

_NUMBER = re.compile(r"^[+-]?\d+(\.\d+)?$")
_RANGE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*[-~～—–]+\s*(\d+(?:\.\d+)?)\s*$")
_BOUND = re.compile(r"^\s*(<=|>=|<|>|≤|≥)\s*(\d+(?:\.\d+)?)\s*$")
_FLAGS = {"↑": "high", "h": "high", "高": "high", "high": "high", "↑↑": "high",
          "↓": "low", "l": "low", "低": "low", "low": "low", "↓↓": "low",
          "危急↑": "critical_high", "危急↓": "critical_low"}
ABNORMAL = {"high", "low", "critical_high", "critical_low", "abnormal"}


def test_id_for(name: str) -> str:
    key = re.sub(r"\s+", "", name).lower()
    candidates = [key] + re.split(r"[()（）]", key)
    for c in candidates:
        if c in _ALIAS:
            return _ALIAS[c]
    base = re.split(r"[(（]", key)[0] or key  # "名称(缩写)" and "名称" form one series
    return "unmapped_" + hashlib.sha1(base.encode()).hexdigest()[:8]


def unit_spelling(unit: str | None) -> str | None:
    if not unit:
        return None
    u = re.sub(r"\s+", "", unit).replace("µ", "μ")
    return _UNIT_SPELLINGS.get(u.lower(), u)


def parse_reference(text: str | None) -> tuple[float | None, float | None]:
    if not text:
        return None, None
    m = _RANGE.match(text)
    if m:
        return float(m.group(1)), float(m.group(2))
    m = _BOUND.match(text)
    if m:
        v = float(m.group(2))
        return (None, v) if m.group(1) in ("<", "<=", "≤") else (v, None)
    return None, None


def normalize(test_id: str, raw_value: str, raw_unit: str | None, ref_low, ref_high) -> dict:
    """Returns value_type, normalized value/unit and reference bounds in the normalized unit."""
    value = raw_value.strip()
    if not _NUMBER.match(value):  # qualitative or censored ("<0.01"): kept raw, never trended
        return {"value_type": "qualitative", "normalized_value": None, "normalized_unit": None,
                "reference_low": ref_low, "reference_high": ref_high}
    unit = unit_spelling(raw_unit)
    canonical = TESTS.get(test_id, (None,))[0]
    factor = 1.0 if unit == canonical else _FACTORS.get((test_id, unit))
    if factor is None:  # no reliable rule: keep the original unit
        factor, canonical = 1.0, unit
    scale = lambda v: None if v is None else _round(v * factor)  # noqa: E731
    return {"value_type": "numeric", "normalized_value": scale(float(value)), "normalized_unit": canonical,
            "reference_low": scale(ref_low), "reference_high": scale(ref_high)}


def abnormal_flag(flag_text: str | None, test_id: str, norm: dict) -> tuple[str, str]:
    reported = _FLAGS.get(re.sub(r"\s+", "", flag_text or "").lower())
    if reported:
        return reported, "report"
    v = norm["normalized_value"]
    if v is None:
        return "unknown", "unknown"
    lo, hi = CRITICAL.get(test_id, (None, None))
    if lo is not None and v < lo:
        return "critical_low", "computed"
    if hi is not None and v > hi:
        return "critical_high", "computed"
    low, high = norm["reference_low"], norm["reference_high"]
    if low is None and high is None:
        return "unknown", "unknown"
    if high is not None and v > high:
        return "high", "computed"
    if low is not None and v < low:
        return "low", "computed"
    return "normal", "computed"


def build_result(item: dict, collected_at: str, precision: str, source_ref: dict) -> dict:
    """item: test_name, raw_value, raw_unit?, reference_text?, flag_text?. ID assigned by caller."""
    test_id = test_id_for(item["test_name"])
    low, high = parse_reference(item.get("reference_text"))
    norm = normalize(test_id, item["raw_value"], item.get("raw_unit"), low, high)
    flag, flag_source = abnormal_flag(item.get("flag_text"), test_id, norm)
    return {
        "test_id": test_id, "test_name": item["test_name"], "specimen": item.get("specimen"),
        "collected_at": collected_at, "time_precision": precision,
        "raw_value": item["raw_value"], "raw_unit": item.get("raw_unit"),
        "normalized_value": norm["normalized_value"], "normalized_unit": norm["normalized_unit"],
        "reference_low": norm["reference_low"], "reference_high": norm["reference_high"],
        "reference_text": item.get("reference_text"),
        "abnormal_flag": flag, "abnormal_flag_source": flag_source,
        "value_type": norm["value_type"], "origin": "user_provided", "source_ref": source_ref,
    }


def _half_step(lab: dict) -> float:
    """Half of the last reported digit, in the normalized unit ("1.7" mg/dL -> 0.05 * 88.4)."""
    raw = lab["raw_value"].strip()
    decimals = len(raw.split(".")[1]) if "." in raw else 0
    raw_num = float(raw)
    factor = lab["normalized_value"] / raw_num if raw_num else 1.0
    return 0.5 * 10 ** -decimals * abs(factor)


def same_value(a: dict, b: dict) -> bool:
    """Compatible results: equal within reported precision (numeric, same normalized unit) or identical raw."""
    if a["value_type"] == b["value_type"] == "numeric" and a["normalized_unit"] == b["normalized_unit"]:
        return abs(a["normalized_value"] - b["normalized_value"]) <= max(_half_step(a), _half_step(b)) + 1e-9
    return (a["raw_value"].strip(), unit_spelling(a["raw_unit"])) == (b["raw_value"].strip(), unit_spelling(b["raw_unit"]))


def add_result(db: DB, patient_id: str, lab: dict) -> tuple[str, dict, list[dict]]:
    """Insert unless an identical result already exists. Returns (status, lab, clashing labs).

    Clash = same patient, same normalized test_id, same minute-precision timestamp, incompatible value.
    Different tests never clash, whatever their timestamps. Both are kept;
    the caller records a conflict and the series excludes them until the user resolves it.
    """
    same_time = db.all("lab_results", patient_id, test_id=lab["test_id"], collected_at=lab["collected_at"])
    same_day = [r for r in db.all("lab_results", patient_id, test_id=lab["test_id"])
                if r["collected_at"][:10] == lab["collected_at"][:10]]
    for r in same_day:  # a result re-quoted in a note (with a report time) is the same result, not a new point
        if same_value(r, lab):
            return "duplicate", r, []
    lab["id"] = db.next_id(patient_id, f"fact_lab_{lab['test_id']}")
    db.put("lab_results", patient_id, lab)
    return "added", lab, [r for r in same_time if r["time_precision"] == lab["time_precision"] == "minute"]


# ----- series -----

def _round(v: float) -> float:
    return round(v, 6)


def _direction(a: float, b: float) -> str:
    if abs(b - a) <= 1e-9 * max(1.0, abs(a), abs(b)):
        return "unchanged"
    return "up" if b > a else "down"


def _change(a: float, b: float) -> dict:
    return {"absolute_change": _round(b - a),
            "percent_change": None if a == 0 else round((b - a) / abs(a) * 100, 2),
            "direction": _direction(a, b)}


def compute_series(test_id: str, points: list[dict], excluded: set[str]) -> dict:
    """points: lab results of one test (any order). Pure function."""
    points = sorted(points, key=lambda p: p["collected_at"])  # stable: ties keep insertion order
    used = [p for p in points if p["id"] not in excluded]
    numeric = [p for p in used if p["value_type"] == "numeric" and p["normalized_value"] is not None]
    units = {p["normalized_unit"] for p in numeric}
    latest = (used or points)[-1]
    series = {
        "id": f"fact_series_{test_id}", "test_id": test_id, "test_name": latest["test_name"],
        "unit": next(iter(units)) if len(units) == 1 else None,
        "point_ids": [p["id"] for p in points], "n_points": len(points),
        "excluded_point_ids": [p["id"] for p in points if p["id"] in excluded],
        "latest_abnormal_flag": latest["abnormal_flag"],
        "origin": "system_computed", "computed_at": now(),
    }
    if len(units) > 1 or len(numeric) < len(used):
        # mixed units or qualitative points: never produce a trend across them
        series["data_status"] = "not_comparable"
        series["unit"] = None
        return series
    if len(numeric) < 2:
        series["data_status"] = "insufficient"
        if numeric:
            series["latest_value"] = numeric[0]["normalized_value"]
        return series

    values = [p["normalized_value"] for p in numeric]
    steps = [d for d in (_direction(a, b) for a, b in zip(values, values[1:])) if d != "unchanged"]
    if not steps:
        pattern = "constant"
    elif all(s == "up" for s in steps):
        pattern = "monotonic_increasing"
    elif all(s == "down" for s in steps):
        pattern = "monotonic_decreasing"
    else:
        pattern = "non_monotonic"
    first_to_latest, previous_to_latest = _change(values[0], values[-1]), _change(values[-2], values[-1])
    series.update({
        "data_status": "sufficient",
        "first_value": values[0], "previous_value": values[-2], "latest_value": values[-1],
        "min_value": min(values), "max_value": max(values),
        "first_to_latest": first_to_latest, "previous_to_latest": previous_to_latest,
        "overall_direction": first_to_latest["direction"], "recent_direction": previous_to_latest["direction"],
        "pattern": pattern,
        "reversal_count": sum(1 for a, b in zip(steps, steps[1:]) if a != b),
        "reference_status_transition": {"first": numeric[0]["abnormal_flag"],
                                        "previous": numeric[-2]["abnormal_flag"],
                                        "latest": numeric[-1]["abnormal_flag"]},
        "latest_abnormal_flag": numeric[-1]["abnormal_flag"],
    })
    return series


def _rank_tier(s: dict, flags: list[str]) -> int:
    if s["latest_abnormal_flag"] in ("critical_high", "critical_low"):
        return 0
    if s["latest_abnormal_flag"] in ABNORMAL:
        return 1
    if any(f in ABNORMAL for f in flags):
        return 2
    t = s.get("reference_status_transition")
    if t and t["first"] != t["latest"]:
        return 3
    return 4


def excluded_lab_ids(db: DB, patient_id: str) -> set[str]:
    out = set()
    for c in db.all("conflicts", patient_id):
        if c["kind"] != "lab_value_mismatch":
            continue
        refs = {x["ref_id"] for x in c["candidates"]}
        chosen = (c.get("resolution") or {}).get("chosen_ref") if c["status"] == "resolved" else None
        out |= refs - {chosen}
    return out


def rebuild_series(db: DB, patient_id: str) -> list[dict]:
    """Recompute every series and the display ranking. Cheap at per-patient scale."""
    by_test: dict[str, list[dict]] = {}
    for r in db.all("lab_results", patient_id):
        by_test.setdefault(r["test_id"], []).append(r)
    excluded = excluded_lab_ids(db, patient_id)
    series = [compute_series(t, pts, excluded) for t, pts in by_test.items()]
    latest_time = {s["test_id"]: max(p["collected_at"] for p in by_test[s["test_id"]]) for s in series}
    series.sort(key=lambda s: latest_time[s["test_id"]], reverse=True)
    series.sort(key=lambda s: _rank_tier(s, [p["abnormal_flag"] for p in by_test[s["test_id"]]]))
    db.delete_all("lab_series", patient_id)
    for rank, s in enumerate(series, 1):
        s["priority_rank"] = rank
        db.put("lab_series", patient_id, s)
    return series
