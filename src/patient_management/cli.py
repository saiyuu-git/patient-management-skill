"""`pm` CLI. Machine-oriented: every command prints JSON; exit 0 ok, 1 domain error, 2 usage error.

DB path: --db, else $PM_DB, else ~/.patient-management/pm.sqlite
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import analysis, conflicts, dashboard, diagnoses, education, evidence, extraction, ingestion, state, tasks
from .persistence import DB, PMError

DEFAULT_DB = Path.home() / ".patient-management" / "pm.sqlite"


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pm", description="Patient Management engine")
    p.add_argument("--db", default=os.environ.get("PM_DB", str(DEFAULT_DB)))
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create or migrate the database")

    pat = sub.add_parser("patient").add_subparsers(dest="action", required=True)
    for name in ("create", "update"):
        c = pat.add_parser(name)
        if name == "update":
            c.add_argument("patient_id")
        else:
            c.add_argument("--id", dest="patient_id")
        c.add_argument("--name", dest="display_name")
        c.add_argument("--alias")
        c.add_argument("--bed")
        c.add_argument("--sex")
        c.add_argument("--age", dest="age_text")
        c.add_argument("--admission-date", dest="admission_date")
    pat.add_parser("list")
    pat.add_parser("show").add_argument("patient_id")

    ing = sub.add_parser("ingest", help="ingest a payload JSON file ('-' = stdin)")
    ing.add_argument("patient_id")
    ing.add_argument("payload")

    prep = sub.add_parser("prepare").add_subparsers(dest="action", required=True)
    c = prep.add_parser("ingest", help="register raw text (.txt/.md or '-') and emit model task packages")
    c.add_argument("patient_id")
    c.add_argument("source")
    c.add_argument("--kind", help="source kind; detected from the first heading when omitted")
    c.add_argument("--date", help="document date (used only as lab collection date fallback)")
    c.add_argument("--out-dir")
    c.add_argument("--max-chars", type=int, default=1200)
    c.add_argument("--text-origin", default="native_text", choices=["native_text", "host_transcription", "ocr", "manual"],
                   help="how the text was obtained (scanned documents: ocr / host_transcription)")

    c = sub.add_parser("submit", help="submit a model output for a task package")
    c.add_argument("package")
    c.add_argument("output", help="model output file or '-'")
    c.add_argument("--out-dir")

    ev = sub.add_parser("evidence", help="external medical evidence (host agent performs the web search)").add_subparsers(
        dest="action", required=True)
    c = ev.add_parser("prepare", help="request JSON (file or '-') -> cache hit, duplicate, or a search package")
    c.add_argument("patient_id")
    c.add_argument("request")
    c.add_argument("--out-dir")
    c = ev.add_parser("submit", help="submit the host agent's evidence bundle for a search package")
    c.add_argument("package")
    c.add_argument("bundle", help="bundle file or '-'")
    c.add_argument("--out-dir")
    ev.add_parser("show").add_argument("patient_id")
    c = ev.add_parser("cache")
    c.add_argument("cache_action", choices=["list", "invalidate"])
    c.add_argument("topic_key", nargs="?")

    an = sub.add_parser("analysis", help="clinical analysis modules (host agent is the model)").add_subparsers(
        dest="action", required=True)
    an.add_parser("plan").add_argument("patient_id")
    c = an.add_parser("evidence", help="prepare the evidence requests the plan needs (cache first)")
    c.add_argument("patient_id")
    c.add_argument("--out-dir")
    c = an.add_parser("prepare")
    c.add_argument("patient_id")
    c.add_argument("module", choices=analysis.MODULES)
    c.add_argument("--out-dir")
    c = an.add_parser("submit")
    c.add_argument("package")
    c.add_argument("output", help="model output file or '-'")
    c.add_argument("--out-dir")
    c = an.add_parser("fallback", help="store the conservative fallback for a module")
    c.add_argument("patient_id")
    c.add_argument("module", choices=analysis.MODULES)
    an.add_parser("show").add_argument("patient_id")

    kn = sub.add_parser('knowledge', help='patient-linked clinical knowledge (host-authored)').add_subparsers(dest='action', required=True)
    kn.add_parser('prepare').add_argument('patient_id')
    c = kn.add_parser('submit')
    c.add_argument('patient_id')
    c.add_argument('output', help="JSON file or '-'")

    lab = sub.add_parser("labs")
    lab.add_argument("patient_id")
    lab.add_argument("--test", help="test_id; include points")

    dx = sub.add_parser("dx").add_subparsers(dest="action", required=True)
    dx.add_parser("list").add_argument("patient_id")
    for name, extra in (("add", ["text"]), ("edit", ["dx_id", "text"]), ("resolve", ["dx_id"]),
                        ("reactivate", ["dx_id"]), ("accept", ["candidate_id"]), ("dismiss", ["candidate_id"])):
        c = dx.add_parser(name)
        c.add_argument("patient_id")
        for a in extra:
            c.add_argument(a)
        if name == "accept":
            c.add_argument("--text", help="user-confirmed wording (defaults to candidate text)")

    tk = sub.add_parser("tasks").add_subparsers(dest="action", required=True)
    tk.add_parser("list").add_argument("patient_id")
    tk.add_parser("hints", help="possible_completed hints (read-only)").add_argument("patient_id")
    c = tk.add_parser("add")
    c.add_argument("patient_id")
    c.add_argument("title")
    c.add_argument("--due")
    c.add_argument("--priority", default="normal")
    c = tk.add_parser("set")
    c.add_argument("patient_id")
    c.add_argument("task_id")
    c.add_argument("status", choices=sorted(tasks.TRANSITIONS))
    for name in ("accept", "dismiss"):
        c = tk.add_parser(name)
        c.add_argument("patient_id")
        c.add_argument("task_id")

    cf = sub.add_parser("conflicts").add_subparsers(dest="action", required=True)
    cf.add_parser("list").add_argument("patient_id")
    c = cf.add_parser("resolve")
    c.add_argument("patient_id")
    c.add_argument("conflict_id")
    c.add_argument("--keep", help="candidate ref_id to keep")
    c.add_argument("--note")

    sub.add_parser("status").add_argument("patient_id")
    c = sub.add_parser("serve", help="local dashboard web service (127.0.0.1 by default)")
    c.add_argument("--host", default="127.0.0.1")
    c.add_argument("--port", type=int, default=8765)
    c.add_argument("--allow-remote", action="store_true", help="explicitly allow a non-loopback address")
    c = sub.add_parser("dashboard", help="fixed dashboard view model (the only structure the frontend reads)")
    c.add_argument("patient_id")
    c.add_argument("--include-background-labs", action="store_true", help="show hidden labs only on explicit user request")
    return p


def _header_args(a) -> dict:
    return {k: getattr(a, k) for k in ("display_name", "alias", "bed", "sex", "age_text", "admission_date")}


def run(argv: list[str]) -> object:
    a = _parser().parse_args(argv)
    if a.db != ":memory:":
        Path(a.db).expanduser().parent.mkdir(parents=True, exist_ok=True)
    db = DB(str(Path(a.db).expanduser()) if a.db != ":memory:" else a.db)
    try:
        return _dispatch(db, a)
    finally:
        db.close()


def _dispatch(db: DB, a) -> object:
    if a.cmd == "init":
        return {"db": a.db, "db_version": db.version, "schema_version": state.SCHEMA_VERSION}
    if a.cmd == "patient":
        if a.action == "create":
            s = state.create_patient(db, a.patient_id, **_header_args(a))
            return {"patient_id": s["patient_id"], "header": s["header"]}
        if a.action == "update":
            return state.update_header(db, a.patient_id, **_header_args(a))
        if a.action == "list":
            return [{"patient_id": pid, "label": state.display_label(state.get_state(db, pid))} for pid in db.patient_ids()]
        return state.get_state(db, a.patient_id)
    if a.cmd == "ingest":
        raw = sys.stdin.read() if a.payload == "-" else Path(a.payload).read_text(encoding="utf-8")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as e:
            raise PMError(f"payload is not valid JSON: {e}")
        return ingestion.ingest(db, a.patient_id, payload)
    if a.cmd == "prepare":
        text = sys.stdin.read() if a.source == "-" else extraction.read_source_file(a.source)
        return extraction.prepare(db, a.patient_id, text, a.kind, a.date, a.out_dir, a.max_chars, a.text_origin)
    if a.cmd == "submit":
        try:
            package = json.loads(Path(a.package).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise PMError(f"cannot read task package: {e}")
        raw = sys.stdin.read() if a.output == "-" else Path(a.output).read_text(encoding="utf-8")
        return extraction.submit(db, package, raw, a.out_dir)
    if a.cmd == "evidence":
        if a.action == "prepare":
            raw = sys.stdin.read() if a.request == "-" else Path(a.request).read_text(encoding="utf-8")
            try:
                spec = json.loads(raw)
            except json.JSONDecodeError as e:
                raise PMError(f"request is not valid JSON: {e}")
            return evidence.prepare(db, a.patient_id, spec, a.out_dir)
        if a.action == "submit":
            try:
                package = json.loads(Path(a.package).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                raise PMError(f"cannot read evidence package: {e}")
            raw = sys.stdin.read() if a.bundle == "-" else Path(a.bundle).read_text(encoding="utf-8")
            return evidence.submit(db, package, raw, a.out_dir)
        if a.action == "show":
            return evidence.show(db, a.patient_id)
        if a.cache_action == "invalidate":
            if not a.topic_key:
                raise PMError("invalidate needs a topic_key")
            return evidence.invalidate(db, a.topic_key)
        return evidence.cache_list(db)
    if a.cmd == "analysis":
        if a.action == "plan":
            return analysis.plan(db, a.patient_id)
        if a.action == "evidence":
            return [{"spec": sp, **evidence.prepare(db, a.patient_id, sp, a.out_dir)} for sp in analysis.evidence_needs(db, a.patient_id)]
        if a.action == "prepare":
            return analysis.prepare(db, a.patient_id, a.module, a.out_dir)
        if a.action == "submit":
            try:
                package = json.loads(Path(a.package).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                raise PMError(f"cannot read analysis package: {e}")
            raw = sys.stdin.read() if a.output == "-" else Path(a.output).read_text(encoding="utf-8")
            return analysis.submit(db, package, raw, a.out_dir)
        if a.action == "fallback":
            return analysis.apply_fallback(db, a.patient_id, a.module)
        return analysis.show(db, a.patient_id)
    if a.cmd == 'knowledge':
        if a.action == 'prepare':
            return education.prepare(db, a.patient_id)
        raw = sys.stdin.read() if a.output == '-' else Path(a.output).read_text(encoding='utf-8')
        try:
            output = json.loads(raw)
        except json.JSONDecodeError as e:
            raise PMError(f'knowledge output is not valid JSON: {e}')
        return education.submit(db, a.patient_id, output)
    if a.cmd == "labs":
        s = state.get_state(db, a.patient_id)
        if a.test:
            series = next((x for x in s["labs"]["series"] if x["test_id"] == a.test), None)
            if series is None:
                raise PMError(f"no series for {a.test}")
            points = [r for r in s["labs"]["results"] if r["test_id"] == a.test]
            return {"series": series, "points": sorted(points, key=lambda r: r["collected_at"])}
        return s["labs"]["series"]
    if a.cmd == "dx":
        pid = a.patient_id
        if a.action == "list":
            return state.get_state(db, pid)["diagnoses"]
        if a.action == "add":
            return diagnoses.add_manual(db, pid, a.text) or {"skipped": "same active diagnosis exists"}
        if a.action == "edit":
            return diagnoses.edit(db, pid, a.dx_id, a.text)
        if a.action in ("resolve", "reactivate"):
            return diagnoses.set_status(db, pid, a.dx_id, "resolved" if a.action == "resolve" else "active")
        if a.action == "accept":
            return diagnoses.accept_candidate(db, pid, a.candidate_id, a.text)
        return diagnoses.dismiss_candidate(db, pid, a.candidate_id)
    if a.cmd == "tasks":
        pid = a.patient_id
        if a.action == "list":
            return state.get_state(db, pid)["tasks"]
        if a.action == "hints":
            return tasks.completion_hints(db, pid)
        if a.action == "add":
            return tasks.add_manual(db, pid, a.title, a.due, a.priority) or {"skipped": "same open task exists"}
        if a.action == "set":
            return tasks.set_status(db, pid, a.task_id, a.status)
        if a.action == "accept":
            return tasks.accept_suggestion(db, pid, a.task_id)
        return tasks.dismiss_suggestion(db, pid, a.task_id)
    if a.cmd == "conflicts":
        if a.action == "list":
            return state.get_state(db, a.patient_id)["conflicts"]
        with state.patient_tx(db, a.patient_id):
            return conflicts.resolve(db, a.patient_id, a.conflict_id, a.keep, a.note)
    if a.cmd == "serve":
        db.close()
        from .server import serve
        serve(str(Path(a.db).expanduser()), a.host, a.port, a.allow_remote)
        return {"stopped": True}
    if a.cmd == "dashboard":
        return dashboard.build_view(db, a.patient_id, include_background_labs=a.include_background_labs)
    if a.cmd == "status":
        s = state.get_state(db, a.patient_id)
        return {
            "patient_id": s["patient_id"], "label": state.display_label(s),
            "persist_status": s["header"]["persist_status"], "last_updated_at": s["header"]["last_updated_at"],
            "admission_date": s["header"]["admission_date"],
            "counts": {"sources": len(s["sources"]), "facts": len(s["facts"]), "labs": len(s["labs"]["results"]),
                       "series": len(s["labs"]["series"]), "investigations": len(s["investigations"]),
                       "diagnoses": sum(d["origin"] == "user_provided" for d in s["diagnoses"]),
                       "candidates_pending": sum(d.get("review_status") == "pending" for d in s["diagnoses"]),
                       "tasks_open": sum(t["origin"] == "explicit" and t["status"] in ("pending", "in_progress")
                                         for t in s["tasks"])},
            "open_conflicts": [c["conflict_id"] for c in s["conflicts"] if c["status"] == "open"],
            "open_extraction_tasks": extraction.open_tasks(db, a.patient_id),
            "ingest_log": db.ingest_log(a.patient_id),
        }
    raise PMError(f"unknown command {a.cmd}")


def main(argv: list[str] | None = None) -> int:
    try:
        out = run(sys.argv[1:] if argv is None else argv)
    except PMError as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0
