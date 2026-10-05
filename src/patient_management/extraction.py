"""Natural-language ingestion with constrained model extraction.

prepare: raw text -> source registration -> deterministic parsing (written immediately)
         -> chunking -> task packages for the chunks the parser could not fully cover.
submit:  model output -> tolerant parse -> enum/shape normalization -> per-item schema filter
         -> anchored verification + write (ingestion.apply_extraction) -> accept / partial / repair / fail.

The model never touches the database. Max 2 attempts per task; after that the task falls back:
deterministic results and the raw source remain, the chunk simply has no model extraction.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from string import Template

from . import ingestion, provenance, schema, state, textparse
from .persistence import DB, PMError

MAX_ATTEMPTS = 2
TEMPLATE = Path(__file__).with_name("prompts") / "source_extraction.txt"
ARRAYS = ("stated_diagnoses", "diagnosis_updates", "facts", "labs", "investigations", "explicit_tasks")
OBJECTS = ("demographics", "stated_admission_date")
HARD_CONSTRAINTS = [
    "Output exactly one JSON object; no Markdown, no prose.",
    "Extract only what the source states; no inference, diagnosis, interpretation, or advice.",
    "Every item carries an exact quote from the source.",
    "Numbers, units, dates and text must appear in the item's quote.",
    "Keep negation/uncertainty words; set assertion present/absent/uncertain.",
    "Hedged diagnoses are facts with kind=impression, never stated_diagnoses.",
    "Never compute or guess dates; copy them or use null.",
    "Use only the listed English enum values.",
]


# ----- source files -----

TEXT_SUFFIXES = {".txt", ".md", ".markdown", ""}
MIN_PDF_TEXT = 50  # fewer extracted chars than this: no usable text layer (scanned?)


def read_source_file(path: str | Path) -> str:
    """Text of a .txt/.md/.pdf source. PDFs go through the first available local extractor."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in TEXT_SUFFIXES:
        return path.read_text(encoding="utf-8")
    if suffix in (".png", ".jpg", ".jpeg", ".heic", ".tif", ".tiff", ".bmp", ".webp"):
        raise PMError(f"{path.name}: images need the host agent's vision/OCR (this project does no OCR); "
                      "transcribe faithfully and pass the text via stdin with --text-origin host_transcription")
    if suffix != ".pdf":
        raise PMError(f"unsupported file type {suffix}; use .txt/.md/.pdf or pass extracted text via stdin")
    text = _pdf_text(path)
    if len(re.sub(r"\s", "", text)) < MIN_PDF_TEXT:
        raise PMError(f"{path.name}: no usable text layer (scanned PDF?); this project does no OCR — use the host agent's "
                      "vision/OCR to transcribe faithfully, then pass the text via stdin with --text-origin host_transcription")
    return text


def _pdf_text(path: Path) -> str:
    custom = os.environ.get("PM_PDF_EXTRACTOR")
    if custom:
        cmd = shlex.split(custom) + [str(path)]
    else:
        try:
            import pypdf  # optional; an explicit host extractor always takes precedence
            return "\n".join(page.extract_text() or "" for page in pypdf.PdfReader(str(path)).pages)
        except ImportError:
            pass
        if not shutil.which("pdftotext"):
            raise PMError("no PDF text extractor available (pypdf, pdftotext or $PM_PDF_EXTRACTOR); "
                          "extract the text with host tools and pass it via stdin")
        cmd = ["pdftotext", "-layout", str(path), "-"]
    done = subprocess.run(cmd, capture_output=True, timeout=120)
    if done.returncode != 0:
        raise PMError(f"PDF extractor failed: {done.stderr.decode(errors='replace')[:200]}")
    return done.stdout.decode("utf-8", errors="replace")


# ----- prepare -----

_INVISIBLE = dict.fromkeys(map(ord, "\ufeff\u200b\u200c\u200d\u2060"), None) | {0xa0: " ", 0x3000: " "}


_PAGE_FOOTER = re.compile(r"(?m)^[ \t]*(?:\d{1,3}(?:[ \t]*/[ \t]*\d{1,3})?|第[ \t]*\d+[ \t]*页(?:[ \t]*共[ \t]*\d+[ \t]*页)?)[ \t]*\n")


def clean_text(text: str) -> str:
    """Deterministic PDF-text cleanup: line endings, invisible characters (BOM/zero-width chars break label
    parsing) and standalone page-number lines (a page break otherwise splits words: "黏\n1\n膜")."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").translate(_INVISIBLE)
    return _PAGE_FOOTER.sub("", text)


def prepare(db: DB, patient_id: str, text: str, kind: str | None = None, document_date: str | None = None,
            out_dir: str | Path | None = None, max_chars: int = 1200, text_origin: str = "native_text") -> dict:
    text = clean_text(text)
    if not text.strip():
        raise PMError("source text is empty")
    kind = kind or textparse.detect_kind(text)
    if kind not in provenance.SOURCE_KINDS:
        raise PMError(f"unknown source kind: {kind}")
    doc = state.parse_when(document_date)
    if document_date and not doc:
        raise PMError("document date must be an explicit date")
    doc_date = doc[0][:10] if doc else None
    fp = provenance.fingerprint(kind, text, {})

    with state.patient_tx(db, patient_id):
        existing = db.source_by_fingerprint(patient_id, fp)
        if existing:
            db.log_ingest(patient_id, existing["source_id"], fp, "duplicate")
            open_tasks = [t for t in db.all("extraction_tasks", patient_id, source_id=existing["source_id"])
                          if t["status"] in ("pending", "repair_needed")]
            out = {"status": "duplicate", "source_id": existing["source_id"], "deterministic": None}
        else:
            source = provenance.register(db, patient_id, kind, fp, text, doc_date, text_origin)
            chunks = textparse.chunk(text, max_chars, default_kind=kind)
            det = textparse.deterministic(text, chunks, doc_date)
            result = ingestion.apply_extraction(db, patient_id, source, "deterministic_parser", text,
                                                det["extraction"], chunks=chunks)
            open_tasks = _make_tasks(db, patient_id, source, text, chunks, det, budget=max_chars)
            db.log_ingest(patient_id, source["source_id"], fp, "prepared")
            out = {"status": "prepared", "source_id": source["source_id"], "source_kind": kind,
                   "chunks": len(chunks), "deterministic": result}
    folder = _folder(db, patient_id, out["source_id"], out_dir)
    out["tasks"] = [{"task_id": t["id"], "status": t["status"], "chunk_ids": [c["chunk_id"] for c in t["chunks"]],
                     "package": str(write_package(db, patient_id, t, folder))} for t in open_tasks]
    return out


def _make_tasks(db: DB, patient_id: str, source: dict, text: str, chunks: list[dict], det: dict,
                budget: int) -> list[dict]:
    """Group chunks needing the model into tasks of <= budget chars (an atomic oversized chunk goes alone)."""
    lines = {ln["no"]: ln for ln in textparse._lines(text)}
    need = [c for c in chunks if any(no not in det["covered_lines"] for no in range(c["line_start"], c["line_end"] + 1))]
    groups, cur = [], []
    for c in need:
        if cur and sum(len(x["text"]) for x in cur) + len(c["text"]) > budget:
            groups.append(cur)
            cur = []
        cur.append(c)
    if cur:
        groups.append(cur)
    tasks = []
    for n, group in enumerate(groups, 1):
        nos = [no for c in group for no in range(c["line_start"], c["line_end"] + 1) if no in det["lab_lines"]]
        task = {"id": f"xt_{source['source_id'][4:]}_{n:02d}", "source_id": source["source_id"],
                "source_kind": source["kind"], "status": "pending", "attempts": 0, "chunks": group,
                "lab_spans": [[lines[no]["start"], lines[no]["end"]] for no in nos],
                "already_captured": [lines[no]["text"].strip() for no in nos], "errors": []}
        tasks.append(db.put("extraction_tasks", patient_id, task))
    return tasks


def _folder(db: DB, patient_id: str, source_id: str, out_dir) -> Path:
    if out_dir:
        folder = Path(out_dir)
    elif db.path != ":memory:":
        folder = Path(db.path).parent / "work" / patient_id / source_id
    else:
        raise PMError("out_dir is required with an in-memory database")
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def build_package(db: DB, patient_id: str, task: dict) -> dict:
    dx = [d["display_text"] for d in db.all("diagnoses", patient_id) if d["status"] == "active"][:20]
    header = db.get_patient(patient_id)["header"]
    context = {"user_diagnoses": dx, "admission_date_known": header["admission_date"] is not None}
    repair = ""
    if task["status"] == "repair_needed":
        repair = ("\nPREVIOUS ATTEMPT WAS REJECTED. Fix these problems and output the complete JSON again:\n" +
                  "\n".join(f"- [{e['code']}] {e['item']}: {e['reason']}" for e in task["errors"][:20]) + "\n")
    source_block = "\n\n".join(
        f"[{c['chunk_id']} | {c['section'] or 'untitled'} | lines {c['line_start']}-{c['line_end']}]\n{c['text']}"
        for c in task["chunks"])
    prompt = Template(TEMPLATE.read_text(encoding="utf-8")).substitute(
        context=("User diagnoses on record: " + ("; ".join(dx) if dx else "none") +
                 "\nAdmission date already recorded: " + ("yes" if context["admission_date_known"] else "no")),
        already="\n".join(task["already_captured"]) or "none",
        repair=repair, source_kind=task["source_kind"], source=source_block)
    return {
        "package_type": "pm.extraction_task", "package_version": 1, "task_type": "source_extraction",
        "task_id": task["id"], "patient_id": patient_id, "source_id": task["source_id"],
        "source_kind": task["source_kind"], "attempt": task["attempts"] + 1, "max_attempts": MAX_ATTEMPTS,
        "chunks": [{k: c[k] for k in ("chunk_id", "section", "line_start", "line_end", "char_start", "char_end", "text")}
                   for c in task["chunks"]],
        "already_captured": task["already_captured"], "context": context,
        "hard_constraints": HARD_CONSTRAINTS,
        "output_schema": schema.inline("model-outputs.schema.json", "/$defs/source_extraction"),
        "instructions": prompt,
        "submit_with": "pm submit <this package file> <model output file>",
    }


def write_package(db: DB, patient_id: str, task: dict, folder: Path) -> Path:
    pkg = build_package(db, patient_id, task)
    path = folder / f"{patient_id}.{task['id']}.attempt{pkg['attempt']}.json"
    path.write_text(json.dumps(pkg, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


# ----- tolerant parsing (syntax only; never changes clinical content) -----

def parse_output(raw: str) -> tuple[dict | None, list[str], str | None]:
    notes: list[str] = []
    s = raw.strip().lstrip("﻿")
    if "```" in s:
        s = re.sub(r"```(?:json|JSON)?", "", s)
        notes.append("removed code fences")
    start = s.find("{")
    if start < 0:
        return None, notes, "no JSON object found"
    end = _matching_brace(s, start)
    if end is None:
        return None, notes, "unbalanced braces"
    if s[:start].strip() or s[end + 1:].strip():
        notes.append("ignored text outside the JSON object")
    body = s[start:end + 1]
    for attempt in (body, _repair(body)):
        try:
            obj = json.loads(attempt)
        except json.JSONDecodeError as e:
            err = f"invalid JSON: {e}"
            continue
        if attempt is not body:
            notes.append("repaired JSON syntax")
        return (obj, notes, None) if isinstance(obj, dict) else (None, notes, "top level is not an object")
    return None, notes, err


def _matching_brace(s: str, start: int) -> int | None:
    depth, in_str, esc = 0, False, False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            esc = ch == "\\" and not esc
            if ch == '"' and not esc:
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
    return None


def _repair(s: str) -> str:
    s = re.sub(r",\s*([}\]])", r"\1", s)                    # trailing commas
    s = re.sub(r'([{\[,:]\s*)[“”]', r'\1"', s)              # CJK quote opening a string
    s = re.sub(r'[“”](\s*[:,}\]])', r'"\1', s)              # CJK quote closing a string
    s = re.sub(r'"\s*：', '":', s)                          # full-width colon after a key
    s = re.sub(r'"\s*，\s*(?=["{\[])', '", ', s)            # full-width comma between values
    s = re.sub(r":\s*None\b", ": null", s)
    return s


# ----- normalization (shape and enum spelling only) -----

_KEY_ALIASES = {"diagnoses": "stated_diagnoses", "diagnosis": "stated_diagnoses", "tasks": "explicit_tasks",
                "plans": "explicit_tasks", "lab_results": "labs", "examinations": "investigations",
                "exams": "investigations", "admission_date": "stated_admission_date", "patient": "demographics",
                "header": "demographics"}
_ENUMS = {
    "kind": {"主诉": "chief_complaint", "症状": "symptom", "体征": "sign", "病史": "history", "既往史": "history",
             "用药": "medication", "药物": "medication", "治疗": "treatment", "操作": "procedure", "手术": "procedure",
             "事件": "event", "生命体征": "vital", "印象": "impression", "其他": "other", "complaint": "chief_complaint"},
    "assertion": {"阳性": "present", "存在": "present", "肯定": "present", "positive": "present", "affirmed": "present",
                  "阴性": "absent", "否定": "absent", "negated": "absent", "negative": "absent",
                  "不确定": "uncertain", "可疑": "uncertain", "possible": "uncertain", "hedged": "uncertain",
                  "未知": "unknown", "不明": "unknown", "ambiguous": "unknown"},
    "category": {"影像": "imaging", "超声": "ultrasound", "彩超": "ultrasound", "心电图": "ecg", "ekg": "ecg",
                 "内镜": "endoscopy", "病理": "pathology", "功能检查": "function_test", "微生物": "microbiology",
                 "其他": "other"},
    "source_kind": {"计划": "progress_note_plan", "诊疗计划": "progress_note_plan", "医嘱": "medical_order",
                    "查房": "ward_round_plan", "用户": "user_plan"},
    "sex": {"男": "male", "女": "female", "男性": "male", "女性": "female", "m": "male", "f": "female"},
    "action": {"修正": "revise", "修改": "revise", "更正": "revise", "撤销": "revoke", "删除": "revoke"},
    "status": {"成功": "ok", "完成": "ok", "信息不足": "insufficient_information"},
}
_STRING_FIELDS = {"raw_value", "text", "quote", "test_name", "raw_unit", "reference_text", "flag_text", "title", "name"}


def _enum(field: str, v):
    if not isinstance(v, str):
        return v
    key = v.strip().lower()
    return _ENUMS.get(field, {}).get(key, _ENUMS.get(field, {}).get(v.strip(), key))


def _clean_item(item: dict) -> dict:
    out = {}
    for k, v in item.items():
        if isinstance(v, str) and v.strip().lower() in ("", "null", "none", "n/a"):
            v = None
        if isinstance(v, (int, float)) and not isinstance(v, bool) and k in _STRING_FIELDS:
            v = str(v)
        if k in _ENUMS and v is not None:
            v = _enum(k, v)
        if k == "findings":
            v = [_clean_item(f) for f in (v if isinstance(v, list) else [v] if isinstance(v, dict) else [])]
        out[k] = v
    return out


def normalize(obj: dict) -> tuple[dict, list[str]]:
    notes = []
    out = {}
    for k, v in obj.items():
        key = _KEY_ALIASES.get(k, k)
        if key != k:
            notes.append(f"renamed field {k} -> {key}")
        out[key] = v
    out.setdefault("module", "source_extraction")
    out["status"] = _enum("status", out.get("status") or "ok")
    for field in ARRAYS:
        v = out.get(field)
        if v is None:
            v = []
        elif isinstance(v, dict):
            notes.append(f"wrapped single {field} object in a list")
            v = [v]
        out[field] = [_clean_item(x) for x in v if isinstance(x, dict)] if isinstance(v, list) else v
    for field in OBJECTS:
        v = out.get(field)
        if isinstance(v, str) and field == "stated_admission_date":
            v = {"date": v, "quote": None}
        out[field] = _clean_item(v) if isinstance(v, dict) else None
    return out, notes


def _filter_items(obj: dict) -> tuple[dict, list[dict], list[str]]:
    """Per-item schema check. Bad items are dropped (V03), the rest kept."""
    base = "/$defs/source_extraction/properties/"
    allowed = set(schema.inline("model-outputs.schema.json", "/$defs/source_extraction")["properties"])
    notes = [f"ignored unknown field {k}" for k in obj if k not in allowed]
    ex, dropped = {}, []
    for field in ARRAYS:
        limit = schema.inline("model-outputs.schema.json", base + field).get("maxItems", 50)
        items = obj[field]
        if len(items) > limit:
            notes.append(f"{field}: truncated to {limit} items")
            items = items[:limit]
        props = set(schema.inline("model-outputs.schema.json", base + field + "/items")["properties"])
        kept, seen = [], set()
        for i, item in enumerate(items):
            extra = [k for k in item if k not in props]
            if extra:
                notes.append(f"{field}[{i}]: ignored unknown fields {extra}")
                item = {k: v for k, v in item.items() if k in props}
            errs = schema.validate(item, "model-outputs.schema.json", base + field + "/items")
            if errs:
                dropped.append({"item": f"{field}[{i}]", "code": "V03", "reason": errs[0]})
                continue
            key = json.dumps(item, sort_keys=True, ensure_ascii=False)
            if key in seen:
                notes.append(f"{field}[{i}]: duplicate item ignored")
                continue
            seen.add(key)
            kept.append(item)
        ex[field] = kept
    for field in OBJECTS:
        v = obj[field]
        if v is not None:
            props = set(schema.inline("model-outputs.schema.json", base + field)["properties"])
            extra = [k for k in v if k not in props]
            if extra:
                notes.append(f"{field}: ignored unknown fields {extra}")
                v = {k: x for k, x in v.items() if k in props}
            errs = schema.validate(v, "model-outputs.schema.json", base + field)
            if errs:
                dropped.append({"item": field, "code": "V03", "reason": errs[0]})
                v = None
        ex[field] = v
    return ex, dropped, notes


def _count(obj: dict) -> int:
    return sum(len(obj.get(f) or []) for f in ARRAYS) + sum(1 for f in OBJECTS if obj.get(f))


# ----- submit -----

def submit(db: DB, package: dict, raw_output: str, out_dir: str | Path | None = None) -> dict:
    task_id, patient_id = package.get("task_id"), package.get("patient_id")
    if not task_id or not patient_id:
        raise PMError("not a task package (missing task_id/patient_id)")
    with state.patient_tx(db, patient_id):
        task = db.get("extraction_tasks", patient_id, task_id)
        if task is None:
            raise PMError(f"no extraction task {task_id} for {patient_id}")
        if task["status"] in ("accepted", "partial", "failed"):
            return {"status": "already_done", "task_id": task_id, "task_status": task["status"],
                    "added": {}, "skipped": [], "rejected": [], "conflicts": [], "notes": []}
        task["attempts"] += 1
        obj, notes, parse_error = parse_output(raw_output)
        fatal, result = [], {"added": {}, "skipped": [], "rejected": [], "conflicts": []}
        if obj is None:
            fatal = [{"item": "$", "code": "V01", "reason": parse_error}]
        else:
            obj, more = normalize(obj)
            notes += more
            if obj["module"] != "source_extraction":
                fatal = [{"item": "module", "code": "V02", "reason": f"expected source_extraction, got {obj['module']}"}]
            elif any(not isinstance(obj[f], list) for f in ARRAYS):
                fatal = [{"item": f, "code": "V02", "reason": "must be an array"} for f in ARRAYS if not isinstance(obj[f], list)]
            else:
                ex, dropped, more = _filter_items(obj)
                notes += more
                source = db.get("sources", patient_id, task["source_id"])
                result = ingestion.apply_extraction(db, patient_id, source, "model",
                                                    db.source_text(patient_id, task["source_id"]), ex,
                                                    chunks=task["chunks"], skip_lab_spans=[tuple(s) for s in task["lab_spans"]])
                result["rejected"] = dropped + result["rejected"]
                kept_anything = result["added"] or result["skipped"] or result["conflicts"]
                if _count(obj) and not kept_anything and obj["status"] == "ok":
                    fatal = result["rejected"] + [{"item": "$", "code": "V15", "reason": "no item passed validation"}]
        if fatal:
            task["errors"] = fatal
            task["status"] = "failed" if task["attempts"] >= MAX_ATTEMPTS else "repair_needed"
        else:
            task["errors"] = result["rejected"]
            task["status"] = "partial" if result["rejected"] else "accepted"
        db.put("extraction_tasks", patient_id, task)
    out = {"status": task["status"], "task_id": task_id, "attempt": task["attempts"], **result,
           "errors": fatal, "notes": notes}
    if task["status"] == "repair_needed":
        folder = _folder(db, patient_id, task["source_id"], out_dir)
        out["repair_package"] = str(write_package(db, patient_id, task, folder))
    if task["status"] == "failed":
        out["fallback"] = "model extraction abandoned for these chunks; deterministic results and raw source are kept"
    return out


def open_tasks(db: DB, patient_id: str) -> list[dict]:
    return [{"task_id": t["id"], "source_id": t["source_id"], "status": t["status"], "attempts": t["attempts"]}
            for t in db.all("extraction_tasks", patient_id) if t["status"] in ("pending", "repair_needed")]
