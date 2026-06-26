"""Auto-mode run status routes (PER IDEA — ideas can auto-run in parallel).

- GET  /api/auto-runs               — every idea's `paperclaw auto` snapshot
- POST /api/auto-run/start          — launch a run (detached) from a topic or ideaId
- GET  /api/ideas/{id}/auto-run     — one idea's overall pipeline status (or null)
- POST /api/ideas/{id}/auto-run/stop — stop that idea's run

Drives the per-idea ⚡ Auto run banner and the CLI `auto status` / `auto stop`.
"""

from typing import List, Optional

from fastapi import APIRouter, Query, Request

from paperclaw import service
from paperclaw.server.models import AutoRunLog, AutoRunStart, AutoRunStatus

router = APIRouter(tags=["auto"])


@router.get("/api/auto-runs", response_model=List[AutoRunStatus])
def list_auto_runs(request: Request):
    return service.list_auto_runs_view(request.app.state.store)


@router.get("/api/ideas/{idea_id}/auto-run", response_model=Optional[AutoRunStatus])
def get_idea_auto_run(idea_id: str, request: Request):
    state = service.idea_auto_run_view(request.app.state.store, idea_id)
    return AutoRunStatus.model_validate(state) if state else None


@router.get("/api/ideas/{idea_id}/auto-run/log", response_model=AutoRunLog)
def get_idea_auto_run_log(idea_id: str, request: Request, from_: int = Query(0, alias="from")):
    """Tail the idea's detached auto-run log from `?from=` (live agent-feedback feed)."""
    return service.read_auto_run_log(request.app.state.store, idea_id, from_)


@router.post("/api/auto-run/start")
def start_auto_run(body: AutoRunStart, request: Request):
    """Launch an auto run (detached) from the web UI; the idea's banner then tracks it.
    With *ideaId* it auto-runs that existing idea; otherwise it creates one from
    *topic*. Ideas run in parallel — only refuses if THAT idea is already running."""
    return service.launch_auto_run(
        request.app.state.store, request.app.state.home, body.topic,
        idea_id=body.idea_id,
        target_positive=body.positive, max_hypotheses=body.max_hypotheses,
        page_limit=body.page_limit, max_depth=body.max_depth,
        experiment_mode=body.experiment_mode, ssh_target_id=body.ssh_target_id,
        writing_style=body.writing_style, benchmark=body.benchmark,
        use_reference_codebase=body.use_reference_codebase,
        fill_page=body.fill_page)


@router.post("/api/ideas/{idea_id}/auto-run/stop")
def stop_idea_auto_run(idea_id: str, request: Request):
    """Stop a running auto pipeline (cancel its experiment + signal the process)."""
    return service.stop_auto_run(request.app.state.store, idea_id)
