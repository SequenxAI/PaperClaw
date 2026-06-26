import json
import mimetypes
import subprocess
import sys

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from paperclaw import iterative_pipeline, jobs, llm, research_pipeline, service
from paperclaw.server.models import (
    AddReferenceRequest,
    ExperimentJob,
    HypothesisDetail,
    HypothesisMap,
    Idea,
    IdeaDomains,
    IdeaDomainsUpdate,
    IdeaLocation,
    IdeaResources,
    IdeaResourcesUpdate,
    IdeaSpec,
    PaperContent,
    PhasePartial,
    ReferencesView,
    ReferenceValidation,
    VenueUpload,
    WorkspaceEntry,
    WorkspaceListing,
)

router = APIRouter(prefix="/api/ideas", tags=["ideas"])


class IdeaCreate(BaseModel):
    title: str
    description: str | None = None


class SpecUpdate(BaseModel):
    content: str


class ChildHypothesis(BaseModel):
    statement: str


class AutoResearchRequest(BaseModel):
    restart: bool = False


class IterativeResearchRequest(BaseModel):
    restart: bool = False
    max_hypotheses: int = Field(default=4, alias="maxHypotheses")
    page_limit: int = Field(default=9, alias="pageLimit")

    model_config = {"populate_by_name": True}


@router.get("", response_model=list[Idea])
def list_ideas(request: Request):
    return request.app.state.store.list_ideas()


@router.post("", response_model=Idea, status_code=201)
def create_idea(body: IdeaCreate, request: Request):
    title = body.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="Idea title must not be empty")
    return request.app.state.store.add_idea(title, body.description)


@router.put("/{idea_id}/activate", response_model=Idea)
def activate_idea(idea_id: str, request: Request):
    idea = request.app.state.store.set_active_idea(idea_id)
    if idea is None:
        raise HTTPException(status_code=404, detail="Idea not found")
    return idea


class IdeaColor(BaseModel):
    color: str | None = None


@router.put("/{idea_id}/color", response_model=Idea)
def set_idea_color(idea_id: str, body: IdeaColor, request: Request):
    """Flag an idea with a sidebar colour (green | yellow | grey | null to clear)."""
    idea = request.app.state.store.set_idea_color(idea_id, body.color)
    if idea is None:
        raise HTTPException(status_code=404, detail="Idea not found, or invalid color")
    return idea


@router.post("/{idea_id}/duplicate", response_model=Idea, status_code=201)
def duplicate_idea(idea_id: str, request: Request):
    """Fork an idea: a new idea copying its IDEA.md spec + ref.bib, titled "(copy)"."""
    idea = request.app.state.store.duplicate_idea(idea_id)
    if idea is None:
        raise HTTPException(status_code=404, detail="Idea not found")
    return idea


@router.delete("/{idea_id}", status_code=204)
def delete_idea(idea_id: str, request: Request):
    if not request.app.state.store.remove_idea(idea_id):
        raise HTTPException(status_code=404, detail="Idea not found")


@router.post("/{idea_id}/reveal", response_model=IdeaLocation)
def reveal_idea(idea_id: str, request: Request):
    """Open the idea's folder in the OS file manager (works when the backend
    runs on the user's machine, e.g. the desktop build). Always returns the
    absolute path so the frontend can show/copy it on headless servers."""
    path = request.app.state.store.idea_path(idea_id)
    if path is None:
        raise HTTPException(status_code=404, detail="Idea not found")

    opener = {
        "darwin": ["open"],
        "win32": ["explorer"],
    }.get(sys.platform, ["xdg-open"])
    opened = False
    try:
        subprocess.Popen(
            [*opener, str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        opened = True
    except OSError:
        pass
    return IdeaLocation(ideaId=idea_id, path=str(path), opened=opened)


@router.get("/{idea_id}/spec", response_model=IdeaSpec)
def get_spec(idea_id: str, request: Request):
    content = request.app.state.store.get_spec(idea_id)
    if content is None:
        raise HTTPException(status_code=404, detail="Idea not found")
    return IdeaSpec(ideaId=idea_id, content=content)


@router.get("/{idea_id}/benchmarks")
def list_idea_benchmarks(idea_id: str, request: Request):
    """Benchmark templates available to this idea (idea scope + its domain + global) —
    the Spec tab → Benchmark picker."""
    return service.list_idea_benchmarks(request.app.state.store, idea_id)


@router.get("/{idea_id}/benchmark")
def get_idea_benchmark(idea_id: str, request: Request, name: str | None = None):
    """A benchmark for the Spec tab → Benchmark view — a specific ``name`` (idea > domain
    > global) or, with none, the one in force. {"name": <title|None>, "content": <md|None>}."""
    return service.get_idea_benchmark(request.app.state.store, idea_id, name)


@router.put("/{idea_id}/spec", response_model=IdeaSpec)
def put_spec(idea_id: str, body: SpecUpdate, request: Request):
    if not request.app.state.store.put_spec(idea_id, body.content):
        raise HTTPException(status_code=404, detail="Idea not found")
    return IdeaSpec(ideaId=idea_id, content=body.content)


@router.get("/{idea_id}/domains", response_model=IdeaDomains)
def get_idea_domains(idea_id: str, request: Request):
    """The domains this idea is connected to (the Spec-tab domain connector)."""
    try:
        return service.get_idea_domains(request.app.state.store, idea_id)
    except service.NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.put("/{idea_id}/domains", response_model=IdeaDomains)
def set_idea_domains(idea_id: str, body: IdeaDomainsUpdate, request: Request):
    """Connect the idea to a set of domains (mounts each under ./domains/<name>)."""
    try:
        return service.set_idea_domains(request.app.state.store, idea_id, body.domain_ids)
    except service.NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/{idea_id}/resources", response_model=IdeaResources)
def get_idea_resources(idea_id: str, request: Request):
    """The idea's allocated experiment resources (compute + active LLM) for the Resources sub-tab."""
    try:
        return service.get_idea_resources_view(
            request.app.state.store, request.app.state.settings, idea_id)
    except service.NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.put("/{idea_id}/resources", response_model=IdeaResources)
def set_idea_resources(idea_id: str, body: IdeaResourcesUpdate, request: Request):
    """Allocate the idea's experiment resources — used by every experiment of this idea (auto
    or manual from the Hypotheses tab)."""
    try:
        service.set_idea_resources(
            request.app.state.store, idea_id,
            experiment_mode=body.experiment_mode, ssh_target_id=body.ssh_target_id,
            use_reference_codebase=body.use_reference_codebase)
        return service.get_idea_resources_view(
            request.app.state.store, request.app.state.settings, idea_id)
    except service.NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/{idea_id}/save-phase-partial", status_code=204)
def save_phase_partial(idea_id: str, body: PhasePartial, request: Request):
    """Persist a partially-generated phase output so Continue can skip it next run.

    Called by the frontend when the user clicks Stop mid-phase. The raw streaming
    content (possibly without a closing fence) is stripped and saved as the phase
    artifact file so the pipeline treats it as complete on resume.
    """
    idea_path = request.app.state.store.idea_path(idea_id)
    if idea_path is None:
        raise HTTPException(status_code=404, detail="Idea not found")
    saved = research_pipeline.save_phase_partial(idea_path, body.phase, body.content)
    if not saved:
        raise HTTPException(status_code=422, detail="Unknown phase or empty content")


@router.post("/{idea_id}/auto-research-stream")
async def auto_research_stream(idea_id: str, request: Request, body: AutoResearchRequest = AutoResearchRequest()):
    """SSE stream for the 4-phase autonomous research pipeline.

    Phases: plan → experiment → analysis → paper.
    Resumes from saved phase artifacts unless ``restart`` is true.
    Events: phase | delta | phase_done | spec_updated | paper_ready | done | error
    """
    store = request.app.state.store
    settings = request.app.state.settings

    async def event_gen():
        async for event in research_pipeline.stream_auto_research_events(
            store, settings, idea_id, restart=body.restart
        ):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@router.post("/{idea_id}/iterative-research-stream")
async def iterative_research_stream(
    idea_id: str, request: Request, body: IterativeResearchRequest = IterativeResearchRequest()
):
    """SSE stream for the iterative hypothesis-loop pipeline.

    Loop: propose hypothesis → experiment (test) → reflect → … until "enough",
    then select a subset of results and write/compile a LaTeX paper with a
    page-limit check. Resumes from per-round artifacts unless ``restart``.
    Events: round | phase | delta | phase_done | round_done | compile |
    page_check | paper_ready | done | error | needs_domain
    """
    store = request.app.state.store
    settings = request.app.state.settings

    async def event_gen():
        async for event in iterative_pipeline.stream_iterative_research_events(
            store, settings, idea_id, restart=body.restart,
            max_hypotheses=body.max_hypotheses, page_limit=body.page_limit,
        ):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream")


# ── References (ref.bib) ──────────────────────────────────────────────────────

@router.post("/{idea_id}/venue/upload")
def upload_venue_template(idea_id: str, body: VenueUpload, request: Request):
    """Upload a LaTeX venue template into the idea's venue/ dir (zip extracted, or a
    single style/class/tex file). The paper stage then bases the paper on it."""
    import base64
    try:
        data = base64.b64decode(body.content_base64, validate=False)
    except Exception:
        raise HTTPException(status_code=422, detail="content is not valid base64")
    try:
        return service.upload_venue_file(request.app.state.store, idea_id, body.filename, data)
    except service.NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.get("/{idea_id}/references", response_model=ReferencesView)
def get_references(idea_id: str, request: Request):
    try:
        return service.get_references(request.app.state.store, idea_id)
    except service.NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/{idea_id}/references/add", response_model=ReferencesView)
async def add_reference(idea_id: str, body: AddReferenceRequest, request: Request):
    try:
        return await service.add_reference(
            request.app.state.store, idea_id, doi=body.doi, query=body.query
        )
    except service.NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.post("/{idea_id}/references/generate", response_model=ReferencesView)
async def generate_references(idea_id: str, request: Request):
    try:
        return await service.generate_references(
            request.app.state.store, request.app.state.settings, idea_id
        )
    except service.NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except (llm.LLMNotConfigured, llm.LLMError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{idea_id}/references/validate", response_model=list[ReferenceValidation])
async def validate_references(idea_id: str, request: Request):
    try:
        return await service.validate_references(request.app.state.store, idea_id)
    except service.NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))


# ── Hypothesis map ────────────────────────────────────────────────────────────

@router.get("/{idea_id}/hypothesis-map", response_model=HypothesisMap)
def get_hypothesis_map(idea_id: str, request: Request):
    try:
        return service.get_hypothesis_map(request.app.state.store, idea_id)
    except service.NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/{idea_id}/hypothesis-map/generate", response_model=HypothesisMap)
async def generate_hypothesis_map(idea_id: str, request: Request):
    try:
        return await service.generate_hypothesis_map(
            request.app.state.store, request.app.state.settings, idea_id
        )
    except service.NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except (llm.LLMNotConfigured, llm.LLMError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/{idea_id}/hypotheses/{hypothesis_id}", response_model=HypothesisDetail)
def get_hypothesis_detail(idea_id: str, hypothesis_id: str, request: Request):
    try:
        return service.get_hypothesis_detail(request.app.state.store, idea_id, hypothesis_id)
    except service.NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.delete("/{idea_id}/hypotheses/{hypothesis_id}", response_model=HypothesisMap)
def delete_hypothesis(idea_id: str, hypothesis_id: str, request: Request):
    """Remove a hypothesis node (and its subtree + workspace) from the map."""
    try:
        return service.delete_hypothesis_node(request.app.state.store, idea_id, hypothesis_id)
    except service.NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/{idea_id}/hypotheses/{hypothesis_id}/plan", response_model=HypothesisDetail)
async def generate_hypothesis_plan(idea_id: str, hypothesis_id: str, request: Request):
    try:
        return await service.generate_hypothesis_plan(
            request.app.state.store, request.app.state.settings, idea_id, hypothesis_id
        )
    except service.NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except (llm.LLMNotConfigured, llm.LLMError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{idea_id}/hypotheses/{hypothesis_id}/experiment", response_model=HypothesisDetail)
async def run_hypothesis_experiment(idea_id: str, hypothesis_id: str, request: Request):
    try:
        return await service.run_hypothesis_experiment(
            request.app.state.store, request.app.state.settings, idea_id, hypothesis_id
        )
    except service.NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except (llm.LLMNotConfigured, llm.LLMError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{idea_id}/hypotheses/{hypothesis_id}/experiment/start", response_model=ExperimentJob)
def start_experiment_job(idea_id: str, hypothesis_id: str, request: Request):
    """Start (or re-attach to) the DETACHED experiment process for this hypothesis.
    Returns immediately; the process runs independently of the backend, with no
    timeout. Idempotent while a job is alive."""
    try:
        return jobs.start_experiment_job(request.app.state.store, idea_id, hypothesis_id)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/{idea_id}/hypotheses/{hypothesis_id}/experiment/rerun", response_model=ExperimentJob)
def rerun_experiment(idea_id: str, hypothesis_id: str, request: Request):
    """Re-run / retest a hypothesis: clear its experiment + verdict artifacts, reset it to
    untested, and start a fresh detached job (right-click → Rerun, e.g. for an inconclusive one)."""
    try:
        return service.rerun_hypothesis_experiment(request.app.state.store, idea_id, hypothesis_id)
    except service.NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/{idea_id}/hypotheses/{hypothesis_id}/children", response_model=HypothesisMap)
def add_child_hypothesis(idea_id: str, hypothesis_id: str, body: ChildHypothesis, request: Request):
    """Add a sub-hypothesis under this node (right-click → Add sub-hypothesis)."""
    if not body.statement.strip():
        raise HTTPException(status_code=422, detail="statement is required")
    try:
        return service.add_child_hypothesis(request.app.state.store, idea_id, hypothesis_id, body.statement)
    except service.NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/{idea_id}/hypotheses/{hypothesis_id}/experiment/job", response_model=ExperimentJob)
def get_experiment_job(idea_id: str, hypothesis_id: str, request: Request):
    job = jobs.experiment_job(request.app.state.store, idea_id, hypothesis_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No experiment job for this hypothesis")
    return job


@router.post("/{idea_id}/hypotheses/{hypothesis_id}/experiment/cancel", response_model=ExperimentJob)
def cancel_experiment_job(idea_id: str, hypothesis_id: str, request: Request):
    job = jobs.cancel_experiment_job(request.app.state.store, idea_id, hypothesis_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No experiment job for this hypothesis")
    return job


@router.get("/{idea_id}/hypotheses/{hypothesis_id}/experiment/stream")
async def stream_experiment_log(idea_id: str, hypothesis_id: str, request: Request,
                                from_line: int = Query(0, alias="from")):
    """SSE: replay the detached job's event log from *from* then stream live — works
    for an in-flight run, a re-attach after reload, or after a backend restart."""
    store = request.app.state.store

    async def event_gen():
        async for event in jobs.tail_experiment_events(store, idea_id, hypothesis_id, from_line):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@router.get("/{idea_id}/paper-content", response_model=PaperContent)
def get_paper_content(idea_id: str, request: Request, version: int | None = None):
    """Return a paper version's Markdown for in-app rendering (latest if `version`
    is omitted), plus the list of available versions for the selector.

    Always 200 for a valid idea — `content` is null when no paper exists yet,
    so the frontend can decide whether to show the Paper tab without 404 noise.
    """
    store = request.app.state.store
    path = store.idea_path(idea_id)
    if path is None:
        raise HTTPException(status_code=404, detail="Idea not found")

    pdf, md, tex = store.paper_artifacts(idea_id, version)
    if pdf is not None:
        content, shown = None, pdf
    elif md is not None:
        content, shown = md.read_text(encoding="utf-8"), md
    elif tex is not None:  # tex-only version (e.g. compile failed) — show the source
        content, shown = "```latex\n" + tex.read_text(encoding="utf-8") + "\n```", tex
    else:
        content, shown = None, None
    return PaperContent(ideaId=idea_id, content=content, hasPdf=pdf is not None,
                        paperFile=(shown.name if shown else None),
                        versionCount=store.max_paper_version(idea_id),
                        versions=store.paper_version_list(idea_id))


@router.get("/{idea_id}/paper")
def get_paper(idea_id: str, request: Request, download: bool = False, version: int | None = None):
    """Serve a paper version (latest if `version` omitted) — PDF if available, else
    Markdown. Served INLINE by default (so the Paper-tab iframe renders the PDF);
    `?download=1` forces an attachment for the download button."""
    path = request.app.state.store.idea_path(idea_id)
    if path is None:
        raise HTTPException(status_code=404, detail="Idea not found")

    disp = "attachment" if download else "inline"
    pdf, md, tex = request.app.state.store.paper_artifacts(idea_id, version)
    for f, mt in ((pdf, "application/pdf"), (md, "text/markdown"), (tex, "text/x-tex")):
        if f is not None:
            return FileResponse(str(f), media_type=mt, filename=f.name, content_disposition_type=disp)

    raise HTTPException(status_code=404, detail="No paper generated yet — run Auto research first")


@router.get("/{idea_id}/files", response_model=WorkspaceListing)
def list_workspace_files(idea_id: str, request: Request, path: str = Query("")):
    """List the idea's workspace files (code, figures, results, logs) so the UI/CLI
    can browse the directory. ``path`` scopes the listing to a subdir."""
    entries = request.app.state.store.list_idea_files(idea_id, path)
    if entries is None:
        raise HTTPException(status_code=404, detail="Idea or path not found")
    return WorkspaceListing(
        ideaId=idea_id, root=path,
        entries=[WorkspaceEntry(**e) for e in entries],
    )


@router.get("/{idea_id}/raw")
def get_workspace_file(idea_id: str, request: Request, path: str = Query(...)):
    """Serve a single raw workspace file (image, code, json, log…) by relative
    path — what makes inline figures render and lets the browser preview files."""
    fp = request.app.state.store.idea_file(idea_id, path)
    if fp is None:
        raise HTTPException(status_code=404, detail="File not found")
    media = mimetypes.guess_type(fp.name)[0] or "application/octet-stream"
    return FileResponse(str(fp), media_type=media, filename=fp.name)
