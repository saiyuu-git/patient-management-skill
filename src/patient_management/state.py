"""Patient State: create, header rules, full schema-shaped read. The only public view of stored data."""
from __future__ import annotations

import re
import uuid
from contextlib import contextmanager
from datetime import date

from . import conflicts
from .persistence import DB, PMError, now
from . import provenance

SCHEMA_VERSION = "0.6.0"
_EVIDENCE_FIELDS = ("request_id", "question_type", "clinical_question", "topic_key", "status", "external_evidence_status",
                    "evidence_ids", "relevant_fact_ids", "created_at", "updated_at")
SEXES = {"male", "female", "unknown"}
_SEX_WORDS = {"男": "male", "女": "female", "male": "male", "female": "female", "m": "male", "f": "female"}
ADMISSION_SOURCES = {"admission_record": "admission_record", "user_message": "user_stated"}

_DT = re.compile(
    r"(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?"
    r"(?:[\sT]*(\d{1,2})\s*[:时點点]\s*(\d{1,2})\s*分?)?")


def parse_when(text: str | None) -> tuple[str, str] | None:
    """First explicit date(+time) in text -> (iso, precision). No relative dates, no guessing."""
    if not text:
        return None
    m = _DT.search(text)
    if not m:
        return None
    y, mo, d, hh, mm = m.groups()
    try:
        day = date(int(y), int(mo), int(d)).isoformat()
    except ValueError:
        return None
    if hh is not None and int(hh) < 24 and int(mm) < 60:
        return f"{day}T{int(hh):02d}:{int(mm):02d}", "minute"
    return day, "day"


def parse_age(text: str | None) -> dict | None:
    if not text:
        return None
    t = re.sub(r"\s+", "", text)
    y = re.search(r"(\d+(?:\.\d+)?)岁", t)
    mo = re.search(r"(\d+)(?:个)?月", t)
    d = re.search(r"(\d+)(?:天|日龄)", t)
    if y:
        return {"value": round(float(y.group(1)) + (int(mo.group(1)) / 12 if mo else 0), 2), "unit": "year"}
    if mo:
        return {"value": float(mo.group(1)), "unit": "month"}
    if d:
        return {"value": float(d.group(1)), "unit": "day"}
    if re.fullmatch(r"\d+(\.\d+)?", t):
        return {"value": float(t), "unit": "year"}
    return None


def parse_sex(text: str | None) -> str | None:
    return _SEX_WORDS.get((text or "").strip().lower()) if text else None


@contextmanager
def patient_tx(db: DB, patient_id: str):
    """Transaction for one patient's mutation; maintains last_updated_at / persist_status."""
    if db.get_patient(patient_id) is None:
        raise PMError(f"no patient {patient_id}")
    try:
        with db.tx():
            yield
            record = db.get_patient(patient_id)
            record["header"].update(last_updated_at=now(), persist_status="saved")
            db.put_patient(patient_id, record)
    except Exception:
        try:  # best effort; the failed writes are already rolled back
            with db.tx():
                record = db.get_patient(patient_id)
                record["header"]["persist_status"] = "error"
                db.put_patient(patient_id, record)
        except Exception:
            pass
        raise


def create_patient(db: DB, patient_id: str | None = None, **header_fields) -> dict:
    patient_id = patient_id or f"pt_{uuid.uuid4().hex[:10]}"
    if not re.fullmatch(r"pt_[a-z0-9]+", patient_id):
        raise PMError("patient_id must match pt_[a-z0-9]+")
    with db.tx():
        if db.get_patient(patient_id):
            raise PMError(f"patient {patient_id} already exists")
        header = {"display_name": None, "alias": None, "bed": None, "sex": "unknown", "age": None,
                  "admission_date": None, "admission_date_source": None, "admission_date_fact_id": None,
                  "last_updated_at": now(), "persist_status": "saved"}
        db.put_patient(patient_id, {"patient_id": patient_id, "header": header})
    if any(v is not None for v in header_fields.values()):
        update_header(db, patient_id, **header_fields)
    return get_state(db, patient_id)


def update_header(db: DB, patient_id: str, *, display_name=None, alias=None, bed=None, sex=None,
                  age_text=None, admission_date=None) -> dict:
    """Explicit user edit: applies directly and closes open conflicts on the edited fields."""
    with patient_tx(db, patient_id):
        record = db.get_patient(patient_id)
        h = record["header"]
        changed = {}
        if display_name is not None:
            changed["display_name"] = display_name.strip() or None
        if alias is not None:
            changed["alias"] = alias.strip() or None
        if bed is not None:
            changed["bed"] = bed.strip() or None
        if sex is not None:
            s = parse_sex(sex) or (sex if sex in SEXES else None)
            if not s:
                raise PMError(f"unrecognized sex: {sex}")
            changed["sex"] = s
        if age_text is not None:
            age = parse_age(age_text)
            if not age:
                raise PMError(f"unrecognized age: {age_text}")
            changed["age"] = age
        if admission_date is not None:
            parsed = parse_when(admission_date)
            if not parsed:
                raise PMError("admission date must be an explicit date, e.g. 2026-09-28")
            changed.update(admission_date=parsed[0][:10], admission_date_source="user_stated",
                           admission_date_fact_id=None)
        if not changed:
            return h
        src = provenance.manual_source(db, patient_id, {"update_header": changed})
        h.update(changed)
        record.setdefault("header_sources", {}).update({f: src["source_id"] for f in changed})
        db.put_patient(patient_id, record)
        for field in changed:
            conflicts.resolve_subject(db, patient_id, f"header.{field}",
                                      note=f"user set {field} explicitly ({src['source_id']})")
    return db.get_patient(patient_id)["header"]


def apply_source_header(db: DB, patient_id: str, source: dict, fields: dict, quote: str | None) -> list[str]:
    """Header values found in a source. Fills empty fields; differing values become conflicts, never overwrites.

    Bed and age legitimately change, so the newest source wins for those.
    """
    record = db.get_patient(patient_id)
    h, origins = record["header"], record.setdefault("header_sources", {})
    opened = []
    for field, value in fields.items():
        if value is None:
            continue
        if field in ("bed", "age") or h.get(field) in (None, "unknown") or h.get(field) == value:
            h[field] = value
            origins[field] = source["source_id"]
            continue
        c = conflicts.record(db, patient_id, "field_value_mismatch", f"header.{field}", [
            {"value": str(h[field]), "source_id": origins[field], "ref_id": None},
            {"value": str(value), "source_id": source["source_id"], "ref_id": None, "quote": quote},
        ])
        opened.append(c["conflict_id"])
    db.put_patient(patient_id, record)
    return opened


def apply_admission_date(db: DB, patient_id: str, source: dict, date_text: str, quote: str | None,
                         source_text: str | None, section_kind: str | None = None, make_ref=None) -> tuple[str, str | None]:
    """Deterministic rule. Accepted only when
      (a) the quote explicitly states it (入院日期/入院时间 <date>, <date>入院), or
      (b) the quote comes from an admission record (source or section kind) and contains the date.
    A note's own date, earliest lab/note/investigation dates are never used. Returns (outcome, detail).
    """
    from .textparse import EXPLICIT_ADMISSION  # local: textparse depends on labs, not on state

    parsed = parse_when(date_text)
    if not parsed:
        return "rejected", "admission date is not an explicit date"
    value = parsed[0][:10]
    explicit = EXPLICIT_ADMISSION.search(quote or "")
    admission_doc = "admission_record" in (source["kind"], section_kind)
    if not explicit and not admission_doc:
        return "rejected", "quote does not explicitly state the admission date and is not from an admission record"
    if source_text and not provenance.quote_in(quote, source_text):
        return "rejected", "admission date quote not found in source"
    found = parse_when(explicit.group(1) or explicit.group(2)) if explicit else parse_when(quote)
    if not found or found[0][:10] != value:
        return "rejected", "admission date does not match the date written in the quote"
    origin = "admission_record" if admission_doc else "user_stated"
    record = db.get_patient(patient_id)
    h, origins = record["header"], record.setdefault("header_sources", {})
    if h["admission_date"] not in (None, value):
        c = conflicts.record(db, patient_id, "field_value_mismatch", "header.admission_date", [
            {"value": h["admission_date"], "source_id": origins["admission_date"], "ref_id": h["admission_date_fact_id"]},
            {"value": value, "source_id": source["source_id"], "ref_id": None, "quote": quote},
        ])
        return "conflict", c["conflict_id"]
    if h["admission_date"] == value:
        return "unchanged", value
    fact_id = db.next_id(patient_id, "fact_admission_date")
    db.put("facts", patient_id, {
        "id": fact_id, "kind": "event", "label": "入院日期", "text": f"入院日期 {value}",
        "observed_at": value, "time_precision": "day", "assertion": "present", "origin": "user_provided",
        "source_ref": make_ref(quote) if make_ref else provenance.ref(source["source_id"], source["extracted_by"], quote)})
    h.update(admission_date=value, admission_date_source=origin, admission_date_fact_id=fact_id)
    origins["admission_date"] = source["source_id"]
    db.put_patient(patient_id, record)
    return "set", value


def display_label(state: dict) -> str:
    h = state["header"]
    return h["display_name"] or h["alias"] or state["patient_id"]


def get_state(db: DB, patient_id: str) -> dict:
    """Full Patient State per schemas/patient-state.schema.json."""
    record = db.get_patient(patient_id)
    if record is None:
        raise PMError(f"no patient {patient_id}")
    analyses = {r["id"]: {k: v for k, v in r.items() if k != "id"} for r in db.all("analysis_records", patient_id)}
    return {
        "schema_version": SCHEMA_VERSION,
        "patient_id": patient_id,
        "header": record["header"],
        "sources": [{**x, "text_origin": "native_text"} if x.get("text_origin") == "native" else x  # legacy value
                    for x in db.all("sources", patient_id)],
        "facts": db.all("facts", patient_id),
        "labs": {"results": db.all("lab_results", patient_id),
                 "series": sorted(db.all("lab_series", patient_id), key=lambda s: s["priority_rank"])},
        "investigations": db.all("investigations", patient_id),
        "diagnoses": db.all("diagnoses", patient_id) + db.all("diagnosis_candidates", patient_id),
        "tasks": db.all("tasks", patient_id),
        "conflicts": db.all("conflicts", patient_id),
        "evidence_requests": [{k: r[k] for k in _EVIDENCE_FIELDS} for r in db.all("evidence_requests", patient_id)],
        "analyses": analyses,
    }


def put_analysis(db: DB, patient_id: str, module: str, record: dict) -> None:
    """Storage hook for future model modules; validation happens before this call."""
    with patient_tx(db, patient_id):
        db.put("analysis_records", patient_id, {"id": module, **record})
