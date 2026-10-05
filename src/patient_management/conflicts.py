"""Deterministic conflict records. Code never picks a winner; resolution is an explicit user action."""
from __future__ import annotations

from .persistence import DB, PMError, now

KINDS = {"lab_value_mismatch", "locked_diagnosis_change", "field_value_mismatch"}


def record(db: DB, patient_id: str, kind: str, subject: str, candidates: list[dict]) -> dict:
    """Open a conflict, or merge new candidates into the open one for the same subject."""
    if kind not in KINDS:
        raise PMError(f"unknown conflict kind: {kind}")
    clean = [{k: c.get(k) for k in ("value", "source_id", "ref_id", "quote")} for c in candidates]
    existing = [c for c in db.all("conflicts", patient_id, subject=subject, status="open") if c["kind"] == kind]
    if existing:
        conflict = existing[0]
        seen = {(c["value"], c["source_id"], c["ref_id"]) for c in conflict["candidates"]}
        conflict["candidates"] += [c for c in clean if (c["value"], c["source_id"], c["ref_id"]) not in seen]
    else:
        conflict = {"conflict_id": db.next_id(patient_id, "cf"), "kind": kind, "subject": subject,
                    "status": "open", "candidates": clean, "detected_at": now(), "resolution": None}
    return db.put("conflicts", patient_id, conflict)


def resolve(db: DB, patient_id: str, conflict_id: str, chosen_ref: str | None = None,
            note: str | None = None) -> dict:
    conflict = db.get("conflicts", patient_id, conflict_id)
    if not conflict:
        raise PMError(f"no conflict {conflict_id}")
    if conflict["status"] != "open":
        raise PMError(f"{conflict_id} is already resolved")
    if chosen_ref is not None and chosen_ref not in {c["ref_id"] for c in conflict["candidates"]}:
        raise PMError(f"{chosen_ref} is not a candidate of {conflict_id}")
    if conflict["kind"] == "lab_value_mismatch" and chosen_ref is None and not note:
        raise PMError("lab conflicts need --keep <lab id> or a --note explaining why none is kept")
    conflict["status"] = "resolved"
    conflict["resolution"] = {"chosen_ref": chosen_ref, "note": note, "resolved_at": now()}
    db.put("conflicts", patient_id, conflict)
    if conflict["kind"] == "lab_value_mismatch":
        from . import labs  # local import: labs does not depend on conflicts
        labs.rebuild_series(db, patient_id)
    return conflict


def resolve_subject(db: DB, patient_id: str, subject: str, note: str) -> None:
    """Close open conflicts made moot by an explicit user edit of that subject."""
    for c in db.all("conflicts", patient_id, subject=subject, status="open"):
        resolve(db, patient_id, c["conflict_id"], note=note)
