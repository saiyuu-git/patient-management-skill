"""Deterministic ingestion: verify anchored extraction items and write them to Patient State.

Two entry points share one verifier:
  ingest()            structured payload (see below), registers the source itself
  apply_extraction()  items for an already registered source (used by extraction.submit / prepare)

Payload (JSON):
{
  "source": {"kind": "<source kind>", "document_date": "YYYY-MM-DD"|null, "text": "<raw text>"|null},
  "extracted_by": "deterministic_parser" | "model" | "manual",
  "extraction": { ...model-outputs.schema.json#/$defs/source_extraction fields... }
}

Extracted items are user facts (origin=user_provided) whatever extracted them. With raw text, every
item must be anchored: quote inside the source (or the task's chunks), and its text/values inside
the quote. Failing items are rejected with a code, never stored. Model extraction requires raw text.

Reject codes: V03 malformed item, V13 anchoring, V14 admission date rule, V17 hedged diagnosis,
V18 negation/uncertainty dropped. Skip code V19: already captured by the deterministic parser.
"""
from __future__ import annotations

from . import conflicts, diagnoses, labs, provenance, state, tasks, textparse
from .persistence import DB, PMError

FACT_KINDS = {"chief_complaint", "symptom", "sign", "history", "medication", "treatment", "procedure",
              "event", "vital", "impression", "other"}
INV_CATEGORIES = {"imaging", "ultrasound", "ecg", "endoscopy", "pathology", "function_test", "microbiology", "other"}
ASSERTIONS = {"present", "absent", "uncertain", "unknown"}
_SEX_WORD = {"male": "男", "female": "女"}


def ingest(db: DB, patient_id: str, payload: dict) -> dict:
    src_in = payload.get("source") or {}
    kind, text = src_in.get("kind"), src_in.get("text")
    extracted_by = payload.get("extracted_by")
    ex = payload.get("extraction") or {}
    if kind not in provenance.SOURCE_KINDS:
        raise PMError(f"unknown source kind: {kind}")
    if extracted_by not in provenance.EXTRACTED_BY:
        raise PMError(f"unknown extracted_by: {extracted_by}")
    if extracted_by == "model" and not text:
        raise PMError("model extraction requires the raw source text for quote verification")
    doc_date = state.parse_when(src_in.get("document_date"))
    doc_date = doc_date[0][:10] if doc_date else None

    fp = provenance.fingerprint(kind, text, ex)
    with state.patient_tx(db, patient_id):
        existing = db.source_by_fingerprint(patient_id, fp)
        if existing:
            db.log_ingest(patient_id, existing["source_id"], fp, "duplicate")
            return {"status": "duplicate", "source_id": existing["source_id"], "added": {}, "skipped": [],
                    "rejected": [], "conflicts": []}
        source = provenance.register(db, patient_id, kind, fp, text, doc_date)
        result = apply_extraction(db, patient_id, source, extracted_by, text, ex)
        db.log_ingest(patient_id, source["source_id"], fp, "ingested")
    return {"status": "ingested", "source_id": source["source_id"], **result}


def apply_extraction(db: DB, patient_id: str, source: dict, extracted_by: str, text: str | None, ex: dict,
                     chunks: list[dict] | None = None, skip_lab_spans: list[tuple[int, int]] = ()) -> dict:
    """Caller holds the patient transaction. chunks restricts anchoring to those chunks."""
    run = _Run(db, patient_id, source, extracted_by, text, chunks, skip_lab_spans)
    run.header(ex.get("demographics"))
    if ex.get("stated_admission_date"):
        run.admission(ex["stated_admission_date"])
    for i, item in enumerate(ex.get("facts") or []):
        run.fact(item, f"facts[{i}]")
    lab_added = False
    for i, item in enumerate(ex.get("labs") or []):
        lab_added |= run.lab(item, f"labs[{i}]")
    if lab_added:
        labs.rebuild_series(db, patient_id)
    for i, item in enumerate(ex.get("investigations") or []):
        run.investigation(item, f"investigations[{i}]")
    for i, item in enumerate(ex.get("stated_diagnoses") or []):
        run.diagnosis(item, f"stated_diagnoses[{i}]")
    for i, item in enumerate(ex.get("diagnosis_updates") or []):
        run.diagnosis_update(item, f"diagnosis_updates[{i}]")
    for i, item in enumerate(ex.get("explicit_tasks") or []):
        run.task(item, f"explicit_tasks[{i}]")
    return run.result


class _Run:
    def __init__(self, db, patient_id, source, extracted_by, text, chunks, skip_lab_spans):
        self.db, self.pid, self.source, self.by, self.text = db, patient_id, source, extracted_by, text
        self.sid = source["source_id"]
        self.chunks = chunks
        self.skip_lab_spans = list(skip_lab_spans)
        self.doc_date = source.get("document_date")
        self.result = {"added": {}, "skipped": [], "rejected": [], "conflicts": []}

    # ----- bookkeeping -----

    def count(self, key: str) -> None:
        self.result["added"][key] = self.result["added"].get(key, 0) + 1

    def reject(self, where: str, code: str, reason: str) -> bool:
        self.result["rejected"].append({"item": where, "code": code, "reason": reason})
        return False

    def skip(self, where: str, reason: str, code: str | None = None) -> bool:
        self.result["skipped"].append({"item": where, "reason": reason, **({"code": code} if code else {})})
        return False

    # ----- anchoring -----

    def locate(self, quote: str | None):
        """(span, chunk) of quote; span None if not found. ('unchecked', None) when no raw text exists."""
        if not self.text:
            return "unchecked", None
        for c in self.chunks or ():
            found = provenance.locate(quote, c["text"])
            if found:
                return {"chunk_id": c["chunk_id"], "start": c["char_start"] + found[0],
                        "end": c["char_start"] + found[1]}, c
        if self.chunks is None:
            found = provenance.locate(quote, self.text)
            if found:
                return {"chunk_id": None, "start": found[0], "end": found[1]}, None
        return None, None

    def in_scope(self, s: str | None) -> bool:
        """s (a time/name string) appears in the anchoring scope."""
        if not s or not self.text:
            return True
        scope = [c["text"] for c in self.chunks] if self.chunks is not None else [self.text]
        return any(provenance.contains(t, s) for t in scope)

    def anchor(self, where: str, quote, *inside) -> tuple[dict | None, dict | None] | None:
        """Quote must be in scope and every non-empty value in `inside` must be in the quote."""
        span, chunk = self.locate(quote)
        if span is None:
            self.reject(where, "V13", "quote not found in source text")
            return None
        if span != "unchecked":
            for v in inside:
                if v and not provenance.contains(quote, str(v)):
                    self.reject(where, "V13", f"'{v}' not present in its quote")
                    return None
        return (None if span == "unchecked" else span), chunk

    def ref(self, quote, span=None) -> dict:
        return provenance.ref(self.sid, self.by, quote, span)

    # ----- items -----

    def header(self, demo: dict | None) -> None:
        if not demo:
            return
        sex = demo.get("sex")
        sex = state.parse_sex(sex) or (sex if sex in ("male", "female") else None)
        got = self.anchor("demographics", demo.get("quote"), demo.get("name"), demo.get("alias"), demo.get("bed"),
                          demo.get("age_text"), _SEX_WORD.get(sex))
        if got is None:
            return
        fields = {"display_name": demo.get("name"), "alias": demo.get("alias"), "bed": demo.get("bed"),
                  "sex": sex, "age": state.parse_age(demo.get("age_text"))}
        self.result["conflicts"] += state.apply_source_header(self.db, self.pid, self.source, fields, demo.get("quote"))
        self.count("header")

    def admission(self, item: dict) -> None:
        got = self.anchor("stated_admission_date", item.get("quote"))
        if got is None:
            return
        span, chunk = got
        outcome, detail = state.apply_admission_date(
            self.db, self.pid, self.source, item.get("date"), item.get("quote"), None,
            section_kind=chunk["doc_kind"] if chunk else None, make_ref=lambda q: self.ref(q, span))
        if outcome == "set":
            self.count("admission_date")
        elif outcome == "conflict":
            self.result["conflicts"].append(detail)
        elif outcome == "unchanged":
            self.skip("stated_admission_date", "same admission date already set")
        else:
            self.reject("stated_admission_date", "V14", detail)

    def fact(self, item: dict, where: str) -> bool:
        kind, text, quote = item.get("kind"), item.get("text"), item.get("quote")
        if kind not in FACT_KINDS or not text:
            return self.reject(where, "V03", "missing text or unknown kind")
        got = self.anchor(where, quote, text)
        if got is None:
            return False
        if not self.in_scope(item.get("observed_at_text")):
            return self.reject(where, "V13", "observed_at_text not found in source")
        # Faithful extraction: assertion is decided by the source wording, never by the model.
        given = item.get("assertion")
        if given is not None and given not in ASSERTIONS:
            return self.reject(where, "V03", f"bad assertion {given}")
        value, reliable = textparse.assertion_evidence(text, quote) if quote else ("unknown", False)
        if reliable and given not in (None, value):
            return self.reject(where, "V18", f"source wording makes this {value}, not {given}")
        assertion = value if reliable else "unknown"
        when = state.parse_when(item.get("observed_at_text"))
        observed = when[0] if when else None
        for f in self.db.all("facts", self.pid):
            if (f["kind"], f["text"], f.get("assertion"), f["observed_at"]) == (kind, text[:400], assertion, observed):
                return self.skip(where, f"same as {f['id']}")
        self.db.put("facts", self.pid, {
            "id": self.db.next_id(self.pid, f"fact_{kind}"), "kind": kind, "text": text[:400],
            "observed_at": observed, "time_precision": when[1] if when else "unknown", "assertion": assertion,
            "origin": "user_provided", "source_ref": self.ref(quote, got[0])})
        self.count("facts")
        return True

    def lab(self, item: dict, where: str) -> bool:
        if not item.get("test_name") or not item.get("raw_value"):
            return self.reject(where, "V03", "missing test_name or raw_value")
        got = self.anchor(where, item.get("quote"), item["test_name"], item["raw_value"], item.get("raw_unit"),
                          item.get("reference_text"), item.get("flag_text"))
        if got is None:
            return False
        span = got[0]
        if span and any(a < span["end"] and span["start"] < b for a, b in self.skip_lab_spans):
            return self.skip(where, "already captured by deterministic parser", "V19")
        if not self.in_scope(item.get("collected_at_text")):
            return self.reject(where, "V13", "collected_at_text not found in source")
        when = state.parse_when(item.get("collected_at_text"))
        if when is None and self.doc_date and self.source["kind"] == "lab_report":
            when = (self.doc_date, "day")  # a lab report's own date; never for labs quoted in notes
        if when is None:
            return self.reject(where, "V13", "no explicit collection time and no document date")
        lab = labs.build_result(item, when[0], when[1], self.ref(item.get("quote"), span))
        status, stored, clashes = labs.add_result(self.db, self.pid, lab)
        if status == "duplicate":
            return self.skip(where, f"same as {stored['id']}")
        self.count("labs")
        if clashes:
            c = conflicts.record(self.db, self.pid, "lab_value_mismatch", f"lab:{stored['test_id']}@{stored['collected_at']}",
                                 [{"value": f"{r['raw_value']} {r['raw_unit'] or ''}".strip(),
                                   "source_id": r["source_ref"]["source_id"], "ref_id": r["id"],
                                   "quote": r["source_ref"].get("quote")} for r in clashes + [stored]])
            if c["conflict_id"] not in self.result["conflicts"]:
                self.result["conflicts"].append(c["conflict_id"])
        return True

    def investigation(self, item: dict, where: str) -> None:
        name = item.get("name")
        if not name:
            return self.reject(where, "V03", "missing name")
        if not self.in_scope(name):
            return self.reject(where, "V13", "investigation name not found in source")
        if not self.in_scope(item.get("performed_at_text")):
            return self.reject(where, "V13", "performed_at_text not found in source")
        findings = []
        for j, f in enumerate(item.get("findings") or []):
            if not f.get("text"):
                self.reject(f"{where}.findings[{j}]", "V03", "missing text")
                continue
            got = self.anchor(f"{where}.findings[{j}]", f.get("quote"), f["text"])
            if got is not None:
                findings.append((f, got[0]))
        impression = item.get("impression_quote")
        imp_span = None
        if impression:
            got = self.anchor(f"{where}.impression_quote", impression)
            impression, imp_span = (impression, got[0]) if got else (None, None)
        if not findings and not impression:
            return self.reject(where, "V13", f"{name}: no verifiable findings or impression")
        when = state.parse_when(item.get("performed_at_text"))
        performed = when[0] if when else None
        for inv in self.db.all("investigations", self.pid):
            if inv["name"] == name and inv["performed_at"] == performed and \
                    [f["text"] for f in inv["findings"]] == [f["text"][:300] for f, _ in findings] and inv["impression"] == impression:
                return self.skip(where, f"same as {inv['id']}")
        inv_id = self.db.next_id(self.pid, "fact_inv")
        seq = inv_id.rsplit("_", 1)[1]
        first_quote, first_span = (findings[0][0].get("quote"), findings[0][1]) if findings else (impression, imp_span)
        self.db.put("investigations", self.pid, {
            "id": inv_id, "category": item.get("category") if item.get("category") in INV_CATEGORIES else "other",
            "name": name, "body_site": item.get("body_site"), "performed_at": performed,
            "findings": [{"id": f"fact_invf_{seq}_{i:02d}", "text": f["text"][:300]} for i, (f, _) in enumerate(findings, 1)],
            "impression": impression, "origin": "user_provided", "source_ref": self.ref(first_quote, first_span)})
        self.count("investigations")

    def diagnosis(self, item: dict, where: str) -> None:
        text, quote = item.get("text"), item.get("quote")
        if not text:
            return self.reject(where, "V03", "missing text")
        got = self.anchor(where, quote, text)
        if got is None:
            return
        if textparse.hedged_diagnosis(text, quote):
            return self.reject(where, "V17", "hedged wording (考虑/可能/待排/不除外/?) is not a stated diagnosis; "
                                             "use facts with kind=impression, assertion=uncertain")
        if diagnoses.add_user_diagnosis(self.db, self.pid, text, self.ref(quote, got[0])) is None:
            self.skip(where, "same active diagnosis already present")
        else:
            self.count("diagnoses")

    def diagnosis_update(self, item: dict, where: str) -> None:
        if item.get("action") not in ("revise", "resolve", "revoke") or not item.get("target_text"):
            return self.reject(where, "V03", "bad action or missing target")
        if self.anchor(where, item.get("quote"), item.get("new_text")) is None:
            return
        try:
            cid = diagnoses.request_change(self.db, self.pid, self.sid, item["action"], item["target_text"],
                                           item.get("new_text"), item.get("quote"))
        except PMError as e:
            return self.reject(where, "V03", str(e))
        self.result["conflicts"].append(cid)

    def task(self, item: dict, where: str) -> None:
        if not item.get("title") or item.get("source_kind") not in tasks.EXPLICIT_SOURCES:
            return self.reject(where, "V03", "missing title or bad source_kind")
        got = self.anchor(where, item.get("quote"), item["title"])
        if got is None:
            return
        if not self.in_scope(item.get("due_text")):
            return self.reject(where, "V13", "due_text not found in source")
        t = tasks.add_explicit(self.db, self.pid, item["title"], item["source_kind"], self.ref(item.get("quote"), got[0]),
                               item.get("due_text"))
        if t is None:
            self.skip(where, "same open task exists")
        else:
            self.count("tasks")
