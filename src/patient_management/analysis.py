"""Clinical analysis engine: fixed modules, minimal context, schema-bound outputs, validation, fallback.

prepare(module): deterministic context selection -> task package (only the ids/lines this module needs,
                 plus validated external evidence already linked to the patient)
submit(package, output): parse -> per-item schema filter -> clinical validator -> analyses record
                 (+ side effects: candidate diagnoses, AI suggestion tasks); 1 repair, then fallback.
Modules never see the whole record and never decide layout. Each module fails alone.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from pathlib import Path
from string import Template

from . import diagnoses, evidence, schema, state, tasks
from .extraction import parse_output
from .persistence import DB, PMError, now

PROMPT = Path(__file__).with_name("prompts") / "analysis.txt"
MAX_ATTEMPTS = 2
MODULES = ["patient_summary", "diagnosis_candidates", "lab_interpretation", "investigation_analysis", "problem_list",
           "clinical_assessment", "today_focus", "task_suggestions", "handover_summary"]
UPSTREAM = {  # validated outputs a module may read (never raw upstream text from failed modules)
    "problem_list": ["lab_interpretation", "investigation_analysis", "diagnosis_candidates"],
    "clinical_assessment": ["problem_list", "lab_interpretation", "investigation_analysis"],
    "today_focus": ["problem_list"],
    "task_suggestions": ["problem_list", "clinical_assessment"],
    "handover_summary": ["problem_list", "clinical_assessment", "today_focus"],
    "patient_summary": ["problem_list", "clinical_assessment"],
}
TASKS = {
    "patient_summary": "Write a short patient overview: basics, the main clinical problem, current status, recent key change.",
    "diagnosis_candidates": "Propose candidate diagnoses ONLY if the patient data support something not already covered by the "
                            "locked user diagnoses. Prefer none over weak candidates.",
    "lab_interpretation": "For each abnormal lab series listed, state what is abnormal, restate the given trend, and give "
                          "possible explanations in this patient's context.",
    "investigation_analysis": "For each investigation listed, state the key significance of its extracted findings, relation "
                              "to current problems, and change versus the previous comparable study if one is listed.",
    "problem_list": "List the patient's current management problems (not just diagnoses), most important first.",
    "clinical_assessment": "Give a bounded overall assessment: what the illness consists of, what is improving, what is still "
                           "abnormal, what to keep watching, and the main uncertainties.",
    "today_focus": "Choose at most 3 of the FOCUS CANDIDATES, most important first, and phrase each briefly.",
    "task_suggestions": "Suggest at most 5 items worth clinician confirmation that are NOT already explicit tasks.",
    "handover_summary": "Fill the fixed handover fields for quick verbal reporting on a phone.",
}
FIELDS = {
    "patient_summary": '{"module":"patient_summary","status":"ok","one_liner":{"text":str<=120,"evidence_ids":[...]},'
                       '"key_points":[{"aspect":"basic|main_problem|current_status|recent_change","text":str,'
                       '"evidence_ids":[...],"external_refs":[],"certainty":"likely|possible|uncertain"}]<=4}',
    "diagnosis_candidates": '{"module":"diagnosis_candidates","status":"ok|insufficient_information","candidates":[{"display_text":str<=60,'
                            '"certainty":"likely|possible|uncertain","evidence_ids":[...],"external_refs":[],"rationale":str,'
                            '"against":str|null}]<=5}',
    "lab_interpretation": '{"module":"lab_interpretation","status":"ok","items":[{"test_id":str,"abnormality":str,"trend_comment":str,'
                          '"explanations":[{"kind":"association|possible_explanation|insufficient_evidence","text":str,'
                          '"evidence_ids":[...],"external_refs":[],"certainty":"..."}]<=3,"clinical_significance":str|null,'
                          '"change_significance":"meaningful|not_meaningful|uncertain","uncertainty":str|null,'
                          '"evidence_ids":[...],"external_refs":[],"certainty":"..."}]}',
    "investigation_analysis": '{"module":"investigation_analysis","status":"ok","items":[{"investigation_id":"fact_inv_...",'
                              '"key_significance":str,"relation_to_problems":str|null,"compared_with_previous":str|null,'
                              '"uncertainty":str|null,"evidence_ids":[...],"external_refs":[],"certainty":"..."}]}',
    "problem_list": '{"module":"problem_list","status":"ok","problems":[{"title":str<=40,"linked_diagnosis_id":"dx_..."|null,'
                    '"current_status":"active|improving|worsening|stable|resolved|unclear","supporting_evidence_ids":[...],'
                    '"recent_change":str|null,"assessment":str,"uncertainty":str|null,"attention":str|null,'
                    '"external_refs":[],"certainty":"..."}]<=8}',
    "clinical_assessment": '{"module":"clinical_assessment","status":"ok","overall_trend":"improving|stable|worsening|mixed|unclear",'
                           '"composition":[STATEMENT]<=4,"improving":[STATEMENT]<=3,"still_abnormal":[STATEMENT]<=3,'
                           '"watch":[STATEMENT]<=3,"uncertainties":[str]<=3}  STATEMENT={"text":str,"evidence_ids":[...],'
                           '"external_refs":[],"certainty":"..."}',
    "today_focus": '{"module":"today_focus","status":"ok","items":[{"candidate_id":"fc_..","text":str<=80,"evidence_ids":[...]}]<=3}',
    "task_suggestions": '{"module":"task_suggestions","status":"ok","suggestions":[{"title":str<=60,"rationale":str,'
                        '"evidence_ids":[...],"external_refs":[],"priority":"high|normal|low"}]<=5}',
    "handover_summary": '{"module":"handover_summary","status":"ok","opening":[S]<=1,"key_history":[S]<=2,"key_findings":[S]<=3,'
                        '"current_status":[S]<=2,"major_problems":[S]<=3,"today_focus":[S]<=3}  S={"text":str,'
                        '"evidence_ids":[...],"external_refs":[],"certainty":"..."}',
}
ABNORMAL = {"high", "low", "critical_high", "critical_low", "abnormal"}
EVIDENCE_LAB_LIMIT = 3  # at most this many lab questions per patient analysis run (cost control)
CONCEPTS = {"cr": "serum creatinine", "urea": "blood urea", "k": "serum potassium", "na": "serum sodium", "glu": "blood glucose",
            "hb": "hemoglobin", "wbc": "white blood cell count", "plt": "platelet count", "alt": "alanine aminotransferase",
            "ast": "aspartate aminotransferase", "tbil": "total bilirubin", "alb": "serum albumin", "crp": "C-reactive protein",
            "pct": "procalcitonin", "neut_pct": "neutrophil percentage", "lac": "lactate", "ua": "serum uric acid",
            "cl": "serum chloride", "dbil": "direct bilirubin", "tp": "total serum protein", "ggt": "gamma-glutamyl transferase",
            "alp": "alkaline phosphatase", "rbc": "red blood cell count", "esr": "erythrocyte sedimentation rate",
            "hcy": "homocysteine"}


# ----- deterministic context -----

# Problem/diagnosis keyword -> related analytes (test_id or test-name fragment). Small and explicit on purpose.
TOPIC_LABS = {
    "贫血": ["hb", "rbc", "红细胞比积"], "血红蛋白": ["hb"], "出血": ["hb", "plt"],
    "肝": ["alt", "ast", "tbil", "dbil", "ggt", "alp", "alb"], "黄疸": ["tbil", "dbil"],
    "肾": ["cr", "urea", "ua"], "尿酸": ["ua"],
    "感染": ["wbc", "neut_pct", "crp", "esr", "pct", "白介素-6", "降钙素原"], "发热": ["wbc", "neut_pct", "crp", "esr", "白介素-6"],
    "炎": ["crp", "esr", "白介素-6", "wbc"], "钠": ["na"], "钾": ["k"], "电解质": ["k", "na", "cl"],
    "糖": ["glu"], "蛋白": ["alb", "tp"], "营养": ["alb", "tp"], "凝血": ["纤维蛋白原", "凝血酶原"],
    "同型半胱氨酸": ["hcy"], "血小板": ["plt"],
}
ACTIVE = {"worsening", "active", "unclear"}


def topic_matchers(s: dict) -> dict[str, int]:
    """Analyte matchers -> relevance weight: 3 = active problem, 2 = user diagnosis or pending candidate."""
    weights: dict[str, int] = {}
    pl = s["analyses"].get("problem_list")
    problems = pl["output"]["problems"] if pl and pl["status"] in ("ready", "partial") and pl.get("output") else []
    texts = [(p["title"], 3 if p["current_status"] in ACTIVE else 2) for p in problems]
    texts += [(d["display_text"], 2) for d in s["diagnoses"]
              if (d["origin"] == "user_provided" and d["status"] == "active") or d.get("review_status") == "pending"]
    for text, w in texts:
        for kw, targets in TOPIC_LABS.items():
            if kw in text:
                for t in targets:
                    weights[t] = max(weights.get(t, 0), w)
    return weights


def relevance(x: dict, matchers: dict[str, int]) -> int:
    return max([w for m, w in matchers.items() if x["test_id"] == m or m in x["test_name"]], default=0)

def _day(s: str | None) -> str | None:
    return s[:10] if s else None


def index_date(s: dict) -> str:
    """Latest dated patient datum: 'today' for the analysis (records may be historical)."""
    dates = [r["collected_at"] for r in s["labs"]["results"]] + [f["observed_at"] for f in s["facts"] if f["observed_at"]]
    dates += [i["performed_at"] for i in s["investigations"] if i["performed_at"]]
    return max((_day(d) for d in dates if d), default=now()[:10])


def _days_before(d: str | None, ref: str) -> int | None:
    if not d:
        return None
    return (date.fromisoformat(ref) - date.fromisoformat(d[:10])).days


def _lab_lines(s: dict, matchers: dict[str, int] | None = None) -> tuple[dict[str, str], list[dict]]:
    """Abnormal series, code-ranked. Series related to current problems/diagnoses come first and are kept
    beyond the generic caps (≤6 extra), so e.g. an anemia problem always sees Hb."""
    matchers = topic_matchers(s) if matchers is None else matchers
    by_test: dict[str, list[dict]] = {}
    for r in s["labs"]["results"]:
        by_test.setdefault(r["test_id"], []).append(r)
    lines, picked = {}, []
    def order(x):  # critical, then latest-abnormal by objective deviation, then formerly abnormal
        flag = x["latest_abnormal_flag"]
        tier = 0 if flag in ("critical_high", "critical_low") else 1 if flag in ABNORMAL else 2
        return (tier, -relevance(x, matchers), -deviation(s, x), x["priority_rank"])
    counts = {"abnormal": 0, "normalized": 0, "related": 0}
    for x in sorted(s["labs"]["series"], key=order):
        pts = sorted(by_test.get(x["test_id"], []), key=lambda r: r["collected_at"])
        if not any(p["abnormal_flag"] in ABNORMAL for p in pts):
            continue
        group = "abnormal" if x["latest_abnormal_flag"] in ABNORMAL else "normalized"
        if counts[group] >= (8 if group == "abnormal" else 4):  # still abnormal / returned to normal
            if not relevance(x, matchers) or counts["related"] >= 6:
                continue
            group = "related"
        counts[group] += 1
        picked.append(x)
        ref = pts[-1]
        rng = f"ref {ref['reference_low']}-{ref['reference_high']}" if ref["reference_low"] is not None or ref["reference_high"] is not None \
            else f"ref {ref['reference_text'] or 'n/a'}"
        if x["data_status"] == "sufficient":
            t = x["reference_status_transition"]
            lines[x["id"]] = (f"{x['test_name']} series: pattern {x['pattern']}, overall {x['overall_direction']}, recent "
                              f"{x['recent_direction']}, first {x['first_value']} -> latest {x['latest_value']} {x['unit'] or ''}, "
                              f"change {x['first_to_latest']['absolute_change']} ({x['first_to_latest']['percent_change']}%), "
                              f"status {t['first']} -> {t['latest']}, {rng}")
        else:
            latest = f"latest {ref['raw_value']} {ref['raw_unit'] or ''} on {ref['collected_at']}"
            lines[x["id"]] = (f"{x['test_name']}: {x['data_status']} (single/non-comparable), {latest}, "
                              f"flag {x['latest_abnormal_flag']}, {rng}")
        for p in pts[-5:]:
            lines[p["id"]] = f"{p['test_name']} {p['raw_value']} {p['raw_unit'] or ''} on {p['collected_at']} [{p['abnormal_flag']}]"
    return lines, picked


def _fact_lines(s: dict, kinds: set[str] | None = None, limit: int = 20, ref: str | None = None) -> dict[str, str]:
    facts = [f for f in s["facts"] if kinds is None or f["kind"] in kinds]
    facts.sort(key=lambda f: f["observed_at"] or "", reverse=True)  # recent first; undated (history) last
    out = {}
    for f in facts[:limit]:
        when = f" on {f['observed_at']}" if f["observed_at"] else ""
        out[f["id"]] = f"[{f['kind']}] {f['text']} ({f['assertion']}){when}"
    return out


def _dx_lines(s: dict) -> dict[str, str]:
    out = {}
    for d in s["diagnoses"]:
        if d["origin"] == "user_provided" and d["status"] == "active":
            out[d["id"]] = f"USER DIAGNOSIS [locked]: {d['display_text']}"
        elif d.get("review_status") == "pending":
            out[d["id"]] = f"candidate (unconfirmed, {d['certainty']}): {d['display_text']}"
    return out


def _inv_lines(s: dict, limit: int = 8) -> dict[str, str]:
    out = {}
    invs = sorted(s["investigations"], key=lambda i: i["performed_at"] or "", reverse=True)[:limit]
    for inv in invs:
        out[inv["id"]] = f"{inv['name']} on {inv['performed_at']}: impression: {(inv['impression'] or 'n/a')[:300]}"
        for f in inv["findings"]:
            out[f["id"]] = f"  finding of {inv['id']}: {f['text']}"
    return out


def _upstream(s: dict, module: str) -> list[str]:
    out = []
    for m in UPSTREAM.get(module, []):
        rec = s["analyses"].get(m)
        if not rec or rec["status"] not in ("ready", "partial") or not rec.get("output"):
            continue
        out.append(f"VALIDATED {m} (AI analysis, not patient fact): " + json.dumps(_brief(m, rec["output"]), ensure_ascii=False)[:1500])
    return out


def _brief(module: str, out: dict) -> object:
    if module == "problem_list":
        return [{k: p.get(k) for k in ("title", "current_status", "supporting_evidence_ids", "attention")} for p in out["problems"]]
    if module == "lab_interpretation":
        return [{k: i.get(k) for k in ("test_id", "abnormality", "change_significance")} for i in out["items"]]
    if module == "investigation_analysis":
        return [{k: i.get(k) for k in ("investigation_id", "key_significance")} for i in out["items"]]
    if module == "clinical_assessment":
        return {"overall_trend": out["overall_trend"], "watch": [x["text"] for x in out.get("watch", [])]}
    if module == "today_focus":
        return [i["text"] for i in out["items"]]
    if module == "diagnosis_candidates":
        return [c["display_text"] for c in out["candidates"]]
    return out


def _within(d: str | None, ref: str, days: int) -> bool:
    n = _days_before(d, ref)
    return n is not None and 0 <= n <= days


def focus_candidates(s: dict, ref: str, db: DB | None = None) -> list[dict]:
    """Code-generated Today Focus candidates (priority 1 = most important). The model may only pick and phrase."""
    cands = []
    pl = s["analyses"].get("problem_list")
    for p in (pl["output"]["problems"] if pl and pl["status"] in ("ready", "partial") and pl.get("output") else [])[:4]:
        if p["current_status"] in ("worsening", "active", "unclear"):
            cands.append({"kind": "active_problem", "priority": 1 if p["current_status"] == "worsening" else 3,
                          "text": f"问题：{p['title']}（{p['current_status']}）", "evidence_ids": p["supporting_evidence_ids"][:4]})
    for f in s["facts"]:
        if f["kind"] == "event" and _within(f["observed_at"], ref, 0):
            cands.append({"kind": "recent_event", "priority": 2, "text": f["text"][:60], "evidence_ids": [f["id"]]})
    for inv in s["investigations"]:
        if _within(inv["performed_at"], ref, 2):
            cands.append({"kind": "new_investigation", "priority": 2,
                          "text": f"{inv['name']}：{(inv['impression'] or '')[:60]}", "evidence_ids": [inv["id"]]})
    _, series = _lab_lines(s)
    labs = []
    for x in series:
        last = max((r["collected_at"] for r in s["labs"]["results"] if r["test_id"] == x["test_id"]), default=None)
        if x["latest_abnormal_flag"] not in ABNORMAL or not _within(last, ref, 3):
            continue
        t = x.get("reference_status_transition") or {}
        critical = x["latest_abnormal_flag"].startswith("critical")
        newly = t.get("first") not in ABNORMAL if t else False
        labs.append({"kind": "abnormal_lab", "priority": 2 if critical else 3 if newly else 5,
                     "text": f"{x['test_name']} 最新 {x.get('latest_value', '')} {x['unit'] or ''} [{x['latest_abnormal_flag']}]",
                     "evidence_ids": [x["id"]]})
    cands += sorted(labs, key=lambda c: c["priority"])[:2]  # long-standing mild abnormalities never crowd out the rest
    dates = tasks.task_dates(db, s["patient_id"], s["tasks"]) if db else {}
    for t in s["tasks"]:
        if t["origin"] == "explicit" and t["status"] in ("pending", "in_progress"):
            when = dates.get(t["id"])
            if (t.get("due_date") and t["due_date"] <= ref) or _within(when, ref, 1):
                cands.append({"kind": "open_task", "priority": 3, "text": f"待办：{t['title']}", "evidence_ids": [t["id"]],
                              "task_id": t["id"]})
    cands.sort(key=lambda c: c["priority"])  # stable: within a priority, the order above
    cands = cands[:8]
    for i, c in enumerate(cands, 1):
        c["candidate_id"] = f"fc_{i:02d}"
    return cands


def build_context(db: DB, patient_id: str, module: str) -> dict:
    """{lines: {id: text}, sections: [(title, ids)], ext: {ext_id: text}, extra: {...}}. Minimal per module."""
    if module not in MODULES:
        raise PMError(f"unknown analysis module {module}")
    s = state.get_state(db, patient_id)
    ref = index_date(s)
    h = s["header"]
    basics = f"{h['sex']}, age {h['age']['value'] if h['age'] else 'unknown'}, admission {h['admission_date'] or 'unknown'}"
    dx = _dx_lines(s)
    labs, series = _lab_lines(s)
    invs = _inv_lines(s)
    clinical = {"chief_complaint", "symptom", "sign", "vital", "impression", "event", "procedure", "treatment", "history", "medication"}
    background = {f["id"]: f"[{f['kind']}] {f['text']} ({f['assertion']})" for f in s["facts"]  # undated background is never "recent"
                  if f["kind"] in ("chief_complaint", "history", "event") and not f["observed_at"]
                  and f["assertion"] in ("present", "uncertain")}
    background = dict(list(background.items())[:6])
    sections: list[tuple[str, dict[str, str]]] = []
    if module == "lab_interpretation":
        sections = [("ABNORMAL LAB SERIES (code-computed; do not recompute)", labs), ("DIAGNOSES", dx),
                    ("RELATED FACTS", _fact_lines(s, {"symptom", "sign", "vital", "treatment", "medication", "event"}, 15))]
    elif module == "investigation_analysis":
        sections = [("INVESTIGATIONS (extracted findings only)", invs), ("DIAGNOSES", dx),
                    ("RELATED FACTS", _fact_lines(s, {"symptom", "sign", "impression"}, 10))]
    elif module == "diagnosis_candidates":
        sections = [("DIAGNOSES (locked user diagnoses are read-only)", dx),
                    ("FACTS", _fact_lines(s, {"chief_complaint", "symptom", "sign", "history", "impression"}, 20)),
                    ("ABNORMAL LABS", labs), ("INVESTIGATIONS", invs)]
    elif module in ("problem_list", "clinical_assessment"):
        sections = [("DIAGNOSES", dx), ("BACKGROUND", background), ("ABNORMAL LABS", labs), ("INVESTIGATIONS", invs),
                    ("RECENT FACTS", _fact_lines(s, clinical, 25))]
    elif module == "patient_summary":
        sections = [("DIAGNOSES", dx), ("BACKGROUND", background),
                    ("KEY FACTS", _fact_lines(s, {"chief_complaint", "event", "symptom", "sign"}, 12)),
                    ("KEY TREATMENTS", _fact_lines(s, {"treatment", "medication", "procedure"}, 5)),
                    ("INVESTIGATIONS", _inv_lines(s, 3)),
                    ("ABNORMAL LABS", {k: v for k, v in labs.items() if k.startswith("fact_series")})]
    elif module == "task_suggestions":
        open_tasks = {t["id"]: t["title"] for t in s["tasks"] if t["origin"] == "explicit" and t["status"] in ("pending", "in_progress")}
        sections = [("DIAGNOSES", dx), ("ABNORMAL LABS", labs), ("INVESTIGATIONS", _inv_lines(s, 4)),
                    ("RECENT FACTS", _fact_lines(s, clinical, 15)),
                    ("ALREADY EXPLICIT TASKS (do not repeat)", {k: f"(task) {v}" for k, v in open_tasks.items()})]
    elif module == "today_focus":
        cands = focus_candidates(s, ref, db)
        lines = {c["candidate_id"]: f"{c['text']} | evidence {c['evidence_ids'] or [c.get('task_id')]}" for c in cands}
        sections = [("FOCUS CANDIDATES (choose from these only)", lines)]
    elif module == "handover_summary":
        sections = [("BASICS", {"basics": basics}), ("DIAGNOSES", dx), ("BACKGROUND", background), ("ABNORMAL LABS", labs),
                    ("INVESTIGATIONS", invs),
                    ("RECENT FACTS", _fact_lines(s, clinical, 15))]
    all_lines = {k: v for _, sec in sections for k, v in sec.items()}
    if module == "today_focus":  # evidence ids of candidates must resolve too
        for c in focus_candidates(s, ref, db):
            for e in c["evidence_ids"]:
                all_lines.setdefault(e, _any_line(s, e))
    ext = _linked_evidence(db, s, set(all_lines))
    extra = {"basics": basics, "index_date": ref, "upstream": _upstream(s, module)}
    if module == "today_focus":
        extra["candidates"] = focus_candidates(s, ref, db)
    if module == "lab_interpretation":
        extra["series"] = {x["test_id"]: {k: x.get(k) for k in ("id", "overall_direction", "recent_direction", "pattern", "data_status")}
                           for x in series}
    return {"lines": all_lines, "sections": [(t, list(sec)) for t, sec in sections], "ext": ext, "extra": extra,
            "assertions": {f["id"]: f["assertion"] for f in s["facts"]},
            "user_dx": {d["id"]: d["display_text"] for d in s["diagnoses"] if d["origin"] == "user_provided"},
            "inv_findings": {i["id"]: [f["id"] for f in i["findings"]] for i in s["investigations"]}}


def _any_line(s: dict, eid: str) -> str:
    for f in s["facts"]:
        if f["id"] == eid:
            return f["text"]
    for r in s["labs"]["results"] + s["labs"]["series"]:
        if r["id"] == eid:
            return r["test_name"]
    for d in s["diagnoses"]:
        if d["id"] == eid:
            return d["display_text"]
    for i in s["investigations"]:
        if i["id"] == eid:
            return i["name"]
    for t in s["tasks"]:
        if t["id"] == eid:
            return f"(explicit task, {t['status']}) {t['title']}"
    return eid


def _linked_evidence(db: DB, s: dict, ids: set[str]) -> dict[str, str]:
    """Validated external evidence from this patient's answered/cached requests whose facts are in this context."""
    ext_ids = []
    for r in s["evidence_requests"]:
        if r["status"] in ("answered", "cached") and set(r["relevant_fact_ids"]) & ids:
            ext_ids += r["evidence_ids"]
    out = {}
    for item in evidence.items(db, list(dict.fromkeys(ext_ids))):  # content-verified only
        out[item["evidence_id"]] = (f"{item['title']} ({item.get('organization') or item['domain']}, tier {item['source_tier']}, "
                                    f"{item.get('publication_date') or 'n.d.'}): " + " | ".join(item["relevant_claims"]))
    return out


# ----- evidence needs -----

def deviation(s: dict, x: dict) -> float:
    """Objective size of the latest abnormality: distance outside the reference range / range width (no clinical judgment)."""
    pts = sorted((r for r in s["labs"]["results"] if r["test_id"] == x["test_id"] and r["normalized_value"] is not None),
                 key=lambda r: r["collected_at"])
    if not pts:
        return 0.0
    r = pts[-1]
    v, lo, hi = r["normalized_value"], r["reference_low"], r["reference_high"]
    if lo is None and hi is None:
        return 0.5 if r["abnormal_flag"] in ABNORMAL else 0.0  # flagged by the report, range unknown
    width = (hi - lo) if lo is not None and hi is not None and hi > lo else abs(hi or lo or 1) or 1
    if hi is not None and v > hi:
        return (v - hi) / width
    if lo is not None and v < lo:
        return (lo - v) / width
    return 0.0


def evidence_needs(db: DB, patient_id: str) -> list[dict]:
    """Narrow lab questions where external knowledge may change management. Code decides; no patient values.
    Priority: related to an active problem (3) or a diagnosis/candidate (2), reference-status change (2),
    critical value (2); deviation size only breaks ties. Isolated mild abnormalities (score < 2) never use the budget."""
    s = state.get_state(db, patient_id)
    matchers = topic_matchers(s)
    _, series = _lab_lines(s, matchers)
    scored = []
    for x in series:
        if x["latest_abnormal_flag"] not in ABNORMAL:
            continue
        t = x.get("reference_status_transition") or {}
        dynamic = 2 if x["data_status"] == "sufficient" and t.get("first") != t.get("latest") else 0
        critical = 2 if x["latest_abnormal_flag"].startswith("critical") else 0
        score = relevance(x, matchers) + dynamic + critical
        if score >= 2:
            scored.append((score, deviation(s, x), x))
    specs = []
    for _, _, x in sorted(scored, key=lambda t: (-t[0], -t[1])):
        concept = CONCEPTS.get(x["test_id"]) or re.split(r"[(（]", x["test_name"])[0]
        high = x["latest_abnormal_flag"] in ("high", "critical_high")
        trending = x["data_status"] == "sufficient" and x["pattern"] in ("monotonic_increasing", "monotonic_decreasing")
        direction = ("rising" if x["pattern"] == "monotonic_increasing" else "falling") if trending else ("elevated" if high else "low")
        specs.append({"question_type": "lab_interpretation", "concept": concept,
                      "concept_en": concept if re.fullmatch(r"[A-Za-z \-]+", concept) else None,
                      "direction": direction, "relevant_fact_ids": [x["id"]], "max_sources": 3})
        if len(specs) >= EVIDENCE_LAB_LIMIT:
            break
    return specs


def plan(db: DB, patient_id: str) -> dict:
    s = state.get_state(db, patient_id)
    return {"patient_id": patient_id, "index_date": index_date(s),
            "modules": [{"module": m, "status": (s["analyses"].get(m) or {}).get("status", "not_run"),
                         "reads": UPSTREAM.get(m, [])} for m in MODULES],
            "evidence_needs": evidence_needs(db, patient_id),
            "external_evidence": [{k: r[k] for k in ("request_id", "clinical_question", "external_evidence_status")}
                                  for r in s["evidence_requests"]]}


# ----- package -----

def prepare(db: DB, patient_id: str, module: str, out_dir: str | Path | None = None) -> dict:
    ctx = build_context(db, patient_id, module)
    key = hashlib.sha256(json.dumps([ctx["lines"], ctx["ext"], ctx["extra"]], ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
    with state.patient_tx(db, patient_id):
        tid = db.next_id(patient_id, f"at_{module}")
        task = {"id": tid, "module": module, "status": "pending", "attempts": 0, "context": ctx, "input_hash": key,
                "errors": [], "created_at": now()}
        db.put("analysis_tasks", patient_id, task)
        rec = (state.get_state(db, patient_id)["analyses"].get(module) or {})
        if rec.get("status") not in ("ready", "partial"):
            state_rec = {"status": "pending", "output": rec.get("output"), "attempts": 0, "validator_errors": [],
                         "input_hash": key, "generated_at": rec.get("generated_at")}
            db.put("analysis_records", patient_id, {"id": module, **state_rec})
    return {"task_id": tid, "module": module, "package": str(_write(db, patient_id, task, out_dir)),
            "context_ids": len(ctx["lines"]), "external_refs": list(ctx["ext"])}


def build_package(patient_id: str, task: dict) -> dict:
    ctx = task["context"]
    blocks = [f"BASICS: {ctx['extra']['basics']}"]
    for title, ids in ctx["sections"]:
        if ids:
            blocks.append(title + ":\n" + "\n".join(f"[{i}] {ctx['lines'][i]}" for i in ids))
    blocks += ctx["extra"]["upstream"]
    repair = ""
    if task["status"] == "repair_needed":
        repair = ("\nPREVIOUS OUTPUT WAS REJECTED. Fix these problems and output the complete JSON again:\n"
                  + "\n".join(f"- [{e['code']}] {e['item']}: {e['reason']}" for e in task["errors"][:20]) + "\n")
    prompt = Template(PROMPT.read_text(encoding="utf-8")).substitute(
        task=TASKS[task["module"]], fields=FIELDS[task["module"]], index_date=ctx["extra"]["index_date"],
        context="\n\n".join(blocks),
        evidence="\n".join(f"[{k}] {v}" for k, v in ctx["ext"].items()) or "none (do not cite external_refs)", repair=repair)
    return {"package_type": "pm.analysis_task", "package_version": 1, "task_id": task["id"], "patient_id": patient_id,
            "module": task["module"], "attempt": task["attempts"] + 1, "max_attempts": MAX_ATTEMPTS,
            "output_schema": schema.inline("model-outputs.schema.json", f"/$defs/{task['module']}"),
            "instructions": prompt, "submit_with": "pm analysis submit <this package file> <output file>"}


def _write(db: DB, patient_id: str, task: dict, out_dir) -> Path:
    folder = Path(out_dir) if out_dir else Path(db.path).parent / "work" / patient_id / "analysis"
    folder.mkdir(parents=True, exist_ok=True)
    pkg = build_package(patient_id, task)
    path = folder / f"{patient_id}.{task['id']}.attempt{pkg['attempt']}.json"
    path.write_text(json.dumps(pkg, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


# ----- validation -----

_NUM = re.compile(r"\d+(?:\.\d+)?")
_DOSE = re.compile(r"\d+(?:\.\d+)?\s*(?:mg|g|μg|ug|ml|mL|U|IU|片|粒|支)(?![A-Za-z/])|(?:ivgtt|po|qd|bid|tid|q\dh)\b", re.I)
_ORDER = re.compile(r"立即给予|立即予|给予|予以|予\S{0,6}(?:治疗|静滴|口服)|开具|医嘱：|停用|加用|start\s+\w+\s+\d|prescribe", re.I)
_CONFIRMED = re.compile(r"确诊|诊断明确|已明确诊断|明确为|证实为|definitive|confirmed", re.I)
_CAUSAL = re.compile(r"导致|引起|所致|造成|继发于|由于|因为|caused by|due to|because|results? in", re.I)
_HEDGE = re.compile(r"可能|考虑|或许|也许|疑|提示|不除外|不能排除|相关|有关|待|may|might|possibl|suggest|consistent|associated", re.I)
_NEGATION = re.compile(r"无|未|否认|阴性|没有|消失|不伴|正常|\(-\)|（-）|no\b|not\b|absent|negative|normal", re.I)
_TREND_UP = re.compile(r"(较前|逐渐|持续|进行性|呈|逐次)\S{0,3}(升高|上升|增高|增加)|上升趋势|升高趋势|rising|increasing trend")
_TREND_DOWN = re.compile(r"(较前|逐渐|持续|进行性|呈|逐次)\S{0,3}(下降|降低|减少|回落)|下降趋势|降低趋势|falling|decreasing trend")
_HTML = re.compile(r"<[a-zA-Z/][^>]*>|^#{1,6}\s|\|\s*-{3,}|\[[^\]]+\]\([^)]+\)", re.M)


_CLAIM_FIELDS = ("text", "title", "assessment", "abnormality", "key_significance", "rationale", "recent_change",
                 "clinical_significance", "display_text")


def _numbers(text: str) -> set[str]:
    out = set()
    for n in _NUM.findall(text):
        if "." in n or len(n) >= 2:
            out.add(n.rstrip("0").rstrip(".") if "." in n else n)
    return out


def _allowed_numbers(ctx: dict, eids: list[str], ext: list[str]) -> set[str]:
    texts = [ctx["lines"].get(e, "") for e in eids] + [ctx["ext"].get(x, "") for x in ext] + [ctx["extra"]["basics"]]
    return set().union(*(_numbers(t) for t in texts)) if texts else set()


def _texts(item: dict) -> list[str]:
    out = []
    for k, v in item.items():
        if isinstance(v, str) and k not in ("test_id", "investigation_id", "candidate_id", "certainty", "kind", "status",
                                             "current_status", "change_significance", "priority", "aspect", "linked_diagnosis_id"):
            out.append(v)
        elif isinstance(v, list) and v and isinstance(v[0], str) and k == "possible_causes":
            out += v
    return out


def check_item(item: dict, ctx: dict, module: str, where: str) -> list[dict]:
    """Clinical validator for one model item (statement-like object). Returns problems (empty = keep)."""
    probs = []
    bad = lambda code, why: probs.append({"item": where, "code": code, "reason": why})  # noqa: E731
    eids = item.get("evidence_ids") or item.get("supporting_evidence_ids") or []
    ext = item.get("external_refs") or []
    unknown = [e for e in eids if e not in ctx["lines"]]
    if unknown:
        bad("V04", f"evidence ids not in this task's patient data: {unknown}")
    if any(x not in ctx["ext"] for x in ext):
        bad("V04", f"external_refs not provided as validated evidence: {[x for x in ext if x not in ctx['ext']]}")
    text = " ".join(_texts(item))
    if _HTML.search(text):
        bad("V11", "markup inside clinical text")
    extra_nums = _numbers(text) - _allowed_numbers(ctx, eids, ext)
    if extra_nums:
        bad("V05", f"numbers not in the cited evidence: {sorted(extra_nums)}")
    if _DOSE.search(text):
        bad("V07", "dose or administration wording")
    if _CONFIRMED.search(text):
        bad("V20", "presents a hypothesis as confirmed")
    if _CAUSAL.search(text) and not _HEDGE.search(text):
        bad("V23", "causal claim without hedging (association is not causation)")
    claim = " ".join(str(item.get(k) or "") for k in _CLAIM_FIELDS)  # the assertion itself, not caveats
    absent = [e for e in eids if ctx["assertions"].get(e) == "absent"]
    if absent and not _NEGATION.search(claim):
        bad("V22", f"negative facts used as positive support: {absent}")
    shaky = [e for e in eids if ctx["assertions"].get(e) == "unknown"]
    if shaky and (len(shaky) == len(eids) or item.get("certainty") == "likely"):
        bad("V21", f"assertion=unknown facts used as reliable support: {shaky}")
    if item.get("certainty") in ("possible", "uncertain") and module == "problem_list" and not item.get("uncertainty"):
        bad("V09", "uncertain problem without an uncertainty note")
    return probs


def _module_checks(module: str, item: dict, ctx: dict, where: str, s: dict) -> list[dict]:
    probs = []
    bad = lambda code, why: probs.append({"item": where, "code": code, "reason": why})  # noqa: E731
    if module == "lab_interpretation":
        ser = ctx["extra"]["series"].get(item["test_id"])
        if ser is None:
            bad("V04", f"test {item['test_id']} is not among the listed abnormal series")
        else:
            own = {ser["id"]} | {e for e in ctx["lines"] if e.startswith(f"fact_lab_{item['test_id']}_")}
            if not own & set(item["evidence_ids"]):
                bad("V04", "must cite the series or its points")
            words = item["trend_comment"] + " " + item["abnormality"]
            if ser["data_status"] == "sufficient":
                if _TREND_UP.search(words) and ser["overall_direction"] == "down" and ser["recent_direction"] == "down":
                    bad("V08", "describes a rising trend; computed trend is falling")
                if _TREND_DOWN.search(words) and ser["overall_direction"] == "up" and ser["recent_direction"] == "up":
                    bad("V08", "describes a falling trend; computed trend is rising")
    if module == "investigation_analysis":
        iid = item["investigation_id"]
        if iid not in ctx["lines"]:
            bad("V24", f"{iid} is not a listed investigation")
        elif not ({iid} | set(ctx["inv_findings"].get(iid, []))) & set(item["evidence_ids"]):
            bad("V24", "must cite the investigation or its extracted findings")
    if module == "diagnosis_candidates":
        norm = lambda t: re.sub(r"[\s，,。.;；:：、？?]+", "", t)  # noqa: E731
        if any(norm(item["display_text"]) == norm(d) for d in ctx["user_dx"].values()):
            bad("V06", "duplicates a locked user diagnosis")
    if module == "problem_list" and item.get("linked_diagnosis_id") and item["linked_diagnosis_id"] not in ctx["lines"]:
        bad("V04", "linked_diagnosis_id not in patient data")
    if module == "today_focus":
        cand = next((c for c in ctx["extra"]["candidates"] if c["candidate_id"] == item["candidate_id"]), None)
        if cand is None:
            bad("V25", "not a code-generated focus candidate")
        elif not set(item["evidence_ids"]) <= set(cand["evidence_ids"]) | {cand.get("task_id")}:
            bad("V25", "evidence outside the chosen candidate")
    if module in ("diagnosis_candidates", "task_suggestions") and not any(e.startswith("fact_") for e in item["evidence_ids"]):
        bad("V04", "needs at least one patient fact (fact_*) as support")
    if module == "task_suggestions":
        if _ORDER.search(item["title"] + " " + item.get("rationale", "")):
            bad("V07", "suggestion phrased as an order")
        open_titles = [t["title"] for t in s["tasks"] if t["origin"] == "explicit" and t["status"] in ("pending", "in_progress")]
        if any(item["title"].strip() == t for t in open_titles):
            bad("V26", "already an explicit task")
    return probs


def _items_of(module: str, out: dict) -> list[tuple[str, dict]]:
    """(path, item) for every statement-like object in a module output."""
    if module == "patient_summary":
        return ([("one_liner", out["one_liner"])] if out.get("one_liner") else []) + \
            [(f"key_points[{i}]", x) for i, x in enumerate(out.get("key_points", []))]
    lists = {"diagnosis_candidates": ["candidates"], "lab_interpretation": ["items"], "investigation_analysis": ["items"],
             "problem_list": ["problems"], "clinical_assessment": ["composition", "improving", "still_abnormal", "watch"],
             "today_focus": ["items"], "task_suggestions": ["suggestions"],
             "handover_summary": ["opening", "key_history", "key_findings", "current_status", "major_problems", "today_focus"]}[module]
    return [(f"{k}[{i}]", x) for k in lists for i, x in enumerate(out.get(k, []))]


def validate_output(module: str, obj: dict, ctx: dict, s: dict) -> tuple[dict | None, list[dict], list[dict]]:
    """Returns (clean output, dropped-item problems, fatal problems)."""
    if obj.get("module", module) != module:
        return None, [], [{"item": "module", "code": "V02", "reason": f"expected {module}"}]
    obj = {"module": module, **{k: v for k, v in obj.items() if k != "module"}}
    obj.setdefault("status", "ok")
    base = f"/$defs/{module}"
    props = schema.inline("model-outputs.schema.json", base)["properties"]
    problems = [{"item": k, "code": "W", "reason": "unknown field ignored"} for k in obj if k not in props]
    obj = {k: v for k, v in obj.items() if k in props}
    submitted = 0
    for key, spec in props.items():
        if spec.get("type") == "array" and spec.get("items", {}).get("type") == "object":
            kept = []
            for i, item in enumerate(obj.get(key) or []):
                submitted += 1
                errs = schema.validate(item, "model-outputs.schema.json", f"{base}/properties/{key}/items")
                if errs:
                    problems.append({"item": f"{key}[{i}]", "code": "V03", "reason": errs[0]})
                    continue
                kept.append(item)
            obj[key] = kept[: spec.get("maxItems", 50)]
    if module == "patient_summary" and obj.get("one_liner") is not None:
        submitted += 1
        if schema.validate(obj["one_liner"], "model-outputs.schema.json", f"{base}/properties/one_liner"):
            problems.append({"item": "one_liner", "code": "V03", "reason": "malformed"})
            obj["one_liner"] = None
    top = schema.validate({k: (v if not isinstance(v, list) else []) for k, v in obj.items()}, "model-outputs.schema.json", base)
    top = [e for e in top if "items" not in e]
    if top:
        return None, problems, [{"item": "$", "code": "V02", "reason": top[0]}]
    for path, item in _items_of(module, obj):
        if module == "lab_interpretation":  # a bad explanation drops only itself
            kept = []
            for j, ex in enumerate(item.get("explanations") or []):
                p = check_item(ex, ctx, module, f"{path}.explanations[{j}]")
                problems += p
                if not p:
                    kept.append(ex)
            item["explanations"] = kept
        p = check_item(item, ctx, module, path) + _module_checks(module, item, ctx, path, s)
        if p:
            problems += p
            _drop(obj, path)
    if submitted and not any(x for _, x in _items_of(module, obj)) and obj["status"] == "ok":
        return None, problems, problems + [{"item": "$", "code": "V15", "reason": "no item passed validation"}]
    return obj, problems, []


def _drop(obj: dict, path: str) -> None:
    key, _, idx = path.partition("[")
    if not idx:
        obj[key] = None
        return
    i = int(idx.split("]")[0])
    obj[key][i] = None  # mark; compacted below


def _compact(module: str, obj: dict) -> dict:
    for k, v in obj.items():
        if isinstance(v, list):
            obj[k] = [x for x in v if x is not None]
    if module == "problem_list":
        obj["problems"] = order_problems(obj["problems"])
    return obj


# ----- deterministic ordering / fallbacks -----

def order_problems(problems: list[dict], s: dict | None = None) -> list[dict]:
    """Deterministic tiers; the model's order is kept only within a tier.
    1 worsening  2 linked to a user diagnosis  3 other (incl. AI candidates)  4 resolved."""
    def tier(p):
        if p["current_status"] == "resolved":
            return 4
        if p["current_status"] == "worsening":
            return 1
        return 2 if p.get("linked_diagnosis_id") else 3
    return sorted(problems, key=tier)  # stable sort


def fallback_output(db: DB, patient_id: str, module: str) -> dict | None:
    """Deterministic templates that only recombine existing facts. None = UI shows 暂无可靠分析."""
    s = state.get_state(db, patient_id)
    h = s["header"]
    sex = {"male": "男", "female": "女"}.get(h["sex"], "")
    age = f"{h['age']['value']:g}岁" if h["age"] and h["age"]["unit"] == "year" else ""
    dx = [d for d in s["diagnoses"] if d["origin"] == "user_provided" and d["status"] == "active"]
    dx_text = "；".join(d["display_text"] for d in dx[:4]) or "待补充"
    if module == "patient_summary":
        return {"module": module, "status": "ok", "template": True,
                "one_liner": {"text": f"{sex}{age}，诊断：{dx_text}"[:120], "evidence_ids": [d["id"] for d in dx[:4]] or []},
                "key_points": []}
    if module == "today_focus":
        cands = focus_candidates(s, index_date(s), db)[:3]
        return {"module": module, "status": "ok", "template": True,
                "items": [{"candidate_id": c["candidate_id"], "text": c["text"][:80], "evidence_ids": c["evidence_ids"]} for c in cands]}
    if module == "handover_summary":
        _, series = _lab_lines(s)
        stmt = lambda t, e: {"text": t[:300], "evidence_ids": e, "certainty": "likely"}  # noqa: E731
        findings = [stmt(f"{x['test_name']}：{x.get('first_value', '')}→{x.get('latest_value', '')} {x['unit'] or ''}，{x.get('pattern') or x['data_status']}",
                         [x["id"]]) for x in series[:3]]
        focus = fallback_output(db, patient_id, "today_focus")["items"]
        return {"module": module, "status": "ok", "template": True,
                "opening": [stmt(f"{sex}{age}，入院{h['admission_date'] or '待补充'}", [])],
                "key_history": [stmt(f["text"], [f["id"]]) for f in s["facts"] if f["kind"] == "chief_complaint"][:1],
                "key_findings": findings, "current_status": [],
                "major_problems": [stmt(d["display_text"], [d["id"]]) for d in dx[:3]],
                "today_focus": [stmt(c["text"], c["evidence_ids"]) for c in focus]}
    return None


# ----- submit -----

def submit(db: DB, package: dict, raw: str, out_dir: str | Path | None = None) -> dict:
    tid, patient_id, module = package.get("task_id"), package.get("patient_id"), package.get("module")
    if not tid or not patient_id or module not in MODULES:
        raise PMError("not an analysis package")
    with state.patient_tx(db, patient_id):
        task = db.get("analysis_tasks", patient_id, tid)
        if task is None:
            raise PMError(f"no analysis task {tid} for {patient_id}")
        if task["status"] not in ("pending", "repair_needed"):
            return {"status": "already_done", "task_id": tid, "task_status": task["status"]}
        task["attempts"] += 1
        s = state.get_state(db, patient_id)
        obj, notes, err = parse_output(raw)
        fatal, problems, out = [], [], None
        if obj is None:
            fatal = [{"item": "$", "code": "V01", "reason": err}]
        else:
            out, problems, fatal = validate_output(module, obj, task["context"], s)
        if fatal:
            task["errors"] = fatal
            task["status"] = "repair_needed" if task["attempts"] < MAX_ATTEMPTS else "failed"
        else:
            out = _compact(module, out)
            task["status"] = "partial" if problems else "accepted"
            task["errors"] = problems
        db.put("analysis_tasks", patient_id, task)
        if task["status"] == "failed":
            _store(db, patient_id, module, "fallback", fallback_output(db, patient_id, module), task, fatal)
        elif task["status"] in ("accepted", "partial"):
            _side_effects(db, patient_id, module, out, task["context"])
            _store(db, patient_id, module, "partial" if problems else "ready", out, task, problems)
    result = {"status": task["status"], "task_id": tid, "module": module, "problems": problems, "errors": fatal, "notes": notes}
    if task["status"] == "repair_needed":
        result["repair_package"] = str(_write(db, patient_id, task, out_dir))
    if task["status"] == "failed":
        result["fallback"] = "deterministic template" if fallback_output(db, patient_id, module) else "暂无可靠分析"
    return result


def apply_fallback(db: DB, patient_id: str, module: str) -> dict:
    """Host cannot run the module at all: store the conservative fallback directly."""
    with state.patient_tx(db, patient_id):
        _store(db, patient_id, module, "fallback", fallback_output(db, patient_id, module), {"attempts": 0, "input_hash": None}, [])
    return state.get_state(db, patient_id)["analyses"][module]


def _store(db: DB, patient_id: str, module: str, status: str, out: dict | None, task: dict, problems: list[dict]) -> None:
    refs = sorted({x for _, it in (_items_of(module, out) if out and not out.get("template") else [])
                   for x in (it.get("external_refs") or [])} |
                  {x for _, it in (_items_of(module, out) if out and module == "lab_interpretation" else [])
                   for ex in it.get("explanations") or [] for x in ex.get("external_refs") or []})
    db.put("analysis_records", patient_id, {
        "id": module, "status": status, "output": out, "dropped_items": len([p for p in problems if p["code"] != "W"]),
        "attempts": min(task.get("attempts", 0), MAX_ATTEMPTS), "validator_errors": [f"{p['code']} {p['item']}: {p['reason']}"[:200] for p in problems][:50],
        "external_refs": refs[:6], "model_label": None, "input_hash": task.get("input_hash"), "generated_at": now()})


def _side_effects(db: DB, patient_id: str, module: str, out: dict, ctx: dict) -> None:
    if module == "diagnosis_candidates":
        for c in db.all("diagnosis_candidates", patient_id):  # refresh: unreviewed candidates are replaced
            if c["review_status"] == "pending":
                db.delete("diagnosis_candidates", patient_id, c["id"])
        for c in out["candidates"]:
            diagnoses.add_candidate(db, patient_id, c["display_text"], c["certainty"],
                                    [e for e in c["evidence_ids"] if e.startswith("fact_")] or c["evidence_ids"])
    if module == "task_suggestions":
        accepted = {t.get("from_suggestion_id") for t in db.all("tasks", patient_id)}
        for t in db.all("tasks", patient_id):
            if t["origin"] == "ai_suggestion" and not t.get("dismissed") and t["id"] not in accepted:
                db.delete("tasks", patient_id, t["id"])
        for sug in out["suggestions"]:
            facts = [e for e in sug["evidence_ids"] if e.startswith("fact_")]
            if facts:
                tasks.add_suggestion(db, patient_id, sug["title"], facts, sug["priority"], sug.get("rationale"))


def show(db: DB, patient_id: str) -> dict:
    s = state.get_state(db, patient_id)
    return {"patient_id": patient_id, "index_date": index_date(s),
            "analyses": {m: s["analyses"].get(m, {"status": "not_run"}) for m in MODULES}}
