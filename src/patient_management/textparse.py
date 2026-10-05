"""Deterministic text processing: chunking, small reliable parsers, negation/uncertainty cues.

Parser first, model second: parsers only accept forms they can read unambiguously; everything else
is left for constrained model extraction. No clinical interpretation here.
"""
from __future__ import annotations

import re

from . import labs
from .provenance import contains, norm

# ----- dates -----

DATE = r"\d{4}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}\s*日?(?:[\sT]*\d{1,2}\s*[:：时]\s*\d{1,2}\s*分?)?"
_DATE_RE = re.compile(DATE)
EXPLICIT_ADMISSION = re.compile(
    rf"入院(?:日期|时间)\s*[:：]?\s*({DATE})|(?:于|在)?\s*({DATE})\s*(?:入院|收入我?院|收住我?院|收治入院)")

# ----- sections / chunking -----

_DOC_HEADINGS = [  # (pattern, source kind) for document-level headings
    (r"入院记录", "admission_record"),
    (r"(?:首次|日常|术后|术前)?病程(?:记录)?", "progress_note"),
    (r"(?:主任|主治|副主任)?(?:医师)?查房(?:记录)?", "ward_round_note"),
    (r"会诊(?:记录|意见)?", "other"),
    (r"出院(?:记录|小结)", "discharge_summary"),
    (r"(?:检验|化验)(?:报告|结果|单)?|实验室检查|.{0,12}(?:检验|化验)报告单", "lab_report"),
    (r"(?:检查|影像)报告|.{0,12}(?:检查|影像|超声|心电图|脑电图)报告单?", "investigation_report"),
    (r"医嘱(?:单)?", "medical_order"),
]
_SUB_HEADINGS = (r"主诉|现病史|既往史|个人史|婚育史|月经史|家族史|体格检查|专科检查|查体|辅助检查|"
                 r"(?:入院|初步|门诊|目前|当前|修正|补充|出院|最后|术前|术后)?诊断|鉴别诊断|诊断依据|"
                 r"(?:诊疗|治疗)?计划|处理|病情分析|手术记录")
_DECOR = re.compile(r"^[\s#>*【\[]+|[\s*】\]:：]+$")


def heading_of(line: str) -> tuple[str, str | None] | None:
    """(label, doc kind or None) if the line is a heading only (no content after it)."""
    raw = line.strip()
    md = raw.startswith("#")
    core = _DECOR.sub("", _DATE_RE.sub(" ", raw)).strip()
    core = re.sub(r"(?:采集|采样|送检|报告|记录)(?:时间|日期)", "", core)
    core = re.sub(r"[\s()（）]+", "", core)
    if not core:
        return None
    for pat, kind in _DOC_HEADINGS:
        if re.fullmatch(pat, core):
            return raw.lstrip("#").strip(), kind
    if re.fullmatch(_SUB_HEADINGS, core) or (md and len(core) <= 30):
        return raw.lstrip("#").strip(), None
    return None


def _lines(text: str) -> list[dict]:
    out, pos = [], 0
    for no, seg in enumerate(text.splitlines(keepends=True), 1):
        body = seg.rstrip("\r\n")
        out.append({"no": no, "start": pos, "end": pos + len(body), "text": body})
        pos += len(seg)
    return out


_ITEM = re.compile(r"^\s*(?:\d+\s*[.、)）]|[（(]\d+[)）]|[①-⑳])")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{3,}")


def chunk(text: str, max_chars: int = 1200, default_kind: str | None = None) -> list[dict]:
    """Split by headings, then pack lines up to max_chars. Never splits inside a run of lab lines,
    a numbered list, or a markdown table. A single overlong line is split at sentence ends."""
    sections, doc_kind = [], default_kind
    for ln in _lines(text):
        h = heading_of(ln["text"])
        if h or not sections:
            kind = h[1] if h else None
            # lab/investigation headings name a whole document only when they come first
            if kind and not (kind in ("lab_report", "investigation_report") and doc_kind):
                doc_kind = kind
            sections.append({"no": len(sections) + 1, "label": h[0] if h else None, "doc_kind": doc_kind, "lines": []})
        sections[-1]["lines"].append(ln)

    chunks = []
    for sec in sections:
        units = []
        for ln in sec["lines"]:
            if ln["end"] - ln["start"] <= max_chars:
                units.append(ln)
                continue
            for m in re.finditer(r"[^。；;]+[。；;]?", ln["text"]):
                units.append({**ln, "start": ln["start"] + m.start(), "end": ln["start"] + m.end(),
                              "text": m.group(), "piece": True})
        cur: list[dict] = []
        for u in units:
            if cur and u["end"] - cur[0]["start"] > max_chars and _breakable(cur[-1], u):
                chunks.append(_make_chunk(text, cur, sec))
                cur = []
            cur.append(u)
        if cur:
            chunks.append(_make_chunk(text, cur, sec))
    chunks = [c for c in chunks if c["text"].strip()]
    for i, c in enumerate(chunks, 1):
        c["chunk_id"] = f"c{i:02d}"
    return chunks


def _atomic(u: dict) -> bool:
    t = u["text"]
    return bool(_ITEM.match(t) or t.lstrip().startswith("|") or parse_lab_line(t))


def _breakable(prev: dict, nxt: dict) -> bool:
    if prev.get("piece") and nxt.get("piece") and prev["no"] == nxt["no"]:
        return True  # sentence pieces of one overlong line
    return not (_atomic(prev) and _atomic(nxt))


def _make_chunk(text: str, units: list[dict], sec: dict) -> dict:
    start, end = units[0]["start"], units[-1]["end"]
    return {"chunk_id": None, "section_no": sec["no"], "section": sec["label"], "doc_kind": sec["doc_kind"],
            "line_start": units[0]["no"], "line_end": units[-1]["no"],
            "char_start": start, "char_end": end, "text": text[start:end]}


# ----- lab lines -----

_NAME = r"[A-Za-z一-鿿][A-Za-z一-鿿\-+#%]*(?:[(（][A-Za-z一-鿿\-+#%]+[)）])?"
_UNIT = r"(?:[×xX*]\s*)?10\s*[\^*]?\s*\d+\s*/\s*L|[A-Za-zμµ%][A-Za-zμµ/%\d^.]*"
_LAB = re.compile(
    rf"(?P<name>{_NAME})\s*[:：]?\s*(?P<preflag>↑↑|↓↓|↑|↓)?\s*(?P<value>[<>≤≥]?\s*\d+(?:\.\d+)?)(?![\d.])\s*(?P<unit>{_UNIT})?"
    rf"(?:\s*(?P<ref>\d+(?:\.\d+)?\s*[-~～—–]+\s*\d+(?:\.\d+)?|[<>≤≥]\s*\d+(?:\.\d+)?))?"
    rf"(?:\s*(?P<flag>↑↑|↓↓|↑|↓|(?<![A-Za-z])[HL](?![A-Za-z])))?")


_NAME_PREFIX = re.compile(r"^(?:复查|复测|急查|查|测|血清|血浆)")


def _known_name(name: str) -> str | None:
    """Known analyte name, allowing only an explicit verb/specimen prefix ('复查肌酐'), never an arbitrary
    one ('前白蛋白' is not '白蛋白'). A parenthesized abbreviation must agree with the name."""
    base, _, abbr = name.partition("(") if "(" in name else name.partition("（")
    abbr = abbr.rstrip(")）")
    stripped = _NAME_PREFIX.sub("", base)
    for cand in dict.fromkeys([base, stripped]):
        tid = labs.test_id_for(cand)
        if tid.startswith("unmapped_"):
            continue
        if abbr and labs.test_id_for(abbr) not in (tid,) and not labs.test_id_for(abbr).startswith("unmapped_"):
            return None  # e.g. 名称 and 缩写 point to different tests
        return name if cand == base else name[len(base) - len(cand):]
    if abbr and base == stripped and not labs.test_id_for(abbr).startswith("unmapped_"):
        return name  # unlisted Chinese spelling, identified by an unambiguous abbreviation
    return None


def parse_lab_line(line: str) -> list[dict]:
    # "13.4910^9/L": PDF text lost the space between value and unit; split only after a decimal value
    work = re.sub(r"(\d+\.\d*?)(10\^\d+/L)", r"\1 \2", line.replace("|", " "))
    out = []
    for m in _LAB.finditer(work):
        name = _known_name(m.group("name"))
        if not name:
            continue
        value = re.sub(r"\s+", "", m.group("value"))
        unit = re.sub(r"\s+", "", m.group("unit") or "") or None
        out.append({"test_name": name, "raw_value": value, "raw_unit": unit,
                    "reference_text": m.group("ref"), "flag_text": m.group("flag") or m.group("preflag"),
                    "span": (m.start(), m.end())})
    return out


# ----- header / admission / diagnosis lists -----

_HEADER_FIELDS = {
    "name": re.compile(r"姓\s*名\s*[:：][ \t]*([^\s，,；;：:]{1,20})(?=[\s，,；;]|$)"),
    "sex": re.compile(r"性\s*别\s*[:：]\s*(男|女)"),
    "age_text": re.compile(r"年\s*龄\s*[:：]\s*(\d+(?:\.\d+)?\s*(?:岁|个月|月龄|月|天|日龄)(?:\s*\d+\s*个?月)?)"),
    "bed": re.compile(r"床\s*号\s*[:：][ \t]*([^\s，,；;：:]{1,10})(?=[\s，,；;]|$)"),
}
_DX_HEAD = re.compile(
    r"^\s*[#*【\[]*\s*((?:入院|初步|门诊|目前|当前|修正|补充|出院|最后|术前|术后|主要|次要|临床)?诊断)"
    r"\s*[】\]*]*\s*(?:[:：]\s*(?P<rest>.*)|$)")
_SAMPLE_TIME = re.compile(rf"(?:采样|采集|送检|采血)\s*时\s*间\s*[:：]?\s*({DATE})")
_LAB_HEADER = re.compile(r"采集|采样|送检|报告|检验|化验|血常规|生化|尿常规|凝血|肝功|肾功|电解质|血气")

# ----- negation / uncertainty -----

NEG_CUES = ["暂未", "未再", "否认", "未见", "未闻及", "未触及", "未引出", "未诉", "未出现", "未发现", "没有", "不伴", "无"]
NEG_POSTFIX = ["(-)", "阴性"]
POS_POSTFIX = ["(+)", "阳性"]
UNC_CUES = ["不能完全排除", "尚不能排除", "难以排除", "待排除", "不能排除", "不能除外", "不除外", "不排除", "未排除", "未除外", "待排", "待除外", "考虑", "可能", "疑似", "疑诊", "可疑", "倾向", "?"]
_NOT_NEGATION = ["无力", "无尿", "无汗", "无痛性", "无菌", "无创", "无法", "无效"]  # symptoms/terms, not negation
_AFFIRM = ["出现", "伴有", "伴", "发生", "继而"]  # "有" excluded: "否认既往有…" stays negated
_CLAUSE = re.compile(r"[。；;！!\n，,]")


def _clause(quote_n: str, start: int, end: int) -> tuple[str, str]:
    """Normalized prefix/suffix of the clause around [start, end)."""
    left = max([m.end() for m in _CLAUSE.finditer(quote_n, 0, start)], default=0)
    right = next((m.start() for m in _CLAUSE.finditer(quote_n, end)), len(quote_n))
    return quote_n[left:start], quote_n[end:right]


_MAX_SCOPE = 15  # chars between a governing negation cue and the term; farther away = not reliable


def assertion_evidence(text: str, quote: str | None) -> tuple[str, bool]:
    """(assertion, reliable) from explicit cues in the text and its clause.

    Reliable: a single clear cue family (or none = plain affirmative statement).
    Not reliable: negation and uncertainty cues mixed, or a negation cue far from the term.
    """
    t = norm(text)
    q = norm(quote) if quote else t
    pos = q.find(t)
    prefix, suffix = _clause(q, pos, pos + len(t)) if pos >= 0 else ("", "")
    span = prefix + t
    masked = span
    for word in _NOT_NEGATION:
        masked = masked.replace(word, "#" * len(word))
    last, cue = max(((masked.rfind(c), c) for c in NEG_CUES if c in masked), default=(-1, ""))
    # an affirm verb after the cue ends its scope ("无明显诱因出现发热"); one right after it does not ("否认有")
    neg = last >= 0 and not any(a in masked[last + len(cue) + 1:] for a in _AFFIRM)
    tail = t[-4:] + suffix[:4]  # "征(-)" inside the text or right after it
    postfix_neg = any(c in tail for c in NEG_POSTFIX) and not any(c in tail for c in POS_POSTFIX)
    unc = any(c in span + suffix for c in UNC_CUES)
    if unc and (neg or postfix_neg):
        return "uncertain", False
    if unc:
        return "uncertain", True
    if neg:
        return "absent", max(0, len(prefix) - (last + len(cue))) <= _MAX_SCOPE
    if postfix_neg:
        return "absent", True
    return "present", True


def expected_assertion(text: str, quote: str | None) -> str:
    value, reliable = assertion_evidence(text, quote)
    return value if reliable else "unknown"


def hedged_diagnosis(text: str, quote: str | None) -> bool:
    """Uncertainty cue in the diagnosis text or immediately around it in the quote."""
    t = norm(text)
    if any(c in t for c in UNC_CUES):
        return True
    q = norm(quote) if quote else t
    pos = q.find(t)
    if pos < 0:
        return False
    before, after = q[max(0, pos - 4):pos], q[pos + len(t):pos + len(t) + 4]
    return any(c in before or c in after for c in UNC_CUES)


_NOT_DX = re.compile(r"^(?:同贵科|同上|同前|同意|详见|见上|见前|待定|暂无|无$)")


def split_diagnoses(rest: str) -> list[str] | None:
    """Top-level items of a (joined) diagnosis block, verbatim without their numbering.

    The first item's marker style decides the top level ("1." vs "1）"), so nested sub-items stay inside
    their parent. None when boundaries are ambiguous (commas only, no numbering or semicolons).
    """
    rest = rest.strip()
    m = re.match(r"(\d+)\s*([.、．)）])", rest)
    if m:
        marker = "[.、．]" if m.group(2) in ".、．" else "[)）]"
        parts = re.split(rf"(?:^|(?<=[\s；;。]))\d+\s*{marker}", rest)
    elif re.search(r"[；;]", rest):
        parts = re.split(r"[；;]", rest)
    elif re.search(r"[，,、]", rest):
        return None
    else:
        parts = [rest]
    items = [p.strip().rstrip("。；;，,").strip() for p in parts]
    return [i for i in items if i and not _NOT_DX.match(i)]


_LABEL_LINE = re.compile(r"^[一-鿿 ]{2,8}[:：]")  # "医师签名：", "日期：" end a diagnosis block


def _dx_block(lines: list[dict], i: int, first: str) -> tuple[str, int]:
    """Join a diagnosis list wrapped over several PDF lines. Returns (text, index of last line used)."""
    parts, j = ([first] if first.strip() else []), i + 1
    while j < len(lines):
        t = lines[j]["text"].strip()
        if not t or heading_of(lines[j]["text"]) or _LABEL_LINE.match(t) or _DX_HEAD.match(t):
            break
        if re.match(r'^(?:患者|病人|查体|主诉|现病史|既往史|计划|治疗)', t) or parse_lab_line(t):
            break  # clinical narrative or results are not wrapped diagnosis text
        if re.fullmatch(r"\d{1,3}(?:\s*/\s*\d{1,3})?", t):  # page number/footer between wrapped lines
            j += 1
            continue
        if parts and parts[-1].rstrip().endswith("。") and not _ITEM.match(t):
            break
        parts.append(("\n" if _ITEM.match(t) else "") + t)  # new item vs. wrapped continuation
        j += 1
    return "".join(parts), j - 1


# ----- deterministic extraction over a whole source -----

def _blank(t: str, spans: list[tuple[int, int]]) -> str:
    chars = list(t)
    for a, b in spans:
        chars[a:b] = " " * (b - a)
    return "".join(chars)


def deterministic(text: str, chunks: list[dict], document_date: str | None) -> dict:
    """Parse what is unambiguous. Returns {"extraction", "covered_lines", "lab_lines"}.

    covered_lines: lines fully consumed (or headings/blank/date-only) -> no model task needed for them.
    lab_lines: lines whose labs were captured -> model must not extract labs from them again.
    """
    ex = {"demographics": None, "stated_admission_date": None, "stated_diagnoses": [], "facts": [], "labs": []}
    covered, lab_lines = set(), set()
    lines = _lines(text)
    section_of = {no: c["section_no"] for c in chunks for no in range(c["line_start"], c["line_end"] + 1)}
    kind_of = {no: c["doc_kind"] for c in chunks for no in range(c["line_start"], c["line_end"] + 1)}
    block_time, section, consumed = None, object(), set()
    demo, demo_lines = {}, []
    for i, ln in enumerate(lines):
        t, no = ln["text"], ln["no"]
        if section_of.get(no, section) != section:
            block_time, section = None, section_of.get(no)
        stripped = t.strip()
        if not stripped or no in consumed:
            covered.add(no)
            continue
        date_m = _DATE_RE.search(t)
        if heading_of(t):
            covered.add(no)
            if date_m and (_LAB_HEADER.search(t) or kind_of.get(no) == "lab_report"):
                block_time = date_m.group()
            # a "诊断" heading inside a lab/investigation report is the report's conclusion, not a diagnosis list
            if _DX_HEAD.match(t) and kind_of.get(no) not in ("lab_report", "investigation_report"):
                block, last = _dx_block(lines, i, "")
                _add_dx(ex, block)
                consumed.update(range(no + 1, lines[last]["no"] + 1))
            continue
        spans: list[tuple[int, int]] = []

        found = {f: rx.search(t) for f, rx in _HEADER_FIELDS.items()}
        found = {f: m for f, m in found.items() if m}
        if found:
            for f, m in found.items():
                demo.setdefault(f, m.group(1))
                spans.append(m.span())
            demo_lines.append(ln)

        m = EXPLICIT_ADMISSION.search(t)
        if m and not ex["stated_admission_date"]:
            ex["stated_admission_date"] = {"date": m.group(1) or m.group(2), "quote": stripped}
            spans.append(m.span())

        dxm = _DX_HEAD.match(t)
        if dxm and dxm.group(1) == "临床诊断":
            dxm = None  # a form header field (requisitions, consult requests, reports), not a diagnosis list
        if dxm and kind_of.get(no) not in ("lab_report", "investigation_report") and dxm.group("rest"):
            # ("临床诊断" on a lab/investigation report header is a requisition field, not a diagnosis list)
            block, last = _dx_block(lines, i, dxm.group("rest"))
            if _add_dx(ex, block):
                spans.append((0, len(t)))
                consumed.update(range(no + 1, lines[last]["no"] + 1))

        sample = _SAMPLE_TIME.search(t)
        if sample:  # explicit collection-time label, e.g. a report header line with other fields
            block_time = sample.group(1)
        found_labs = parse_lab_line(t)
        if date_m and not found_labs and _residual_empty(t, date_m):
            block_time = date_m.group()  # date-only / lab-block header line
            covered.add(no)
            continue
        dates_before = [(m.end(), m.group()) for m in _DATE_RE.finditer(t)]
        for lab in found_labs:
            # the time of a lab is the last explicit time written before it on the line ("…ESR 90mm/h。<next time> 凝血…")
            prior = [d for end, d in dates_before if end <= lab["span"][0]]
            when = prior[-1] if prior else block_time
            if not prior and len(dates_before) == 1:
                when = dates_before[0][1]  # one explicit timestamp may follow the result
            if when is None and document_date is None:
                continue  # no collection time: leave the line to the model, never guess
            ex["labs"].append({**{k: v for k, v in lab.items() if k != "span"},
                               "collected_at_text": when, "quote": stripped})
            spans.append(lab["span"])
            lab_lines.add(no)

        if _TABLE_SEP.match(t) or (i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]["text"])):
            covered.add(no)
            continue
        residual = _DATE_RE.sub("", _blank(t.replace("|", " "), spans))
        if not re.search(r"[一-鿿A-Za-z]", residual):
            covered.add(no)

    if demo:
        first, last = demo_lines[0], demo_lines[-1]
        quote = text[first["start"]:last["end"]].strip()
        if len(quote) > 400:
            quote = first["text"].strip()
        ex["demographics"] = {"name": demo.get("name"), "alias": None, "bed": demo.get("bed"),
                              "sex": demo.get("sex"), "age_text": demo.get("age_text"), "quote": quote}
    return {"extraction": ex, "covered_lines": covered, "lab_lines": lab_lines}


def _add_dx(ex: dict, block: str) -> bool:
    """Diagnosis items -> stated diagnoses, hedged items -> uncertain impressions. False if ambiguous."""
    items = split_diagnoses(block)
    if items is None:
        return False
    for item in items:
        quote = item  # located whitespace-insensitively, so items wrapped across lines still anchor
        if hedged_diagnosis(item, block):
            ex["facts"].append({"kind": "impression", "text": item, "assertion": "uncertain", "quote": quote})
        else:
            ex["stated_diagnoses"].append({"text": item, "quote": quote})
    return True


def _residual_empty(line: str, date_match) -> bool:
    rest = line[:date_match.start()] + line[date_match.end():]
    rest = _LAB_HEADER.sub("", rest)
    rest = re.sub(r"时间|日期|结果|[\s:：()（）\-|]", "", rest)
    return len(re.findall(r"[一-鿿A-Za-z]", rest)) <= 2


def detect_kind(text: str) -> str:
    for ln in _lines(text):
        h = heading_of(ln["text"])
        if h and h[1]:
            return h[1]
        if ln["text"].strip():
            break
    return "other"
