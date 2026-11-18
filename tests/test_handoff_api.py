"""HTTP 端到端：批次摄入 → 水位 → 签收 → 迟到修订 → 版本导出 → 幂等重跑。"""


def _pay(rid, ts, status=500, **kw):
    return {"request_id": rid, "timestamp": ts, "service": "pay",
            "status": status, **kw}


def _tick(ts):
    return {"request_id": f"tick-{ts}", "timestamp": ts,
            "service": "_watermark", "status": 200}


def test_full_handoff_lifecycle_and_versioned_export(client):
    # 1. 批次摄入
    batch = [_pay("r1", "2026-11-15T01:05:00Z"),
             _pay("r2", "2026-11-15T01:20:00Z")]
    r = client.post("/api/buckets/ingest", json={"events": batch})
    assert r.status_code == 200
    run_id = r.json()["run_id"]
    assert r.json()["valid_count"] == 2

    # 2. 推进水位
    r = client.post("/api/watermark/revise",
                    json={"revisions": [_tick("2026-11-15T02:00:00Z")]})
    assert r.json()["watermark"] == "2026-11-15T01:45:00Z"

    # 3. 首次签收
    r = client.post("/api/handoffs/sign-off")
    assert r.status_code == 200
    v1 = r.json()
    assert v1["version"] == 1 and v1["run_id"] == "handoff-v1"

    # 4. DST 回拨迟到修订：r2 挪到另一个 UTC 小时
    r = client.post("/api/watermark/revise", json={"revisions": [
        _pay("r2", "2026-11-01T01:30:00", zone="America/New_York", fold=1)]})
    body = r.json()
    assert body["moved_count"] == 1 and body["late_count"] == 1

    # 5. 当前桶（值班视角）已是修正后的数
    head = client.get("/api/buckets/head").json()
    pay = {b["hour"]: b for b in head["buckets"] if b["service"] == "pay"}
    assert pay["2026-11-15T01:00:00Z"]["count"] == 1
    assert pay["2026-11-01T06:00:00Z"]["count"] == 1

    # 6. v1 冻结版原样可查（按版本号与 run_id 两种取法）
    csv_v1 = client.get("/api/handoffs/export", params={"version": 1})
    assert csv_v1.headers["x-frozen"] == "1"
    assert "2026-11-15T01:00:00Z,pay,2,2" in csv_v1.text
    csv_by_run = client.get("/api/handoffs/export",
                            params={"run_id": "handoff-v1"})
    assert csv_by_run.text == csv_v1.text

    # 旧导出入口带版本参数同样取冻结版
    legacy = client.get("/api/export/buckets.csv", params={"version": 1})
    assert legacy.text == csv_v1.text
    assert legacy.headers["x-run-id"] == "handoff-v1"

    # 7. 第二次签收
    v2 = client.post("/api/handoffs/sign-off").json()
    assert v2["version"] == 2
    listings = client.get("/api/handoffs").json()["handoffs"]
    assert [h["version"] for h in listings] == [1, 2]

    # 8. 同批重跑幂等：run_id 复用、桶不重复增减
    again = client.post("/api/buckets/ingest", json={"events": batch}).json()
    assert again["reused"] is True and again["run_id"] == run_id
    head2 = client.get("/api/buckets/head").json()
    assert sum(b["count"] for b in head2["buckets"]) == \
        sum(b["count"] for b in head["buckets"])


def test_revision_rerun_is_idempotent(client):
    client.post("/api/buckets/ingest", json={"events": [
        _pay("r1", "2026-11-15T01:05:00Z")]})
    client.post("/api/watermark/revise",
                json={"revisions": [_tick("2026-11-15T02:00:00Z")]})
    rev = [_pay("r1", "2026-11-15T03:05:00Z")]
    first = client.post("/api/watermark/revise", json={"revisions": rev}).json()
    second = client.post("/api/watermark/revise", json={"revisions": rev}).json()
    assert first["run_id"] == second["run_id"]
    assert second["reused"] is True
    head = client.get("/api/buckets/head").json()
    pay = [b for b in head["buckets"] if b["service"] == "pay"]
    assert sum(b["count"] for b in pay) == 1


def test_quarantine_and_conflict_via_api(client):
    r = client.post("/api/buckets/ingest", json={"events": [
        _pay("ok", "2026-11-15T01:00:00Z"),
        {"request_id": "gap", "timestamp": "2026-03-08T02:30:00",
         "zone": "America/New_York", "service": "pay", "status": 500},
    ]})
    assert r.json()["quarantine_count"] == 1
    conflict = client.post("/api/buckets/ingest",
                           json={"events": [_pay("ok", "2026-11-15T09:00:00Z",
                                                 status=200)]}).json()
    assert conflict["quarantine"][0]["reason"] == "DUPLICATE_CONFLICT"


def test_signoff_without_watermark_422(client):
    client.post("/api/buckets/ingest",
                json={"events": [_pay("r1", "2026-11-15T01:00:00Z")]})
    r = client.post("/api/handoffs/sign-off")
    assert r.status_code == 422
    assert r.json()["error"] == "WATERMARK_REQUIRED"


def test_unknown_version_422(client):
    r = client.get("/api/handoffs/export", params={"version": 7})
    assert r.status_code == 422
    assert r.json()["error"] == "VERSION_NOT_FOUND"


def test_legacy_demo_endpoints_still_work(client):
    # 既有 demo 行为与页面不受账本改造影响
    for path in ["/api/logs/import", "/api/time/normalize", "/api/events/dedupe",
                 "/api/buckets/rebuild", "/api/watermark/advance"]:
        assert client.post(path, json={}).status_code == 200
    for path in ["/logs", "/time", "/events", "/buckets", "/watermark", "/runs"]:
        assert client.get(path).status_code == 200
    assert client.get("/api/export/buckets.csv").text.startswith(
        "hour,service,count,errors")
