from __future__ import annotations
import csv
import io

from app.errors import DomainError
from app.manifest import canonical_digest, run_id
from .timeutil import normalize_utc, hour_key, advance_watermark


def _is_error(status) -> int:
    return 1 if int(status) >= 500 else 0


def _batch_digest(events: list[dict]) -> str:
    # 只取影响计算的字段做摘要，保证同一批输入稳定派生 run_id
    basis = [{k: e.get(k) for k in ("request_id", "timestamp", "service", "status", "zone", "fold")}
             for e in events]
    return canonical_digest(basis)


def _head_totals(rows: list[dict]) -> dict:
    return {"count": sum(r["count"] for r in rows),
            "errors": sum(r["errors"] for r in rows),
            "buckets": len(rows)}


def _ingest_one(db, store, event: dict, batch_id: str) -> dict:
    """处理单个事件，返回归类：inserted / moved / restated / duplicate / quarantine。"""
    request_id = str(event.get("request_id", ""))
    if not request_id:
        return {"kind": "quarantine", "reason": "EMPTY_VALUE",
                "raw_value": event.get("timestamp", ""), "request_id": request_id}
    try:
        utc_ts, used_fold = normalize_utc(
            event["timestamp"], event.get("zone"), int(event.get("fold", 0) or 0))
    except DomainError as exc:
        return {"kind": "quarantine", "reason": exc.code,
                "raw_value": event.get("timestamp", ""), "request_id": request_id}
    hour = hour_key(utc_ts)
    service = str(event.get("service", ""))
    is_error = _is_error(event.get("status", 0))
    raw_value = str(event.get("timestamp", ""))
    existing = store.get_event(db, request_id)
    if existing is not None:
        # 完全一致即重复，幂等跳过
        if (existing["event_ts"] == utc_ts and existing["hour"] == hour
                and existing["service"] == service
                and existing["is_error"] == is_error):
            return {"kind": "duplicate", "request_id": request_id}
        # 冲突：同键不同业务值且非补正场景一律隔离，保持首条采信
        raise DomainError("DUPLICATE_CONFLICT", f"request_id={request_id} 与已采信事件冲突")
    db.execute(
        "INSERT INTO events(request_id, event_ts, hour, service, status, is_error, "
        "raw_value, zone, fold, batch_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (request_id, utc_ts, hour, service, int(event.get("status", 0)), is_error,
         raw_value, event.get("zone"), used_fold, batch_id))
    store.apply_bucket_delta(db, hour, service, 1, is_error)
    db.execute(
        "INSERT INTO corrections(request_id, batch_id, kind, old_hour, new_hour, "
        "old_is_error, new_is_error, old_ts, new_ts, raw_value, reason) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (request_id, batch_id, "INSERT", None, hour, None, is_error,
         None, utc_ts, raw_value, "OK"))
    return {"kind": "inserted", "request_id": request_id, "hour": hour}


def ingest_batch(store, events: list[dict]) -> dict:
    """摄入一个批次：摘要幂等；新事件落权威表并给当前桶加贡献。"""
    digest = _batch_digest(events)
    existing = store.get_run(digest)
    if existing is not None:  # 同批重跑：原样返回，不再增减
        return {"status": "IDEMPOTENT", "run_id": existing["run_id"], "digest": digest,
                "reused": True, **existing["result"]}
    batch = run_id(events)
    inserted = moved = restated = duplicates = 0
    quarantine: list[dict] = []
    with store.connect() as db:
        for event in events:
            try:
                outcome = _ingest_one(db, store, event, batch)
            except DomainError as exc:
                quarantine.append({"request_id": str(event.get("request_id", "")),
                                   "reason": exc.code, "raw_value": event.get("timestamp", "")})
                continue
            kind = outcome["kind"]
            if kind == "inserted":
                inserted += 1
            elif kind == "duplicate":
                duplicates += 1
            else:
                quarantine.append(outcome)
        rows = store.head_buckets_tx(db)
        totals = _head_totals(rows)
        wm = store.get_watermark_tx(db)
        result = {"input_count": len(events), "valid_count": inserted,
                  "quarantine_count": len(quarantine), "duplicate_count": duplicates,
                  "moved_count": moved, "restated_count": restated,
                  "watermark": wm, **totals}
        store.record(digest, batch, result, db=db)
    return {"status": "READY", "run_id": batch, "digest": digest, "reused": False,
            **result, "quarantine": quarantine, "buckets": rows}


def apply_revisions(store, revisions: list[dict], minutes: int = 15) -> dict:
    """迟到修订：补正时间/状态后移走旧桶贡献、写入新桶；水位只前进。

    每条修订形如 {request_id, timestamp, service, status, zone?, fold?}。
    """
    digest = _batch_digest(revisions)
    existing = store.get_run(digest)
    if existing is not None:
        return {"status": "IDEMPOTENT", "run_id": existing["run_id"], "digest": digest,
                "reused": True, **existing["result"]}
    batch = run_id(revisions)
    moved = restated = inserted = 0
    late = 0
    quarantine: list[dict] = []
    observed: list[str] = []
    with store.connect() as db:
        wm_before = store.get_watermark_tx(db)
        for rev in revisions:
            request_id = str(rev.get("request_id", ""))
            try:
                utc_ts, used_fold = normalize_utc(
                    rev["timestamp"], rev.get("zone"), int(rev.get("fold", 0) or 0))
            except DomainError as exc:
                quarantine.append({"request_id": request_id, "reason": exc.code,
                                   "raw_value": rev.get("timestamp", "")})
                continue
            observed.append(utc_ts)
            hour = hour_key(utc_ts)
            service = str(rev.get("service", ""))
            is_error = _is_error(rev.get("status", 0))
            old = store.get_event(db, request_id)
            if old is None:
                # 迟到的新请求：仍进入修订并加贡献
                db.execute(
                    "INSERT INTO events(request_id, event_ts, hour, service, status, "
                    "is_error, raw_value, zone, fold, batch_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (request_id, utc_ts, hour, service, int(rev.get("status", 0)),
                     is_error, str(rev.get("timestamp", "")), rev.get("zone"),
                     used_fold, batch))
                store.apply_bucket_delta(db, hour, service, 1, is_error)
                db.execute(
                    "INSERT INTO corrections(request_id, batch_id, kind, new_hour, "
                    "new_is_error, new_ts, raw_value, reason) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (request_id, batch, "LATE_INSERT", hour, is_error, utc_ts,
                     str(rev.get("timestamp", "")), "LATE"))
                inserted += 1
                if wm_before is not None and utc_ts < wm_before:
                    late += 1
                continue
            if wm_before is not None and utc_ts < wm_before:
                late += 1
            # 移走旧桶贡献
            store.apply_bucket_delta(db, old["hour"], old["service"], -1, -old["is_error"])
            # 写入新桶贡献
            store.apply_bucket_delta(db, hour, service, 1, is_error)
            kind = "MOVE" if old["hour"] != hour else "RESTATE"
            if kind == "MOVE":
                moved += 1
            else:
                restated += 1
            db.execute(
                "UPDATE events SET event_ts=?, hour=?, service=?, status=?, is_error=?, "
                "raw_value=?, zone=?, fold=?, batch_id=?, "
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE request_id=?",
                (utc_ts, hour, service, int(rev.get("status", 0)), is_error,
                 str(rev.get("timestamp", "")), rev.get("zone"), used_fold, batch,
                 request_id))
            db.execute(
                "INSERT INTO corrections(request_id, batch_id, kind, old_hour, new_hour, "
                "old_is_error, new_is_error, old_ts, new_ts, raw_value, reason) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (request_id, batch, kind, old["hour"], hour, old["is_error"], is_error,
                 old["event_ts"], utc_ts, str(rev.get("timestamp", "")),
                 "LATE" if late else "OK"))
        if observed:
            wm_move = advance_watermark(wm_before, observed, minutes)
            # 水位不能倒退：advance_watermark 内部取 max，这里再断言一次
            if wm_before is not None and wm_move["new"] < wm_before:
                raise DomainError("WATERMARK_REGRESSION", "水位不允许倒退")
            store.set_watermark(db, wm_move["new"])
        else:
            wm_move = {"old": wm_before, "new": wm_before, "advanced": False}
        rows = store.head_buckets_tx(db)
        totals = _head_totals(rows)
        # 守恒校验：修订不改变权威事件总数
        event_total = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        if totals["count"] != event_total:
            raise DomainError("CONSERVATION_BROKEN",
                              f"桶合计 {totals['count']} != 权威事件 {event_total}")
        result = {"input_count": len(revisions), "valid_count": moved + restated + inserted,
                  "quarantine_count": len(quarantine), "duplicate_count": 0,
                  "moved_count": moved, "restated_count": restated,
                  "late_count": late, "watermark_old": wm_before,
                  "watermark": wm_move["new"], "watermark_advanced": wm_move["advanced"],
                  **totals}
        store.record(digest, batch, result, db=db)
    return {"status": "READY", "run_id": batch, "digest": digest, "reused": False,
            **result, "quarantine": quarantine, "buckets": rows,
            "corrections": store.list_corrections()}


def sign_off(store) -> dict:
    """交接签收：冻结当前桶为不可变版本，返回版本号与 run_id。"""
    with store.connect() as db:
        wm = store.get_watermark_tx(db)
        if wm is None:
            raise DomainError("WATERMARK_REQUIRED", "签收前必须先推进水位")
        rows = store.head_buckets_tx(db)
        version = store.latest_handoff_version(db) + 1
        rid = f"handoff-v{version}"
        digest = canonical_digest({"wm": wm, "rows": rows})
        totals = _head_totals(rows)
        version = store.insert_handoff(db, rid, wm, digest, rows, totals)
    return {"status": "SIGNED", "version": version, "run_id": rid,
            "watermark": wm, "digest": digest, **totals, "rows": rows}


def export_csv(rows: list[dict]) -> str:
    out = io.StringIO()
    writer = csv.DictWriter(
        out, fieldnames=["hour", "service", "count", "errors"], lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def export_version(store, *, version=None, run_id=None) -> dict:
    """取签收当时那版数字；未指定版本时导出当前修正后的桶。"""
    if version is None and run_id is None:
        rows = store.head_buckets()
        return {"version": None, "run_id": None, "watermark": store.get_watermark(),
                "rows": rows, "csv": export_csv(rows), "frozen": False,
                **_head_totals(rows)}
    handoff = store.get_handoff(version=version, run_id=run_id)
    if handoff is None:
        raise DomainError("VERSION_NOT_FOUND", "指定的签收版本不存在")
    return {"version": handoff["version"], "run_id": handoff["run_id"],
            "watermark": handoff["wm_ts"], "digest": handoff["digest"],
            "rows": handoff["rows"], "csv": export_csv(handoff["rows"]),
            "frozen": True, "created_at": handoff["created_at"],
            **handoff["totals"]}
