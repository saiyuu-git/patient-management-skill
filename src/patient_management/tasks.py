"""TODO state machine. explicit and ai_suggestion never cross; only a user accept creates an explicit copy."""
from __future__ import annotations

import re

from . import provenance
from .persistence import DB, PMError, now
from .state import parse_when, patient_tx

TRANSITIONS = {
    "pending": {"in_progress", "completed", "cancelled"},
    "in_progress": {"completed", "cancelled"},
    "completed": set(),
    "cancelled": set(),
}
EXPLICIT_SOURCES = {"user_plan", "medical_order", "progress_note_plan", "ward_round_plan"}
PRIORITIES = {"high", "normal", "low"}


def _due(due_text: str | None) -> str | None:
    parsed = parse_when(due_text)
    return parsed[0][:10] if parsed else None


def _get(db: DB, patient_id: str, task_id: str) -> dict:
    task = db.get("tasks", patient_id, task_id)
    if not task:
        raise PMError(f"no task {task_id}")
    return task


def add_explicit(db: DB, patient_id: str, title: str, explicit_source: str, source_ref: dict,
                 due_text: str | None = None, priority: str = "normal", from_suggestion_id=None) -> dict | None:
    """Returns None when an identical open explicit task already exists."""
    allowed = EXPLICIT_SOURCES | ({"user_accepted_suggestion"} if from_suggestion_id else set())
    if explicit_source not in allowed:
        raise PMError(f"explicit_source not allowed here: {explicit_source}")
    if priority not in PRIORITIES:
        raise PMError(f"bad priority {priority}")
    title = title.strip()
    for t in db.all("tasks", patient_id):
        if t["origin"] == "explicit" and t["status"] in ("pending", "in_progress") and t["title"] == title:
            return None
    ts = now()
    task = {"id": db.next_id(patient_id, "task"), "title": title, "detail": None, "origin": "explicit",
            "explicit_source": explicit_source, "status": "pending", "priority": priority,
            "due_date": _due(due_text), "evidence_ids": [], "source_ref": source_ref,
            "from_suggestion_id": from_suggestion_id, "created_at": ts, "updated_at": ts}
    return db.put("tasks", patient_id, task)


def add_manual(db: DB, patient_id: str, title: str, due_text: str | None = None, priority: str = "normal"):
    with patient_tx(db, patient_id):
        src = provenance.manual_source(db, patient_id, {"add_task": title, "due": due_text})
        return add_explicit(db, patient_id, title, "user_plan", provenance.ref(src["source_id"], "manual"),
                            due_text, priority)


def add_suggestion(db: DB, patient_id: str, title: str, evidence_ids: list[str], priority: str = "normal",
                   detail: str | None = None) -> dict:
    """Entry point for the future task_suggestions module. Always ai_suggestion, always pending."""
    if not evidence_ids:
        raise PMError("suggestion needs at least one evidence id")
    if priority not in PRIORITIES:
        raise PMError(f"bad priority {priority}")
    with patient_tx(db, patient_id):
        unknown = set(evidence_ids) - provenance.known_fact_ids(db, patient_id)
        if unknown:
            raise PMError(f"unknown evidence ids: {sorted(unknown)}")
        ts = now()
        task = {"id": db.next_id(patient_id, "task"), "title": title.strip(), "detail": detail,
                "origin": "ai_suggestion", "status": "pending", "priority": priority, "due_date": None,
                "evidence_ids": list(dict.fromkeys(evidence_ids)), "from_suggestion_id": None,
                "dismissed": False, "created_at": ts, "updated_at": ts}
        return db.put("tasks", patient_id, task)


def set_status(db: DB, patient_id: str, task_id: str, status: str) -> dict:
    with patient_tx(db, patient_id):
        task = _get(db, patient_id, task_id)
        if task["origin"] != "explicit":
            raise PMError("AI suggestions have no workflow status; accept or dismiss them")
        if status not in TRANSITIONS.get(task["status"], set()):
            raise PMError(f"cannot move {task_id} from {task['status']} to {status}")
        provenance.manual_source(db, patient_id, {"set_task_status": task_id, "status": status})
        task.update(status=status, updated_at=now())
        return db.put("tasks", patient_id, task)


def set_completion(db: DB, patient_id: str, task_id: str, completed: bool) -> dict:
    """Explicit user checkbox action; never called by a model or completion hint."""
    if type(completed) is not bool:
        raise PMError("completed must be boolean")
    with patient_tx(db, patient_id):
        task = _get(db, patient_id, task_id)
        if task["origin"] != "explicit" or task["status"] == "cancelled":
            raise PMError("only non-cancelled explicit tasks support completion")
        status = "completed" if completed else "pending"
        if task["status"] == status:
            return task
        provenance.manual_source(db, patient_id, {"user_task_completion": task_id, "completed": completed})
        task.update(status=status, updated_at=now())
        return db.put("tasks", patient_id, task)


def _open_suggestion(db: DB, patient_id: str, task_id: str) -> dict:
    task = _get(db, patient_id, task_id)
    if task["origin"] != "ai_suggestion":
        raise PMError(f"{task_id} is not an AI suggestion")
    if task.get("dismissed"):
        raise PMError(f"{task_id} was dismissed")
    if any(t.get("from_suggestion_id") == task_id for t in db.all("tasks", patient_id)):
        raise PMError(f"{task_id} was already accepted")
    return task


def accept_suggestion(db: DB, patient_id: str, task_id: str) -> dict:
    """User action only: creates a new explicit task. The suggestion record keeps origin=ai_suggestion."""
    with patient_tx(db, patient_id):
        sug = _open_suggestion(db, patient_id, task_id)
        src = provenance.manual_source(db, patient_id, {"accept_suggestion": task_id})
        task = add_explicit(db, patient_id, sug["title"], "user_accepted_suggestion",
                            provenance.ref(src["source_id"], "manual"), priority=sug["priority"],
                            from_suggestion_id=task_id)
        if task is None:
            raise PMError("an identical open task already exists")
        return task


def dismiss_suggestion(db: DB, patient_id: str, task_id: str) -> dict:
    with patient_tx(db, patient_id):
        sug = _open_suggestion(db, patient_id, task_id)
        sug.update(dismissed=True, updated_at=now())
        return db.put("tasks", patient_id, sug)


# ----- possible_completed hints (read-only; never change task status) -----

_INV_TERMS = {"MRI": ("MR", "核磁", "磁共振"), "MRS": ("MRS",), "CT": ("CT",), "EEG": ("脑电图",), "ECG": ("心电图",),
              "US": ("超声", "B超", "彩超"), "XR": ("X线", "胸片")}
_LAB_TERMS = {"肝功": ["alt", "ast", "tbil", "alb", "ggt", "alp"], "肾功": ["cr", "urea", "ua"], "电解质": ["k", "na", "cl"],
              r"(?<!输)血常规": ["wbc", "hb", "plt", "neut_pct", "rbc"], "C反应蛋白": ["crp"], "CRP": ["crp"], "血沉": ["esr"],
              "同型半胱氨酸": ["hcy"], "血糖": ["glu"], "白介素-6": ["白介素-6"], "IL-6": ["白介素-6"], "甲功": ["促甲状腺"],
              "凝血": ["凝血酶原", "纤维蛋白原"], "血脂": ["胆固醇", "甘油三酯"]}
_COMPLETION_VERBS = re.compile(r"完善|复查|检查|行|查|请|会诊|监测")


def task_dates(db: DB, patient_id: str, task_list: list[dict]) -> dict[str, str | None]:
    """A task's date = the latest explicit date written before it in its source (its note/summary date)."""
    out, texts = {}, {}
    for t in task_list:
        ref = t.get("source_ref") or {}
        span, sid = ref.get("span"), ref.get("source_id")
        if not span or not sid:
            out[t["id"]] = None
            continue
        text = texts.setdefault(sid, db.source_text(patient_id, sid) or "")
        dates = [parse_when(m.group()) for m in re.finditer(r"\d{4}-\d{2}-\d{2}", text[:span["start"]])]
        out[t["id"]] = max((d[0][:10] for d in dates if d), default=None)
    return out


def completion_hints(db: DB, patient_id: str) -> list[dict]:
    """Deterministic 'possible_completed' hints: a matching result/record exists on or after the task's date.
    The task keeps its status; only an explicit user action completes it."""
    open_tasks = [t for t in db.all("tasks", patient_id) if t["origin"] == "explicit" and t["status"] in ("pending", "in_progress")]
    dates = task_dates(db, patient_id, open_tasks)
    invs = db.all("investigations", patient_id)
    results = db.all("lab_results", patient_id)
    sources = db.all("sources", patient_id)
    hints = []
    for t in open_tasks:
        title, since = t["title"], dates.get(t["id"])
        if not since or not _COMPLETION_VERBS.search(title):
            continue  # undated tasks are never matched (no ordering evidence)
        ev = []
        for terms in _INV_TERMS.values():
            if any(w in title for w in terms):
                ev += [i["id"] for i in invs if any(w in i["name"] for w in terms) and (i["performed_at"] or "")[:10] >= since]
        for key, targets in _LAB_TERMS.items():  # keys are regex fragments ("输血常规" is not 血常规)
            if re.search(key, title):
                ev += [r["id"] for r in results if r["collected_at"][:10] >= since and
                       (r["test_id"] in targets or any(x in r["test_name"] for x in targets))][:3]
        m = re.search(r"请(\S{1,6}?)科?会诊", title)
        if m:
            dept = re.escape(m.group(1))
            own = (t.get("source_ref") or {}).get("source_id")
            record = re.compile(rf"(?:会诊科室|被邀请科室)\s*[:：]\s*{dept}|{dept}\S{{0,3}}会诊(?:记录|意见)")
            for src in sources:
                text = db.source_text(patient_id, src["source_id"]) or ""
                if src["source_id"] != own and record.search(text) and (src.get("document_date") or since) >= since:
                    facts = [f["id"] for f in db.all("facts", patient_id) if f["source_ref"]["source_id"] == src["source_id"]]
                    ev += facts[:2] or [src["source_id"]]
        ev = list(dict.fromkeys(ev))
        if ev:
            hints.append({"task_id": t["id"], "hint": "possible_completed", "evidence_ids": ev[:5],
                          "reason": f"matching record on or after {since}", "display": "可能已完成，待确认"})
    return hints
