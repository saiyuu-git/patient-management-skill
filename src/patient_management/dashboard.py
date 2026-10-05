"""Dashboard view model: the only structure the fixed frontend reads.

Module keys, order, titles and all UI copy are code constants; model output only fills `data`.
Each module is built independently: a failing builder yields state=error for that module only.
"""
from __future__ import annotations

from datetime import date, timedelta
import re

from . import analysis, education, evidence, provenance, state, tasks
from .persistence import DB, now

VIEW_VERSION = "0.1"
PRESENTATION = {"sections": ["overview", "examinations", "tasks", "knowledge"],
                "overview": ["diagnoses", "clinical_assessment", "handover"],
                "examinations": ["lab_trends", "investigations"]}
ORDER = ["header", "overview", "diagnoses", "today_focus", "tasks", "lab_trends", "lab_interpretations",
         "investigations", "clinical_assessment", "problem_list", "handover", "knowledge"]
TITLES = {"header": "患者信息", "overview": "患者概览", "diagnoses": "诊断", "today_focus": "今日重点", "tasks": "今日待办",
          "lab_trends": "关键检验趋势", "lab_interpretations": "检验趋势临床分析", "investigations": "辅助检查",
          "clinical_assessment": "病情分析", "problem_list": "当前管理问题", "handover": "汇报", "knowledge": "知识补充"}
COPY = {"no_analysis": "暂无可靠分析", "partial": "仅显示已核实的分析内容", "stale": "资料已更新，分析待更新",
        "error": "该模块暂时无法显示", "loading": "分析中…", "no_labs": "暂无检验数据", "no_inv": "暂无辅助检查",
        "no_dx": "诊断：待补充", "no_tasks": "暂无待办", "no_admission": "入院：待补充", "no_ext": "暂无可靠外部医学依据",
        "ai": "智能辅助分析", "ai_suggestion": "智能建议 · 需医生确认", "candidate": "候选诊断 · 待医生确认",
        "possible_completed": "可能已完成，待确认", "no_knowledge": "暂无与当前患者相关的可靠知识补充"}
SEX = {"male": "男", "female": "女", "unknown": "—"}
PERSIST = {"saved": "已更新", "updating": "更新中", "error": "更新失败"}
FLAG = {"high": "偏高", "low": "偏低", "critical_high": "危急偏高", "critical_low": "危急偏低", "abnormal": "异常",
        "normal": "正常", "unknown": ""}
TASK_STATUS = {"pending": "待处理", "in_progress": "进行中", "completed": "已完成", "cancelled": "已取消"}
CERTAINTY = {"likely": "很可能", "possible": "可能", "uncertain": "不确定"}
PATTERN = {"constant": "无变化", "monotonic_increasing": "逐次升高", "monotonic_decreasing": "逐次降低"}
ARROW = {"up": "↑", "down": "↓", "unchanged": "—"}


def _comparison(baseline=None, basis="none"):
    return {"status": "available" if baseline else "unavailable", "baseline_date": baseline, "basis": basis}


def _prior_analysis(db, s, module):
    """Immediate previous saved analysis, not an intermediate pending/error event."""
    current = s["analyses"].get(module) or {}
    seen_current = False
    for row in reversed(db.history(s["patient_id"], "analysis_records", module)):
        rec = row["data"]
        if rec.get("status") not in ("ready", "partial") or not rec.get("output"):
            continue
        if not seen_current:
            if rec.get("output") == current.get("output"):
                seen_current = True
            continue
        if rec.get("generated_at") and rec.get("generated_at") == current.get("generated_at"):
            continue
        return rec["output"], _comparison(row["at"][:10], "previous_version")
    return None, _comparison()


def _change(item, previous):
    text = lambda x: "".join(x.get("text", "").split())
    if any(text(item) == text(old) for old in previous):
        return None
    ids = set(item.get("evidence_ids", []))
    return "updated" if ids and any(ids & set(old.get("evidence_ids", [])) for old in previous) else "new"


def _problem_rank(s, evidence_ids):
    rec = s["analyses"].get("problem_list") or {}
    problems = (rec.get("output") or {}).get("problems", []) if rec.get("status") in ("ready", "partial", "stale") else []
    return next((i for i, p in enumerate(problems, 1) if set(evidence_ids) & set(p.get("supporting_evidence_ids", []))), 999)


def _module(key: str, st: str, message: str | None = None, data: dict | None = None) -> dict:
    return {"key": key, "title": TITLES[key], "state": st, "message": message, "data": data or {}}


def _from_record(rec: dict | None) -> tuple[str, str | None, dict | None]:
    """analysis record -> (state, fixed message, output)."""
    status = (rec or {}).get("status", "not_run")
    out = (rec or {}).get("output")
    return {
        "not_run": ("empty", COPY["no_analysis"], None),
        "pending": ("loading", COPY["loading"], None),
        "ready": ("ready", None, out),
        "partial": ("partial", COPY["partial"], out),
        "stale": ("partial", COPY["stale"], out),
        "failed": ("error", COPY["error"], None),
        "fallback": ("partial" if out else "empty", COPY["no_analysis"], out),
    }.get(status, ("error", COPY["error"], None))


def _refs(db: DB, ids: list[str] | None) -> list[dict]:
    """Only content-verified external evidence reaches the UI."""
    return [{"id": x["evidence_id"], "title": x["title"], "organization": x.get("organization"), "tier": x["source_tier"],
             "url": x["url"]} for x in evidence.items(db, list(ids or []))]


def _stmt(db: DB, x: dict) -> dict:
    return {"text": x["text"], "certainty": CERTAINTY.get(x.get("certainty"), ""), "evidence_ids": x.get("evidence_ids", []),
            "external_refs": _refs(db, x.get("external_refs")), "label": COPY["ai"]}


def _header(db, s, ref):
    h = s["header"]
    age = f"{h['age']['value']:g}{ {'year': '岁', 'month': '月', 'day': '天'}[h['age']['unit']] }" if h["age"] else "—"
    return _module("header", "ready", None, {
        "label": state.display_label(s), "bed": h["bed"], "sex": SEX[h["sex"]], "age": age,
        "admission": f"入院：{h['admission_date']}" if h["admission_date"] else COPY["no_admission"],
        "updated_at": h["last_updated_at"], "persist": PERSIST.get(h["persist_status"], ""), "index_date": ref})


def _analysis_module(db, s, key, module, shape):
    st, msg, out = _from_record(s["analyses"].get(module))
    if out is None:
        if module in ("patient_summary", "today_focus", "handover_summary"):  # deterministic templates
            out = analysis.fallback_output(db, s["patient_id"], module)
            st, msg = ("partial", COPY["no_analysis"]) if st != "loading" else (st, msg)
        if out is None:
            return _module(key, st, msg)
    return _module(key, st, msg, {"template": bool(out.get("template")), **shape(out)})


def _overview(db, s, ref):
    def shape(o):
        return {"one_liner": (o.get("one_liner") or {}).get("text"),
                "points": [{"aspect": p["aspect"], **_stmt(db, p)} for p in o.get("key_points", [])]}
    return _analysis_module(db, s, "overview", "patient_summary", shape)


def _diagnoses(db, s, ref):
    user = [{"id": d["id"], "text": d["display_text"], "status": d["status"], "locked": True}
            for d in s["diagnoses"] if d["origin"] == "user_provided"]
    cands = [{"id": d["id"], "text": d["display_text"], "certainty": CERTAINTY.get(d.get("certainty"), ""),
              "label": COPY["candidate"]} for d in s["diagnoses"] if d.get("review_status") == "pending"]
    yesterday = (date.fromisoformat(now()[:10]) - timedelta(days=1)).isoformat()
    baseline = {r["id"]: r["data"] for r in db.history(s["patient_id"], "diagnoses") if r["at"][:10] <= yesterday}
    sources = {x["source_id"]: x for x in s["sources"]}
    for item in user:
        old = baseline.get(item["id"])
        dx = next(d for d in s["diagnoses"] if d["id"] == item["id"])
        src = sources.get((dx.get("source_ref") or {}).get("source_id"), {})
        historical_import = src.get("document_date") and src["document_date"][:10] <= yesterday
        item["change"] = ("updated" if old and (old.get("display_text"), old.get("status")) != (item["text"], item["status"])
                          else "new" if baseline and not old and not historical_import else None)
    old_candidates = [r["data"] for r in db.history(s["patient_id"], "diagnosis_candidates") if r["at"][:10] <= yesterday]
    for item in cands:
        item["change"] = "new" if old_candidates and not any(x.get("display_text") == item["text"] for x in old_candidates) else None
    if not user and not cands:
        return _module("diagnoses", "empty", COPY["no_dx"])
    return _module("diagnoses", "ready", None if user else COPY["no_dx"], {"user": user, "candidates": cands,
                   "comparison": _comparison(yesterday, "previous_day") if baseline else _comparison()})


def _today_focus(db, s, ref):
    return _analysis_module(db, s, "today_focus", "today_focus",
                            lambda o: {"items": [{"text": i["text"], "evidence_ids": i["evidence_ids"]} for i in o["items"]]})


def _tasks(db, s, ref):
    hints = {h["task_id"]: h for h in tasks.completion_hints(db, s["patient_id"])}
    accepted = {t.get("from_suggestion_id") for t in s["tasks"]}
    explicit = [{"id": t["id"], "title": t["title"], "status": TASK_STATUS[t["status"]], "due_date": t.get("due_date"),
                 "hint": COPY["possible_completed"] if t["id"] in hints else None,
                 "hint_evidence_ids": hints[t["id"]]["evidence_ids"] if t["id"] in hints else []}
                for t in s["tasks"] if t["origin"] == "explicit"]
    explicit.sort(key=lambda t: (t["status"] == "已完成", list(TASK_STATUS.values()).index(t["status"])))
    ai = [{"id": t["id"], "title": t["title"], "rationale": t.get("detail"), "label": COPY["ai_suggestion"]}
          for t in s["tasks"] if t["origin"] == "ai_suggestion" and not t.get("dismissed") and t["id"] not in accepted]
    if not explicit and not ai:
        return _module("tasks", "empty", COPY["no_tasks"])
    return _module("tasks", "ready", None, {"explicit": explicit, "ai_suggestions": ai})


def _trend_label(x: dict) -> str:
    if x["data_status"] == "insufficient":
        return "单次结果"
    if x["data_status"] == "not_comparable":
        return "未计算趋势"
    if x["pattern"] in PATTERN:
        return PATTERN[x["pattern"]]
    return f"有升有降 · 总体{ARROW[x['overall_direction']]} · 最近{ARROW[x['recent_direction']]}"


def _change_line(x: dict) -> str | None:
    if x["data_status"] != "sufficient":
        return None
    def part(label, c):
        pct = f" ({c['percent_change']:+g}%)" if c["percent_change"] is not None else ""
        return f"{label} {c['absolute_change']:+g}{pct}"
    return f"{part('较前次', x['previous_to_latest'])} · {part('较首次', x['first_to_latest'])}"


def _lab_trends(db, s, ref):
    matchers = analysis.topic_matchers(s)
    problems = (s["analyses"].get("problem_list") or {}).get("output") or {}
    linked = {eid for p in problems.get("problems", []) if p.get("current_status") != "resolved"
              for eid in p.get("supporting_evidence_ids", [])}
    linked |= {eid for xs in ((s["analyses"].get("clinical_assessment") or {}).get("output") or {}).values()
               if isinstance(xs, list) for item in xs if isinstance(item, dict) for eid in item.get("evidence_ids", [])}
    by_test: dict[str, list[dict]] = {}
    for r in s["labs"]["results"]:
        by_test.setdefault(r["test_id"], []).append(r)
    rows = []
    for x in sorted(s["labs"]["series"], key=lambda x: x["priority_rank"]):
        pts = sorted(by_test.get(x["test_id"], []), key=lambda r: r["collected_at"])[-20:]
        last = pts[-1] if pts else None
        rows.append({"series_id": x["id"], "test_id": x["test_id"], "name": x["test_name"], "unit": x["unit"] or (last or {}).get("raw_unit"),
                     "priority_rank": 0 if "critical" in x["latest_abnormal_flag"] else _problem_rank(s, [x["id"]] + [p["id"] for p in pts]),
                     "featured": bool(analysis.relevance(x, matchers) or x["id"] in linked or
                                      any(p["id"] in linked for p in pts) or "critical" in x["latest_abnormal_flag"]),
                     "latest": last["raw_value"] if last else None, "latest_flag": FLAG.get(x["latest_abnormal_flag"], ""),
                     "trend_label": _trend_label(x), "change_line": _change_line(x),
                     "reference": {"low": last["reference_low"], "high": last["reference_high"]} if last else None,
                     "points": [{"t": p["collected_at"], "v": p["normalized_value"], "raw": p["raw_value"],
                                 "flag": FLAG.get(p["abnormal_flag"], "")} for p in pts],
                     "label": "计算"})
    if not rows:
        return _module("lab_trends", "empty", COPY["no_labs"])
    rows.sort(key=lambda x: (x["priority_rank"], not x["featured"]))
    return _module("lab_trends", "ready", None, {"series": rows})


def _lab_interpretations(db, s, ref):
    names = {x["test_id"]: x["test_name"] for x in s["labs"]["series"]}
    def shape(o):
        return {"items": [{"test_id": i["test_id"], "name": names.get(i["test_id"], i["test_id"]), "abnormality": i["abnormality"],
                           "trend": i["trend_comment"], "significance": i.get("clinical_significance"),
                           "explanations": [{"kind": e["kind"], **_stmt(db, e)} for e in i.get("explanations", [])],
                           "uncertainty": i.get("uncertainty"), "certainty": CERTAINTY.get(i["certainty"], ""),
                           "external_refs": _refs(db, i.get("external_refs")), "label": COPY["ai"]} for i in o["items"]]}
    if not s["labs"]["results"]:
        return _module("lab_interpretations", "empty", COPY["no_labs"])
    return _analysis_module(db, s, "lab_interpretations", "lab_interpretation", shape)


def _investigations(db, s, ref):
    if not s["investigations"]:
        return _module("investigations", "empty", COPY["no_inv"])
    st, msg, out = _from_record(s["analyses"].get("investigation_analysis"))
    by_id = {i["investigation_id"]: i for i in (out or {}).get("items", [])}
    rows = []
    for inv in sorted(s["investigations"], key=lambda i: i["performed_at"] or "", reverse=True):
        a = by_id.get(inv["id"])
        rows.append({"id": inv["id"], "name": inv["name"], "date": inv["performed_at"], "impression": inv["impression"],
                     "priority_rank": _problem_rank(s, [inv["id"]] + [f["id"] for f in inv["findings"]]),
                     "featured": bool(a and a.get("relation_to_problems")),
                     "findings": [f["text"] for f in inv["findings"]],
                     "analysis": {"significance": a["key_significance"], "relation": a.get("relation_to_problems"),
                                  "compared": a.get("compared_with_previous"), "uncertainty": a.get("uncertainty"),
                                  "label": COPY["ai"]} if a else None})
    if out is None:  # reliable findings still shown
        st, msg = ("loading", COPY["loading"]) if st == "loading" else ("partial", COPY["no_analysis"])
    rows.sort(key=lambda x: (x["priority_rank"], not x["featured"]))
    return _module("investigations", st, msg, {"items": rows})


def _assessment(db, s, ref):
    previous, comparison = _prior_analysis(db, s, "clinical_assessment")
    def shape(o):
        return {"overall_trend": o["overall_trend"], "comparison": comparison,
                "trend_change": "updated" if previous and previous.get("overall_trend") != o["overall_trend"] else None,
                **{k: [{**_stmt(db, x), "change": _change(x, previous.get(k, [])) if previous else None}
                       for x in o.get(k, [])] for k in ("composition", "improving", "still_abnormal", "watch")},
                "uncertainties": o.get("uncertainties", []),
                "uncertainty_changes": ["new" if previous and u not in previous.get("uncertainties", []) else None
                                        for u in o.get("uncertainties", [])]}
    return _analysis_module(db, s, "clinical_assessment", "clinical_assessment", shape)


def _problems(db, s, ref):
    def shape(o):
        return {"items": [{"title": p["title"], "status": p["current_status"], "assessment": p["assessment"],
                           "recent_change": p.get("recent_change"), "uncertainty": p.get("uncertainty"), "attention": p.get("attention"),
                           "certainty": CERTAINTY.get(p["certainty"], ""), "evidence_ids": p["supporting_evidence_ids"],
                           "label": COPY["ai"]} for p in o["problems"]]}
    return _analysis_module(db, s, "problem_list", "problem_list", shape)


def _handover(db, s, ref):
    fields = ("opening", "key_history", "key_findings", "current_status", "major_problems", "today_focus")
    previous, comparison = _prior_analysis(db, s, "handover_summary")
    return _analysis_module(db, s, "handover", "handover_summary",
                            lambda o: {"comparison": comparison, "changes": {
                                k: [_change(x, previous.get(k, [])) if previous else None for x in o.get(k, [])] for k in fields},
                                **{k: [x["text"] for x in o.get(k, [])] for k in fields}})


BUILDERS = {"header": _header, "overview": _overview, "diagnoses": _diagnoses, "today_focus": _today_focus, "tasks": _tasks,
            "lab_trends": _lab_trends, "lab_interpretations": _lab_interpretations, "investigations": _investigations,
            "clinical_assessment": _assessment, "problem_list": _problems, "handover": _handover}


def _knowledge(db, s, ref):
    """Existing patient-linked, fresh, verified evidence only; never search or infer during rendering."""
    record = s['analyses'].get('knowledge_supplement')
    if record:
        known, links = education.eligible(db, s['patient_id'])
        items = [{**item, 'external_refs': _refs(db, item['external_refs'])}
                 for item in (record.get('output') or {}).get('knowledge_items', (record.get('output') or {}).get('items', []))
                 if set(item['evidence_ids']) <= known and all(
                     eid in links and set(item['evidence_ids']) & links[eid] for eid in item['external_refs'])]
        return _module('knowledge', 'ready' if items else 'empty', None if items else COPY['no_knowledge'], {'items': items})
    known = provenance.known_fact_ids(db, s["patient_id"]) | {d["id"] for d in s["diagnoses"]}
    linked = {}
    for request in s["evidence_requests"]:
        if request["status"] in ("answered", "cached") and set(request["relevant_fact_ids"]) & known:
            for eid in request["evidence_ids"]:
                linked.setdefault(eid, []).extend(request["relevant_fact_ids"])
    fresh = {eid for entry in db.all_global("evidence_cache") if evidence.cache_status(entry) == "fresh"
             for eid in entry["evidence_ids"]}
    items = []
    for src in sorted(evidence.items(db, [eid for eid in linked if eid in fresh]), key=lambda x: x["source_tier"])[:3]:
        items.append({"title": src["title"], "claims": src["relevant_claims"][:3],
                      "applicability": src.get("applicability"), "limitations": src.get("limitations"),
                      "evidence_ids": list(dict.fromkeys(linked[src["evidence_id"]])),
                      "external_refs": _refs(db, [src["evidence_id"]])})
    return _module("knowledge", "ready" if items else "empty", None if items else COPY["no_knowledge"], {"items": items})


BUILDERS["knowledge"] = _knowledge


def build_view(db: DB, patient_id: str, *, include_background_labs: bool = False) -> dict:
    s = state.get_state(db, patient_id)
    ref = analysis.index_date(s)
    modules = {}
    for key in ORDER:
        try:
            modules[key] = BUILDERS[key](db, s, ref)
        except Exception:  # one broken module never breaks the page
            modules[key] = _module(key, "error", COPY["error"])
    if not include_background_labs:
        interpreted = {x["test_id"] for x in modules["lab_interpretations"]["data"].get("items", [])}
        series = modules["lab_trends"]["data"].get("series", [])
        shown = [x for x in series if not re.search(r"血型|血小板抗体|不规则抗体", x["name"]) and not (
            x["trend_label"] == "未计算趋势" and x["test_id"] not in interpreted and "危急" not in (x["latest_flag"] or ""))]
        if series and not shown:
            modules["lab_trends"] = _module("lab_trends", "empty", COPY["no_labs"])
        elif series:
            modules["lab_trends"]["data"]["series"] = shown
    return {"view_version": VIEW_VERSION, "patient_id": patient_id, "generated_at": now(), "index_date": ref,
            "order": ORDER, "presentation": PRESENTATION, "copy": COPY, "modules": modules}
