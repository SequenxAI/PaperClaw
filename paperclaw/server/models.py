"""Pydantic models shared across API routes (mirror frontend/src/types)."""

from typing import Literal, Optional

from pydantic import BaseModel, Field


class Domain(BaseModel):
    """A research domain — the ground brainstorming digests, backed by DOMAIN.md."""

    id: str
    name: str
    created_at: float = Field(alias="createdAt")
    is_selected: bool = Field(default=True, alias="isSelected")
    # Reference codebase: the domain's downloaded canonical implementation, reused by
    # experiments for ideas pinned to this domain.
    codebase_url: Optional[str] = Field(default=None, alias="codebaseUrl")
    codebase_files: int = Field(default=0, alias="codebaseFiles")

    model_config = {"populate_by_name": True}


class DomainSpec(BaseModel):
    domain_id: str = Field(alias="domainId")
    content: str

    model_config = {"populate_by_name": True}


class Seed(BaseModel):
    """A brainstorm seed — a spark or a full idea draft awaiting /pin_idea."""

    id: str
    text: str
    created_at: float = Field(alias="createdAt")
    draft: Optional[str] = None  # full IDEA.md draft when brainstormed from domains

    model_config = {"populate_by_name": True}


class Idea(BaseModel):
    """A research idea — the first-class unit of work, backed by an IDEA.md spec."""

    id: str
    title: str
    description: Optional[str] = None
    created_at: float = Field(alias="createdAt")
    is_active: bool = Field(default=False, alias="isActive")
    # A user flag/label colour for the sidebar (e.g. green = done, yellow = in progress,
    # grey = parked). None = unflagged.
    color: Optional[str] = None

    model_config = {"populate_by_name": True}


class IdeaSpec(BaseModel):
    idea_id: str = Field(alias="ideaId")
    content: str

    model_config = {"populate_by_name": True}


class IdeaDomains(BaseModel):
    """The domains an idea is connected to (an idea may connect to several)."""

    idea_id: str = Field(alias="ideaId")
    domain_ids: list[str] = Field(default_factory=list, alias="domainIds")

    model_config = {"populate_by_name": True}


class IdeaDomainsUpdate(BaseModel):
    """Request body to set an idea's connected domains."""

    domain_ids: list[str] = Field(default_factory=list, alias="domainIds")

    model_config = {"populate_by_name": True}


class IdeaResources(BaseModel):
    """An idea's allocated experiment resources — the compute (mode + SSH GPU host) every
    experiment of this idea uses (auto OR manual), plus the active LLM (read-only) and the
    SSH remotes available to pick from."""

    idea_id: str = Field(alias="ideaId")
    experiment_mode: Optional[str] = Field(default=None, alias="experimentMode")
    ssh_target_id: Optional[str] = Field(default=None, alias="sshTargetId")
    use_reference_codebase: bool = Field(default=True, alias="useReferenceCodebase")
    ssh_targets: list["SSHTarget"] = Field(default_factory=list, alias="sshTargets")
    llm_provider: Optional[str] = Field(default=None, alias="llmProvider")
    llm_model: Optional[str] = Field(default=None, alias="llmModel")
    llm_base_url: Optional[str] = Field(default=None, alias="llmBaseUrl")
    llm_key_configured: bool = Field(default=False, alias="llmKeyConfigured")

    model_config = {"populate_by_name": True}


class IdeaResourcesUpdate(BaseModel):
    """Request body to set an idea's allocated resources (any omitted field is unchanged)."""

    experiment_mode: Optional[str] = Field(default=None, alias="experimentMode")
    ssh_target_id: Optional[str] = Field(default=None, alias="sshTargetId")
    use_reference_codebase: Optional[bool] = Field(default=None, alias="useReferenceCodebase")

    model_config = {"populate_by_name": True}


class PaperContent(BaseModel):
    """Generated paper Markdown for an idea (content is null if none yet)."""

    idea_id: str = Field(alias="ideaId")
    content: Optional[str] = None
    has_pdf: bool = Field(default=False, alias="hasPdf")
    paper_file: Optional[str] = Field(default=None, alias="paperFile")  # the shown version's filename
    version_count: int = Field(default=0, alias="versionCount")
    versions: list[int] = []  # all available paper versions, e.g. [1, 2, 3]

    model_config = {"populate_by_name": True}


MessageRole = Literal["user", "assistant", "system"]


class Message(BaseModel):
    id: str
    role: MessageRole
    content: str
    timestamp: float
    status: Optional[Literal["sending", "sent", "streaming", "error"]] = None
    status_message: Optional[str] = Field(default=None, alias="statusMessage")
    spec_updated: bool = Field(default=False, alias="specUpdated")
    map_updated: bool = Field(default=False, alias="mapUpdated")    # hypothesis map regenerated
    paper_updated: bool = Field(default=False, alias="paperUpdated")  # paper.md (re)written
    codebase_updated: bool = Field(default=False, alias="codebaseUpdated")  # domain ref codebase changed
    served_model: Optional[str] = Field(default=None, alias="servedModel")
    created_idea_id: Optional[str] = Field(default=None, alias="createdIdeaId")
    created_domain_id: Optional[str] = Field(default=None, alias="createdDomainId")
    question: Optional[dict] = None  # {"prompt", "options", "allowFreeText"}
    # Streamed activity feed, PERSISTED so it survives a reload / context switch:
    # the chained tool-call timeline (text + tool rows), the reasoning, and the plan.
    thinking: Optional[str] = None
    parts: Optional[list[dict]] = None   # [{kind:"text",text} | {kind:"tool",name,arg?,detail?}]
    todos: Optional[list[dict]] = None   # [{content, status}]

    model_config = {"populate_by_name": True}


ResourceType = Literal["paper", "link", "dataset", "code"]


class Resource(BaseModel):
    id: str
    type: ResourceType
    title: str
    authors: Optional[list[str]] = None
    year: Optional[int] = None
    url: Optional[str] = None
    venue: Optional[str] = None
    summary: Optional[str] = None
    relevance: Optional[int] = None


class Reference(BaseModel):
    """One parsed BibTeX entry from an idea's ref.bib."""

    key: str
    type: str = "article"
    title: str = ""
    authors: list[str] = []
    year: Optional[int] = None
    doi: Optional[str] = None
    venue: Optional[str] = None


class ReferenceValidation(BaseModel):
    key: str
    status: Literal["VERIFIED", "MISMATCH", "NOT_FOUND", "UNKNOWN"]
    detail: str = ""


class ReferencesView(BaseModel):
    idea_id: str = Field(alias="ideaId")
    entries: list[Reference] = []
    bibtex: str = ""

    model_config = {"populate_by_name": True}


class AddReferenceRequest(BaseModel):
    doi: Optional[str] = None
    query: Optional[str] = None

    model_config = {"populate_by_name": True}


class HypothesisNode(BaseModel):
    """One node in an idea's hypothesis map (root hypotheses → sub-hypotheses)."""

    id: str
    statement: str
    rationale: Optional[str] = None
    test: Optional[str] = None  # one-line way to test it / decision criterion
    status: str = "untested"    # untested | supported | refuted | inconclusive | blocked
    # DERIVED at serve time (not persisted): the node's current PROGRESS stage so the
    # map shows "planned"/"experiment" for an untested node that's actually mid-run,
    # not a flat "untested". Verdicted/blocked nodes mirror `status`.
    stage: Optional[str] = None  # untested | planned | experiment | supported | refuted | inconclusive | blocked
    children: list["HypothesisNode"] = []


class HypothesisMap(BaseModel):
    idea_id: str = Field(alias="ideaId")
    nodes: list[HypothesisNode] = []
    generated_at: float = Field(default=0.0, alias="generatedAt")

    model_config = {"populate_by_name": True}


HypothesisNode.model_rebuild()  # resolve the self-referencing 'children' forward ref


class WorkspaceEntry(BaseModel):
    """One file or directory in an idea's workspace (relative path + size)."""

    path: str  # relative to the idea root, posix-style
    size: int = 0
    is_dir: bool = Field(default=False, alias="isDir")

    model_config = {"populate_by_name": True}


class WorkspaceListing(BaseModel):
    """A listing of an idea's workspace (or a subdir of it) for the file browser."""

    idea_id: str = Field(alias="ideaId")
    root: str = ""  # the subdir this listing is rooted at ("" = idea root)
    entries: list[WorkspaceEntry] = []

    model_config = {"populate_by_name": True}


class HypothesisDetail(BaseModel):
    """A single hypothesis's per-directory pipeline artifacts (#6)."""

    idea_id: str = Field(alias="ideaId")
    hypothesis_id: str = Field(alias="hypothesisId")
    status: str = "untested"  # + blocked when the plan is infeasible
    plan: Optional[str] = None
    code: Optional[str] = None        # phase 1: generated run.py (executed mode)
    experiment: Optional[str] = None  # phase 2: run results (markdown)
    report: Optional[str] = None
    log: Optional[str] = None  # executed-run stdout/stderr
    figures: list[str] = []

    model_config = {"populate_by_name": True}


class ChatContext(BaseModel):
    """A conversation entry in the history browser."""

    context_id: str = Field(alias="contextId")
    kind: Literal["scratch", "domain", "seed", "idea"]
    title: str
    message_count: int = Field(alias="messageCount")
    last_timestamp: float = Field(alias="lastTimestamp")

    model_config = {"populate_by_name": True}


class Skill(BaseModel):
    """A chat slash-command exposed to the user."""

    command: str
    description: str
    # only useful with an active idea (the / menu hides it otherwise)
    requires_idea: bool = Field(default=False, alias="requiresIdea")

    model_config = {"populate_by_name": True}


class PhasePartial(BaseModel):
    """Partial streaming content for a pipeline phase, sent when the user stops mid-run."""

    phase: str
    content: str


class IdeaLocation(BaseModel):
    idea_id: str = Field(alias="ideaId")
    path: str
    opened: bool

    model_config = {"populate_by_name": True}


class SettingsView(BaseModel):
    """Settings as exposed to the frontend — API key masked."""

    provider: str
    base_url: Optional[str] = Field(default=None, alias="baseUrl")
    model: str
    api_key_masked: str = Field(default="", alias="apiKeyMasked")
    has_key: bool = Field(default=False, alias="hasKey")
    # Image generation (paper figures)
    image_base_url: Optional[str] = Field(default=None, alias="imageBaseUrl")
    image_model: Optional[str] = Field(default=None, alias="imageModel")
    image_key_masked: str = Field(default="", alias="imageKeyMasked")
    has_image_key: bool = Field(default=False, alias="hasImageKey")
    # OpenAlex literature search (api key masked)
    openalex_key_masked: str = Field(default="", alias="openalexKeyMasked")
    has_openalex_key: bool = Field(default=False, alias="hasOpenalexKey")

    model_config = {"populate_by_name": True}


class SettingsUpdate(BaseModel):
    provider: Optional[str] = None
    base_url: Optional[str] = Field(default=None, alias="baseUrl")
    model: Optional[str] = None
    api_key: Optional[str] = Field(default=None, alias="apiKey")
    image_base_url: Optional[str] = Field(default=None, alias="imageBaseUrl")
    image_model: Optional[str] = Field(default=None, alias="imageModel")
    image_api_key: Optional[str] = Field(default=None, alias="imageApiKey")
    openalex_api_key: Optional[str] = Field(default=None, alias="openalexApiKey")

    model_config = {"populate_by_name": True}


# ── Doctor / environment readiness ───────────────────────────────────────────


class EnvCheck(BaseModel):
    """One environment readiness check for `doctor`."""

    key: str
    label: str
    status: str  # "ok" | "warn" | "fail"
    detail: str = ""
    hint: Optional[str] = None  # how to fix, when not ok

    model_config = {"populate_by_name": True}


class DoctorReport(BaseModel):
    ok: bool  # True when no check failed (warnings allowed)
    checks: list[EnvCheck]

    model_config = {"populate_by_name": True}


# ── Writing styles (prose-style guides for paper writing) ─────────────────────


class WritingStyle(BaseModel):
    name: str
    scope: str  # "global" | "domain"
    title: str

    model_config = {"populate_by_name": True}


class Benchmark(BaseModel):
    """A benchmark template — a fixed protocol + a published, cited leaderboard."""

    name: str
    scope: str  # "global" | "domain"
    title: str

    model_config = {"populate_by_name": True}


# ── Experiment jobs (detached, monitored) ────────────────────────────────────


class ExperimentJob(BaseModel):
    job_id: str = Field(alias="jobId")
    idea_id: str = Field(alias="ideaId")
    idea_title: str = Field(default="", alias="ideaTitle")
    hypothesis_id: str = Field(alias="hypothesisId")
    # running (PID alive) | done | error | cancelled | interrupted (running but PID gone)
    status: Literal["running", "done", "error", "cancelled", "interrupted"]
    started_at: float = Field(default=0.0, alias="startedAt")
    updated_at: float = Field(default=0.0, alias="updatedAt")
    error: Optional[str] = None

    model_config = {"populate_by_name": True}


class AutoRunStatus(BaseModel):
    """Live status of an `paperclaw auto` run — the OVERALL pipeline phase, so the
    CLI (`auto status`) and web UI can watch it from outside the launching terminal."""

    topic: str
    # running | done | error | stopped (Ctrl+C) | interrupted (process gone) — both resumable
    status: Literal["running", "done", "error", "stopped", "interrupted"] = "running"
    # doctor | domain | idea | hypotheses | paper | done
    phase: str = "doctor"
    label: str = ""  # the latest phase label
    idea_id: Optional[str] = Field(default=None, alias="ideaId")
    idea_title: Optional[str] = Field(default=None, alias="ideaTitle")
    domain_id: Optional[str] = Field(default=None, alias="domainId")
    domain_name: Optional[str] = Field(default=None, alias="domainName")
    round: int = 0
    positives: int = 0
    target_positive: int = Field(default=2, alias="targetPositive")
    max_hypotheses: int = Field(default=6, alias="maxHypotheses")
    page_limit: int = Field(default=8, alias="pageLimit")
    max_depth: int = Field(default=3, alias="maxDepth")  # hypothesis-map depth cap
    current_hypothesis_id: Optional[str] = Field(default=None, alias="currentHypothesisId")
    paper_ready: bool = Field(default=False, alias="paperReady")
    started_at: float = Field(default=0.0, alias="startedAt")
    updated_at: float = Field(default=0.0, alias="updatedAt")
    error: Optional[str] = None

    model_config = {"populate_by_name": True}


class AutoRunLog(BaseModel):
    """A chunk of a detached auto-run's live log (for the agent-feedback panel)."""

    text: str
    next: int
    running: bool


class VenueUpload(BaseModel):
    """Upload a LaTeX venue template (a `.zip` export, or a single `.sty/.cls/.tex`)
    into an idea's venue/ dir — base64 so binary zips ride a plain JSON body."""

    filename: str
    content_base64: str = Field(alias="contentBase64")

    model_config = {"populate_by_name": True}


class AutoRunStart(BaseModel):
    """Settings to launch an `auto` run from the web UI (mirrors the CLI flags).

    Either *topic* (create a new domain + idea) or *ideaId* (auto-run an EXISTING
    idea — the per-idea ⚡ Auto run button) is required."""

    topic: str = ""
    idea_id: Optional[str] = Field(default=None, alias="ideaId")
    positive: int = 2
    max_hypotheses: int = Field(default=6, alias="maxHypotheses")
    page_limit: int = Field(default=8, alias="pageLimit")
    max_depth: int = Field(default=3, alias="maxDepth")  # hypothesis-map depth cap
    # Per-run overrides from the web UI's Auto settings window (None = use the global
    # RunConfig / default). experimentMode + sshTargetId pick the environment + coding
    # agent for THIS run; writingStyle injects a prose-style guide into the paper;
    # useReferenceCodebase False skips reusing the domain's downloaded reference repo.
    experiment_mode: Optional[str] = Field(default=None, alias="experimentMode")
    ssh_target_id: Optional[str] = Field(default=None, alias="sshTargetId")
    writing_style: Optional[str] = Field(default=None, alias="writingStyle")
    benchmark: Optional[str] = Field(default=None, alias="benchmark")  # benchmark template name
    use_reference_codebase: bool = Field(default=True, alias="useReferenceCodebase")
    # iteratively verify (read_pdf) the main text fills the page limit (ends at the
    # last allowed page) — the paper page-fill compliance check.
    fill_page: bool = Field(default=False, alias="fillPage")

    model_config = {"populate_by_name": True}


# ── Hardware / environment ───────────────────────────────────────────────────
# Detected compute resources available for running experiments. The "local"
# machine is whichever host runs the backend/CLI; remotes are reached over SSH.


class SSHTarget(BaseModel):
    """A remote machine reachable over SSH (key-based — no password stored)."""

    id: str
    host: str
    user: str
    port: int = 22
    key_path: Optional[str] = Field(default=None, alias="keyPath")
    label: Optional[str] = None

    model_config = {"populate_by_name": True}


class GpuInfo(BaseModel):
    name: str
    memory_total_mb: Optional[int] = Field(default=None, alias="memoryTotalMb")

    model_config = {"populate_by_name": True}


class DiskInfo(BaseModel):
    name: str
    model: Optional[str] = None
    size_gb: Optional[float] = Field(default=None, alias="sizeGb")
    kind: str = "unknown"  # SSD | HDD | NVMe | unknown
    transport: Optional[str] = None

    model_config = {"populate_by_name": True}


class HardwareInfo(BaseModel):
    """One machine's detected resources (a local host or an SSH remote)."""

    scope: Literal["local", "remote"]
    label: str  # "local" or "user@host"
    reachable: bool = True
    error: Optional[str] = None
    os: Optional[str] = None
    cpu_model: Optional[str] = Field(default=None, alias="cpuModel")
    cpu_cores: Optional[int] = Field(default=None, alias="cpuCores")
    cpu_threads: Optional[int] = Field(default=None, alias="cpuThreads")
    mem_total_gb: Optional[float] = Field(default=None, alias="memTotalGb")
    gpus: list[GpuInfo] = []
    disks: list[DiskInfo] = []
    detected_at: float = Field(default=0.0, alias="detectedAt")

    model_config = {"populate_by_name": True}


class RunConfig(BaseModel):
    """How experiment code is executed. ``simulated`` keeps the original
    LLM-narrated pipeline; ``executed`` runs the agentic coding agent on the local host;
    ``ssh`` runs that SAME agentic bash loop ON a configured SSH remote — it needs only
    ``ssh_target_id`` (the SSH connection; no interpreter / extra settings, since the agent
    sets up the env and runs everything with bash on the remote); ``cli`` shells out to an
    external headless coding-agent CLI (``agent_command``, e.g. ``claude`` / ``opencode`` /
    ``openhands``) and streams its stdout live. Experiments run with NO wall-clock timeout."""

    experiment_mode: Literal["simulated", "executed", "ssh", "cli"] = Field(
        default="simulated", alias="experimentMode"
    )
    # Interpreter for the legacy single-shot runners only; NOT used by the agentic
    # `executed`/`ssh` loop (the agent runs python itself via bash on its host).
    python_path: Optional[str] = Field(default=None, alias="pythonPath")  # None = backend/remote default
    ssh_target_id: Optional[str] = Field(default=None, alias="sshTargetId")
    # Shell command template for ``cli`` mode. Placeholders: {prompt} (shell-quoted
    # task), {task_file} (path to task.md), {dir} (working dir). e.g.
    # "claude -p {prompt} --dangerously-skip-permissions" or "opencode run {prompt}".
    # For `claude -p` the runner auto-adds `--output-format stream-json --verbose` so
    # the agent's steps stream live (plain `-p` prints only the final result).
    agent_command: Optional[str] = Field(default=None, alias="agentCommand")
    max_attempts: int = Field(default=4, alias="maxAttempts")

    model_config = {"populate_by_name": True}


class HardwareView(BaseModel):
    """Persisted hardware snapshot + SSH config, as exposed to the frontend/CLI."""

    machines: list[HardwareInfo] = []
    ssh_targets: list[SSHTarget] = Field(default_factory=list, alias="sshTargets")
    run_config: RunConfig = Field(default_factory=RunConfig, alias="runConfig")
    markdown: Optional[str] = None  # rendered HARDWARE.md
    updated_at: float = Field(default=0.0, alias="updatedAt")

    model_config = {"populate_by_name": True}


class SSHTargetsUpdate(BaseModel):
    ssh_targets: list[SSHTarget] = Field(default_factory=list, alias="sshTargets")

    model_config = {"populate_by_name": True}
