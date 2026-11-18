from fastapi import APIRouter, Request
from fastapi.responses import Response
from .service import execute
from app.ledger.pipeline import sign_off, export_version
router = APIRouter(tags=["M6-批次对账"])

@router.get("/runs")
def page(request: Request):
    data = execute({"store": request.app.state.store})
    store = request.app.state.store
    data["handoffs"] = store.list_handoffs()
    data["head_watermark"] = store.get_watermark()
    return request.app.state.templates.TemplateResponse(request, "m6_runs.html", {"title": "批次对账", "data": data})

@router.get("/api/runs")
def runs(request: Request):
    execute({"store": request.app.state.store})
    return {"runs": request.app.state.store.all()}

@router.post("/api/handoffs/sign-off")
def signoff(request: Request):
    """交接签收：冻结当前桶为不可变版本。"""
    return sign_off(request.app.state.store)

@router.get("/api/handoffs")
def handoffs(request: Request):
    return {"handoffs": request.app.state.store.list_handoffs()}

@router.get("/api/handoffs/export")
def handoff_export(request: Request, version: int | None = None, run_id: str | None = None):
    """按版本取那版数字；不带版本则导出当前修正后的桶。"""
    data = export_version(request.app.state.store, version=version, run_id=run_id)
    filename = f"buckets-v{data['version']}.csv" if data["version"] else "buckets-head.csv"
    return Response(data["csv"], media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}",
                             "X-Run-Id": data["run_id"] or "",
                             "X-Frozen": "1" if data["frozen"] else "0"})

@router.get("/api/export/buckets.csv")
def export(request: Request, version: int | None = None, run_id: str | None = None):
    if version is None and run_id is None:
        data = execute({"store": request.app.state.store})
        return Response(data["csv"], media_type="text/csv",
                        headers={"Content-Disposition": "attachment; filename=evidence.csv"})
    data = export_version(request.app.state.store, version=version, run_id=run_id)
    return Response(data["csv"], media_type="text/csv",
                    headers={"Content-Disposition":
                             f"attachment; filename=buckets-v{data['version']}.csv",
                             "X-Run-Id": data["run_id"], "X-Frozen": "1"})
