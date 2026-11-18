from __future__ import annotations
import json
from pathlib import Path
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    digest TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    result TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    request_id TEXT PRIMARY KEY,
    event_ts TEXT NOT NULL,
    hour TEXT NOT NULL,
    service TEXT NOT NULL,
    status INTEGER NOT NULL,
    is_error INTEGER NOT NULL,
    raw_value TEXT NOT NULL,
    zone TEXT,
    fold INTEGER NOT NULL DEFAULT 0,
    batch_id TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS corrections (
    correction_id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    old_hour TEXT,
    new_hour TEXT,
    old_is_error INTEGER,
    new_is_error INTEGER,
    old_ts TEXT,
    new_ts TEXT,
    raw_value TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS bucket_head (
    hour TEXT NOT NULL,
    service TEXT NOT NULL,
    count INTEGER NOT NULL,
    errors INTEGER NOT NULL,
    PRIMARY KEY (hour, service)
);
CREATE TABLE IF NOT EXISTS watermark (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    wm_ts TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS handoffs (
    version INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL UNIQUE,
    wm_ts TEXT NOT NULL,
    digest TEXT NOT NULL,
    rows_json TEXT NOT NULL,
    totals_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
"""


class Store:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)

    def connect(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    # ---- M6 既有：输入摘要 -> 稳定运行编号（同时充当批次幂等登记） ----
    def record(self, digest: str, run_id: str, result: dict, db=None) -> dict:
        payload = json.dumps(result, ensure_ascii=False, sort_keys=True)
        if db is None:
            with self.connect() as own:
                own.execute("INSERT OR IGNORE INTO runs VALUES (?, ?, ?)",
                            (digest, run_id, payload))
                row = own.execute(
                    "SELECT run_id, result FROM runs WHERE digest=?", (digest,)).fetchone()
        else:  # 复用调用方事务，避免写锁竞争
            db.execute("INSERT OR IGNORE INTO runs VALUES (?, ?, ?)",
                       (digest, run_id, payload))
            row = db.execute(
                "SELECT run_id, result FROM runs WHERE digest=?", (digest,)).fetchone()
        return {"run_id": row[0], "result": json.loads(row[1])}

    def count(self) -> int:
        with self.connect() as db:
            return db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]

    def all(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT run_id, digest, result FROM runs ORDER BY run_id").fetchall()
        return [{"run_id": r[0], "digest": r[1], "result": json.loads(r[2])} for r in rows]

    # ---- 账本查询 ----
    def get_run(self, digest: str):
        with self.connect() as db:
            row = db.execute("SELECT run_id, result FROM runs WHERE digest=?", (digest,)).fetchone()
        return None if row is None else {"run_id": row[0], "result": json.loads(row[1])}

    def get_event(self, db: sqlite3.Connection, request_id: str):
        return db.execute("SELECT * FROM events WHERE request_id=?", (request_id,)).fetchone()

    def list_events(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM events ORDER BY event_ts, request_id").fetchall()
        return [dict(r) for r in rows]

    def list_corrections(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM corrections ORDER BY correction_id").fetchall()
        return [dict(r) for r in rows]

    def head_buckets(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT hour, service, count, errors FROM bucket_head "
                "ORDER BY hour, service").fetchall()
        return [dict(r) for r in rows]

    def get_watermark(self):
        with self.connect() as db:
            row = db.execute("SELECT wm_ts FROM watermark WHERE id=1").fetchone()
        return None if row is None else row[0]

    def get_watermark_tx(self, db: sqlite3.Connection):
        row = db.execute("SELECT wm_ts FROM watermark WHERE id=1").fetchone()
        return None if row is None else row[0]

    def head_buckets_tx(self, db: sqlite3.Connection) -> list[dict]:
        return [dict(r) for r in db.execute(
            "SELECT hour, service, count, errors FROM bucket_head "
            "ORDER BY hour, service").fetchall()]

    def set_watermark(self, db: sqlite3.Connection, wm_ts: str) -> None:
        db.execute(
            "INSERT INTO watermark(id, wm_ts) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET wm_ts=excluded.wm_ts, "
            "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')", (wm_ts,))

    def list_handoffs(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT version, run_id, wm_ts, digest, totals_json, created_at "
                "FROM handoffs ORDER BY version").fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["totals"] = json.loads(item.pop("totals_json"))
            out.append(item)
        return out

    def get_handoff(self, *, version=None, run_id=None):
        with self.connect() as db:
            if version is not None:
                row = db.execute("SELECT * FROM handoffs WHERE version=?", (version,)).fetchone()
            else:
                row = db.execute("SELECT * FROM handoffs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            return None
        return {"version": row["version"], "run_id": row["run_id"], "wm_ts": row["wm_ts"],
                "digest": row["digest"], "rows": json.loads(row["rows_json"]),
                "totals": json.loads(row["totals_json"]), "created_at": row["created_at"]}

    def latest_handoff_version(self, db: sqlite3.Connection) -> int:
        row = db.execute("SELECT COALESCE(MAX(version),0) FROM handoffs").fetchone()
        return row[0]

    def insert_handoff(self, db: sqlite3.Connection, run_id: str, wm_ts: str,
                       digest: str, rows: list[dict], totals: dict) -> int:
        cur = db.execute(
            "INSERT INTO handoffs(run_id, wm_ts, digest, rows_json, totals_json) "
            "VALUES (?,?,?,?,?)",
            (run_id, wm_ts, digest,
             json.dumps(rows, ensure_ascii=False, sort_keys=True),
             json.dumps(totals, ensure_ascii=False, sort_keys=True)))
        return int(cur.lastrowid)

    # ---- 桶增减：delta 为负即移走贡献，count 归零的行删除（水位语义在调用方保证） ----
    def apply_bucket_delta(self, db: sqlite3.Connection, hour: str, service: str,
                           d_count: int, d_errors: int) -> None:
        row = db.execute(
            "SELECT count, errors FROM bucket_head WHERE hour=? AND service=?",
            (hour, service)).fetchone()
        if row is None:
            db.execute(
                "INSERT INTO bucket_head(hour, service, count, errors) VALUES (?,?,?,?)",
                (hour, service, d_count, d_errors))
            return
        count, errors = row[0] + d_count, row[1] + d_errors
        if count == 0:
            db.execute("DELETE FROM bucket_head WHERE hour=? AND service=?", (hour, service))
        else:
            db.execute(
                "UPDATE bucket_head SET count=?, errors=? WHERE hour=? AND service=?",
                (count, errors, hour, service))
