"""SQLite persistence. Each row = patient_id + id + a few indexed columns + JSON payload in schema shape.

Only this module knows SQL. Everything above it deals in schema-shaped dicts.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime


class PMError(Exception):
    """Domain error: invalid request or forbidden transition. CLI exits 1."""


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# Append-only. Never edit a released migration; add a new one.
MIGRATIONS = [
    """
    CREATE TABLE patients (patient_id TEXT PRIMARY KEY, data TEXT NOT NULL);
    CREATE TABLE counters (
        patient_id TEXT NOT NULL REFERENCES patients ON DELETE CASCADE,
        prefix TEXT NOT NULL, n INTEGER NOT NULL, PRIMARY KEY (patient_id, prefix));
    CREATE TABLE sources (
        patient_id TEXT NOT NULL REFERENCES patients ON DELETE CASCADE,
        id TEXT NOT NULL, fingerprint TEXT NOT NULL, data TEXT NOT NULL,
        PRIMARY KEY (patient_id, id), UNIQUE (patient_id, fingerprint));
    CREATE TABLE source_texts (
        patient_id TEXT NOT NULL REFERENCES patients ON DELETE CASCADE,
        id TEXT NOT NULL, text TEXT NOT NULL, PRIMARY KEY (patient_id, id));
    CREATE TABLE ingest_log (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id TEXT NOT NULL REFERENCES patients ON DELETE CASCADE,
        source_id TEXT NOT NULL, fingerprint TEXT NOT NULL, result TEXT NOT NULL, at TEXT NOT NULL);
    CREATE TABLE facts (
        patient_id TEXT NOT NULL REFERENCES patients ON DELETE CASCADE,
        id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY (patient_id, id));
    CREATE TABLE lab_results (
        patient_id TEXT NOT NULL REFERENCES patients ON DELETE CASCADE,
        id TEXT NOT NULL, test_id TEXT NOT NULL, collected_at TEXT NOT NULL, data TEXT NOT NULL,
        PRIMARY KEY (patient_id, id));
    CREATE INDEX lab_results_by_test ON lab_results (patient_id, test_id, collected_at);
    CREATE TABLE lab_series (
        patient_id TEXT NOT NULL REFERENCES patients ON DELETE CASCADE,
        id TEXT NOT NULL, test_id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY (patient_id, id));
    CREATE TABLE investigations (
        patient_id TEXT NOT NULL REFERENCES patients ON DELETE CASCADE,
        id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY (patient_id, id));
    CREATE TABLE diagnoses (
        patient_id TEXT NOT NULL REFERENCES patients ON DELETE CASCADE,
        id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY (patient_id, id));
    CREATE TABLE diagnosis_candidates (
        patient_id TEXT NOT NULL REFERENCES patients ON DELETE CASCADE,
        id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY (patient_id, id));
    CREATE TABLE tasks (
        patient_id TEXT NOT NULL REFERENCES patients ON DELETE CASCADE,
        id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY (patient_id, id));
    CREATE TABLE conflicts (
        patient_id TEXT NOT NULL REFERENCES patients ON DELETE CASCADE,
        id TEXT NOT NULL, subject TEXT NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL,
        PRIMARY KEY (patient_id, id));
    CREATE TABLE analysis_records (
        patient_id TEXT NOT NULL REFERENCES patients ON DELETE CASCADE,
        id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY (patient_id, id));
    CREATE TABLE events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id TEXT NOT NULL REFERENCES patients ON DELETE CASCADE,
        at TEXT NOT NULL, entity TEXT NOT NULL, entity_id TEXT NOT NULL, data TEXT NOT NULL);
    """,
    """
    CREATE TABLE extraction_tasks (
        patient_id TEXT NOT NULL REFERENCES patients ON DELETE CASCADE,
        id TEXT NOT NULL, source_id TEXT NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL,
        PRIMARY KEY (patient_id, id));
    """,
    """
    CREATE TABLE evidence_requests (
        patient_id TEXT NOT NULL REFERENCES patients ON DELETE CASCADE,
        id TEXT NOT NULL, topic_key TEXT NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL,
        PRIMARY KEY (patient_id, id));
    -- global: external evidence is shared across patients and never contains patient data
    CREATE TABLE evidence_items (id TEXT PRIMARY KEY, data TEXT NOT NULL);
    CREATE TABLE evidence_cache (id TEXT PRIMARY KEY, question_type TEXT NOT NULL, data TEXT NOT NULL);
    """,
    """
    CREATE TABLE analysis_tasks (
        patient_id TEXT NOT NULL REFERENCES patients ON DELETE CASCADE,
        id TEXT NOT NULL, module TEXT NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL,
        PRIMARY KEY (patient_id, id));
    """,
]

# Payload key holding the row id, and extra indexed columns, per table.
_KEY = {"sources": "source_id", "conflicts": "conflict_id", "evidence_requests": "request_id"}
_COLUMNS = {
    "sources": ("fingerprint",),
    "lab_results": ("test_id", "collected_at"),
    "lab_series": ("test_id",),
    "conflicts": ("subject", "status"),
    "extraction_tasks": ("source_id", "status"),
    "evidence_requests": ("topic_key", "status"),
    "analysis_tasks": ("module", "status"),
}
_NO_AUDIT = {"lab_series"}  # derived; rebuilt often


class DB:
    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._depth = 0
        self._migrate()

    def _migrate(self) -> None:
        current = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if current > len(MIGRATIONS):
            raise PMError(f"database version {current} is newer than this engine ({len(MIGRATIONS)})")
        for version in range(current, len(MIGRATIONS)):
            self.conn.executescript(
                f"BEGIN; {MIGRATIONS[version]} PRAGMA user_version = {version + 1}; COMMIT;")

    @property
    def version(self) -> int:
        return self.conn.execute("PRAGMA user_version").fetchone()[0]

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self):
        """Re-entrant transaction: only the outermost level commits or rolls back."""
        if self._depth == 0:
            self.conn.execute("BEGIN IMMEDIATE")
        self._depth += 1
        try:
            yield self
        except BaseException:
            self._depth -= 1
            if self._depth == 0:
                self.conn.execute("ROLLBACK")
            raise
        self._depth -= 1
        if self._depth == 0:
            self.conn.execute("COMMIT")

    # ----- patients -----

    def put_patient(self, patient_id: str, data: dict) -> None:
        self.conn.execute(
            "INSERT INTO patients (patient_id, data) VALUES (?, ?) "
            "ON CONFLICT (patient_id) DO UPDATE SET data = excluded.data",
            (patient_id, _dump(data)))

    def get_patient(self, patient_id: str) -> dict | None:
        row = self.conn.execute("SELECT data FROM patients WHERE patient_id = ?", (patient_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def patient_ids(self) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT patient_id FROM patients ORDER BY rowid")]

    # ----- generic entities -----

    def put(self, table: str, patient_id: str, obj: dict, **columns) -> dict:
        """columns: indexed values not stored in the payload (e.g. a source fingerprint)."""
        key = obj[_KEY.get(table, "id")]
        cols = _COLUMNS.get(table, ())
        names = ", ".join(("patient_id", "id") + cols + ("data",))
        marks = ", ".join("?" * (len(cols) + 3))
        updates = ", ".join(f"{c} = excluded.{c}" for c in cols + ("data",))
        self.conn.execute(
            f"INSERT INTO {table} ({names}) VALUES ({marks}) "
            f"ON CONFLICT (patient_id, id) DO UPDATE SET {updates}",
            (patient_id, key, *(columns[c] if c in columns else obj[c] for c in cols), _dump(obj)))
        if table not in _NO_AUDIT:
            self.conn.execute(
                "INSERT INTO events (patient_id, at, entity, entity_id, data) VALUES (?, ?, ?, ?, ?)",
                (patient_id, now(), table, key, _dump(obj)))
        return obj

    def get(self, table: str, patient_id: str, key: str) -> dict | None:
        row = self.conn.execute(
            f"SELECT data FROM {table} WHERE patient_id = ? AND id = ?", (patient_id, key)).fetchone()
        return json.loads(row[0]) if row else None

    def all(self, table: str, patient_id: str, **where) -> list[dict]:
        clause = "".join(f" AND {k} = ?" for k in where)
        rows = self.conn.execute(
            f"SELECT data FROM {table} WHERE patient_id = ?{clause} ORDER BY rowid",
            (patient_id, *where.values()))
        return [json.loads(r[0]) for r in rows]

    def delete(self, table: str, patient_id: str, key: str) -> None:
        self.conn.execute(f"DELETE FROM {table} WHERE patient_id = ? AND id = ?", (patient_id, key))
        self.conn.execute("INSERT INTO events (patient_id, at, entity, entity_id, data) VALUES (?, ?, ?, ?, ?)",
                          (patient_id, now(), table, key, '{"deleted": true}'))

    def delete_all(self, table: str, patient_id: str) -> None:
        self.conn.execute(f"DELETE FROM {table} WHERE patient_id = ?", (patient_id,))

    def next_id(self, patient_id: str, prefix: str, width: int = 3) -> str:
        """Per-patient monotonically increasing ID, e.g. dx_001."""
        self.conn.execute(
            "INSERT INTO counters (patient_id, prefix, n) VALUES (?, ?, 1) "
            "ON CONFLICT (patient_id, prefix) DO UPDATE SET n = n + 1", (patient_id, prefix))
        n = self.conn.execute(
            "SELECT n FROM counters WHERE patient_id = ? AND prefix = ?", (patient_id, prefix)).fetchone()[0]
        return f"{prefix}_{n:0{width}d}"

    # ----- global (not patient-scoped) tables -----

    def put_global(self, table: str, key: str, obj: dict, **columns) -> dict:
        cols = tuple(columns)
        names = ", ".join(("id",) + cols + ("data",))
        marks = ", ".join("?" * (len(cols) + 2))
        updates = ", ".join(f"{c} = excluded.{c}" for c in cols + ("data",))
        self.conn.execute(f"INSERT INTO {table} ({names}) VALUES ({marks}) ON CONFLICT (id) DO UPDATE SET {updates}",
                          (key, *columns.values(), _dump(obj)))
        return obj

    def get_global(self, table: str, key: str) -> dict | None:
        row = self.conn.execute(f"SELECT data FROM {table} WHERE id = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def all_global(self, table: str, **where) -> list[dict]:
        clause = " AND ".join(f"{k} = ?" for k in where)
        rows = self.conn.execute(f"SELECT data FROM {table}{' WHERE ' + clause if where else ''} ORDER BY rowid",
                                 tuple(where.values()))
        return [json.loads(r[0]) for r in rows]

    # ----- sources -----

    def source_by_fingerprint(self, patient_id: str, fingerprint: str) -> dict | None:
        row = self.conn.execute(
            "SELECT data FROM sources WHERE patient_id = ? AND fingerprint = ?",
            (patient_id, fingerprint)).fetchone()
        return json.loads(row[0]) if row else None

    def put_source_text(self, patient_id: str, source_id: str, text: str) -> None:
        self.conn.execute("INSERT INTO source_texts (patient_id, id, text) VALUES (?, ?, ?)",
                          (patient_id, source_id, text))

    def source_text(self, patient_id: str, source_id: str) -> str | None:
        row = self.conn.execute("SELECT text FROM source_texts WHERE patient_id = ? AND id = ?",
                                (patient_id, source_id)).fetchone()
        return row[0] if row else None

    def log_ingest(self, patient_id: str, source_id: str, fingerprint: str, result: str) -> None:
        self.conn.execute(
            "INSERT INTO ingest_log (patient_id, source_id, fingerprint, result, at) VALUES (?, ?, ?, ?, ?)",
            (patient_id, source_id, fingerprint, result, now()))

    def fingerprint(self, patient_id: str) -> str:
        """Cheap change marker for one patient (any audited write or header update changes it)."""
        seq = self.conn.execute("SELECT MAX(seq) FROM events WHERE patient_id = ?", (patient_id,)).fetchone()[0]
        rec = self.get_patient(patient_id) or {}
        return f"{seq or 0}:{(rec.get('header') or {}).get('last_updated_at', '')}:{(rec.get('header') or {}).get('persist_status', '')}"

    def ingest_log(self, patient_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT source_id, result, at FROM ingest_log WHERE patient_id = ? ORDER BY seq", (patient_id,))
        return [{"source_id": s, "result": r, "at": a} for s, r, a in rows]

    def history(self, patient_id: str, entity: str, entity_id: str | None = None) -> list[dict]:
        """Patient-scoped audit versions; SQL stays behind the persistence boundary."""
        rows = self.conn.execute(
            "SELECT at, entity_id, data FROM events WHERE patient_id = ? AND entity = ? "
            "AND (? IS NULL OR entity_id = ?) ORDER BY seq", (patient_id, entity, entity_id, entity_id))
        return [{"at": at, "id": key, "data": json.loads(data)} for at, key, data in rows]


def _dump(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)
