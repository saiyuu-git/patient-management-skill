"""Server-side HTML rendering of the Dashboard View Model. The ONLY input is the view (never Patient State).

Every dynamic value goes through esc(); model/patient text is never interpreted as HTML.
Layout, labels, navigation and styling are fixed here and in static/app.css.
"""
from __future__ import annotations

import json
import re
from html import escape

from .dashboard import ORDER, TITLES

NAV = [("患者概览", "overview"), ("辅助检查", "examinations"), ("今日待办", "tasks"), ("知识补充", "knowledge")]
STATE_CLASS = {"ready": "", "partial": "is-partial", "empty": "is-empty", "loading": "is-loading", "error": "is-error"}
ASPECT = {"basic": "基本情况", "main_problem": "主要问题", "current_status": "当前状态", "recent_change": "近期变化"}
KIND = {"association": "相关", "possible_explanation": "可能解释", "insufficient_evidence": "证据不足"}
TREND = {"improving": "好转", "stable": "稳定", "worsening": "加重", "mixed": "有好有坏", "unclear": "不明确"}
PSTATUS = {"active": "进行中", "improving": "好转", "worsening": "加重", "stable": "稳定", "resolved": "已解决", "unclear": "不明确"}
HANDOVER = [("opening", "患者简况"), ("key_history", "相关病史"), ("current_status", "目前病情"),
            ("major_problems", "主要问题"), ("today_focus", "今日重点")]
LONG = 140  # characters before a text block becomes collapsible
CLINICAL_LABELS = {"critical_high": "危急偏高", "critical_low": "危急偏低", "high": "偏高", "low": "偏低",
                   "normal": "正常", "abnormal": "异常", "unknown": "尚不明确", "uncertain": "尚不确定",
                   "possible": "可能", "likely": "很可能", "insufficient_evidence": "依据不足"}


def _clinical_copy(text):
    """Localize known technical labels in analysis only; preserve source wording and medical abbreviations."""
    return re.sub(r"(?<![A-Za-z0-9_])(?:" + "|".join(CLINICAL_LABELS) + r")(?![A-Za-z0-9_])",
                  lambda m: CLINICAL_LABELS[m.group().lower()], str(text or ""), flags=re.I)


def esc(x) -> str:
    return escape("" if x is None else str(x), quote=True)


def _text(x, cls: str = "") -> str:
    """Plain text paragraph; long text collapses (no hover needed)."""
    t = "" if x is None else str(x)
    if len(t) > LONG:
        return (f'<details class="clamp {cls}"><summary><span class="clamp-preview">{esc(t[:LONG])}…</span>'
                f'<span class="more">展开</span></summary><p>{esc(t)}</p></details>')
    return f'<p class="{cls}">{esc(t)}</p>'


def _badge(text, cls: str = "") -> str:
    return f'<span class="badge {cls}">{esc(text)}</span>' if text else ""


def _change_badge(change):
    return _badge({"new": "新", "updated": "更新"}.get(change), "change")


def _comparison_note(data):
    c = data.get("comparison")
    if not c:
        return ""
    text = (f'较{c["baseline_date"]}{"记录" if c["basis"] == "previous_day" else "分析版本"}比较'
            if c["status"] == "available" else '暂无此前记录可供比较')
    return f'<p class="comparison-note">{esc(text)}</p>'


def _refs(refs: list[dict]) -> str:
    links = []
    for r in refs or []:
        url = str(r.get("url") or "")
        if not url.startswith(("https://", "http://")):
            continue  # never render javascript:/data: links
        links.append(f'<a href="{esc(url)}" target="_blank" rel="noopener noreferrer">{esc(r.get("organization") or r.get("title"))}</a>')
    return f'<div class="refs">来源：{"".join(links)}</div>' if links else ""


def _stmts(items: list[dict], label: str | None = None) -> str:
    if not items:
        return ""
    rows = "".join(f'<li>{_change_badge(i.get("change"))}{_text(_clinical_copy(i.get("text")))}{_badge(i.get("certainty"), "muted")}{_refs(i.get("external_refs"))}</li>'
                   for i in items)
    head = f'<h4>{esc(label)}</h4>' if label else ""
    return f'{head}<ul class="stmts">{rows}</ul>'


def _ai_tag(copy: dict) -> str:
    return _badge(copy.get("ai"), "ai")


# ----- modules -----

def _overview(d, copy):
    out = []
    if d.get("one_liner"):
        out.append(f'<p class="lead">{esc(_clinical_copy(d["one_liner"]))}</p>')
    points = ''.join(f'<div class="kv"><span class="k">{esc(ASPECT.get(p.get("aspect"), ""))}</span>'
                     f'<div class="v">{_text(p.get("text"))}</div></div>' for p in d.get("points", []))
    if points:
        out.append(f'<details class="more-list"><summary>患者概览详情</summary>{points}</details>')
    if d.get("template"):
        out.append('<p class="note">根据已有资料整理</p>')
    return "".join(out)


def _diagnoses(d, copy):
    if not d.get("user") and not d.get("candidates"):
        return ""  # the module's fixed empty message already covers this case
    items = [f'<li class="dx"><span class="lock" aria-label="已锁定">🔒</span><span class="dx-text">{esc(x["text"])}</span>'
             f'{_change_badge(x.get("change"))}{_badge("已解决", "muted") if x.get("status") == "resolved" else ""}</li>' for x in d.get("user", [])]
    user = f'<ol class="list diagnosis-list">{"".join(items)}</ol>'
    out = [_comparison_note(d), user if items else f'<p class="empty-text">{esc(copy["no_dx"])}</p>']
    cands = d.get("candidates", [])
    if cands:
        rows = "".join(f'<li><span class="dx-text">{esc(c["text"])}</span>{_change_badge(c.get("change"))}{_badge(c.get("certainty"), "muted")}</li>' for c in cands)
        out.append(f'<section class="pending-box"><h4>{esc(copy["candidate"])}</h4><ul class="list">{rows}</ul></section>')
    return "".join(out)


def _today_focus(d, copy):
    rows = "".join(f'<li>{esc(i["text"])}</li>' for i in d.get("items", []))
    note = '<p class="note">根据已有资料整理</p>' if d.get("template") else ""
    return f'<ol class="focus">{rows}</ol>{note}' if rows else ""


def _tasks(d, copy):
    out = []
    ex = d.get("explicit", [])
    if ex:
        rows = [
            f'<li class="task st-{esc(t["status"])}"><label class="task-check"><input type="checkbox" data-task="{esc(t["id"])}"'
            f'{" checked" if t["status"] == "已完成" else ""}{" disabled" if t["status"] == "已取消" else ""}>'
            f'{_badge("用户", "source-user")}<span class="task-title">{esc(t["title"])}</span></label>'
            f'<span class="task-meta">{_badge(t["status"], "status") if t["status"] != "已完成" else ""}{_badge(t.get("due_date"), "muted")}'
            f'{_badge(t.get("hint"), "hint")}</span></li>' for t in ex]
        out.append(f'<h4>明确待办 <small>{len(ex)} 项</small></h4><ul class="list tasks">{"".join(rows)}</ul>'
                   '<p class="task-feedback" role="status"></p>')
    ai = d.get("ai_suggestions", [])
    if ai:
        rows = "".join(f'<li>{_badge("AI", "source-ai")}<span class="task-title">{esc(t["title"])}</span>{_text(t.get("rationale"), "sub")}</li>' for t in ai)
        out.append(f'<section class="pending-box"><h4>{esc(copy["ai_suggestion"])}</h4><ul class="list">{rows}</ul></section>')
    return "".join(out)


def _lab_trends(d, copy, selector=True):
    rows = []
    tabs = []
    for i, x in enumerate(d.get("series", [])):
        tabs.append(f'<button type="button" role="tab" data-select-lab="{esc(x["test_id"])}"'
                    f' aria-selected="{"true" if i == 0 else "false"}">{esc(x["name"])}</button>')
        pts = [p for p in x.get("points", []) if p.get("v") is not None]
        ref = x.get("reference") or {}
        flag = x.get("latest_flag")
        flag_cls = "crit" if "危急" in (flag or "") else "flag" if flag and flag != "正常" else "muted"
        last = (x.get("points") or [{}])[-1]
        head = (f'<summary><span class="lab-name">{esc(x["name"])}</span>'
                f'<span class="lab-latest">{esc(x.get("latest"))} <small>{esc(x.get("unit"))}</small></span>'
                f'{_badge(flag, flag_cls)}<span class="lab-trend">{esc(x.get("trend_label"))}</span></summary>')
        ref_txt = f'参考范围 {esc(ref.get("low"))}–{esc(ref.get("high"))}' if ref.get("low") is not None or ref.get("high") is not None else "参考范围 —"
        body = [f'<p class="sub">{esc((last.get("t") or "").replace("T", " "))} · {ref_txt}</p>']
        if x.get("change_line"):
            body.append(f'<p class="sub">{esc(x["change_line"])}</p>')
        if len(pts) >= 2 and x.get("trend_label") != "未计算趋势":
            data = json.dumps({"points": pts, "ref": ref, "unit": x.get("unit")}, ensure_ascii=False)
            body.append(f'<div class="chart" data-chart="{esc(data)}" role="img" aria-label="{esc(x["name"])}趋势图"></div>')
        elif pts or x.get("points"):
            label = "原始结果" if x.get("trend_label") == "未计算趋势" else "单次结果"
            body.append(f'<div class="single"><span>{esc(x.get("latest"))}</span><small>{esc(x.get("unit"))}</small><em>{label}</em></div>')
        table = "".join(f'<tr><td>{esc((p.get("t") or "").replace("T", " "))}</td><td>{esc(p.get("raw"))}</td><td>{esc(p.get("flag"))}</td></tr>'
                        for p in reversed(x.get("points", [])))
        body.append(f'<details class="points"><summary>全部数据点（{len(x.get("points", []))}）</summary>'
                    f'<table><tbody>{table}</tbody></table></details>')
        rows.append(f'<details class="lab" data-series="{esc(x["test_id"])}" open>{head}{"".join(body)}</details>')
    return ((f'<div class="selector" role="tablist" aria-label="检验项目">{"".join(tabs)}</div>' if selector else '')
            + f'<p class="note">标记仅表示超出参考范围，不代表病情好坏。</p>{"".join(rows)}')


def _lab_interp(d, copy, selector=True):
    out = []
    tabs = []
    for n, i in enumerate(d.get("items", [])):
        tabs.append(f'<button type="button" role="tab" data-select-interp="{n}"'
                    f' aria-selected="{"true" if n == 0 else "false"}">{esc(i.get("name"))}</button>')
        ex = "".join(f'<li>{_badge(KIND.get(e.get("kind"), ""), "muted")}{_text(_clinical_copy(e.get("text")))}{_refs(e.get("external_refs"))}</li>'
                     for e in i.get("explanations", []))
        out.append(f'<article class="item interpretation" data-interpretation="{n}"><h4>{esc(i.get("name"))} {_ai_tag(copy)}</h4>'
                   f'<div class="kv"><span class="k">检验结果</span><div class="v">{_text(_clinical_copy(i.get("abnormality")))}</div></div>'
                   f'<div class="kv"><span class="k">变化趋势</span><div class="v">{_text(_clinical_copy(i.get("trend")))}</div></div>'
                   + (f'<div class="kv"><span class="k">临床意义</span><div class="v">{_text(_clinical_copy(i.get("significance")))}</div></div>' if i.get("significance") else "")
                   + (f'<ul class="stmts">{ex}</ul>' if ex else "")
                   + (f'<p class="unc">尚待明确：{esc(_clinical_copy(i["uncertainty"]))}</p>' if i.get("uncertainty") else "")
                   + f'{_refs(i.get("external_refs"))}</article>')
    return (f'<div class="selector" role="tablist" aria-label="检验解读项目">{"".join(tabs)}</div>' if selector else '') + "".join(out)


def _investigations(d, copy, selector=True):
    out = []
    tabs = []
    for n, inv in enumerate(d.get("items", [])):
        tabs.append(f'<button type="button" role="tab" data-select-inv="{n}"'
                    f' aria-selected="{"true" if n == 0 else "false"}">{esc(inv.get("name"))}</button>')
        a = inv.get("analysis")
        finds = "".join(f'<li>{_text(f)}</li>' for f in inv.get("findings", []))
        ana = (f'<div class="ai-box">{_ai_tag(copy)}{_text(a.get("significance"))}'
               + (f'<p class="sub">与当前病情的关系：{esc(a["relation"])}</p>' if a.get("relation") else "")
               + (f'<p class="sub">与既往比较：{esc(a["compared"])}</p>' if a.get("compared") else "")
               + (f'<p class="unc">尚待明确：{esc(a["uncertainty"])}</p>' if a.get("uncertainty") else "") + '</div>') if a else ""
        out.append(f'<article class="item investigation" data-investigation="{n}"><h4>{esc(inv.get("name"))}<small>{esc((inv.get("date") or "")[:16].replace("T", " "))}</small></h4>'
                   + (f'<div class="kv"><span class="k">结论</span><div class="v">{_text(inv.get("impression"))}</div></div>' if inv.get("impression") else "")
                   + (f'<ul class="stmts findings">{finds}</ul>' if finds else "") + ana + '</article>')
    return (f'<div class="selector" role="tablist" aria-label="辅助检查">{"".join(tabs)}</div>' if selector else '') + "".join(out)


def _assessment(d, copy):
    if not d:
        return ""
    fields = [("composition", "主要问题"), ("improving", "正在好转"), ("still_abnormal", "仍需关注"), ("watch", "观察重点")]
    tabs = ''.join(f'<button type="button" role="tab" data-select-assessment="{key}"'
                   f' aria-selected="{"true" if n == 0 else "false"}">{label}</button>'
                   for n, (key, label) in enumerate(fields))
    panels = ''.join(f'<div class="assessment-panel" data-assessment="{key}">{_stmts(d.get(key), label)}</div>'
                     for key, label in fields)
    uncertainty = ('<section class="uncertainty-section"><h4>尚待明确的问题</h4><ul class="stmts">'
                   + ''.join(f'<li>{_change_badge((d.get("uncertainty_changes") or [None] * len(d["uncertainties"]))[n])}{_text(u)}</li>'
                             for n, u in enumerate(d.get("uncertainties", []))) + '</ul></section>') if d.get("uncertainties") else ''
    return (_comparison_note(d) + f'<p class="lead">总体：{esc(TREND.get(d.get("overall_trend"), d.get("overall_trend")))} {_change_badge(d.get("trend_change"))} {_ai_tag(copy)}</p>'
            f'<div class="selector" role="tablist" aria-label="病情分析栏目">{tabs}</div>{panels}{uncertainty}')


def _problems(d, copy):
    out = []
    for i, p in enumerate(d.get("items", []), 1):
        out.append(f'<article class="item problem"><h4><span class="num">{i}</span>{esc(p.get("title"))}{_badge(PSTATUS.get(p.get("status"), ""), "status")}</h4>'
                   + _text(p.get("assessment"))
                   + (f'<p class="sub">近期变化：{esc(p["recent_change"])}</p>' if p.get("recent_change") else "")
                   + (f'<p class="sub">观察重点：{esc(p["attention"])}</p>' if p.get("attention") else "")
                   + (f'<p class="unc">尚待明确：{esc(p["uncertainty"])}</p>' if p.get("uncertainty") else "") + '</article>')
    return f'<div class="ai-head">{_ai_tag(copy)}</div>' + "".join(out) if out else ""


def _handover(d, copy):
    rows = []
    for key, label in HANDOVER:
        texts = list(d.get(key) or [])
        changes = list((d.get("changes") or {}).get(key, [None] * len(texts)))
        if key == "major_problems":
            findings_changes = (d.get("changes") or {}).get("key_findings", [None] * len(d.get("key_findings", [])))
            for n, text in enumerate(d.get("key_findings", [])):
                if text not in texts:
                    texts.append(text)
                    changes.append(findings_changes[n])
        if not texts:
            continue
        row = f'<div class="kv"><span class="k">{esc(label)}</span><div class="v">' + ''.join(
            _change_badge(changes[n]) + _text(t) for n, t in enumerate(texts)) + '</div></div>'
        rows.append(row)
    rows = ''.join(rows)
    note = '<p class="note">根据已有资料整理</p>' if d.get("template") else ""
    return f'{_comparison_note(d)}{rows}{note}<button type="button" class="copy" data-copy>复制汇报文本</button>' if rows else ""


BODY = {"overview": _overview, "diagnoses": _diagnoses, "today_focus": _today_focus, "tasks": _tasks, "lab_trends": _lab_trends,
        "lab_interpretations": _lab_interp, "investigations": _investigations, "clinical_assessment": _assessment,
        "problem_list": _problems, "handover": _handover}


def _knowledge(d, copy):
    return ''.join('<article class="item knowledge-item">' + f'<h4>{esc(item["title"])}</h4>'
                   + '<p class="note">医学知识参考，不等同于患者诊断或医嘱</p>'
                   + (''.join(f'<h4>{label}</h4>' + _text(item[key]) for key, label in (
                       ('why_relevant', '为何值得关注'), ('knowledge', '临床要点'), ('clinical_connection', '联系本例')))
                      + _text('不确定性：' + {'low':'低', 'moderate':'中', 'high':'高'}[item['uncertainty']], 'unc')
                      if 'knowledge' in item else (''.join(f'<h4>{label}</h4>' + _text(item.get(key)) for key, label in (
                       ('what_it_is', '这是什么'), ('why_relevant', '与这位患者的关系'),
                       ('mechanism_or_explanation', '如何理解'), ('uncertainty', '需要区分')) if item.get(key))
                      if 'what_it_is' in item else ''.join(_text(claim) for claim in item["claims"])))
                   + (_text(item.get("applicability"), "sub") if item.get("applicability") else '')
                   + (_text(item.get("limitations"), "unc") if item.get("limitations") else '')
                   + _refs(item["external_refs"]) + '</article>' for item in d.get("items", []))


BODY["knowledge"] = _knowledge


def module_html(m: dict, copy: dict) -> str:
    """One fixed card. State and message come from the view; a render failure is contained to this card."""
    key, st = m["key"], m["state"]
    try:
        body = "" if st in ("loading", "error") else BODY[key](m.get("data") or {}, copy)
    except Exception:
        st, body = "error", ""
        m = {**m, "message": copy.get("error")}
    msg = m.get("message")
    banner = f'<p class="state-msg">{esc(msg)}</p>' if msg else ""
    if st == "loading":
        banner += '<div class="skeleton" aria-hidden="true"></div>'
    return (f'<section class="card {STATE_CLASS.get(st, "")}" id="m-{esc(key)}" data-module="{esc(key)}" data-state="{esc(st)}">'
            f'<h3 class="card-title">{esc(m["title"])}</h3>{banner}{body}</section>')


def header_html(m: dict, modules: dict | None = None) -> str:
    d = m.get("data") or {}
    persist = d.get("persist") or ""
    pcls = {"已更新": "ok", "更新中": "busy", "更新失败": "fail"}.get(persist, "")
    bed = f'<span class="bed">{esc(d.get("bed"))}</span>' if d.get("bed") else ""
    avatar_style = {"男": "male", "女": "female"}.get(d.get("sex"), "unknown")
    return (f'<header class="ph" id="m-header" data-module="header" data-state="{esc(m["state"])}">'
            f'<div class="patient-identity"><span class="avatar avatar-{avatar_style}" aria-hidden="true"></span><div>'
            f'<div class="ph-row"><h1 class="ph-name">{esc(d.get("label"))}</h1>{bed}'
            f'<span class="ph-demo">{esc(d.get("sex"))} · {esc(d.get("age"))}</span></div>'
            f'<div class="ph-row sub"><span>{esc(d.get("admission"))}</span>'
            f'<span>更新 {esc((d.get("updated_at") or "")[5:16].replace("T", " "))}</span>'
            f'<span class="persist {pcls}">{esc(persist)}</span></div></div></div>'
            '</header>')


def fragments(view: dict) -> dict:
    copy = view["copy"]
    modules = view["modules"]
    keys = ("diagnoses", "clinical_assessment", "handover")
    tabs = ''.join(f'<button type="button" role="tab" data-overview-tab="{k}" aria-controls="m-{k}" '
                   f'aria-selected="{"true" if n == 0 else "false"}">{esc(TITLES[k])}</button>' for n, k in enumerate(keys))
    overview = ('<div class="card overview-group" id="m-overview" data-module="overview"><div class="overview-heading">'
                '<h2 class="section-title">患者概览</h2><div class="overview-tabs" role="tablist" aria-label="患者概览栏目">'
                + tabs + '</div></div>' + ''.join(module_html(modules[k], copy).replace(
                    'data-module=', f'data-overview-pane="{k}" role="tabpanel" data-module=', 1) for k in keys) + '</div>')
    return {"fingerprint": view.get("fingerprint"), "header": header_html(view["modules"]["header"], view["modules"]),
            "modules": {"overview": _major_section("overview", "患者概览", overview),
                        "examinations": _major_section("examinations", "辅助检查", examinations_html(modules, copy)),
                        "tasks": _major_section("tasks", "今日待办", module_html(modules["tasks"], copy)),
                        "knowledge": _major_section("knowledge", "知识补充", module_html(modules["knowledge"], copy))}}


def _major_section(key, title, content):
    return (f'<details class="major-section" data-section-root="{key}" open>'
            f'<summary><span class="major-title">{title}</span><span class="collapse-control" aria-hidden="true"><span class="collapse-copy"></span><span class="collapse-chevron"></span></span></summary>'
            f'<div class="major-content">{content}</div></details>')


def examinations_html(modules, copy):
    """One list/detail section; labs include their existing interpretation directly under the chart."""
    interpretations = {i["test_id"]: i for i in modules["lab_interpretations"]["data"].get("items", [])}
    tabs, panels = [], []
    for kind, entries in (("labs", modules["lab_trends"]["data"].get("series", [])),
                          ("investigations", modules["investigations"]["data"].get("items", []))):
        rows, details = [], []
        for item in entries:
            key = f'{kind}-{item["series_id" if kind == "labs" else "id"]}'
            meta = (f'{esc(item.get("latest"))} {esc(item.get("unit"))} · {esc(item.get("trend_label"))}' if kind == "labs"
                    else esc((item.get("date") or "")[:10]))
            row = (f'<button type="button" class="exam-row" data-open-detail="{key}"><span>{esc(item["name"])}'
                   f'<small>{meta}</small></span><span aria-hidden="true">›</span></button>')
            rows.append(row)
            if kind == "labs":
                content = _lab_trends({"series": [item]}, copy, selector=False)
                interp = interpretations.get(item["test_id"])
                content += '<div class="inline-analysis"><h4>临床分析</h4>' + (
                    _lab_interp({"items": [interp]}, copy, selector=False) if interp else
                    f'<p class="state-msg">{esc(copy["no_analysis"])}</p>') + '</div>'
            else:
                content = _investigations({"items": [item]}, copy, selector=False)
                if not item.get("analysis"):
                    content += f'<p class="state-msg">{esc(copy["no_analysis"])}</p>'
            details.append(f'<div class="exam-detail" data-detail="{key}"><button type="button" class="back-list" data-back-list>‹ 返回项目列表</button>{content}</div>')
        message = modules["lab_trends" if kind == "labs" else "investigations"].get("message")
        notice = f'<p class="state-msg">{esc(message)}</p>' if message else ''
        panels.append(f'<div data-exam-kind="{kind}">{notice}<div data-exam-list>{"".join(rows)}</div>{"".join(details)}</div>')
        tabs.append(f'<button type="button" data-exam-mode="{kind}" aria-pressed="{"true" if kind == "labs" else "false"}">{"检验" if kind == "labs" else "检查"}</button>')
    return ('<section class="card" id="m-examinations" data-module="examinations"><div class="exam-heading">'
            '<h3 class="card-title">辅助检查</h3><div class="exam-switch" aria-label="检验或检查">'
            + ''.join(tabs) + '</div></div>' + ''.join(panels) + '</section>')


def page(view: dict) -> str:
    f = fragments(view)
    pid = esc(view["patient_id"])
    cards = "".join(f["modules"].values())
    sidebar = ''.join(f'<a href="#m-{k}" data-nav="{k}" aria-label="{label}" title="{label}"><span class="sidebar-label">{label}</span></a>'
                      for label, k in NAV)
    switch = ('<footer class="view-switch" aria-label="显示版本"><span class="sidebar-label">显示版本</span>'
              '<button type="button" data-display-mode="mobile" aria-pressed="false" title="移动版">移动版</button>'
              '<button type="button" data-display-mode="desktop" aria-pressed="false" title="桌面版">桌面版</button></footer>')
    return (f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
            f'<title>{esc((view["modules"]["header"]["data"] or {}).get("label"))} · 患者管理</title>'
            f'<link rel="stylesheet" href="/static/app.css"></head>'
            f'<body data-patient="{pid}" data-fingerprint="{esc(view.get("fingerprint"))}">'
            '<div class="appbar"><a class="brand" href="#top" data-nav="overview" data-home>'
            '<span class="brand-mark" aria-hidden="true"></span>患者管理助手</a>'
            '<button type="button" class="menu-toggle" aria-controls="sidebar" aria-expanded="false" aria-label="打开导航">☰</button></div>'
            f'<aside class="sidebar" id="sidebar" aria-label="患者导航"><button type="button" class="sidebar-toggle" '
            f'aria-label="收起导航" aria-expanded="true">‹ <span>收起导航</span></button>{sidebar}{switch}</aside>'
            '<button type="button" class="menu-backdrop" aria-label="关闭导航" hidden></button>'
            f'<div class="workspace">{f["header"]}<main id="grid" class="grid">{cards}</main></div>'
            f'<div id="tip" class="chart-tip" aria-live="polite" hidden></div><script src="/static/app.js" defer></script></body></html>')
