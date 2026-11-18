from fastapi import APIRouter, Body, Request
from .service import execute
from app.ledger.pipeline import ingest_batch
router = APIRouter(tags=["M4-小时分桶"])

@router.get("/buckets")
def page(request: Request):
    data = execute()
    data["head_buckets"] = request.app.state.store.head_buckets()
    data["watermark"] = request.app.state.store.get_watermark()
    return request.app.state.templates.TemplateResponse(request, "m4_buckets.html", {"title": "小时分桶", "data": data})

@router.post("/api/buckets/rebuild")
def api(payload: dict = Body(default={})):
    return execute(payload or None)

@router.post("/api/buckets/ingest")
def ingest(request: Request, payload: dict = Body(default={})):
    """持久化批次摄入：权威表 + 当前桶，按输入摘要幂等。"""
    events = payload.get("events", [])
    return ingest_batch(request.app.state.store, events)

@router.get("/api/buckets/head")
def head(request: Request):
    store = request.app.state.store
    rows = store.head_buckets()
    return {"watermark": store.get_watermark(), "buckets": rows,
            "count": sum(r["count"] for r in rows),
            "errors": sum(r["errors"] for r in rows)}
