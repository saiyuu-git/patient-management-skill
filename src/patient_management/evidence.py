"""External medical evidence: request -> host-agent web search -> validated bundle -> global cache.

The engine never searches. It decides WHETHER a search is needed (cache, dedup), WHAT to search
(code-built question and queries from templates), and validates WHAT comes back. External evidence
(ext_*) lives in global tables and never enters patient facts; patients only hold evidence_requests.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from string import Template
from urllib.parse import urlsplit

from . import provenance, schema, state
from .extraction import parse_output
from .persistence import DB, PMError, now

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = json.loads((ROOT / "knowledge" / "source-registry.json").read_text(encoding="utf-8"))
TEMPLATES = json.loads((ROOT / "knowledge" / "query-templates.json").read_text(encoding="utf-8"))
PROMPT = Path(__file__).with_name("prompts") / "evidence_search.txt"

QUESTIONS = {  # code-built, narrow clinical questions
    "lab_interpretation": "What clinical contexts can explain {dir}{concept}?",
    "trend_significance": "What is the clinical significance of a {dir}trend in {concept}?",
    "investigation_interpretation": "Which conditions are associated with the finding: {concept}?",
    "candidate_diagnosis": "Which conditions should be considered for {concept}, and what supports them?",
    "differential_diagnosis": "How is {concept} distinguished from conditions with similar presentation?",
    "red_flag": "Which red flags or danger signs are associated with {concept}?",
    "guideline_standard": "What do current guidelines state about the diagnosis and assessment of {concept}?",
}
DIR_EN = {"elevated": "elevated ", "low": "low ", "rising": "rising ", "falling": "falling "}
TIER = {"official_guideline": 1, "government_health_agency": 1, "professional_society_statement": 1,
        "consensus_statement": 1, "systematic_review": 2, "peer_reviewed_review": 2, "original_research": 3,
        "institutional_education": 4}
DEFAULT_SOURCES = {"guideline_standard": ["official_guideline", "professional_society_statement", "consensus_statement"]}
FALLBACK_SOURCES = ["official_guideline", "systematic_review", "peer_reviewed_review"]
TTL_DAYS = {"guideline_standard": 180}  # ponytail: one TTL per question type; refine only if real use shows a need
DEFAULT_TTL_DAYS = 365
DEDUP_HOURS = 24
PENDING_TTL_HOURS = 72  # unanswered requests expire instead of staying pending forever
MAX_ATTEMPTS = 2
_LATEST = re.compile(r"latest|current|newest|up[- ]to[- ]date|最新|当前|现行|目前.*(指南|标准|共识)", re.I)
_PATIENT_WORDS = re.compile(r"该患者|本患者|此患者|患者本人|this patient|the patient'?s|our patient", re.I)
_IDS = re.compile(r"\b(?:fact|dx|task|src|pt|evq)_[a-z0-9_]+\b")
_DATES = re.compile(r"\d{4}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}|(?<!\d)(?:19|20)\d{6}(?!\d)|\d{1,2}\s*月\s*\d{1,2}\s*日")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def _norm(s: str) -> str:
    return re.sub(r"[\s\W_]+", " ", provenance.norm(s)).strip()


def _parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _now_dt() -> datetime:
    return _parse_dt(now())


# ----- request -----

def topic_key(qtype: str, concept: str, direction: str | None, qualifiers: list[str]) -> str:
    return "|".join([qtype, direction or "", _norm(concept), "+".join(sorted(_norm(q) for q in qualifiers))])


def _bigrams(s: str) -> set[str]:
    s = s.replace(" ", "")
    return {s[i:i + 2] for i in range(len(s) - 1)} or {s}


def _similar_key(a: str, b: str) -> bool:
    """Same type/direction/qualifiers and near-identical concept (e.g. 'serum creatinine' vs 'serum creatinine level')."""
    pa, pb = a.split("|"), b.split("|")
    if pa[0] != pb[0] or pa[1] != pb[1] or pa[3] != pb[3]:
        return False
    ga, gb = _bigrams(pa[2]), _bigrams(pb[2])
    return len(ga & gb) / len(ga | gb) >= 0.85


def _fact_lines(db: DB, patient_id: str) -> dict[str, str]:
    """fact_id -> compact, identifier-free text for applicability context."""
    s = state.get_state(db, patient_id)
    out = {f["id"]: f"{f['text']} ({f.get('assertion', 'present')})" for f in s["facts"]}
    for r in s["labs"]["results"]:
        out[r["id"]] = f"{r['test_name']} {r['raw_value']} {r['raw_unit'] or ''} {r['collected_at'][:10]} {r['abnormal_flag']}".strip()
    for x in s["labs"]["series"]:
        if x["data_status"] == "sufficient":
            out[x["id"]] = (f"{x['test_name']}: {x['pattern']}, first {x['first_value']} -> latest {x['latest_value']} "
                            f"{x['unit'] or ''}; reference {x['reference_status_transition']['first']} -> "
                            f"{x['reference_status_transition']['latest']}")
        else:
            out[x["id"]] = f"{x['test_name']}: {x['data_status']}"
    for inv in s["investigations"]:
        out[inv["id"]] = f"{inv['name']}: {inv['impression'] or ''}".strip()
        out.update({f["id"]: f"{inv['name']} finding: {f['text']}" for f in inv["findings"]})
    return {k: v[:160] for k, v in out.items()}


def _identifiers(db: DB, patient_id: str) -> list[str]:
    h = db.get_patient(patient_id)["header"]
    return [v for v in (h.get("display_name"), h.get("alias"), h.get("bed"), patient_id) if v and len(v) >= 2]


def _patient_numbers(db: DB, patient_id: str) -> set[str]:
    nums = set()
    for r in db.all("lab_results", patient_id):
        nums |= set(_NUMBER.findall(r["raw_value"]))
        if r["normalized_value"] is not None:
            nums.add(f"{r['normalized_value']:g}")
    for f in db.all("facts", patient_id):
        nums |= set(_NUMBER.findall(f["text"]))
    return {n for n in nums if len(n.replace(".", "")) >= 2}


def _privacy_problem(text: str, idents: list[str], patient_nums: set[str] = frozenset(),
                     allow_threshold: bool = False) -> str | None:
    """Search terms carry medical concepts only: no identifiers, ids, dates, record numbers or patient values."""
    if any(provenance.contains(text, i) for i in idents):
        return "contains a patient identifier"
    if _IDS.search(text) or _DATES.search(text):
        return "contains record ids or dates"
    nums = [n for n in _NUMBER.findall(text) if len(n.replace(".", "")) >= 2 or "." in n]
    if any(n in patient_nums for n in nums):
        return "contains a patient-specific value"
    if nums and not allow_threshold:
        return "contains numbers (set allow_threshold only for a guideline threshold)"
    return None


def build_request(db: DB, patient_id: str, spec: dict) -> dict:
    errs = schema.validate(spec, "evidence.schema.json", "/$defs/request_input")
    if errs:
        raise PMError(f"invalid evidence request: {errs[0]}")
    qtype, concept, qualifiers = spec["question_type"], spec["concept"].strip(), spec.get("qualifiers") or []
    idents, pnums = _identifiers(db, patient_id), _patient_numbers(db, patient_id)
    for field in [concept, spec.get("concept_en") or "", *qualifiers]:
        problem = _privacy_problem(field, idents, pnums, spec.get("allow_threshold", False))
        if problem:
            raise PMError(f"search terms must not carry patient data: '{field}' {problem}")
    if qtype in ("candidate_diagnosis", "differential_diagnosis"):
        user_dx = [d["display_text"] for d in db.all("diagnoses", patient_id) if d["status"] == "active"]
        if any(_norm(d) == _norm(concept) for d in user_dx):
            raise PMError("concept is already a user-provided diagnosis; no search needed to establish it")
    lines = _fact_lines(db, patient_id)
    unknown = [f for f in spec["relevant_fact_ids"] if f not in lines]
    if unknown:
        raise PMError(f"unknown patient fact ids: {unknown}")
    direction = spec.get("direction")
    english = spec.get("concept_en") or (concept if not re.search(r"[一-鿿]", concept) else None)
    recency = spec.get("recency") or "any"
    if any(_LATEST.search(t) for t in [concept, *qualifiers]):
        recency = "latest"  # "latest/current guideline" always searches again
    header = db.get_patient(patient_id)["header"]
    age = header.get("age")
    queries = build_queries(qtype, concept, english, direction, qualifiers, recency)
    for q in queries:  # defense in depth on the assembled queries (the year suffix is the only allowed date-like token)
        problem = _privacy_problem(re.sub(r"\b20\d\d\b", "", q), idents, pnums, True)
        if problem:
            raise PMError(f"assembled query rejected: {problem}")
    return {
        "patient_id": patient_id,
        "clinical_question": QUESTIONS[qtype].format(dir=DIR_EN.get(direction, ""), concept=english or concept)
        + (f" Context: {'; '.join(qualifiers)}." if qualifiers else ""),
        "question_type": qtype, "concept": concept,
        "patient_context": {"age_years": age["value"] if age and age["unit"] == "year" else None, "sex": header["sex"],
                            "relevant_facts": [{"fact_id": f, "text": lines[f]} for f in spec["relevant_fact_ids"]]},
        "relevant_fact_ids": list(dict.fromkeys(spec["relevant_fact_ids"])),
        "preferred_source_types": spec.get("preferred_source_types") or DEFAULT_SOURCES.get(qtype, FALLBACK_SOURCES),
        "recency_requirement": recency, "max_sources": spec.get("max_sources", 3),
        "queries": queries,
        "topic_key": topic_key(qtype, english or concept, direction, qualifiers),
    }


def build_queries(qtype: str, concept: str, english: str | None, direction: str | None, qualifiers: list[str],
                  recency: str) -> list[str]:
    """Fixed templates; the model never writes free-form queries."""
    out = []
    langs = (["zh"] if re.search(r"[一-鿿]", concept) else []) + (["en"] if english else [])
    for lang in langs:
        term = concept if lang == "zh" else english
        dir_word = TEMPLATES["direction_words"][lang].get(direction, "")
        for tpl in TEMPLATES[lang][qtype]:
            q = tpl.format(concept=term, dir=dir_word)
            if qualifiers:
                q += " " + " ".join(qualifiers[:2])
            if recency == "latest":
                q += " " + TEMPLATES["latest_suffix"][lang].format(year=now()[:4])
            out.append(re.sub(r"\s+", " ", q).strip()[:120])
    return list(dict.fromkeys(out))[:TEMPLATES["max_queries"]]


# ----- cache -----

def cache_status(entry: dict) -> str:
    if entry["invalidated"]:
        return "invalidated"
    return "fresh" if _now_dt() < _parse_dt(entry["expires_at"]) else "stale"


def _cache_lookup(db: DB, key: str) -> dict | None:
    exact = db.get_global("evidence_cache", key)
    if exact:
        return exact
    qtype = key.split("|")[0]
    return next((e for e in db.all_global("evidence_cache", question_type=qtype) if _similar_key(key, e["topic_key"])), None)


def invalidate(db: DB, key: str) -> dict:
    with db.tx():
        entry = db.get_global("evidence_cache", key)
        if not entry:
            raise PMError(f"no cache entry {key}")
        entry["invalidated"] = True
        return db.put_global("evidence_cache", key, entry, question_type=entry["question_type"])


def cache_list(db: DB) -> list[dict]:
    return [{**{k: e[k] for k in ("topic_key", "question_type", "retrieved_at", "expires_at", "usage_count")},
             "status": cache_status(e), "sources": len(e["evidence_ids"])} for e in db.all_global("evidence_cache")]


# ----- prepare -----

def _external_status(status: str, evidence_ids: list[str]) -> str:
    if status in ("answered", "cached"):
        return "available" if evidence_ids else "none_found"
    return {"pending": "pending", "repair_needed": "pending", "unavailable": "unavailable",
            "expired": "unavailable"}.get(status, "none_found")


def _save_request(db: DB, patient_id: str, rec: dict) -> dict:
    rec["external_evidence_status"] = _external_status(rec["status"], rec["evidence_ids"])
    rec["updated_at"] = now()
    return db.put("evidence_requests", patient_id, rec)


def expire_pending(db: DB, patient_id: str) -> list[str]:
    """Pending requests older than PENDING_TTL_HOURS become 'expired'. Caller holds the transaction."""
    out = []
    for r in db.all("evidence_requests", patient_id):
        if r["status"] in ("pending", "repair_needed") and \
                _now_dt() - _parse_dt(r["updated_at"]) > timedelta(hours=PENDING_TTL_HOURS):
            r["status"] = "expired"
            _save_request(db, patient_id, r)
            out.append(r["request_id"])
    return out


def prepare(db: DB, patient_id: str, spec: dict, out_dir: str | Path | None = None) -> dict:
    """Returns {'status': cached|duplicate|search_needed, ...}. Only search_needed produces a package."""
    with state.patient_tx(db, patient_id):
        expire_pending(db, patient_id)
        req = build_request(db, patient_id, spec)
        key, force = req["topic_key"], spec.get("force_refresh") or req["recency_requirement"] == "latest"
        for old in db.all("evidence_requests", patient_id, topic_key=key):
            if old["status"] in ("pending", "repair_needed"):
                return {"status": "duplicate", "request_id": old["request_id"], "reason": "same request already pending",
                        "package": old.get("package_path")}
            recent = _now_dt() - _parse_dt(old["updated_at"]) < timedelta(hours=DEDUP_HOURS)
            if not force and recent and old["status"] in ("answered", "cached"):
                return {"status": "duplicate", "request_id": old["request_id"], "reason": f"answered within {DEDUP_HOURS}h",
                        "evidence_ids": old["evidence_ids"]}
        rid = db.next_id(patient_id, "evq")
        req.update(request_id=rid, generated_at=now())
        rec = {"request_id": rid, "question_type": req["question_type"], "clinical_question": req["clinical_question"],
               "topic_key": key, "status": "pending", "evidence_ids": [], "relevant_fact_ids": req["relevant_fact_ids"],
               "created_at": req["generated_at"], "attempts": 0, "request": req}
        hit = None if force else _cache_lookup(db, key)
        if hit and not items(db, hit["evidence_ids"]):
            hit = None  # cache built from unverified sources is never reused
        if hit and cache_status(hit) == "fresh":
            hit.update(usage_count=hit["usage_count"] + 1, last_used_at=now())
            db.put_global("evidence_cache", hit["topic_key"], hit, question_type=hit["question_type"])
            rec.update(status="cached", evidence_ids=[x["evidence_id"] for x in items(db, hit["evidence_ids"])],
                       cache_key=hit["topic_key"])
            _save_request(db, patient_id, rec)
            return {"status": "cached", "request_id": rid, "cache_key": hit["topic_key"], "evidence_ids": rec["evidence_ids"],
                    "external_evidence_status": rec["external_evidence_status"]}
        reason = "forced refresh" if force else (f"cache {cache_status(hit)}" if hit else "cache miss")
        _save_request(db, patient_id, rec)
    path = _write_package(db, patient_id, rec, out_dir)
    with db.tx():
        rec["package_path"] = str(path)
        db.put("evidence_requests", patient_id, rec)
    return {"status": "search_needed", "request_id": rid, "reason": reason, "package": str(path),
            "queries": req["queries"], "external_evidence_status": "pending"}


def build_package(rec: dict) -> dict:
    req = rec["request"]
    repair = ""
    if rec["status"] == "repair_needed":
        repair = ("\nPREVIOUS SUBMISSION WAS REJECTED. Fix these problems and submit the complete JSON again:\n"
                  + "\n".join(f"- [{e['code']}] {e['item']}: {e['reason']}" for e in rec.get("errors", [])[:20]) + "\n")
    hints = ", ".join(o["name"] for o in REGISTRY["organizations"])
    prompt = Template(PROMPT.read_text(encoding="utf-8")).substitute(
        request_id=req["request_id"], question=req["clinical_question"], max_sources=req["max_sources"],
        recency=req["recency_requirement"], queries="\n".join(f"- {q}" for q in req["queries"]),
        preferred=", ".join(req["preferred_source_types"]), hints=hints,
        disallowed=", ".join(REGISTRY["disallowed_domains"]),
        context=json.dumps(req["patient_context"], ensure_ascii=False), repair=repair)
    return {"package_type": "pm.evidence_request", "package_version": 1, "task_type": "evidence_search",
            "request": req, "attempt": rec["attempts"] + 1, "max_attempts": MAX_ATTEMPTS,
            "output_schema": schema.inline("evidence.schema.json", "/$defs/evidence_bundle"),
            "instructions": prompt, "submit_with": "pm evidence submit <this package file> <bundle file>"}


def _write_package(db: DB, patient_id: str, rec: dict, out_dir) -> Path:
    if out_dir:
        folder = Path(out_dir)
    elif db.path != ":memory:":
        folder = Path(db.path).parent / "work" / patient_id / "evidence"
    else:
        raise PMError("out_dir is required with an in-memory database")
    folder.mkdir(parents=True, exist_ok=True)
    pkg = build_package(rec)
    path = folder / f"{patient_id}.{rec['request_id']}.attempt{pkg['attempt']}.json"
    path.write_text(json.dumps(pkg, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


# ----- validation -----

def _domain(url: str) -> str | None:
    if not isinstance(url, str) or re.search(r"\s", url) or len(url) > 500:
        return None
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or "." not in host or host.startswith(".") or host.endswith("."):
        return None
    return host[4:] if host.startswith("www.") else host


def _matches(domain: str, listed: str) -> bool:
    return domain == listed or domain.endswith("." + listed)


def _in_registry(domain: str) -> bool:
    return any(_matches(domain, d) for org in REGISTRY["organizations"] for d in org["domains"])


def _url_key(url: str) -> str:
    """host + path, ignoring scheme, www., query and trailing slash."""
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    return (host[4:] if host.startswith("www.") else host) + p.path.rstrip("/")


def is_verified(item: dict | None) -> bool:
    """Only content-verified sources are evidence. Legacy items without the flag count as unverified."""
    return bool(item) and item.get("verification") == "page_accessed"


def validate_source(src: dict, idents: list[str], metadata: list[dict] | None) -> tuple[dict | None, list[dict]]:
    """Returns (evidence_item without id/first_seen | None, problems)."""
    probs = []
    reject = lambda code, why: (None, probs + [{"code": code, "reason": why}])  # noqa: E731
    if src.get("verification") != "page_accessed":  # hard rule: a snippet is source discovery, never evidence
        return reject("E16", "content not verified (snippet_only or unstated): kept as unverified_candidate only")
    errs = schema.validate(src, "evidence.schema.json", "/$defs/source")
    for e in errs:
        if "url" in e:
            return reject("E03", e)
        if "title" in e:
            return reject("E04", e)
        if "publication_date" in e or "accessed_at" in e:
            return reject("E05", e)
        if "source_type" in e:
            return reject("E06", f"unsupported source_type: {e}")
        if "relevant_claims" in e:
            return reject("E08", e)
    if errs:
        return reject("E01", errs[0])
    domain = _domain(src["url"])
    if not domain:
        return reject("E03", f"invalid URL: {src['url']}")
    if any(_matches(domain, d) for d in REGISTRY["disallowed_domains"]):
        return reject("E07", f"{domain} is not acceptable medical evidence (social/forum/commercial/encyclopedia)")
    texts = [c["claim"] for c in src["relevant_claims"]] + [src.get("applicability") or "", src.get("limitations") or ""]
    for t in texts:  # evidence may contain thresholds and durations, but never this patient
        if any(provenance.contains(t, i) for i in idents) or _IDS.search(t) or _PATIENT_WORDS.search(t):
            return reject("E09", "patient-specific content inside external evidence")
    verified = False
    if metadata:
        hits = [m for m in metadata if _domain(m.get("url", "")) and _url_key(m["url"]) == _url_key(src["url"])]
        if not hits:
            return reject("E12", "URL not present in the host's search results")
        title = hits[0].get("title")
        if title and len(_bigrams(_norm(title)) & _bigrams(_norm(src["title"]))) / max(1, len(_bigrams(_norm(src["title"])))) < 0.5:
            return reject("E12", "title does not match the search result")
        verified = True
    in_registry = _in_registry(domain)
    tier = TIER[src["source_type"]]  # tier follows the source type; the registry only marks tier_basis
    if src.get("source_tier") not in (None, tier):
        probs.append({"code": "E13", "reason": f"declared tier {src['source_tier']} replaced by {tier}"})
    return {"title": src["title"].strip(), "organization": src.get("organization"), "authors": src.get("authors"),
            "source_type": src["source_type"], "source_tier": tier, "tier_basis": "registry" if in_registry else "declared",
            "publication_date": src.get("publication_date"), "accessed_at": src["accessed_at"], "url": src["url"],
            "domain": domain, "relevant_claims": [c["claim"].strip() for c in src["relevant_claims"]],
            "applicability": src.get("applicability"), "limitations": src.get("limitations"),
            "verified_against_search_metadata": verified, "verification": "page_accessed"}, probs


# ----- submit -----

def submit(db: DB, package: dict, raw: str, out_dir: str | Path | None = None) -> dict:
    req = package.get("request") or {}
    rid, patient_id = req.get("request_id"), req.get("patient_id")
    if not rid or not patient_id:
        raise PMError("not an evidence request package")
    with state.patient_tx(db, patient_id):
        expire_pending(db, patient_id)
        rec = db.get("evidence_requests", patient_id, rid)
        if rec is None:
            raise PMError(f"no evidence request {rid} for {patient_id}")
        if rec["status"] == "expired":
            return {"status": "expired", "request_id": rid, "display": "暂无可靠外部医学依据",
                    "reason": f"not submitted within {PENDING_TTL_HOURS}h; prepare a new request"}
        if rec["status"] not in ("pending", "repair_needed"):
            return {"status": "already_done", "request_id": rid, "request_status": rec["status"]}
        rec["attempts"] += 1
        obj, notes, err = parse_output(raw)
        fatal, problems, items = [], [], []
        if obj is None:
            fatal = [{"item": "$", "code": "E01", "reason": err}]
        else:
            extra = [k for k in obj if k not in ("module", "request_id", "search_status", "search_metadata", "sources")]
            if extra:  # e.g. "summary"/"answer": medical conclusions without a source are discarded
                problems.append({"item": "$", "code": "E10", "reason": f"unsourced fields ignored: {extra}"})
            obj.setdefault("module", "evidence_bundle")
            if obj.get("request_id") != rid or obj["module"] != "evidence_bundle":
                fatal = [{"item": "request_id", "code": "E02", "reason": f"bundle is not for {rid}"}]
            elif obj.get("search_status") == "unavailable":
                rec.update(status="unavailable")
            elif obj.get("search_status") == "no_results" or (obj.get("search_status") == "ok" and not obj.get("sources")):
                rec.update(status="no_results")
            elif obj.get("search_status") != "ok":
                fatal = [{"item": "search_status", "code": "E01", "reason": "search_status must be ok/no_results/unavailable"}]
            else:
                idents = _identifiers(db, patient_id)
                seen, unverified = set(), []
                for i, src in enumerate(obj.get("sources") or []):
                    if not isinstance(src, dict):
                        problems.append({"item": f"sources[{i}]", "code": "E01", "reason": "not an object"})
                        continue
                    item, probs = validate_source(src, idents, obj.get("search_metadata"))
                    problems += [{"item": f"sources[{i}]", **p} for p in probs]
                    if any(p["code"] == "E16" for p in probs):
                        unverified.append({"url": str(src.get("url"))[:500], "title": str(src.get("title"))[:300],
                                           "status": "unverified_candidate"})
                    if item and _url_key(item["url"]) in seen:
                        problems.append({"item": f"sources[{i}]", "code": "E14", "reason": "duplicate source"})
                    elif item:
                        seen.add(_url_key(item["url"]))
                        items.append(item)
                items.sort(key=lambda x: (x["source_tier"], x["tier_basis"] != "registry"))
                limit = rec["request"]["max_sources"]
                if len(items) > limit:
                    problems.append({"item": "sources", "code": "E11", "reason": f"{len(items)} sources; kept best {limit}"})
                    items = items[:limit]
                rec["unverified_candidates"] = unverified
                if not items and unverified and len(unverified) == len(obj.get("sources") or []):
                    rec.update(status="no_results")  # honest: nothing verifiable; not a model error, no repair
                elif not items:
                    fatal = problems + [{"item": "sources", "code": "E15", "reason": "no source passed validation"}]
                else:
                    rec.update(status="answered", evidence_ids=_store(db, rec, items))
        if fatal:
            rec["errors"] = fatal
            rec["status"] = "failed" if rec["attempts"] >= MAX_ATTEMPTS else "repair_needed"
        else:
            rec["errors"] = problems
        _save_request(db, patient_id, rec)
    out = {"status": rec["status"], "request_id": rid, "evidence_ids": rec["evidence_ids"],
           "external_evidence_status": rec["external_evidence_status"], "problems": problems, "errors": fatal, "notes": notes}
    if rec["status"] == "repair_needed":
        out["repair_package"] = str(_write_package(db, patient_id, rec, out_dir))
    if rec["external_evidence_status"] in ("unavailable", "none_found"):
        out["display"] = "暂无可靠外部医学依据"
    return out


def _store(db: DB, rec: dict, items: list[dict]) -> list[str]:
    ids = []
    for item in items:
        eid = "ext_" + hashlib.sha1(_url_key(item["url"]).encode()).hexdigest()[:10]
        old = db.get_global("evidence_items", eid)
        if old:  # same source seen before: keep first_seen, merge claims
            item["relevant_claims"] = list(dict.fromkeys(old["relevant_claims"] + item["relevant_claims"]))
        db.put_global("evidence_items", eid, {"evidence_id": eid, **item,
                                              "first_seen_at": old["first_seen_at"] if old else now()})
        ids.append(eid)
    req = rec["request"]
    ttl = TTL_DAYS.get(req["question_type"], DEFAULT_TTL_DAYS)
    entry = {"topic_key": req["topic_key"], "question_type": req["question_type"], "concept": req["concept"],
             "evidence_ids": ids, "retrieved_at": now(),
             "expires_at": (_now_dt() + timedelta(days=ttl)).isoformat(timespec="seconds"),
             "source_dates": [i["publication_date"] for i in items], "invalidated": False,
             "usage_count": 0, "last_used_at": None}
    db.put_global("evidence_cache", req["topic_key"], entry, question_type=req["question_type"])
    return ids


# ----- read -----

def items(db: DB, evidence_ids: list[str]) -> list[dict]:
    """Validated (content-verified) evidence only."""
    return [x for x in (db.get_global("evidence_items", e) for e in evidence_ids) if is_verified(x)]


def show(db: DB, patient_id: str) -> dict:
    with state.patient_tx(db, patient_id):
        expire_pending(db, patient_id)
    reqs = state.get_state(db, patient_id)["evidence_requests"]
    ids = list(dict.fromkeys(e for r in reqs for e in r["evidence_ids"]))
    return {"patient_id": patient_id, "requests": reqs, "evidence": items(db, ids)}
