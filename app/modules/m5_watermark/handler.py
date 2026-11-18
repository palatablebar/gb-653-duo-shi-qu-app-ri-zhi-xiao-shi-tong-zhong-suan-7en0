from fastapi import APIRouter, Body, Request
from .service import execute
from app.ledger.pipeline import apply_revisions
router = APIRouter(tags=["M5-迟到修订"])

@router.get("/watermark")
def page(request: Request):
    data = execute()
    store = request.app.state.store
    data["head_watermark"] = store.get_watermark()
    data["corrections"] = store.list_corrections()
    return request.app.state.templates.TemplateResponse(request, "m5_watermark.html", {"title": "迟到修订", "data": data})

@router.post("/api/watermark/advance")
def api(payload: dict = Body(default={})):
    return execute(payload or None)

@router.post("/api/watermark/revise")
def revise(request: Request, payload: dict = Body(default={})):
    """迟到修订：补正后移桶重算，水位只前进；同批重跑不重复增减。"""
    revisions = payload.get("revisions", payload.get("events", []))
    return apply_revisions(request.app.state.store, revisions,
                           int(payload.get("minutes", 15)))

@router.get("/api/watermark/state")
def state(request: Request):
    store = request.app.state.store
    return {"watermark": store.get_watermark(),
            "corrections": store.list_corrections()}
