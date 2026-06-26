"""Benchmark-template library routes.

GET  /api/benchmarks[?domainId=]          — list global (+ domain) benchmark templates
GET  /api/benchmarks/{name}[?domainId=]   — one template's markdown
POST /api/benchmarks                      — create/overwrite a template (paste/upload)
"""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from paperclaw import service
from paperclaw.server.models import Benchmark

router = APIRouter(prefix="/api/benchmarks", tags=["benchmarks"])


class BenchmarkSave(BaseModel):
    name: str
    content: str
    domainId: str | None = None


@router.get("", response_model=list[Benchmark])
def list_benchmarks(request: Request, domainId: str | None = None):
    return service.list_benchmarks(request.app.state.store, domainId)


@router.get("/{name}")
def get_benchmark(name: str, request: Request, domainId: str | None = None):
    md = service.get_benchmark(request.app.state.store, domainId, name)
    if md is None:
        raise HTTPException(status_code=404, detail="Benchmark not found")
    return {"name": name, "content": md}


@router.post("")
def save_benchmark(body: BenchmarkSave, request: Request):
    saved = service.save_benchmark(
        request.app.state.store, body.name, body.content, body.domainId)
    if saved is None:
        raise HTTPException(status_code=422, detail="Invalid benchmark name")
    return {"name": saved}
