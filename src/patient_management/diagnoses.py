"""Diagnosis two-track state machine.

Track A (diagnoses table): user_provided, verbatim, locked. Changed only by explicit user commands.
Track B (diagnosis_candidates table): model_inferred, pending review. Never auto-promoted.
"""
from __future__ import annotations

import re

from . import conflicts, provenance
from .persistence import DB, PMError, now
from .state import patient_tx

CERTAINTY = {"likely", "possible", "uncertain"}


def _norm(text: str) -> str:
    return re.sub(r"[\s，,。.;；:：、]+", "", text).lower()


def _user_dx(db: DB, patient_id: str, dx_id: str) -> dict:
    dx = db.get("diagnoses", patient_id, dx_id)
    if not dx:
        raise PMError(f"no user diagnosis {dx_id}")
    return dx


def add_user_diagnosis(db: DB, patient_id: str, text: str, source_ref: dict, from_candidate_id=None) -> dict | None:
    """Store verbatim. Returns None if the same active text already exists."""
    if not text.strip():
        raise PMError("diagnosis text is empty")
    for d in db.all("diagnoses", patient_id):
        if d["status"] == "active" and _norm(d["display_text"]) == _norm(text):
            return None
    dx = {"id": db.next_id(patient_id, "dx"), "display_text": text.strip(), "origin": "user_provided",
          "locked": True, "status": "active", "evidence_ids": [], "source_ref": source_ref,
          "from_candidate_id": from_candidate_id, "internal_code": None, "updated_at": now()}
    return db.put("diagnoses", patient_id, dx)


def request_change(db: DB, patient_id: str, source_id: str, action: str, target_text: str,
                   new_text: str | None, quote: str | None) -> str:
    """A source asks to revise/resolve/revoke a locked diagnosis: never applied, recorded as conflict."""
    target = next((d for d in db.all("diagnoses", patient_id) if _norm(d["display_text"]) == _norm(target_text)), None)
    if target is None:
        raise PMError(f"no user diagnosis matching '{target_text}'")
    proposal = {"revise": f"revise to: {new_text}", "resolve": "mark resolved", "revoke": "revoke"}[action]
    c = conflicts.record(db, patient_id, "locked_diagnosis_change", target["id"], [
        {"value": target["display_text"], "source_id": target["source_ref"]["source_id"], "ref_id": target["id"]},
        {"value": proposal, "source_id": source_id, "ref_id": None, "quote": quote},
    ])
    return c["conflict_id"]


# ----- explicit user commands (Track A) -----

def edit(db: DB, patient_id: str, dx_id: str, new_text: str) -> dict:
    with patient_tx(db, patient_id):
        dx = _user_dx(db, patient_id, dx_id)
        src = provenance.manual_source(db, patient_id, {"edit_diagnosis": dx_id, "text": new_text})
        dx.update(display_text=new_text.strip(), updated_at=now(),
                  source_ref=provenance.ref(src["source_id"], "manual"))
        db.put("diagnoses", patient_id, dx)  # previous text kept in the events table
        conflicts.resolve_subject(db, patient_id, dx_id, note=f"user edited {dx_id}")
    return dx


def set_status(db: DB, patient_id: str, dx_id: str, status: str) -> dict:
    if status not in ("active", "resolved"):
        raise PMError("user diagnosis status must be active or resolved")
    with patient_tx(db, patient_id):
        dx = _user_dx(db, patient_id, dx_id)
        provenance.manual_source(db, patient_id, {"set_diagnosis_status": dx_id, "status": status})
        dx.update(status=status, updated_at=now())
        db.put("diagnoses", patient_id, dx)
        conflicts.resolve_subject(db, patient_id, dx_id, note=f"user set {dx_id} {status}")
    return dx


def add_manual(db: DB, patient_id: str, text: str) -> dict | None:
    with patient_tx(db, patient_id):
        src = provenance.manual_source(db, patient_id, {"add_diagnosis": text})
        return add_user_diagnosis(db, patient_id, text, provenance.ref(src["source_id"], "manual"))


# ----- Track B -----

def add_candidate(db: DB, patient_id: str, text: str, certainty: str, evidence_ids: list[str]) -> dict:
    """Entry point for the future diagnosis_candidates module (after validation)."""
    if certainty not in CERTAINTY:
        raise PMError(f"bad certainty {certainty}")
    if not evidence_ids:
        raise PMError("candidate needs at least one evidence id")
    with patient_tx(db, patient_id):
        unknown = set(evidence_ids) - provenance.known_fact_ids(db, patient_id)
        if unknown:
            raise PMError(f"unknown evidence ids: {sorted(unknown)}")
        if any(_norm(d["display_text"]) == _norm(text) for d in db.all("diagnoses", patient_id)):
            raise PMError("candidate duplicates a user diagnosis")
        cand = {"id": db.next_id(patient_id, "dx"), "display_text": text.strip(), "origin": "model_inferred",
                "locked": False, "status": "candidate", "review_status": "pending", "certainty": certainty,
                "evidence_ids": list(dict.fromkeys(evidence_ids)), "internal_code": None, "updated_at": now()}
        return db.put("diagnosis_candidates", patient_id, cand)


def _pending_candidate(db: DB, patient_id: str, cand_id: str) -> dict:
    cand = db.get("diagnosis_candidates", patient_id, cand_id)
    if not cand:
        raise PMError(f"no candidate {cand_id}")
    if cand["review_status"] != "pending":
        raise PMError(f"{cand_id} already {cand['review_status']}")
    return cand


def dismiss_candidate(db: DB, patient_id: str, cand_id: str) -> dict:
    with patient_tx(db, patient_id):
        cand = _pending_candidate(db, patient_id, cand_id)
        cand.update(review_status="dismissed", updated_at=now())
        return db.put("diagnosis_candidates", patient_id, cand)


def accept_candidate(db: DB, patient_id: str, cand_id: str, text: str | None = None) -> dict:
    """User action only. Creates a NEW user diagnosis; the candidate record itself is never promoted."""
    with patient_tx(db, patient_id):
        cand = _pending_candidate(db, patient_id, cand_id)
        final_text = text or cand["display_text"]
        src = provenance.manual_source(db, patient_id, {"accept_candidate": cand_id, "text": final_text})
        dx = add_user_diagnosis(db, patient_id, final_text, provenance.ref(src["source_id"], "manual"),
                                from_candidate_id=cand_id)
        if dx is None:
            raise PMError("an identical user diagnosis already exists")
        cand.update(review_status="accepted", updated_at=now())
        db.put("diagnosis_candidates", patient_id, cand)
        return dx
