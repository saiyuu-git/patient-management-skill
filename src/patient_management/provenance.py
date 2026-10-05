"""Source registration, fingerprints, quote verification, source_ref construction."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid

from .persistence import DB, PMError, now

SOURCE_KINDS = {"admission_record", "progress_note", "ward_round_note", "lab_report", "investigation_report",
                "medical_order", "discharge_summary", "user_message", "other"}
EXTRACTED_BY = {"deterministic_parser", "model", "manual"}


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text)


def fingerprint(kind: str, text: str | None, extraction: dict) -> str:
    """Stable across whitespace changes. With raw text, identity = (kind, text); else = (kind, extraction)."""
    basis = {"kind": kind, "text": _squash(text)} if text else {"kind": kind, "extraction": extraction}
    return hashlib.sha256(json.dumps(basis, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _norm_map(s: str) -> tuple[str, list[int]]:
    """NFKC-folded, whitespace-free, lowercased copy of s + index map back to s.

    Tolerates full/half-width and spacing differences only; never changes words or digits.
    """
    out, idx = [], []
    for i, ch in enumerate(s):
        for c in unicodedata.normalize("NFKC", ch):
            if not c.isspace():
                out.append(c.lower())
                idx.append(i)
    return "".join(out), idx


def norm(s: str) -> str:
    return _norm_map(s)[0]


def contains(haystack: str | None, needle: str | None) -> bool:
    return bool(haystack) and bool(needle) and norm(needle) in norm(haystack)


def locate(quote: str | None, text: str | None) -> tuple[int, int] | None:
    """Char span (end exclusive) of quote in text, exact first, then tolerant."""
    if not quote or not text:
        return None
    i = text.find(quote)
    if i >= 0:
        return i, i + len(quote)
    nt, idx = _norm_map(text)
    nq = norm(quote)
    j = nt.find(nq) if nq else -1
    return (idx[j], idx[j + len(nq) - 1] + 1) if j >= 0 else None


def quote_in(quote: str | None, text: str | None) -> bool:
    """True when no raw text is available to check against."""
    return True if not text else locate(quote, text) is not None


def register(db: DB, patient_id: str, kind: str, fp: str, text: str | None = None,
             document_date: str | None = None, text_origin: str | None = None) -> dict:
    if kind not in SOURCE_KINDS:
        raise PMError(f"unknown source kind: {kind}")
    source_id = db.next_id(patient_id, "src")
    source = {"source_id": source_id, "kind": kind, "document_date": document_date, "received_at": now()}
    if text:
        db.put_source_text(patient_id, source_id, text)
        source["storage_ref"] = f"sqlite:source_texts/{source_id}"
    if text_origin:
        text_origin = "native_text" if text_origin == "native" else text_origin
        if text_origin not in ("native_text", "host_transcription", "ocr", "manual"):
            raise PMError(f"unknown text_origin: {text_origin}")
        source["text_origin"] = text_origin
    db.put("sources", patient_id, source, fingerprint=fp)
    return source


def manual_source(db: DB, patient_id: str, action: dict) -> dict:
    """Every explicit user action gets its own user_message source (never deduplicated)."""
    fp = fingerprint("user_message", None, {"action": action, "nonce": uuid.uuid4().hex})
    return register(db, patient_id, "user_message", fp)


def ref(source_id: str, extracted_by: str, quote: str | None = None, span: dict | None = None) -> dict:
    if extracted_by not in EXTRACTED_BY:
        raise PMError(f"unknown extracted_by: {extracted_by}")
    out = {"source_id": source_id, "extracted_by": extracted_by}
    if quote:
        out["quote"] = quote[:400]
    if span:
        out["span"] = span
    return out


def known_fact_ids(db: DB, patient_id: str) -> set[str]:
    """Everything a model may cite as evidence."""
    ids = {f["id"] for f in db.all("facts", patient_id)}
    ids |= {r["id"] for r in db.all("lab_results", patient_id)}
    ids |= {s["id"] for s in db.all("lab_series", patient_id)}
    for inv in db.all("investigations", patient_id):
        ids.add(inv["id"])
        ids |= {f["id"] for f in inv["findings"]}
    return ids
