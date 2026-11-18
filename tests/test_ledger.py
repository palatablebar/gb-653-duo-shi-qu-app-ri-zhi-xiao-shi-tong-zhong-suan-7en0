import pytest

from app.store import Store
from app.ledger.pipeline import (ingest_batch, apply_revisions, sign_off,
                                 export_version)
from app.ledger.timeutil import normalize_utc, hour_key, advance_watermark
from app.errors import DomainError


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "ledger.db"))


# ---------- M2 时间规则在账本内的复用：DST 折叠小时 ----------
def test_fold_distinguishes_fall_back_hours():
    # 2026-11-01 美国东部回拨，01:30 出现两次；fold=0 是 EDT(-4)，fold=1 是 EST(-5)
    early, _ = normalize_utc("2026-11-01T01:30:00", "America/New_York", 0)
    late, _ = normalize_utc("2026-11-01T01:30:00", "America/New_York", 1)
    assert early == "2026-11-01T05:30:00Z"
    assert late == "2026-11-01T06:30:00Z"
    assert hour_key(early) == "2026-11-01T05:00:00Z"
    assert hour_key(late) == "2026-11-01T06:00:00Z"


def test_dst_gap_rejected():
    with pytest.raises(DomainError) as exc:
        normalize_utc("2026-03-08T02:30:00", "America/New_York")
    assert exc.value.code == "DST_GAP"


# ---------- 水位只前进 ----------
def test_watermark_never_moves_backward():
    move = advance_watermark("2026-11-15T01:00:00Z", ["2026-11-15T00:40:00Z"])
    assert move["new"] == "2026-11-15T01:00:00Z"
    assert move["advanced"] is False


def test_watermark_advances_after_window():
    move = advance_watermark("2026-11-15T01:00:00Z", ["2026-11-15T01:30:00Z"])
    assert move["new"] == "2026-11-15T01:15:00Z"
    assert move["advanced"] is True


# ---------- 端到端：签收 → 迟到修订挪桶 → 旧版原样可查 ----------
def _pay(rid, ts, status=500, **kw):
    return {"request_id": rid, "timestamp": ts, "service": "pay",
            "status": status, **kw}


def _tick(ts):
    """只用于推进水位的事件，独立服务，不污染 pay 桶核对。"""
    return {"request_id": f"tick-{ts}", "timestamp": ts,
            "service": "_watermark", "status": 200}


def test_signed_numbers_frozen_while_head_is_corrected(store):
    # 初始批次：r1、r2 都落在 01:00 UTC 桶，各 1 条错误
    ingest_batch(store, [_pay("r1", "2026-11-15T01:05:00Z"),
                         _pay("r2", "2026-11-15T01:20:00Z")])
    apply_revisions(store, [_tick("2026-11-15T02:00:00Z")])
    wm = store.get_watermark()
    assert wm == "2026-11-15T01:45:00Z"

    # 第一次交接签收：pay 的 01 桶 count=2 errors=2
    v1 = sign_off(store)
    assert v1["version"] == 1
    frozen_v1 = export_version(store, version=1)
    pay_v1 = [r for r in frozen_v1["rows"] if r["service"] == "pay"]
    assert {(r["hour"], r["count"], r["errors"]) for r in pay_v1} == \
        {("2026-11-15T01:00:00Z", 2, 2)}

    # 迟到补正：r2 真实时间在另一小时（DST 回拨折叠小时，fold=1）
    out = apply_revisions(store, [_pay("r2", "2026-11-01T01:30:00",
                                       zone="America/New_York", fold=1)])
    assert out["moved_count"] == 1
    assert out["late_count"] == 1
    head = {r["hour"]: r for r in store.head_buckets() if r["service"] == "pay"}
    # 旧桶移走 1 条贡献
    assert head["2026-11-15T01:00:00Z"]["count"] == 1
    assert head["2026-11-15T01:00:00Z"]["errors"] == 1
    # 新桶（fold=1 → 06:00 UTC）得到贡献
    assert head["2026-11-01T06:00:00Z"]["count"] == 1

    # 已签收的 v1 数字原样可查，不受修订影响
    again_v1 = export_version(store, run_id="handoff-v1")
    assert again_v1["rows"] == frozen_v1["rows"]
    assert again_v1["frozen"] is True
    assert "2026-11-15T01:00:00Z,pay,2,2" in again_v1["csv"]

    # 第二次签收看到修正后的数（pay 总量守恒为 2）
    v2 = sign_off(store)
    assert v2["version"] == 2
    frozen_v2 = export_version(store, version=2)
    pay_v2 = [r for r in frozen_v2["rows"] if r["service"] == "pay"]
    assert sum(r["count"] for r in pay_v2) == 2
    assert {(r["hour"], r["count"]) for r in pay_v2} == \
        {("2026-11-15T01:00:00Z", 1), ("2026-11-01T06:00:00Z", 1)}


def test_watermark_not_regressed_by_late_revision(store):
    ingest_batch(store, [_pay("r1", "2026-11-15T03:00:00Z")])
    apply_revisions(store, [_tick("2026-11-15T04:00:00Z")])
    wm_before = store.get_watermark()
    # 迟到事件时间远早于水位，推进后水位保持不变
    out = apply_revisions(store, [_pay("r1", "2026-11-15T01:00:00Z")])
    assert store.get_watermark() == wm_before
    assert out["watermark_advanced"] is False
    assert out["late_count"] == 1


def test_rerun_same_batch_is_idempotent(store):
    batch = [_pay("r1", "2026-11-15T01:05:00Z"),
             _pay("r2", "2026-11-15T01:40:00Z")]
    first = ingest_batch(store, batch)
    second = ingest_batch(store, batch)
    assert first["run_id"] == second["run_id"]
    assert second["reused"] is True
    # 桶没有被加两遍
    rows = store.head_buckets()
    assert sum(r["count"] for r in rows) == 2

    # 修订批重跑同样不重复增减
    rev = [_pay("r2", "2026-11-15T02:05:00Z")]
    rev1 = apply_revisions(store, rev)
    rev2 = apply_revisions(store, rev)
    assert rev1["run_id"] == rev2["run_id"]
    assert rev2["reused"] is True
    head = store.head_buckets()
    assert sum(r["count"] for r in head) == 2
    assert len(store.list_corrections()) == 3  # 2 插入 + 1 挪桶，重跑不留痕


def test_total_conservation_across_revisions(store):
    ingest_batch(store, [_pay(f"r{i}", f"2026-11-15T01:{10+i:02d}:00Z")
                         for i in range(5)])
    apply_revisions(store, [_tick("2026-11-15T02:00:00Z")])
    apply_revisions(store, [_pay("r0", "2026-11-15T03:00:00Z", status=200),
                            _pay("r1", "2026-11-15T03:30:00Z")])
    rows = [r for r in store.head_buckets() if r["service"] == "pay"]
    assert sum(r["count"] for r in rows) == 5  # 挪桶不改变总量
    # 错误数守恒：r0 由 500 改 200，错误 -1；r1 仍是错误
    assert sum(r["errors"] for r in rows) == 4
    moved = [c for c in store.list_corrections() if c["kind"] == "MOVE"]
    assert len(moved) == 2
    assert {c["old_hour"] for c in moved} == {"2026-11-15T01:00:00Z"}


def test_late_new_event_enters_revision(store):
    ingest_batch(store, [_pay("r1", "2026-11-15T03:00:00Z")])
    apply_revisions(store, [_tick("2026-11-15T04:00:00Z")])
    out = apply_revisions(store, [_pay("late1", "2026-11-15T01:00:00Z")])
    assert out["valid_count"] == 1
    assert out["late_count"] == 1
    pay_rows = [r for r in store.head_buckets() if r["service"] == "pay"]
    assert sum(r["count"] for r in pay_rows) == 2


def test_quarantine_bad_time_and_conflict(store):
    out = ingest_batch(store, [
        _pay("good", "2026-11-15T01:00:00Z"),
        {"request_id": "gap", "timestamp": "2026-03-08T02:30:00",
         "zone": "America/New_York", "service": "pay", "status": 500},
        {"request_id": "", "timestamp": "2026-11-15T01:00:00Z",
         "service": "pay", "status": 500},
    ])
    assert out["valid_count"] == 1
    assert out["quarantine_count"] == 2
    reasons = {q["reason"] for q in out["quarantine"]}
    assert reasons == {"DST_GAP", "EMPTY_VALUE"}

    # 同键冲突隔离，首条采信不动
    conflict = ingest_batch(store, [_pay("good", "2026-11-15T05:00:00Z", status=200)])
    assert conflict["quarantine_count"] == 1
    assert conflict["quarantine"][0]["reason"] == "DUPLICATE_CONFLICT"


def test_sign_off_requires_watermark(store):
    ingest_batch(store, [_pay("r1", "2026-11-15T01:00:00Z")])
    with pytest.raises(DomainError) as exc:
        sign_off(store)
    assert exc.value.code == "WATERMARK_REQUIRED"


def test_export_unknown_version_raises(store):
    with pytest.raises(DomainError) as exc:
        export_version(store, version=99)
    assert exc.value.code == "VERSION_NOT_FOUND"


def test_head_export_when_no_version(store):
    ingest_batch(store, [_pay("r1", "2026-11-15T01:00:00Z")])
    data = export_version(store)
    assert data["frozen"] is False
    assert data["version"] is None
    assert data["csv"].startswith("hour,service,count,errors")
