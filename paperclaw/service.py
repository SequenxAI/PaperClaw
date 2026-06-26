"""Shared application logic used by BOTH the HTTP routes and the local CLI.

Anything involving an LLM round-trip lives here so the two surfaces cannot
drift: chat (with all block protocols), brainstorm generation, and auto
domain creation.
"""

import asyncio
import difflib
import json
import os
import re
import shutil
import time
import uuid
from typing import AsyncIterator

from paperclaw import benchmarks, codebase, hardware, literature, llm, references, writing_styles
from paperclaw import tools as _tools
from paperclaw.agents import deep_chat
from paperclaw.config import LLMSettings, paperclaw_home
from paperclaw.domains import (
    AUTO_DOMAIN_SYSTEM,
    DOMAIN_CHAT_SYSTEM,
    DOMAIN_ENRICH_SYSTEM,
    DOMAIN_MATCH_SYSTEM,
    DOMAIN_WIZARD_RULE,
    QUESTION_RULE,
    SUGGESTIONS_SYSTEM,
)
from paperclaw.ideas import (
    BRAINSTORM_DRAFT_SYSTEM,
    BRAINSTORM_SYSTEM,
    CHAT_SYSTEM,
    DOMAIN_REFERENCE_NOTE,
    GENERATE_PLAN_DIRECTIVE,
    GENERATE_REPORT_DIRECTIVE,
    HYPOTHESIS_MAP_DIRECTIVE,
    IDEA_GENERATION_DIRECTIVE,
    SCRATCH_SYSTEM,
    SEED_CHAT_SYSTEM,
    SETUP_VENUE_DIRECTIVE,
    WRITE_PAPER_DIRECTIVE,
)
from paperclaw.prompts.benchmarks import SETUP_BENCHMARK_DIRECTIVE
from paperclaw.prompts.domains import DOMAIN_TOOL_ADDENDUM
from paperclaw.prompts.hardware import HARDWARE_ASSESS_SYSTEM
from paperclaw.prompts.writing_styles import DEFAULT_STYLE
from paperclaw.prompts.ideas import (
    CHAT_TOOL_ADDENDUM,
    DEEP_CHAT_ADDENDUM,
    HYPOTHESIS_MAP_SYSTEM,
    REFERENCE_QUERIES_SYSTEM,
    brainstorm_directive,
)
from paperclaw.server.models import (
    Domain,
    DoctorReport,
    EnvCheck,
    HardwareInfo,
    HardwareView,
    HypothesisDetail,
    HypothesisMap,
    HypothesisNode,
    Idea,
    Message,
    Reference,
    ReferencesView,
    ReferenceValidation,
    RunConfig,
    Seed,
    SSHTarget,
)
from paperclaw.server.store import DOMAIN_PREFIX, SCRATCH_ID, SEED_PREFIX, Store
from paperclaw.skills import (
    GENERATE_PLAN_COMMAND,
    GENERATE_REPORT_COMMAND,
    HYPOTHESIS_MAP_COMMAND,
    IDEA_GENERATION_COMMAND,
    PIN_IDEA_COMMAND,
    SETUP_BENCHMARK_COMMAND,
    SETUP_CODEBASE_COMMAND,
    SETUP_VENUE_COMMAND,
    VALIDATE_REFERENCES_COMMAND,
    WRITE_PAPER_COMMAND,
)


class NotFound(Exception):
    """Entity referenced by the request does not exist."""


def _make_tool_executor(base_dir):
    """Return a callable(tool_name, inputs) -> str | list bound to *base_dir*.
    (Most tools return a string; read_image returns Anthropic content blocks.)"""
    import pathlib
    bd = pathlib.Path(base_dir)

    def executor(name: str, inputs: dict):
        fn = _tools.EXECUTORS.get(name)
        if fn is None:
            return f"Unknown tool: {name!r}"
        return fn(bd, inputs)

    return executor


# ── Model-emitted block protocols (tolerant of tag case / spacing) ──────────
def _block(tag: str) -> re.Pattern:
    return re.compile(r"```\s*" + tag + r"\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


SPEC_BLOCK = _block(r"idea\.md")
NEW_IDEA_BLOCK = _block("new-idea")
NEW_DOMAIN_BLOCK = _block("new-domain")
SEED_DRAFT_BLOCK = _block("seed-draft")
DOMAIN_SPEC_BLOCK = _block(r"domain\.md")
QUESTION_BLOCK = _block("question")
IDEA_DRAFT_BLOCK = _block("idea-draft")

HISTORY_LIMIT = 40
GENERATE_COUNT = 5
DRAFT_COUNT = 3
MAX_GENERATE_COUNT = 12
DOMAIN_SPEC_CHAR_LIMIT = 6000


def _clamp_count(count: int | None, default: int) -> int:
    """Clamp a requested seed count to [1, MAX_GENERATE_COUNT]; None → default."""
    if count is None:
        return default
    return max(1, min(MAX_GENERATE_COUNT, count))


def title_from_spec(content: str, fallback: str = "Untitled idea") -> str:
    for line in content.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


def _parse_question_json(raw: str) -> dict | None:
    """Tolerant parse — models emit smart quotes, single quotes, trailing commas."""
    fixed_quotes = (
        raw.replace("“", '"').replace("”", '"')
        .replace("‘", "'").replace("’", "'")
    )
    no_trailing = re.sub(r",\s*([}\]])", r"\1", fixed_quotes)
    candidates = [raw, fixed_quotes, no_trailing]
    if '"' not in no_trailing and "'" in no_trailing:
        candidates.append(no_trailing.replace("'", '"'))
    for cand in candidates:
        try:
            data = json.loads(cand)
            if isinstance(data, dict) and data.get("prompt"):
                return data
        except (json.JSONDecodeError, TypeError):
            continue
    return None


def _extract_question(reply: str) -> tuple[str, dict | None]:
    match = QUESTION_BLOCK.search(reply)
    if not match:
        return reply, None
    question = None
    data = _parse_question_json(match.group(1).strip())
    if data:
        question = {
            "prompt": str(data["prompt"]),
            "options": [str(o) for o in data.get("options", [])][:5],
            "allowFreeText": bool(data.get("allowFreeText", True)),
        }
    return QUESTION_BLOCK.sub("", reply).strip(), question


# ── Chat ────────────────────────────────────────────────────────────────────
def _cleanup_domain_mounts(store: Store, idea_id: str) -> None:
    """Remove any ``<idea>/domains/<slug>`` (or legacy ``<idea>/domain``) SYMLINKS an
    earlier build created. The deepagents sandbox resolves a read THROUGH such a symlink
    and rejects it ('Path … outside root directory'), which crashed the chat turn — so we
    no longer mount domains as symlinks; the agent reads them via `bash` + absolute paths."""
    idea_dir = store.idea_path(idea_id)
    if idea_dir is None:
        return
    try:
        legacy = idea_dir / "domain"
        if legacy.is_symlink():
            legacy.unlink()
        mount = idea_dir / "domains"
        if mount.is_dir():
            for child in mount.iterdir():
                if child.is_symlink():
                    child.unlink()
            try:
                mount.rmdir()  # drop it if now empty (it only ever held symlinks)
            except OSError:
                pass
    except OSError:
        pass


def _idea_chat_system(store: Store, idea_id: str, spec: str) -> str:
    """System prompt for an idea chat: CHAT_SYSTEM(spec) PLUS, for every CONNECTED domain,
    its DOMAIN.md inlined and its folder's absolute path (read via `bash`, since it's outside
    the sandbox), PLUS the in-force benchmark's published SOTA table — so 'compare to the
    benchmark' uses the cited published numbers, not the idea's own experiment results."""
    system = CHAT_SYSTEM.format(spec=spec)
    domain_ids = resolve_domain_ids_for_idea(store, idea_id)
    _cleanup_domain_mounts(store, idea_id)
    names = {d.id: d.name for d in store.list_domains()}
    blocks = []
    for did in domain_ids:
        md = store.get_domain_spec(did)
        ddir = store.domain_path(did)
        if md and ddir:
            blocks.append(f"### Domain: {names.get(did, '') or did}  (files at `{ddir}` — read with `bash`)\n\n{md}")
    if blocks:  # connected domain(s) → reference note + inline each DOMAIN.md
        system += "\n\n" + DOMAIN_REFERENCE_NOTE + "\n\n".join(blocks)
    bench = idea_benchmark_note(store, idea_id)
    if bench:  # the published, cited SOTA table to compare against (≠ the idea's own results)
        system += "\n\n" + bench
    return system


# Markers of a TRANSIENT model error (network/timeout/5xx/rate-limit/overload) — the kind a
# user can just retry by typing "continue", vs. a genuine config/runtime failure.
_TRANSIENT_ERR_MARKERS = (
    "apiconnectionerror", "apitimeouterror", "connecterror", "connecttimeout",
    "readtimeout", "writetimeout", "pooltimeout", "remoteprotocolerror", "readerror",
    "ratelimiterror", "internalservererror", "serviceunavailable", "apistatuserror",
    "connection error", "connection reset", "timed out", "timeout", "temporarily unavailable",
    "overloaded", "rate limit", "bad gateway", "gateway timeout", " 502", " 503", " 504",
)


def _is_transient_chat_error(exc: Exception) -> bool:
    """True for a network/timeout/5xx/rate-limit error the user can resume from (just retry),
    vs. a genuine config/runtime failure that needs attention."""
    blob = f"{type(exc).__name__}: {exc}".lower()
    return any(m in blob for m in _TRANSIENT_ERR_MARKERS)


def _interrupted_chat_message(acc_parts: list[dict], exc: Exception, deep: bool) -> str:
    """The assistant message to persist when a chat turn fails: the agent's PARTIAL text so far
    (so 'continue' has context) followed by a resume-oriented note. A transient drop gets a soft
    'type continue to resume'; a real failure keeps the actionable hint."""
    partial = "".join(p.get("text", "") for p in acc_parts if p.get("kind") == "text").strip()
    if _is_transient_chat_error(exc):
        note = (f"\n\n⚠️ Lost the connection to the model ({type(exc).__name__}) — usually transient "
                "(it was retried automatically). Your progress above is saved; type **continue** to "
                "resume from where it left off.")
    elif deep:
        note = (f"\n\n⚠️ The chat agent hit an error ({type(exc).__name__}: {exc}). Type **continue** to "
                "retry, or set `chat_agent` back to 'builtin' in Settings/.env if it persists.")
    else:
        note = f"\n\n⚠️ {exc}"
    return (partial + note).strip()


async def send_chat(
    store: Store,
    settings: LLMSettings,
    content: str,
    idea_id: str | None = None,
    seed_id: str | None = None,
    domain_id: str | None = None,
) -> list[Message]:
    """Full chat turn: context resolution, slash commands, LLM call, and all
    block protocols. Returns [user_message, assistant_message]."""
    # Resolve conversation context: domain > seed draft > idea > scratch
    seed = store.get_seed(seed_id) if seed_id else None
    if seed_id and seed is None:
        raise NotFound("Seed not found")
    domain_spec = store.get_domain_spec(domain_id) if domain_id else None
    if domain_id and domain_spec is None:
        raise NotFound("Domain not found")

    if domain_spec is not None:
        context_id = f"{DOMAIN_PREFIX}{domain_id}"
        system = DOMAIN_CHAT_SYSTEM.format(spec=domain_spec)
    elif seed is not None:
        context_id = f"{SEED_PREFIX}{seed.id}"
        system = SEED_CHAT_SYSTEM.format(draft=seed.draft or f"# {seed.text}\n\n_(no draft yet)_")
    elif idea_id:
        spec = store.get_spec(idea_id)
        if spec is None:
            raise NotFound("Idea not found")
        context_id = idea_id
        system = _idea_chat_system(store, idea_id, spec)
    else:
        context_id = SCRATCH_ID
        system = SCRATCH_SYSTEM + "\n" + DOMAIN_WIZARD_RULE
    system = system + "\n" + QUESTION_RULE

    stripped = content.strip()

    # /pin_idea is handled directly — no LLM round-trip needed
    if stripped.lower().startswith(PIN_IDEA_COMMAND):
        user_msg = store.add_message(context_id, "user", content)
        if seed is None:
            assistant = store.add_message(
                context_id, "assistant",
                "⚙️ /pin_idea works inside a brainstormed draft conversation — "
                "select a seed in the Brainstorm bar first.",
            )
            return [user_msg, assistant]
        idea = store.pin_seed(seed.id)
        assistant = store.add_message(
            idea.id, "assistant",
            f"💡 *(Idea pinned: {idea.title})* — this conversation moved here with it.",
            created_idea_id=idea.id,
        )
        return [user_msg, assistant]

    # /setup_codebase: download a DOMAIN's reference codebase — handled server-side.
    # off-thread: the download/extract is blocking and would freeze the event loop.
    if stripped.lower().startswith(SETUP_CODEBASE_COMMAND):
        return await asyncio.to_thread(
            _setup_codebase_messages,
            store, context_id, content,
            stripped[len(SETUP_CODEBASE_COMMAND):].strip(), idea_id, domain_id)

    # /idea_generation forces the model to crystallize the conversation now
    llm_content = content
    if stripped.lower().startswith(IDEA_GENERATION_COMMAND):
        rest = stripped[len(IDEA_GENERATION_COMMAND):].strip()
        llm_content = (rest or "Create the idea from our conversation.") + IDEA_GENERATION_DIRECTIVE

    history = store.list_messages(context_id)[-HISTORY_LIMIT:]
    llm_messages = [
        {"role": m.role, "content": m.content} for m in history if m.role != "system"
    ]
    llm_messages.append({"role": "user", "content": llm_content})

    user_msg = store.add_message(context_id, "user", content)

    # Resolve a base directory for tools (idea or domain folder).
    base_dir = None
    if idea_id and seed is None and domain_spec is None:
        base_dir = store.idea_path(idea_id)
    elif domain_id and domain_spec is not None:
        base_dir = store.domain_path(domain_id)

    # Tool addenda go to every provider: chat_with_tools wires the tools to both
    # Anthropic (native tool_use) and OpenAI-compatible endpoints (function calls),
    # so both need the instructions describing apply_patch / read_file / openalex_search.
    if base_dir is not None:
        if domain_spec is not None:
            system = system + "\n" + DOMAIN_TOOL_ADDENDUM
        elif idea_id and seed is None:
            system = system + "\n" + CHAT_TOOL_ADDENDUM

    try:
        if base_dir is not None:
            result = await llm.chat_with_tools(
                settings, system, llm_messages,
                tools=_tools.ALL_TOOLS,
                executor=_make_tool_executor(base_dir),
            )
        else:
            result = await llm.chat(settings, system, llm_messages)
    except llm.LLMNotConfigured as exc:
        assistant = store.add_message(context_id, "assistant", f"⚙️ {exc}")
        return [user_msg, assistant]
    except llm.LLMError as exc:
        assistant = store.add_message(context_id, "assistant", f"⚠️ {exc}")
        return [user_msg, assistant]

    reply = result.text
    spec_updated = False
    created_idea_id: str | None = None
    created_domain_id: str | None = None

    # New domain (wizard completion)
    new_domain = NEW_DOMAIN_BLOCK.search(reply)
    if new_domain:
        block = new_domain.group(1)
        name = title_from_spec(block)
        domain = store.add_domain(name, spec=block)
        created_domain_id = domain.id
        reply = NEW_DOMAIN_BLOCK.sub(f"🌐 *(Domain created: {name})*", reply).strip()

    # Domain spec revision (inside a domain conversation)
    if domain_spec is not None:
        match = DOMAIN_SPEC_BLOCK.search(reply)
        if match:
            store.put_domain_spec(domain_id, match.group(1))
            reply = DOMAIN_SPEC_BLOCK.sub("🌐 *(DOMAIN.md updated)*", reply).strip()
            spec_updated = True
        elif "DOMAIN.md" in result.files_modified:
            spec_updated = True

    # New idea spawned by the model (scratch crystallization or spin-off)
    new_idea = NEW_IDEA_BLOCK.search(reply)
    if new_idea:
        block = new_idea.group(1)
        title = title_from_spec(block)
        idea = store.add_idea(title)
        store.put_spec(idea.id, block)
        created_idea_id = idea.id
        reply = NEW_IDEA_BLOCK.sub(f"💡 *(Idea created: {title})*", reply).strip()

    # Seed draft revision
    if seed is not None:
        match = SEED_DRAFT_BLOCK.search(reply)
        if match:
            draft = match.group(1)
            store.put_seed_draft(seed.id, draft, title=title_from_spec(draft))
            reply = SEED_DRAFT_BLOCK.sub("📝 *(draft updated)*", reply).strip()
            spec_updated = True

    # In-place spec update for the current idea
    if idea_id and seed is None and domain_spec is None:
        match = SPEC_BLOCK.search(reply)
        if match:
            store.put_spec(idea_id, match.group(1))
            reply = SPEC_BLOCK.sub("📋 *(IDEA.md updated)*", reply).strip()
            spec_updated = True
        elif "IDEA.md" in result.files_modified:
            spec_updated = True

    reply, question = _extract_question(reply)

    assistant = store.add_message(
        context_id,
        "assistant",
        reply,
        spec_updated=spec_updated,
        served_model=result.model,
        created_idea_id=created_idea_id,
        created_domain_id=created_domain_id,
        question=question,
    )

    # Wizard ran in scratch — carry the whole conversation into the new domain
    if created_domain_id and context_id == SCRATCH_ID:
        store.move_scratch_to_domain(created_domain_id)

    return [user_msg, assistant]


# ── Brainstorm ──────────────────────────────────────────────────────────────
def _existing_idea_labels(store: Store, query: str, limit: int = 20) -> list[str]:
    """Titles of pinned ideas + brainstorm seeds, FUZZY-RANKED by relevance to *query*
    (token overlap, then sequence similarity), top *limit*. Fed to the brainstormer so it
    stops regenerating near-duplicates of what already exists — pinned ideas were the gap
    (only seeds were considered before, so prior ideas kept reappearing)."""
    labels: list[str] = []
    seen: set[str] = set()
    for idea in store.list_ideas():       # pinned ideas — the main source of duplicates
        t = (idea.title or "").strip()
        if t and t.lower() not in seen:
            seen.add(t.lower()); labels.append(t)
    for s in store.list_seeds():          # seeds / drafts not yet pinned
        t = (s.text or "").strip()
        if t and t.lower() not in seen:
            seen.add(t.lower()); labels.append(t)
    if not labels or not query.strip():
        return labels[:limit]
    q = query.lower()
    qtokens = set(re.findall(r"\w+", q))

    def score(label: str) -> tuple[int, float]:  # 模糊搜索: rank by relevance to the query
        lt = label.lower()
        overlap = len(qtokens & set(re.findall(r"\w+", lt)))
        return (overlap, difflib.SequenceMatcher(None, q, lt).ratio())

    return sorted(labels, key=score, reverse=True)[:limit]


async def _build_brainstorm(
    store: Store,
    settings: LLMSettings,
    hint: str | None,
    idea_types: list[str] | None,
    emphasis: list[str] | None,
    count: int | None,
) -> tuple[str, str, int, str, list[str]]:
    """Assemble the brainstorm LLM call, running the (single) literature search.

    Returns ``(system, prompt, max_tokens, mode, statuses)`` where *mode* is
    ``"draft"`` (domain-grounded full IDEA.md drafts) or ``"seed"`` (one-liners) and
    *statuses* are progress messages to surface. Shared by the blocking and streaming
    paths so the search runs ONCE and both build the prompt identically."""
    directive = brainstorm_directive(idea_types, emphasis)
    domain_specs = store.selected_domain_specs()
    statuses: list[str] = []

    if domain_specs:
        n = _clamp_count(count, DRAFT_COUNT)
        query = " ".join(domain.name for domain, _ in domain_specs[:2])
        # Anonymous OpenAlex (no key) is rate-limited to nothing — skip the search rather
        # than always reporting "Found 0 papers" and waiting on a pointless request.
        if settings.openalex_api_key:
            statuses.append(f"Searching OpenAlex for papers on '{query}'…")
            papers = await literature.search_recent_papers(query)
            m = len(papers)
            statuses.append(f"Found {m} paper{'s' if m != 1 else ''}. Brainstorming idea drafts…")
            paper_ctx = literature.format_papers_for_prompt(papers)
        else:
            statuses.append("No OpenAlex key (Settings → Academic search) — skipping literature "
                            "search. Brainstorming idea drafts…")
            paper_ctx = ""
        domains_text = "\n\n".join(spec[:DOMAIN_SPEC_CHAR_LIMIT] for _, spec in domain_specs)
        system = BRAINSTORM_DRAFT_SYSTEM.format(n=n, domains=domains_text)
        prompt = "Brainstorm idea drafts from the domain specs."
        if directive:
            prompt += f" {directive}"
        if hint:
            prompt += f" Focus: {hint}"
        if paper_ctx:
            prompt += f"\n\nAdditional context from recent literature:\n{paper_ctx}"
        existing = _existing_idea_labels(store, f"{query} {hint or ''}")
        if existing:
            prompt += ("\n\nThese ideas ALREADY EXIST — generate genuinely DIFFERENT ones, "
                       "do NOT restate or lightly reword any of these:\n"
                       + "\n".join(f"- {t}" for t in existing))
        return system, prompt, 8192, "draft", statuses

    n = _clamp_count(count, GENERATE_COUNT)
    search_query = hint or "machine learning research"
    statuses.append("Brainstorming idea seeds…")
    if settings.openalex_api_key:  # skip the rate-limited anonymous search when no key
        papers = await literature.search_recent_papers(search_query, limit=5)
        paper_ctx = literature.format_papers_for_prompt(papers)
    else:
        paper_ctx = ""
    prompt = "Generate research idea seeds."
    if directive:
        prompt += f" {directive}"
    if hint:
        prompt += f" Focus area: {hint}"
    if paper_ctx:
        prompt += f"\n\nRecent papers for inspiration:\n{paper_ctx}"
    existing = _existing_idea_labels(store, hint or search_query)
    if existing:
        prompt += ("\n\nThese ideas ALREADY EXIST — generate genuinely DIFFERENT ones, "
                   "do NOT restate or lightly reword any of these:\n"
                   + "\n".join(f"- {t}" for t in existing))
    return BRAINSTORM_SYSTEM.format(n=n), prompt, 512, "seed", statuses


def _persist_brainstorm(store: Store, text: str, mode: str) -> list[Seed]:
    """Parse an LLM brainstorm reply into seeds and persist them (draft blocks for
    domain mode, one line per seed otherwise)."""
    created: list[Seed] = []
    if mode == "draft":
        for match in IDEA_DRAFT_BLOCK.finditer(text):
            draft = match.group(1).strip()
            if draft:
                created.append(store.add_seed(title_from_spec(draft, "Untitled draft"), draft=draft))
    else:
        for line in text.splitlines():
            t = line.strip().lstrip("-*0123456789. ").strip()
            if t:
                created.append(store.add_seed(t))
    return created


async def generate_seeds(
    store: Store,
    settings: LLMSettings,
    hint: str | None = None,
    idea_types: list[str] | None = None,
    emphasis: list[str] | None = None,
    count: int | None = None,
) -> list[Seed]:
    """Domain-grounded full drafts when domains are selected; one-liners otherwise.
    Searches OpenAlex to inject real recent papers into the context.
    ``idea_types`` / ``emphasis`` constrain the kind of ideas (see
    prompts.ideas.brainstorm_directive); ``count`` overrides the default number.
    Raises llm.LLMNotConfigured / llm.LLMError on failure."""
    system, prompt, max_tokens, mode, _ = await _build_brainstorm(
        store, settings, hint, idea_types, emphasis, count)
    result = await llm.chat(settings, system, [{"role": "user", "content": prompt}],
                            max_tokens=max_tokens)
    created = _persist_brainstorm(store, result.text, mode)
    if not created and mode == "draft":
        raise llm.LLMError("Model returned no idea drafts — try again")
    return created


# ── Domains ─────────────────────────────────────────────────────────────────
SUGGESTION_COUNT = 4


def _fallback_suggestions(spec: str) -> list[str]:
    name = title_from_spec(spec, "this domain")
    return [
        f"Summarize the current SOTA in {name}",
        f"What are the open problems in {name}?",
        "Update the SOTA papers in this spec",
        f"Propose a concrete experiment in {name}",
    ]


async def domain_suggestions(
    store: Store, settings: LLMSettings, domain_id: str
) -> list[str]:
    """Domain-tailored conversation starters. LLM-generated and cached on disk
    (invalidated when the spec changes); template fallback when no LLM."""
    spec = store.get_domain_spec(domain_id)
    if spec is None:
        raise NotFound("Domain not found")

    cached = store.get_domain_suggestions(domain_id)
    if cached:
        return cached

    try:
        result = await llm.chat(
            settings,
            SUGGESTIONS_SYSTEM.format(n=SUGGESTION_COUNT),
            [{"role": "user", "content": spec[:DOMAIN_SPEC_CHAR_LIMIT]}],
            max_tokens=300,
        )
    except (llm.LLMNotConfigured, llm.LLMError):
        return _fallback_suggestions(spec)  # not cached — retry once LLM works

    suggestions = []
    for line in result.text.splitlines():
        text = line.strip().lstrip("-*0123456789. ").strip()
        if text:
            suggestions.append(text)
    suggestions = suggestions[:SUGGESTION_COUNT]
    if not suggestions:
        return _fallback_suggestions(spec)
    store.put_domain_suggestions(domain_id, suggestions)
    return suggestions


# ── References (ref.bib) ──────────────────────────────────────────────────────
# Per-idea citation management. BibTeX is built from OpenAlex/Crossref (real,
# Scholar-compatible keys — never scraped from Google Scholar). Validation flags
# fabricated/wrong citations. Network lookups run in a worker thread.


def get_references(store: Store, idea_id: str) -> ReferencesView:
    if store.idea_path(idea_id) is None:
        raise NotFound("Idea not found")
    bib = store.get_ref_bib(idea_id)
    entries = [Reference.model_validate(e) for e in references.parse_bibtex(bib)]
    return ReferencesView(ideaId=idea_id, entries=entries, bibtex=bib)


async def add_reference(
    store: Store, idea_id: str, doi: str | None = None, query: str | None = None
) -> ReferencesView:
    if store.idea_path(idea_id) is None:
        raise NotFound("Idea not found")
    if not (doi or query):
        raise ValueError("Provide a DOI or a search query")
    entry = await asyncio.to_thread(references.build_entry, doi, query)
    if not entry:
        raise ValueError("No matching paper found — try a different DOI or query")
    bib = store.get_ref_bib(idea_id)
    parsed = references.parse_bibtex(entry)
    if not (parsed and parsed[0]["key"] in references.keys_in(bib)):  # skip duplicates by key
        bib = (bib.rstrip() + "\n\n" + entry.strip() + "\n") if bib.strip() else entry.strip() + "\n"
        store.put_ref_bib(idea_id, bib)
    return get_references(store, idea_id)


def _fallback_ref_queries(spec: str) -> list[str]:
    """No-LLM fallback: idea title + Keywords-section terms."""
    queries: list[str] = []
    title = title_from_spec(spec, "")
    if title:
        queries.append(title)
    m = re.search(r"##\s*Keywords\s*\n(.*?)(?=\n## |\Z)", spec, re.DOTALL | re.IGNORECASE)
    if m:
        kw = re.sub(r"[<>_*`-]", " ", m.group(1))
        for part in re.split(r"[,\n;]", kw):
            part = part.strip()
            if part and "tbd" not in part.lower() and len(part) > 2:
                queries.append(part)
    return queries[:5] or ["machine learning"]


async def _reference_queries(settings: LLMSettings, spec: str) -> list[str]:
    try:
        result = await llm.chat(
            settings, REFERENCE_QUERIES_SYSTEM,
            [{"role": "user", "content": spec[:DOMAIN_SPEC_CHAR_LIMIT]}], max_tokens=400,
        )
        qs = [ln.strip().lstrip("-*0123456789. ").strip() for ln in result.text.splitlines()]
        qs = [q for q in qs if q][:10]
        if qs:
            return qs
    except (llm.LLMNotConfigured, llm.LLMError):
        pass
    return _fallback_ref_queries(spec)


async def generate_references(store: Store, settings: LLMSettings, idea_id: str, limit: int = 40) -> ReferencesView:
    """Populate ref.bib from OpenAlex using keyword queries derived from the idea —
    targets AT LEAST 40 real papers (never fabricated). Merges with any existing
    entries (dedup by key)."""
    spec = store.get_spec(idea_id)
    if spec is None:
        raise NotFound("Idea not found")
    queries = await _reference_queries(settings, spec)
    existing_keys = references.keys_in(store.get_ref_bib(idea_id))
    seen: set[str] = set()
    new_entries: list[str] = []
    for q in queries:
        papers = await literature.search_recent_papers(q, limit=12)
        for p in papers:
            ident = (p.get("doi") or p.get("title", "")).lower().strip()
            if not ident or ident in seen:
                continue
            seen.add(ident)
            entry = references.bibtex_from_paper(p)
            parsed = references.parse_bibtex(entry)
            key = parsed[0]["key"] if parsed else None
            if key and key in existing_keys:
                continue
            if key:
                existing_keys.add(key)
            new_entries.append(entry.strip())
            if len(new_entries) >= limit:
                break
        if len(new_entries) >= limit:
            break
    if new_entries:
        bib = store.get_ref_bib(idea_id)
        joined = "\n\n".join(new_entries) + "\n"
        store.put_ref_bib(idea_id, (bib.rstrip() + "\n\n" + joined) if bib.strip() else joined)
    return get_references(store, idea_id)


async def validate_references(store: Store, idea_id: str) -> list[ReferenceValidation]:
    if store.idea_path(idea_id) is None:
        raise NotFound("Idea not found")
    entries = references.parse_bibtex(store.get_ref_bib(idea_id))
    results = await asyncio.to_thread(references.validate_all, entries)
    return [ReferenceValidation.model_validate(r) for r in results]


_REF_ICON = {"VERIFIED": "✓", "MISMATCH": "⚠", "NOT_FOUND": "✗", "UNKNOWN": "?"}


async def stream_validate_references(store: Store, idea_id: str) -> AsyncIterator[str]:
    """Validate ref.bib entry-by-entry against Crossref/OpenAlex, yielding a markdown
    status line per entry so the user sees progress live. Returns the full report text."""
    entries = references.parse_bibtex(store.get_ref_bib(idea_id))
    if not entries:
        yield "📚 No references in `ref.bib` yet — add some first (the References tab, or `cite`)."
        return
    yield f"🔎 Validating **{len(entries)}** reference(s) against Crossref / OpenAlex…\n\n"
    counts: dict[str, int] = {}
    for e in entries:
        r = await asyncio.to_thread(references.validate_entry, e)
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        yield f"{_REF_ICON.get(r['status'], '·')} `{r['key']}` — **{r['status']}**: {r['detail']}\n"
    bad = counts.get("MISMATCH", 0) + counts.get("NOT_FOUND", 0)
    summary = (f"\n**Done** — {counts.get('VERIFIED', 0)} verified"
               + (f", {bad} need attention" if bad else " (all clear)")
               + (f", {counts.get('UNKNOWN', 0)} unknown (network)" if counts.get("UNKNOWN") else "")
               + ". Fix MISMATCH/NOT_FOUND entries (likely wrong DOI or fabricated) before submitting.")
    yield summary


# ── Hypothesis map ────────────────────────────────────────────────────────────
# A small tree of testable hypotheses (root claims → sub-hypotheses), generated
# from the idea's "Root Hypotheses"/gap/motivation and shown in the Hypothesis tab.

_JSON_BLOCK = re.compile(r"```json\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


def _clean_nodes(nodes: list) -> list[dict]:
    """Normalise raw LLM nodes (no ids yet); drop empties; recurse into children."""
    out: list[dict] = []
    for n in nodes if isinstance(nodes, list) else []:
        if not isinstance(n, dict):
            continue
        statement = str(n.get("statement", "")).strip()
        if not statement:
            continue
        out.append({
            "statement": statement,
            "rationale": (n.get("rationale") or None),
            "test": (n.get("test") or None),
            "status": n.get("status") or "untested",
            "children": _clean_nodes(n.get("children") or []),
        })
    return out


def assign_hypothesis_ids(nodes: list[dict], parent_id: str = "", start: int = 0) -> list[dict]:
    """Give nodes hierarchical ids: roots H1, H2…; children H1.1, H1.2…; etc.
    ``start`` offsets numbering when appending to existing siblings (expansion)."""
    for i, n in enumerate(nodes):
        n["id"] = f"{parent_id}.{start + i + 1}" if parent_id else f"H{start + i + 1}"
        assign_hypothesis_ids(n["children"], n["id"])
    return nodes


def _parse_nodes_raw(text: str) -> list[dict]:
    m = _JSON_BLOCK.search(text)
    raw = m.group(1) if m else text
    raw = re.sub(r",\s*([}\]])", r"\1", raw)  # tolerate trailing commas
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        b, e = raw.find("{"), raw.rfind("}")
        try:
            data = json.loads(raw[b:e + 1]) if b != -1 and e != -1 else {}
        except json.JSONDecodeError:
            data = {}
    return _clean_nodes(data.get("nodes") or [])


def _parse_map_json(text: str) -> list[dict]:
    return assign_hypothesis_ids(_parse_nodes_raw(text))


def _node_stage(idea_path, hid: str, status: str, running: set[str]) -> str:
    """The node's CURRENT progress stage (derived from its workspace artifacts + any
    live experiment job), so an 'untested' node that's actually mid-run shows
    'planned'/'experiment' instead of a flat 'untested'. Verdicted/blocked → status."""
    if status and status != "untested":
        return status  # supported | refuted | inconclusive | blocked
    hdir = (idea_path / "hypotheses" / hid) if idea_path is not None else None
    if hid in running:
        return "experiment"  # an experiment job is running for it right now
    if hdir is not None and ((hdir / "results.json").is_file() or (hdir / "experiment.md").is_file()):
        return "experiment"  # experiment ran (results in), awaiting a verdict
    if hdir is not None and (hdir / "plan.md").is_file():
        return "planned"     # a testing plan exists, not yet run
    return "untested"


def _annotate_stages(store: Store, idea_id: str, nodes: list[dict]) -> None:
    """Set each node's derived `stage` in place (recursively)."""
    from paperclaw import jobs
    idea_path = store.idea_path(idea_id)
    running = {j["hypothesisId"] for j in jobs.list_experiment_jobs(store)
               if j.get("ideaId") == idea_id and j.get("status") == "running"}

    def walk(ns: list[dict]) -> None:
        for n in ns:
            n["stage"] = _node_stage(idea_path, n.get("id", ""), n.get("status") or "untested", running)
            walk(n.get("children") or [])
    walk(nodes)


def get_hypothesis_map(store: Store, idea_id: str) -> HypothesisMap:
    if store.idea_path(idea_id) is None:
        raise NotFound("Idea not found")
    data = store.get_hypothesis_map(idea_id)
    if not data:
        return HypothesisMap(ideaId=idea_id, nodes=[], generatedAt=0.0)
    data["ideaId"] = idea_id  # the file may have been written by the edit tools w/o it
    _annotate_stages(store, idea_id, data.get("nodes", []))  # derived progress stage
    return HypothesisMap.model_validate(data)


def _find_node(nodes: list[dict], hid: str) -> dict | None:
    for n in nodes:
        if n.get("id") == hid:
            return n
        found = _find_node(n.get("children") or [], hid)
        if found:
            return found
    return None


def _find_parent(nodes: list[dict], hid: str, parent: dict | None = None) -> dict | None:
    """The node whose `children` contains *hid*, or None if *hid* is a root (or absent —
    callers only pass a valid id, so None unambiguously means 'root level')."""
    for n in nodes:
        if n.get("id") == hid:
            return parent
        found = _find_parent(n.get("children") or [], hid, n)
        if found is not None:
            return found
    return None


def _node_depth(hid: str) -> int:
    """Tree depth of a hierarchical id: roots `H1`→1, `H1.2`→2, `H1.2.3`→3."""
    return (hid or "").count(".") + 1


def _remove_node(nodes: list[dict], hid: str) -> bool:
    """Remove node *hid* (and its subtree) from the tree in place. True if found."""
    for i, n in enumerate(nodes):
        if n.get("id") == hid:
            nodes.pop(i)
            return True
        if _remove_node(n.get("children") or [], hid):
            return True
    return False


def _format_node_for_plan(node: dict) -> str:
    parts = [f"# {node.get('statement', '').strip()}"]
    if node.get("rationale"):
        parts.append(f"Rationale: {node['rationale']}")
    for child in node.get("children") or []:
        line = f"- {child.get('statement', '').strip()}"
        if child.get("test"):
            line += f" (test: {child['test']})"
        parts.append(line)
    return "\n".join(parts)


_FENCE_RE = re.compile(r"```[\w-]*\s*\n(.*?)```", re.DOTALL)


def _extract_fenced(text: str, tag: str) -> str:
    m = re.search(r"```" + tag + r"\s*\n(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m2 = _FENCE_RE.search(text)
    return m2.group(1).strip() if m2 else text.strip()


async def _write_hypothesis_plan(store: Store, settings: LLMSettings, idea_id: str, node: dict) -> None:
    """Generate and persist hypotheses/<id>/plan.md for one node."""
    from paperclaw.prompts.pipeline import HYPOTHESIS_PLAN_SYSTEM
    spec = store.get_spec(idea_id) or ""
    idea_path = store.idea_path(idea_id)
    if idea_path is None:
        return
    hw = (store.get_hardware_md() or "No hardware detected (assume a modest single-GPU / CPU machine).")[:2000]
    result = await llm.chat(
        settings, HYPOTHESIS_PLAN_SYSTEM,
        [{"role": "user", "content":
          f"IDEA.md:\n{spec}{idea_benchmark_note(store, idea_id)}\n\n"
          f"Target hypothesis:\n{_format_node_for_plan(node)}\n\nAvailable hardware:\n{hw}"}],
        max_tokens=1400,
    )
    hdir = idea_path / "hypotheses" / node["id"]
    hdir.mkdir(parents=True, exist_ok=True)
    (hdir / "plan.md").write_text(_extract_fenced(result.text, "plan"), encoding="utf-8")


async def generate_hypothesis_plan(store: Store, settings: LLMSettings, idea_id: str, hid: str) -> HypothesisDetail:
    """Generate the testing plan for a single hypothesis (the per-hypothesis
    "Generate plan" button), then return its updated detail."""
    if store.idea_path(idea_id) is None:
        raise NotFound("Idea not found")
    if not re.fullmatch(r"[A-Za-z0-9.]+", hid) or ".." in hid:
        raise NotFound("Invalid hypothesis id")
    node = _find_node((store.get_hypothesis_map(idea_id) or {}).get("nodes", []), hid)
    if node is None:
        raise NotFound("Hypothesis not found")
    await _write_hypothesis_plan(store, settings, idea_id, node)
    return get_hypothesis_detail(store, idea_id, hid)


async def stream_hypothesis_experiment(store: Store, settings: LLMSettings, idea_id: str, hid: str) -> AsyncIterator[dict]:
    """Run one hypothesis's experiment with FULL streamed feedback — thinking,
    code/text generation, and execution status. Events: phase | thinking | delta |
    phase_done | hypothesis_status | done | error.
    """
    from paperclaw import iterative_pipeline as ip
    from paperclaw.prompts.pipeline import EXPERIMENT_SYSTEM, HYPOTHESIS_PLAN_SYSTEM

    idea_path = store.idea_path(idea_id)
    if idea_path is None:
        yield {"type": "error", "message": "Idea not found"}
        return
    if not re.fullmatch(r"[A-Za-z0-9.]+", hid) or ".." in hid:
        yield {"type": "error", "message": "Invalid hypothesis id"}
        return
    node = _find_node((store.get_hypothesis_map(idea_id) or {}).get("nodes", []), hid)
    if node is None:
        yield {"type": "error", "message": "Hypothesis not found"}
        return

    hdir = idea_path / "hypotheses" / hid
    hdir.mkdir(parents=True, exist_ok=True)
    spec = store.get_spec(idea_id) or ""
    # If a benchmark template is in force (persisted by the pipeline, or the domain default),
    # reframe BOTH the plan and the experiment: run only the new method on its fixed protocol
    # and compare to the cited baselines.
    base_ctx = f"IDEA.md:\n{spec}" + idea_benchmark_note(store, idea_id)
    # Effective run config: a per-job override pinned by the (auto-run) pipeline —
    # experiment mode / SSH target / reference-codebase reuse for THIS run — else the
    # global config. Without this the detached child would silently ignore per-run
    # overrides (re-reading the global config + always linking the codebase).
    # Experiments run as detached, monitored jobs with NO wall-clock timeout — the
    # process is tracked, not killed.
    from paperclaw import jobs
    override = jobs.read_run_override(hdir)
    run_cfg = override["runConfig"] if override else store.get_run_config()
    use_ref_cb = override["useReferenceCodebase"] if override else True
    cb_path = ip._resolve_domain_codebase(store, idea_id, spec) if use_ref_cb else None  # domain ref codebase

    # ── plan (stream its generation if missing) ───────────────────────────────
    plan = (hdir / "plan.md").read_text(encoding="utf-8") if (hdir / "plan.md").is_file() else None
    if plan is None:
        yield {"type": "phase", "phase": "plan", "label": "Writing testing plan…"}
        hw = (store.get_hardware_md() or "No hardware detected (assume a modest single-GPU / CPU machine).")[:2000]
        raw = ""
        try:
            async for ev in llm.stream_chat_thinking(
                settings, HYPOTHESIS_PLAN_SYSTEM,
                [{"role": "user", "content":
                  f"{base_ctx}\n\nTarget hypothesis:\n{_format_node_for_plan(node)}\n\nAvailable hardware:\n{hw}"}],
                max_tokens=1400,
            ):
                if ev["type"] == "thinking":
                    yield {"type": "thinking", "text": ev["text"]}
                else:
                    raw += ev["text"]
                    yield {"type": "delta", "text": ev["text"]}
        except (llm.LLMNotConfigured, llm.LLMError) as exc:
            yield {"type": "error", "message": str(exc)}
            return
        plan = _extract_fenced(raw, "plan")
        (hdir / "plan.md").write_text(plan, encoding="utf-8")
    yield {"type": "phase_done", "phase": "plan", "content": plan}

    if ip._is_infeasible(plan):
        ip._set_node_status(store, idea_id, hid, "blocked")
        yield {"type": "hypothesis_status", "hypothesisId": hid, "status": "blocked"}
        yield {"type": "done"}
        return

    # ── experiment: write code + run (or simulate) ────────────────────────────
    _exec_mode = run_cfg.experiment_mode in ("executed", "ssh", "cli")
    yield {"type": "phase", "phase": "experiment",
           "label": "Writing code & running the experiment…" if _exec_mode else "Simulating experiment results…"}
    exp = None
    if run_cfg.experiment_mode in ("executed", "ssh", "cli"):
        target = ip._resolve_ssh_target(store, run_cfg) if run_cfg.experiment_mode == "ssh" else None
        if run_cfg.experiment_mode == "ssh" and target is None:
            exp = "_SSH experiment mode is selected but no SSH remote is configured (Settings → Hardware)._"
        else:
            runner = ip._select_experiment_runner(settings, base_ctx, plan, hdir, run_cfg, target, cb_path)
            result = None
            try:
                async for ev in runner:
                    if ev["type"] == "thinking":
                        yield {"type": "thinking", "text": ev["text"]}
                    elif ev["type"] in ("delta", "status"):
                        yield {"type": "delta", "text": ev["text"]}
                    elif ev["type"] == "result":
                        result = ev["result"]
            except Exception as exc:
                yield {"type": "delta", "text": f"\n[execution error: {exc}]\n"}
            exp = (result or {}).get("markdown") or "_No experiment results produced._"
    else:
        raw = ""
        try:
            async for ev in llm.stream_chat_thinking(
                settings, EXPERIMENT_SYSTEM,
                [{"role": "user", "content": f"{base_ctx}\n\nPlan under test:\n{plan}"}], max_tokens=1600,
            ):
                if ev["type"] == "thinking":
                    yield {"type": "thinking", "text": ev["text"]}
                else:
                    raw += ev["text"]
                    yield {"type": "delta", "text": ev["text"]}
        except (llm.LLMNotConfigured, llm.LLMError) as exc:
            yield {"type": "error", "message": str(exc)}
            return
        exp = _extract_fenced(raw, "experiment-results")
    (hdir / "experiment.md").write_text(exp, encoding="utf-8")
    yield {"type": "phase_done", "phase": "experiment", "content": exp}
    yield {"type": "done"}


async def run_hypothesis_experiment(store: Store, settings: LLMSettings, idea_id: str, hid: str) -> HypothesisDetail:
    """Run ONE hypothesis's experiment on demand (the "Run experiment" button):
    ensure a plan, then writes code + runs it (executed/ssh) or narrates results
    (simulated). Returns the updated detail (code / results / log now visible)."""
    from paperclaw import iterative_pipeline as ip
    from paperclaw.prompts.pipeline import EXPERIMENT_SYSTEM

    idea_path = store.idea_path(idea_id)
    if idea_path is None:
        raise NotFound("Idea not found")
    if not re.fullmatch(r"[A-Za-z0-9.]+", hid) or ".." in hid:
        raise NotFound("Invalid hypothesis id")
    node = _find_node((store.get_hypothesis_map(idea_id) or {}).get("nodes", []), hid)
    if node is None:
        raise NotFound("Hypothesis not found")

    hdir = idea_path / "hypotheses" / hid
    hdir.mkdir(parents=True, exist_ok=True)
    if not (hdir / "plan.md").is_file():
        await _write_hypothesis_plan(store, settings, idea_id, node)
    plan = (hdir / "plan.md").read_text(encoding="utf-8") if (hdir / "plan.md").is_file() else ""

    if ip._is_infeasible(plan):  # respect the feasibility gate
        ip._set_node_status(store, idea_id, hid, "blocked")
        return get_hypothesis_detail(store, idea_id, hid)

    run_cfg = store.get_run_config()
    spec = store.get_spec(idea_id) or ""
    base_ctx = f"IDEA.md:\n{spec}"
    cb_path = ip._resolve_domain_codebase(store, idea_id, spec)  # domain reference codebase, if any
    if run_cfg.experiment_mode in ("executed", "ssh", "cli"):
        target = ip._resolve_ssh_target(store, run_cfg) if run_cfg.experiment_mode == "ssh" else None
        if run_cfg.experiment_mode == "ssh" and target is None:
            exp = "_SSH experiment mode is selected but no SSH remote is configured (Settings → Hardware)._"
        else:
            runner = ip._select_experiment_runner(settings, base_ctx, plan, hdir, run_cfg, target, cb_path)
            result = None
            async for ev in runner:
                if ev["type"] == "result":
                    result = ev["result"]
            exp = (result or {}).get("markdown") or "_No experiment results produced._"
    else:
        res = await llm.chat(
            settings, EXPERIMENT_SYSTEM,
            [{"role": "user", "content": f"{base_ctx}\n\nPlan under test:\n{plan}"}], max_tokens=1600,
        )
        exp = _extract_fenced(res.text, "experiment-results")
    (hdir / "experiment.md").write_text(exp, encoding="utf-8")
    return get_hypothesis_detail(store, idea_id, hid)


_CODE_CAP = 60_000  # cap the aggregated code view; the Files tab has everything


def _collect_experiment_code(hdir) -> str | None:
    """All of the agent's experiment code, concatenated with per-file headers — so the
    'generated code' view is COMPLETE and CONSISTENT across runners (the agentic `executed`
    /`ssh` runners and the `cli` agent build a MULTI-file layout, not just run.py). Top-level
    `*.py` only, so a copied reference codebase in a subdir doesn't bloat it; the 📁 Files
    tab still lists everything."""
    if not hdir.is_dir():
        return None
    files = sorted(p for p in hdir.glob("*.py") if p.is_file())
    if not files:
        return None
    blocks: list[str] = []
    total = 0
    for p in files:
        try:
            body = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        block = f"# ===== {p.name} =====\n{body}"
        blocks.append(block)
        total += len(block)
        if total >= _CODE_CAP:
            blocks.append(f"# … (truncated — {len(files)} code files in all; open the 📁 Files tab)")
            break
    return "\n\n".join(blocks) if blocks else None


def get_hypothesis_detail(store: Store, idea_id: str, hid: str) -> HypothesisDetail:
    """Per-hypothesis pipeline artifacts (plan / experiment / report / log / figures)."""
    idea_path = store.idea_path(idea_id)
    if idea_path is None:
        raise NotFound("Idea not found")
    if not re.fullmatch(r"[A-Za-z0-9.]+", hid) or ".." in hid:  # ids are H1.2.3 — no path escapes
        raise NotFound("Invalid hypothesis id")
    hdir = idea_path / "hypotheses" / hid

    def rd(name: str) -> str | None:
        p = hdir / name
        return p.read_text(encoding="utf-8") if p.is_file() else None

    node = _find_node((store.get_hypothesis_map(idea_id) or {}).get("nodes", []), hid)
    figures = sorted(p.name for p in hdir.glob("*.png")) if hdir.is_dir() else []
    return HypothesisDetail(
        ideaId=idea_id, hypothesisId=hid,
        status=(node or {}).get("status", "untested"),
        plan=rd("plan.md"), code=_collect_experiment_code(hdir), experiment=rd("experiment.md"),
        report=rd("report.md"), log=rd("stdout.log"), figures=figures,
    )


async def expand_hypothesis(store: Store, settings: LLMSettings, idea_id: str, hid: str,
                            report: str, max_depth: int | None = None) -> int:
    """Grow the map: add LLM-proposed sub-hypotheses motivated by node *hid*'s report.
    Normally they become CHILDREN of *hid* (one level deeper). But if *max_depth* is set
    and *hid* is already AT the depth cap, grow SIDEWAYS instead — attach the new nodes
    as SIBLINGS (under *hid*'s parent, or as new roots) so the search explores breadth
    at the limit rather than going deeper. Each node is expanded at most once. Returns
    #added."""
    from paperclaw.prompts.pipeline import HYPOTHESIS_EXPAND_SYSTEM
    data = store.get_hypothesis_map(idea_id)
    if not data:
        return 0
    node = _find_node(data.get("nodes", []), hid)
    if node is None or node.get("expanded"):
        return 0
    # Never grow children under an UNVERDICTED, blocked, or INCONCLUSIVE parent — a
    # sub-hypothesis must be motivated by a real, interpretable result. An inconclusive
    # node (e.g. its experiment produced no usable results.json) has no signal to branch
    # from; expanding it just spawns degenerate "did the pipeline run?" meta-hypotheses,
    # so skip it and let the loop try a sibling / re-run instead.
    if (node.get("status") or "untested") in ("untested", "blocked", "inconclusive"):
        return 0
    node["expanded"] = True  # one expansion per node — avoids runaway tree growth
    new_nodes: list[dict] = []
    try:
        result = await llm.chat(
            settings, HYPOTHESIS_EXPAND_SYSTEM,
            [{"role": "user", "content": f"Hypothesis: {node.get('statement', '')}\n\nReport:\n{report}"}],
            max_tokens=600,
        )
        new_nodes = _parse_nodes_raw(result.text)[:2]
    except (llm.LLMNotConfigured, llm.LLMError):
        new_nodes = []
    if max_depth is not None and max_depth >= 1 and _node_depth(hid) >= max_depth:
        # At the depth cap → grow sideways: place new nodes alongside *hid* (siblings),
        # i.e. under its parent, or at the root level if *hid* is a root.
        parent = _find_parent(data.get("nodes", []), hid)
        siblings = parent.setdefault("children", []) if parent else data.setdefault("nodes", [])
        assign_hypothesis_ids(new_nodes, parent["id"] if parent else "", start=len(siblings))
        siblings.extend(new_nodes)
    else:
        existing = node.setdefault("children", [])
        assign_hypothesis_ids(new_nodes, node["id"], start=len(existing))  # H1.3, H1.4…
        existing.extend(new_nodes)
    store.put_hypothesis_map(idea_id, data)
    return len(new_nodes)


async def autoexpand_from_report(store: Store, settings: LLMSettings, idea_id: str, hid: str) -> int:
    """After a report is written for *hid* (the /generate_report skill), reliably grow
    the map WITHOUT depending on the agent editing JSON: read its verdict, set node
    *hid*'s status from it, then auto-generate follow-up sub-hypotheses (same as the
    pipeline does). No verdict yet (e.g. "run the experiment first") ⇒ no-op. Returns
    the number of follow-up hypotheses added."""
    from paperclaw import iterative_pipeline as ip
    idea_path = store.idea_path(idea_id)
    if idea_path is None:
        return 0
    report_path = idea_path / "hypotheses" / hid / "report.md"
    if not report_path.is_file():
        return 0
    report = report_path.read_text(encoding="utf-8", errors="ignore")
    if not ip._VERDICT_RE.search(report):
        return 0  # report has no Verdict (no results yet) — don't touch the map
    ip._set_node_status(store, idea_id, hid, ip._verdict_to_status(report))
    try:
        return await expand_hypothesis(store, settings, idea_id, hid, report)
    except (llm.LLMNotConfigured, llm.LLMError):
        return 0


async def generate_hypothesis_map(store: Store, settings: LLMSettings, idea_id: str) -> HypothesisMap:
    """LLM-generate the hypothesis tree from IDEA.md and persist it."""
    spec = store.get_spec(idea_id)
    if spec is None:
        raise NotFound("Idea not found")
    result = await llm.chat(
        settings, HYPOTHESIS_MAP_SYSTEM,
        [{"role": "user", "content": spec}], max_tokens=2000,
    )
    raw_nodes = _parse_map_json(result.text)
    for n in raw_nodes:
        n["children"] = []  # roots only — children are added later, after a verdict
    nodes = [HypothesisNode.model_validate(n) for n in raw_nodes]
    hmap = HypothesisMap(ideaId=idea_id, nodes=nodes, generatedAt=time.time())
    store.put_hypothesis_map(idea_id, hmap.model_dump(by_alias=True))
    # Create a testing plan for every ROOT hypothesis up front (#4), best-effort.
    for root in raw_nodes:
        try:
            await _write_hypothesis_plan(store, settings, idea_id, root)
        except (llm.LLMNotConfigured, llm.LLMError):
            break  # no LLM — skip plans, the map still stands
        except Exception:
            continue
    return hmap


def _normalize_written_map(store: Store, idea_id: str) -> bool:
    """The chat agent wrote `.hypothesis_map.json` (via /generate_hypothesis_map) —
    normalize it (clean fields, ROOTS ONLY, hierarchical ids H1/H2…) and persist.
    Returns True if a non-empty map resulted, so the reply can flag `map_updated`."""
    data = store.get_hypothesis_map(idea_id)
    if not data or not isinstance(data.get("nodes"), list):
        return False
    nodes = _clean_nodes(data["nodes"])
    for n in nodes:
        n["children"] = []  # roots only — children come later, after a verdict
    assign_hypothesis_ids(nodes)
    try:
        hmap = HypothesisMap(
            ideaId=idea_id, generatedAt=time.time(),
            nodes=[HypothesisNode.model_validate(n) for n in nodes],
        )
    except Exception:
        return False
    store.put_hypothesis_map(idea_id, hmap.model_dump(by_alias=True))
    return bool(nodes)


def delete_hypothesis_node(store: Store, idea_id: str, hid: str) -> HypothesisMap:
    """Remove a hypothesis node (and its subtree) from the map, plus its workspace
    dir. Returns the updated map. Raises NotFound if the idea or node is missing."""
    idea_path = store.idea_path(idea_id)
    if idea_path is None:
        raise NotFound("Idea not found")
    data = store.get_hypothesis_map(idea_id)
    if not data or not _remove_node(data.get("nodes", []), hid):
        raise NotFound("Hypothesis not found")
    store.put_hypothesis_map(idea_id, data)
    if re.fullmatch(r"[A-Za-z0-9.]+", hid) and ".." not in hid:  # best-effort cleanup
        hdir = idea_path / "hypotheses" / hid
        if hdir.is_dir():
            shutil.rmtree(hdir, ignore_errors=True)
    data["ideaId"] = idea_id
    return HypothesisMap.model_validate(data)


def add_child_hypothesis(store: Store, idea_id: str, parent_hid: str, statement: str) -> HypothesisMap:
    """Manually add a sub-hypothesis under a parent node (the graph's right-click 'add
    sub-hypothesis'). Reuses the chat hypothesis tool's id scheme + persistence, then returns
    the refreshed map."""
    idea_path = store.idea_path(idea_id)
    if idea_path is None:
        raise NotFound("Idea not found")
    from paperclaw.tools import hypothesis as _hyp
    res = _hyp.add(idea_path, {"parent_id": (parent_hid or "").strip(), "statement": statement})
    if res.startswith("Error"):
        raise NotFound(res)
    return get_hypothesis_map(store, idea_id)


def rerun_hypothesis_experiment(store: Store, idea_id: str, hid: str) -> dict:
    """Clear a hypothesis's experiment + verdict artifacts, reset it to untested, and start a
    FRESH detached experiment job (the graph's right-click 'rerun / retest', e.g. when a run
    came back INCONCLUSIVE). Cancels any in-flight job first."""
    idea_path = store.idea_path(idea_id)
    if idea_path is None:
        raise NotFound("Idea not found")
    if not re.fullmatch(r"[A-Za-z0-9.]+", hid or "") or ".." in (hid or ""):
        raise NotFound("Hypothesis not found")
    hdir = idea_path / "hypotheses" / hid
    if not hdir.is_dir():
        raise NotFound("Hypothesis not found")
    from paperclaw import iterative_pipeline as ip, jobs
    jobs.cancel_experiment_job(store, idea_id, hid)  # stop any running job before clearing
    for name in ("results.json", "experiment.md", "report.md", "events.jsonl"):
        (hdir / name).unlink(missing_ok=True)
    ip._set_node_status(store, idea_id, hid, "untested")  # so the verdict is recomputed
    return jobs.start_experiment_job(store, idea_id, hid)


# ── Hardware / environment ────────────────────────────────────────────────────
# Shared by the HTTP routes and the CLI: probe the local host (+ any SSH remotes)
# for compute resources, render HARDWARE.md, and persist a snapshot. Detection is
# deterministic (subprocess probes in hardware.py); the LLM only adds a short
# best-effort capability assessment.


def get_hardware_view(store: Store) -> HardwareView:
    """Return the persisted hardware snapshot + SSH config (no re-detection)."""
    state = store.get_hardware_state()
    return HardwareView(
        machines=[HardwareInfo.model_validate(m) for m in state.get("machines", [])],
        sshTargets=[SSHTarget.model_validate(t) for t in state.get("sshTargets", [])],
        runConfig=store.get_run_config(),
        markdown=store.get_hardware_md(),
        updatedAt=state.get("updatedAt", 0.0),
    )


def save_ssh_targets(store: Store, targets: list[SSHTarget]) -> HardwareView:
    """Persist the SSH target list (does not re-detect — call detect_hardware next)."""
    state = store.get_hardware_state()
    state["sshTargets"] = [t.model_dump(by_alias=True) for t in targets]
    store.save_hardware_state(state)
    return get_hardware_view(store)


def save_run_config(store: Store, cfg: RunConfig) -> HardwareView:
    """Persist the experiment-execution config (simulated vs executed, etc.)."""
    store.save_run_config(cfg)
    return get_hardware_view(store)


# ── Doctor — environment readiness ───────────────────────────────────────────

def environment_report(settings: LLMSettings, home=None) -> DoctorReport:
    """Check that the key environment is ready: PaperClaw home, the LLM config, the chat
    agent, the LaTeX toolchain (for paper compilation), and image generation.

    Pure + fast — no LLM calls — so it's safe to run anywhere (CLI, route, UI)."""
    from paperclaw import images, iterative_pipeline  # lazy: avoid load cycles

    checks: list[EnvCheck] = []

    # 1. PaperClaw home — exists + writable.
    h = home or paperclaw_home()
    try:
        h.mkdir(parents=True, exist_ok=True)
        probe = h / ".doctor_write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        checks.append(EnvCheck(key="home", label="PaperClaw home", status="ok", detail=str(h)))
    except OSError as exc:
        checks.append(EnvCheck(key="home", label="PaperClaw home", status="fail",
                               detail=f"{h} — {exc}", hint="check permissions / disk space"))

    # 2. LLM provider / model / API key.
    model = settings.model or "(unset)"
    where = f"{settings.provider} · {model}"
    if settings.api_key:
        checks.append(EnvCheck(key="llm", label="LLM API", status="ok", detail=where))
    else:
        env_key = "ANTHROPIC_API_KEY" if settings.provider == "anthropic" else "OPENAI_API_KEY"
        checks.append(EnvCheck(
            key="llm", label="LLM API", status="fail", detail=f"{where} — no API key",
            hint=f"set one via `paperclaw settings set --api-key …` or ${env_key}"))

    # 3. Chat agent (deepagents is the default editor; falls back to the built-in loop).
    agent = (settings.chat_agent or "deepagents").lower()
    if agent == "builtin":
        checks.append(EnvCheck(key="chat_agent", label="Chat agent", status="ok",
                               detail="builtin tool loop"))
    elif deep_chat.available():
        checks.append(EnvCheck(key="chat_agent", label="Chat agent", status="ok",
                               detail="deepagents"))
    else:
        checks.append(EnvCheck(
            key="chat_agent", label="Chat agent", status="warn",
            detail="deepagents requested but not installed — using the builtin loop",
            hint="pip install 'paperclaw[deepagents]'"))

    # 3b. Coding agent (runs experiments). RECOMMENDED: the `claude` CLI (cli mode);
    #     otherwise the in-process agent (deepagents-style) using the configured API key.
    from paperclaw.config import claude_cli_available
    mode = Store(h).get_run_config().experiment_mode
    claude_ok = claude_cli_available()
    if claude_ok and mode == "cli":
        checks.append(EnvCheck(key="coding_agent", label="Coding agent", status="ok",
                               detail="claude CLI (recommended) · experiment mode: cli"))
    elif claude_ok:
        checks.append(EnvCheck(
            key="coding_agent", label="Coding agent", status="ok",
            detail=f"claude CLI detected (recommended) · experiment mode: {mode}",
            hint="switch experiment mode to 'cli' to use claude (Settings → Experiment execution)"))
    elif mode == "cli":
        checks.append(EnvCheck(
            key="coding_agent", label="Coding agent", status="ok",
            detail="experiment mode is 'cli' but the claude CLI was not found — "
                   "experiments fall back to the in-process agent (+ configured API key)",
            hint="install claude (`npm i -g @anthropic-ai/claude-code`) for the recommended headless agent"))
    else:
        checks.append(EnvCheck(
            key="coding_agent", label="Coding agent", status="ok",
            detail=f"in-process agent + API key · experiment mode: {mode}",
            hint="install the `claude` CLI for the recommended headless agent (then set mode 'cli')"))

    # 4. LaTeX toolchain (paper compilation). Prefer a real TeX Live (Overleaf-match).
    tex = iterative_pipeline.latex_status()
    if tex["texlive_bindir"]:
        eng = tex["engines"]
        have = [n for n in ("pdflatex", "xelatex", "lualatex") if eng.get(n)]
        missing = [n for n in ("xelatex", "lualatex", "biber") if not eng.get(n)]
        detail = f"TeX Live at {tex['texlive_bindir']} · engines: {', '.join(have) or 'none'}"
        checks.append(EnvCheck(
            key="latex", label="LaTeX (paper compile)", status="ok", detail=detail,
            hint=(f"optional: tlmgr install {' '.join(missing)}" if missing else None)))
    elif tex["tectonic"]:
        checks.append(EnvCheck(
            key="latex", label="LaTeX (paper compile)", status="warn",
            detail="no full TeX Live — using tectonic fallback",
            hint="install TeX Live for Overleaf-faithful output"))
    elif tex["pdflatex"]:
        checks.append(EnvCheck(
            key="latex", label="LaTeX (paper compile)", status="warn",
            detail="only raw pdflatex (no latexmk/TeX Live tree)",
            hint="install a full TeX Live or tectonic"))
    else:
        checks.append(EnvCheck(
            key="latex", label="LaTeX (paper compile)", status="fail",
            detail="no LaTeX engine found",
            hint="install TeX Live (latexmk) or tectonic to compile papers"))

    # 5. Image generation (optional — paper figures).
    if images.is_configured(settings):
        checks.append(EnvCheck(key="images", label="Image generation", status="ok",
                               detail=settings.image_model or "configured"))
    else:
        checks.append(EnvCheck(
            key="images", label="Image generation", status="warn",
            detail="not configured — figures fall back to matplotlib/TikZ",
            hint="set image API in Settings (optional) for AI raster figures"))

    # 6. OpenAlex literature search (optional — domain survey / SOTA / references).
    if settings.openalex_api_key:
        checks.append(EnvCheck(key="openalex", label="Literature search (OpenAlex)", status="ok",
                               detail="API key configured (dedicated budget)"))
    else:
        checks.append(EnvCheck(
            key="openalex", label="Literature search (OpenAlex)", status="warn",
            detail="no API key — anonymous (per-IP) requests can be rate-limited (HTTP 429)",
            hint="add an OpenAlex API key in Settings to avoid 'Found 0 papers'"))

    ok = all(c.status != "fail" for c in checks)
    return DoctorReport(ok=ok, checks=checks)


async def detect_hardware(store: Store, settings: LLMSettings) -> HardwareView:
    """Probe local + every configured SSH remote, write HARDWARE.md, persist.

    The blocking subprocess probes run in a worker thread so the event loop stays
    free; the optional LLM assessment degrades gracefully when no key is set.
    """
    state = store.get_hardware_state()
    targets = [SSHTarget.model_validate(t) for t in state.get("sshTargets", [])]

    def _probe() -> list[HardwareInfo]:
        machines = [hardware.detect_local()]
        machines.extend(hardware.detect_remote(t) for t in targets)
        return machines

    machines = await asyncio.to_thread(_probe)
    md = hardware.render_markdown(machines)

    # Best-effort capability note appended to HARDWARE.md — never blocks the run.
    try:
        result = await llm.chat(
            settings, HARDWARE_ASSESS_SYSTEM,
            [{"role": "user", "content": md}], max_tokens=400,
        )
        note = result.text.strip()
        if note:
            md = md.rstrip() + "\n\n## Assessment\n\n" + note + "\n"
    except (llm.LLMNotConfigured, llm.LLMError):
        pass

    now = time.time()
    store.save_hardware_state({
        "sshTargets": [t.model_dump(by_alias=True) for t in targets],
        "machines": [m.model_dump(by_alias=True) for m in machines],
        "updatedAt": now,
    })
    store.put_hardware_md(md)
    return HardwareView(
        machines=machines, sshTargets=targets,
        runConfig=store.get_run_config(), markdown=md, updatedAt=now,
    )


# ── Domain reference codebase ────────────────────────────────────────────────

_REFERENCE_CODEBASE_RE = re.compile(
    r"##\s*Reference Codebase\s*\n(.*?)(?:\n##\s|\Z)", re.IGNORECASE | re.DOTALL)
_GITHUB_URL_RE = re.compile(r"https?://github\.com/[^\s)\]>`\"']+", re.IGNORECASE)


def codebase_url_from_spec(spec: str) -> str | None:
    """The first GitHub repo URL in the DOMAIN.md `## Reference Codebase` section."""
    m = _REFERENCE_CODEBASE_RE.search(spec or "")
    section = m.group(1) if m else ""
    url = _GITHUB_URL_RE.search(section)
    return url.group(0).rstrip(".,);") if url else None


def set_domain_codebase(store: Store, domain_id: str, url: str) -> Domain:
    """Download *url* into the domain's `codebase/` dir and record the metadata.
    Raises NotFound if the domain is missing, codebase.CodebaseError on download
    failure."""
    if store.domain_path(domain_id) is None:
        raise NotFound("Domain not found")
    info = codebase.download_codebase(url, store.domain_codebase_dir(domain_id))
    return store.set_domain_codebase_meta(domain_id, info["url"], info["fileCount"])


def clear_domain_codebase(store: Store, domain_id: str) -> Domain:
    if store.domain_path(domain_id) is None:
        raise NotFound("Domain not found")
    return store.clear_domain_codebase(domain_id)


# ── Writing styles (prose-style guides for paper writing) ────────────────────

def list_writing_styles(store: Store, domain_id: str | None = None) -> list[dict]:
    """Available writing-style guides — global + (optionally) a domain's, as dicts."""
    domain_dir = store.domain_path(domain_id) if domain_id else None
    return writing_styles.list_styles(store.home, domain_dir)


def get_writing_style(store: Store, domain_id: str | None, name: str) -> str | None:
    """A writing-style guide's markdown (domain-scoped first, then global)."""
    domain_dir = store.domain_path(domain_id) if domain_id else None
    return writing_styles.get_style(store.home, domain_dir, name)


def resolve_writing_style(store: Store, domain_id: str | None, name: str | None) -> str | None:
    """The prose-style guide to apply for a paper: a chosen NAME (domain-first), else
    the house DEFAULT_STYLE. Single source of truth for the 'no style ⇒ default voice'
    rule, so the prose voice always comes from a writing style, not the system prompt."""
    if name:
        return get_writing_style(store, domain_id, name)
    return DEFAULT_STYLE


def save_writing_style(store: Store, name: str, content: str,
                       domain_id: str | None = None) -> str | None:
    domain_dir = store.domain_path(domain_id) if domain_id else None
    return writing_styles.save_style(store.home, domain_dir, name, content)


# ── Benchmark templates (fixed protocol + published, cited baselines) ─────────

def list_benchmarks(store: Store, domain_id: str | None = None) -> list[dict]:
    """Available benchmark templates — global + (optionally) a domain's, as dicts."""
    domain_dir = store.domain_path(domain_id) if domain_id else None
    return benchmarks.list_benchmarks(store.home, domain_dir)


def get_benchmark(store: Store, domain_id: str | None, name: str) -> str | None:
    """A benchmark template's markdown (domain-scoped first, then global)."""
    domain_dir = store.domain_path(domain_id) if domain_id else None
    return benchmarks.get_benchmark(store.home, domain_dir, name)


def resolve_benchmark(store: Store, domain_id: str | None, name: str | None) -> str | None:
    """The benchmark markdown to apply for a run: a chosen NAME (domain-first); if no name
    is given, the domain's SOLE benchmark when it has exactly one (else None — no benchmark)."""
    if name:
        return get_benchmark(store, domain_id, name)
    if domain_id:
        domain_only = [b for b in list_benchmarks(store, domain_id) if b["scope"] == "domain"]
        if len(domain_only) == 1:
            return get_benchmark(store, domain_id, domain_only[0]["name"])
    return None


def save_benchmark(store: Store, name: str, content: str,
                   domain_id: str | None = None) -> str | None:
    domain_dir = store.domain_path(domain_id) if domain_id else None
    return benchmarks.save_benchmark(store.home, domain_dir, name, content)


def _idea_benchmark_md(store: Store, idea_id: str) -> str | None:
    """The benchmark markdown in force for an idea — the persisted ``.benchmark.md`` the
    pipeline writes for the run (so a DETACHED experiment job sees it), else the idea's
    domain default benchmark, else None."""
    idea_path = store.idea_path(idea_id)
    if idea_path is not None:
        f = idea_path / ".benchmark.md"
        if f.is_file():
            return f.read_text(encoding="utf-8", errors="ignore")
    return resolve_benchmark(store, resolve_domain_id_for_idea(store, idea_id), None)


def idea_benchmark_note(store: Store, idea_id: str) -> str:
    """The BENCHMARK_NOTE-wrapped benchmark for an idea, or '' — used to reframe the
    plan/experiment context wherever it's rebuilt from disk (not via the pipeline's
    in-process base_ctx)."""
    from paperclaw.prompts.benchmarks import BENCHMARK_NOTE
    md = _idea_benchmark_md(store, idea_id)
    return BENCHMARK_NOTE.replace("{benchmark}", md) if md else ""


def list_idea_benchmarks(store: Store, idea_id: str) -> list[dict]:
    """Benchmark templates available to an idea (for the Spec tab's Benchmark picker):
    its OWN benchmarks/ (idea scope, e.g. from /setup_benchmark in the idea chat) + its
    pinned domain's + global."""
    idea_dir = store.idea_path(idea_id)
    domain_id = resolve_domain_id_for_idea(store, idea_id)
    domain_dir = store.domain_path(domain_id) if domain_id else None
    return benchmarks.list_benchmarks(store.home, domain_dir, idea_dir)


def get_idea_benchmark(store: Store, idea_id: str, name: str | None = None) -> dict:
    """A benchmark for the Spec tab's Benchmark view: a specific *name* (idea > domain >
    global) when given, else the one in FORCE for the idea (persisted .benchmark.md, or the
    domain's sole one). ``{"name": <title or None>, "content": <markdown or None>}``."""
    if name:
        idea_dir = store.idea_path(idea_id)
        domain_id = resolve_domain_id_for_idea(store, idea_id)
        domain_dir = store.domain_path(domain_id) if domain_id else None
        md = benchmarks.get_benchmark(store.home, domain_dir, name, idea_dir=idea_dir)
    else:
        md = _idea_benchmark_md(store, idea_id)
    title = None
    if md:
        m = re.search(r"^#\s+(.+)$", md, re.MULTILINE)
        title = m.group(1).strip() if m else None
    return {"name": title, "content": md}


def upload_venue_file(store: Store, idea_id: str, filename: str, data: bytes) -> dict:
    """Place an uploaded LaTeX venue template into the idea's ``venue/`` dir — a
    ``.zip`` (e.g. an Overleaf export) is extracted, a single ``.sty/.cls/.tex/.bst/
    .bib`` is saved as-is — so the paper stage BASES the paper on it. Path-traversal
    guarded. Returns the resulting venue file list."""
    from pathlib import Path
    idir = store.idea_path(idea_id)
    if idir is None:
        raise NotFound(f"idea {idea_id} not found")
    venue = idir / "venue"
    venue.mkdir(parents=True, exist_ok=True)
    name = os.path.basename((filename or "template").strip()) or "template"
    if name.lower().endswith(".zip"):
        import io
        import zipfile
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                for member in zf.namelist():
                    if member.endswith("/"):
                        continue
                    rel = Path(member)
                    if rel.is_absolute() or ".." in rel.parts:
                        continue  # skip path-traversal entries
                    dest = venue / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(zf.read(member))
        except zipfile.BadZipFile as exc:
            raise ValueError(f"not a valid .zip file: {exc}")
    else:
        (venue / name).write_bytes(data)
    files = sorted(str(p.relative_to(venue)) for p in venue.rglob("*") if p.is_file())
    return {"files": files}


_STYLE_ARG_RE = re.compile(r"--style(?:=|\s+)([A-Za-z0-9._-]+)")


def _extract_style_arg(text: str) -> tuple[str, str | None]:
    """Pull a `--style <name>` / `--style=<name>` flag out of a command's args,
    returning (remaining_args, style_name|None)."""
    m = _STYLE_ARG_RE.search(text or "")
    if not m:
        return text, None
    cleaned = (text[:m.start()] + text[m.end():]).strip()
    return cleaned, m.group(1)


def resolve_domain_ids_for_idea(store: Store, idea_id: str | None) -> list[str]:
    """The ids of the domains an idea is connected to. An idea may connect to SEVERAL.
    EXPLICIT connections (`.domains.json`, set via the picker / on brainstorm) are
    authoritative; with none recorded, fall back to name-matching every domain whose
    name appears in IDEA.md (so older / brainstorm-drafted ideas are auto-connected)."""
    if not idea_id:
        return []
    explicit = store.get_idea_domain_ids(idea_id)
    if explicit is not None:  # connected at least once (may be an explicit empty list)
        return [d for d in explicit if store.domain_path(d) is not None]
    spec = store.get_spec(idea_id)
    if not spec:
        return []
    low = spec.lower()
    return [d.id for d in store.list_domains() if d.name and d.name.lower() in low]


def resolve_domain_id_for_idea(store: Store, idea_id: str | None) -> str | None:
    """The idea's PRIMARY (first) connected domain id, or None. Single-domain callers
    (pipeline context, benchmark default) use this; see resolve_domain_ids_for_idea."""
    ids = resolve_domain_ids_for_idea(store, idea_id)
    return ids[0] if ids else None


def get_idea_domains(store: Store, idea_id: str) -> dict:
    """The idea's connected domain ids, for the connect UI → {ideaId, domainIds}."""
    if store.idea_path(idea_id) is None:
        raise NotFound("Idea not found")
    return {"ideaId": idea_id, "domainIds": resolve_domain_ids_for_idea(store, idea_id)}


def set_idea_domains(store: Store, idea_id: str, domain_ids: list[str]) -> dict:
    """Explicitly connect an idea to a set of domains (the picker). Validates the ids and
    persists `.domains.json` (authoritative from now on); the connected domains are surfaced
    to the chat agent in its system prompt (inlined DOMAIN.md + abs paths read via `bash`).
    Returns {ideaId, domainIds}."""
    if store.idea_path(idea_id) is None:
        raise NotFound("Idea not found")
    valid = [d for d in domain_ids if store.domain_path(d) is not None]
    store.set_idea_domain_ids(idea_id, valid)
    _cleanup_domain_mounts(store, idea_id)  # retire any stale symlinks from the old scheme
    return {"ideaId": idea_id, "domainIds": valid}


_EXP_MODES = ("simulated", "executed", "ssh", "cli")


def get_idea_resources_view(store: Store, settings: LLMSettings, idea_id: str) -> dict:
    """The idea's allocated experiment resources for the Resources sub-tab: the EFFECTIVE
    compute (mode + SSH GPU host every experiment uses), the SSH remotes to choose from, and
    the active LLM (read-only; key shown as configured, never its value)."""
    if store.idea_path(idea_id) is None:
        raise NotFound("Idea not found")
    cfg = store.effective_run_config(idea_id)
    alloc = store.get_idea_resources(idea_id) or {}
    return {
        "ideaId": idea_id,
        "experimentMode": cfg.experiment_mode,
        "sshTargetId": cfg.ssh_target_id,
        "useReferenceCodebase": bool(alloc.get("useReferenceCodebase", True)),
        "sshTargets": store.get_hardware_state().get("sshTargets", []),
        "llmProvider": settings.provider,
        "llmModel": settings.model,
        "llmBaseUrl": settings.base_url,
        "llmKeyConfigured": bool(settings.api_key),
    }


def set_idea_resources(store: Store, idea_id: str, *, experiment_mode=None, ssh_target_id=None,
                       use_reference_codebase=None) -> None:
    """Pin the idea's experiment resources (each omitted field is left unchanged). Every
    experiment of this idea — auto OR manual from the Hypotheses tab — then runs with them
    (via store.effective_run_config)."""
    if store.idea_path(idea_id) is None:
        raise NotFound("Idea not found")
    alloc = store.get_idea_resources(idea_id) or {}
    if experiment_mode is not None:
        if experiment_mode and experiment_mode not in _EXP_MODES:
            raise NotFound(f"invalid experiment mode: {experiment_mode}")
        alloc["experimentMode"] = experiment_mode or None
    if ssh_target_id is not None:
        alloc["sshTargetId"] = ssh_target_id or None
    if use_reference_codebase is not None:
        alloc["useReferenceCodebase"] = bool(use_reference_codebase)
    store.set_idea_resources(idea_id, alloc)


def _setup_codebase_messages(store: Store, context_id: str, content: str,
                             arg: str, idea_id: str | None, domain_id: str | None):
    """Server-side /setup_codebase: download a domain's reference codebase now."""
    user_msg = store.add_message(context_id, "user", content)
    target = domain_id or resolve_domain_id_for_idea(store, idea_id)
    if not target:
        assistant = store.add_message(
            context_id, "assistant",
            "⚙️ /setup_codebase sets a DOMAIN's reference codebase — open a domain "
            "(or pin this idea to a domain) first.")
        return [user_msg, assistant]
    url = arg or codebase_url_from_spec(store.get_domain_spec(target) or "")
    if not url:
        assistant = store.add_message(
            context_id, "assistant",
            "⚙️ Usage: /setup_codebase <github-url> — e.g. "
            "`/setup_codebase https://github.com/owner/repo`.")
        return [user_msg, assistant]
    try:
        domain = set_domain_codebase(store, target, url)
        assistant = store.add_message(
            context_id, "assistant",
            f"📦 Reference codebase set: {url} — downloaded {domain.codebase_files} files. "
            "Experiments for ideas in this domain can now reuse it.",
            codebase_updated=True)
    except (codebase.CodebaseError, NotFound) as exc:
        assistant = store.add_message(
            context_id, "assistant", f"⚠️ Couldn't set the reference codebase: {exc}")
    return [user_msg, assistant]


def _auto_fetch_codebase(store: Store, domain: Domain, spec: str) -> int:
    """Best-effort: download the codebase the LLM named in `## Reference Codebase`.
    Returns the file count (0 if none / failed) — never raises (domain still created)."""
    url = codebase_url_from_spec(spec)
    if not url:
        return 0
    try:
        info = codebase.download_codebase(url, store.domain_codebase_dir(domain.id))
        store.set_domain_codebase_meta(domain.id, info["url"], info["fileCount"])
        return info["fileCount"]
    except codebase.CodebaseError:
        return 0


async def auto_create_domain(store: Store, settings: LLMSettings, prompt: str) -> Domain:
    """Auto mode: the LLM writes the full DOMAIN.md from a short prompt.
    Runs two concurrent OpenAlex searches: broad (5 years, by citation) and
    SOTA (last 2 years, by recency) to ground the spec in real papers.
    Raises llm.LLMNotConfigured / llm.LLMError on failure."""
    broad, sota = await literature.search_for_domain(prompt)
    paper_ctx = literature.format_domain_papers_for_prompt(broad, sota)
    user_content = f"{prompt}\n\n{paper_ctx}" if paper_ctx else prompt
    result = await llm.chat(
        settings, AUTO_DOMAIN_SYSTEM,
        [{"role": "user", "content": user_content}], max_tokens=4096,
    )
    match = NEW_DOMAIN_BLOCK.search(result.text)
    spec = match.group(1) if match else result.text
    name = title_from_spec(spec, prompt[:60])
    domain = store.add_domain(name, spec=spec)
    # off-thread: the codebase download/extract is blocking — keep it off the event loop.
    await asyncio.to_thread(_auto_fetch_codebase, store, domain, spec)  # updates domain.json on disk
    return next((d for d in store.list_domains() if d.id == domain.id), domain)


def _domain_general_target(spec: str) -> str:
    m = re.search(r"##\s*General Target\s*\n+([^\n]+)", spec or "")
    return (m.group(1).strip() if m else "")[:160]


async def find_similar_domain(store: Store, settings: LLMSettings, topic: str) -> "Domain | None":
    """An EXISTING domain that covers essentially the same field as *topic* (so auto
    mode reuses + enriches it instead of duplicating), or None. Best-effort: returns
    None if there are no domains or the LLM is unavailable/uncertain."""
    domains = store.list_domains()
    if not domains:
        return None
    listing = "\n".join(
        f"{i}. {d.name}" + (f" — {gt}" if (gt := _domain_general_target(store.get_domain_spec(d.id) or "")) else "")
        for i, d in enumerate(domains, 1))
    try:
        result = await llm.chat(
            settings, DOMAIN_MATCH_SYSTEM,
            [{"role": "user", "content": f'New topic: "{topic}"\n\nExisting domains:\n{listing}'}],
            max_tokens=8)
    except (llm.LLMNotConfigured, llm.LLMError):
        return None
    m = re.search(r"\d+", result.text or "")
    idx = int(m.group(0)) if m else 0
    return domains[idx - 1] if 1 <= idx <= len(domains) else None


async def enrich_domain(store: Store, settings: LLMSettings, domain: Domain) -> Domain:
    """Refresh an existing domain's DOMAIN.md with RECENT ADVANCES (live OpenAlex
    SOTA/recent search → re-summarized recent-papers sections). Best-effort: keeps the
    current spec if the search or LLM fails."""
    spec = store.get_domain_spec(domain.id) or ""
    try:
        broad, sota = await literature.search_for_domain(domain.name)
        paper_ctx = literature.format_domain_papers_for_prompt(broad, sota)
    except Exception:
        paper_ctx = ""
    user = f"Current DOMAIN.md:\n{spec}" + (f"\n\n{paper_ctx}" if paper_ctx else "")
    try:
        result = await llm.chat(settings, DOMAIN_ENRICH_SYSTEM,
                                [{"role": "user", "content": user}], max_tokens=4096)
    except (llm.LLMNotConfigured, llm.LLMError):
        return domain
    m = DOMAIN_SPEC_BLOCK.search(result.text)
    new_spec = (m.group(1).strip() if m else "").strip()
    if new_spec:
        store.put_domain_spec(domain.id, new_spec)
    return next((d for d in store.list_domains() if d.id == domain.id), domain)


async def auto_create_idea(store: Store, settings: LLMSettings, domain: Domain) -> Idea:
    """Auto mode: generate ONE complete IDEA.md grounded in *domain* (pinned to it by
    name, with default/SOTA baselines + a standard benchmark in its Main Result) and
    create the idea. Reuses the brainstorm-draft prompt. Raises
    llm.LLMNotConfigured/llm.LLMError on failure."""
    spec = store.get_domain_spec(domain.id) or domain.name
    system = BRAINSTORM_DRAFT_SYSTEM.format(n=1, domains=spec[:DOMAIN_SPEC_CHAR_LIMIT])
    result = await llm.chat(
        settings, system,
        [{"role": "user", "content": (
            "Draft ONE strong, concrete, testable research idea from this domain spec. "
            f"Pin the domain '{domain.name}' by name in the Domain & Literature section. "
            "In Main Result, name the DEFAULT/SOTA baseline algorithm(s) the method will "
            "beat and the standard benchmark dataset + primary metric to report.")}],
        max_tokens=4096,
    )
    m = IDEA_DRAFT_BLOCK.search(result.text)
    draft = (m.group(1).strip() if m else result.text.strip())
    if not draft:
        raise llm.LLMError("Model returned no idea draft — try again")
    idea = store.add_idea(title_from_spec(draft, f"{domain.name} idea"))
    store.put_spec(idea.id, draft)
    return idea


def _auto_run_apply(st: dict, ev: dict) -> None:
    """Fold one pipeline event into the persisted auto-run status snapshot (`st`)."""
    t = ev.get("type")
    if t == "phase":
        ph = ev.get("phase", "")
        st["label"] = ev.get("label", st.get("label", ""))
        if ph in ("doctor", "domain", "idea"):
            st["phase"] = ph
        elif ph in ("hypothesis_map", "references") or ph.startswith("hyp"):
            st["phase"] = "hypotheses"
        elif ph in ("figures", "paper", "compile"):
            st["phase"] = "paper"
    elif t == "domain_created":
        st["domainId"], st["domainName"] = ev.get("domainId"), ev.get("name")
    elif t == "idea_created":
        st["ideaId"], st["ideaTitle"] = ev.get("ideaId"), ev.get("title")
    elif t == "round":
        st["phase"] = "hypotheses"
        st["round"] = ev.get("round", st.get("round", 0))
        st["currentHypothesisId"] = ev.get("hypothesisId")
    elif t in ("round_done", "positives"):  # 'positives' seeds prior-run supported count
        st["positives"] = ev.get("positives", st.get("positives", 0))
    elif t == "paper_ready":
        st["paperReady"] = True
    elif t == "done":
        st["status"], st["phase"] = "done", "done"
    elif t in ("error", "needs_domain"):
        st["status"], st["error"] = "error", ev.get("message", "error")


def _put_auto_status(store: Store, st: dict) -> None:
    """Persist the snapshot under its idea (per-idea, so ideas run in parallel). For a
    topic run this is a no-op until `idea_created` sets ideaId — the idea's dir exists
    only then; the foreground terminal still streams the early doctor/domain phases."""
    iid = st.get("ideaId")
    if iid:
        store.put_idea_auto_run(iid, st)


async def _persist_auto_status(store: Store, st: dict, events) -> AsyncIterator[dict]:
    """Forward *events*, folding each into the per-idea auto-run snapshot. If the
    stream closes while still 'running' (Ctrl+C / cancel / crash), records it as
    'stopped' (or 'error') so `auto status` / the web UI show it was interrupted."""
    _put_auto_status(store, st)
    try:
        async for ev in events:
            _auto_run_apply(st, ev)
            st["updatedAt"] = time.time()
            _put_auto_status(store, st)
            yield ev
    finally:
        if st["status"] == "running":
            st["status"] = "error" if st.get("error") else "stopped"
            st["updatedAt"] = time.time()
            _put_auto_status(store, st)


async def stream_auto_research(
    store: Store,
    settings: LLMSettings,
    home,
    topic: str,
    *,
    target_positive: int = 2,
    max_hypotheses: int = 6,
    page_limit: int = 8,
    max_depth: int = 3,
    experiment_mode: str | None = None,
    ssh_target_id: str | None = None,
    writing_style: str | None = None,
    benchmark: str | None = None,
    use_reference_codebase: bool = True,
    fill_page: bool = False,
) -> AsyncIterator[dict]:
    """Public auto-mode entry: runs the pipeline AND persists a PER-IDEA status
    snapshot (once the idea is created) on every event, so `auto status` and the web
    UI can watch the overall phase from outside the launching terminal. **Stop** with
    Ctrl+C (records 'stopped'); **resume** later with `stream_auto_resume`. The per-run
    overrides (experiment_mode / ssh_target_id / writing_style / use_reference_codebase)
    are forwarded to the iterative pipeline."""
    now = time.time()
    st = {
        "topic": topic, "status": "running", "phase": "doctor", "label": "Starting…",
        "ideaId": None, "ideaTitle": None, "domainId": None, "domainName": None,
        "round": 0, "positives": 0, "targetPositive": target_positive,
        "maxHypotheses": max_hypotheses, "pageLimit": page_limit, "maxDepth": max_depth,
        "currentHypothesisId": None, "paperReady": False, "pid": os.getpid(),
        "startedAt": now, "updatedAt": now, "error": None,
    }
    async for ev in _persist_auto_status(store, st, _auto_research_events(
        store, settings, home, topic,
        target_positive=target_positive, max_hypotheses=max_hypotheses, page_limit=page_limit,
        max_depth=max_depth,
        experiment_mode=experiment_mode, ssh_target_id=ssh_target_id,
        writing_style=writing_style, benchmark=benchmark, use_reference_codebase=use_reference_codebase,
        fill_page=fill_page,
    )):
        yield ev


def _single_resumable_run(store: Store) -> dict | None:
    """The most-recently-updated resumable auto run (stopped/interrupted/error, or a
    crashed 'running'), for a no-arg `auto resume`. None if there are none."""
    runs = [r for r in store.list_auto_runs() if r.get("ideaId")]
    cands = [r for r in runs if r.get("status") in ("stopped", "interrupted", "error")]
    if not cands:
        cands = [r for r in runs if r.get("status") != "done"]
    cands.sort(key=lambda r: r.get("updatedAt", 0), reverse=True)
    return cands[0] if cands else None


async def stream_auto_resume(store: Store, settings: LLMSettings, home,
                             idea_id: str | None = None) -> AsyncIterator[dict]:
    """Resume an `auto` run on its EXISTING idea — the iterative pipeline is resumable
    (it skips hypotheses/phases whose artifacts already exist and tests the remaining
    'untested' nodes), so this continues from where Ctrl+C left off and then writes the
    paper. Reuses the stored stop settings (positives / max / page limit). With no
    *idea_id* it resumes the single stopped/interrupted run, if there's exactly one."""
    from paperclaw import iterative_pipeline as ip
    prev = store.get_idea_auto_run(idea_id) if idea_id else _single_resumable_run(store)
    if not prev or not prev.get("ideaId"):
        yield {"type": "error",
               "message": 'No auto run to resume — start one with: paperclaw run "<topic>"'}
        return
    idea_id = prev["ideaId"]
    st = {**prev, "status": "running", "label": "Resuming…", "error": None,
          "pid": os.getpid(), "updatedAt": time.time()}

    async def _events():
        yield {"type": "delta", "text": f"↻ Resuming auto run on idea {idea_id}…\n"}
        async for ev in ip.stream_iterative_research_events(
            store, settings, idea_id, restart=False,
            max_hypotheses=int(prev.get("maxHypotheses", 6)),
            page_limit=int(prev.get("pageLimit", 8)),
            target_positive=int(prev.get("targetPositive", 2)),
            max_depth=int(prev.get("maxDepth", 3)),
        ):
            yield ev

    async for ev in _persist_auto_status(store, st, _events()):
        yield ev


async def stream_auto_idea(
    store: Store,
    settings: LLMSettings,
    home,
    idea_id: str,
    *,
    target_positive: int = 2,
    max_hypotheses: int = 6,
    page_limit: int = 8,
    max_depth: int = 3,
    experiment_mode: str | None = None,
    ssh_target_id: str | None = None,
    writing_style: str | None = None,
    benchmark: str | None = None,
    use_reference_codebase: bool = True,
    fill_page: bool = False,
) -> AsyncIterator[dict]:
    """Auto-run an EXISTING idea (skip domain/idea creation): run the iterative
    pipeline on it and persist auto-run status (banner-tracked, resumable). This is
    what the web UI's per-idea ⚡ Auto run + `paperclaw run --idea <id>` use."""
    from paperclaw import iterative_pipeline as ip
    idea = next((i for i in store.list_ideas() if i.id == idea_id), None)
    if idea is None:
        # Record the failure in the idea's snapshot (if its dir exists) so the
        # banner/`auto status` show an error instead of a stuck "running".
        prev = store.get_idea_auto_run(idea_id) or {}
        store.put_idea_auto_run(idea_id, {**prev, "ideaId": idea_id, "status": "error",
                                          "error": f"idea {idea_id} not found", "updatedAt": time.time()})
        yield {"type": "error", "message": f"idea {idea_id} not found"}
        return
    now = time.time()
    st = {
        "topic": idea.title, "status": "running", "phase": "hypotheses", "label": "Starting…",
        "ideaId": idea_id, "ideaTitle": idea.title, "domainId": None, "domainName": None,
        "round": 0, "positives": 0, "targetPositive": target_positive,
        "maxHypotheses": max_hypotheses, "pageLimit": page_limit, "maxDepth": max_depth,
        "currentHypothesisId": None, "paperReady": False, "pid": os.getpid(),
        "startedAt": now, "updatedAt": now, "error": None,
    }

    async def _events():
        async for ev in ip.stream_iterative_research_events(
            store, settings, idea_id, restart=False,
            max_hypotheses=max_hypotheses, page_limit=page_limit, target_positive=target_positive,
            max_depth=max_depth,
            experiment_mode=experiment_mode, ssh_target_id=ssh_target_id,
            writing_style=writing_style, benchmark=benchmark, use_reference_codebase=use_reference_codebase,
            fill_page=fill_page):
            yield ev

    async for ev in _persist_auto_status(store, st, _events()):
        yield ev


def idea_auto_run_view(store: Store, idea_id: str) -> dict | None:
    """An idea's auto-run snapshot, reconciled: a 'running' run whose process is gone
    (backend/CLI crash) is downgraded to 'interrupted' so the banner doesn't hang."""
    from paperclaw import jobs
    st = store.get_idea_auto_run(idea_id)
    if st and st.get("status") == "running" and not jobs._pid_alive(st.get("pid")):
        st = {**st, "status": "interrupted", "label": "Interrupted (process gone)",
              "updatedAt": time.time()}
        store.put_idea_auto_run(idea_id, st)
    return st


def read_auto_run_log(store: Store, idea_id: str, offset: int = 0) -> dict:
    """Tail the idea's detached auto-run log from byte *offset* — the rendered live
    agent output (phases / thinking / streamed text). Returns ``{text, next, running}``
    so the web UI's feedback panel can poll for new output."""
    idir = store.idea_path(idea_id)
    text, nxt = "", max(0, offset)
    if idir is not None:
        p = idir / "auto_run.log"
        if p.is_file():
            size = p.stat().st_size
            if nxt < size:
                with open(p, "rb") as f:
                    f.seek(nxt)
                    text = f.read().decode("utf-8", errors="replace")
            nxt = size
    view = idea_auto_run_view(store, idea_id) or {}
    return {"text": text, "next": nxt, "running": view.get("status") == "running"}


def list_auto_runs_view(store: Store) -> list[dict]:
    """All ideas' auto-run snapshots, each reconciled (running-but-dead → interrupted)."""
    out: list[dict] = []
    for st in store.list_auto_runs():
        iid = st.get("ideaId")
        out.append(idea_auto_run_view(store, iid) if iid else st)
    return [s for s in out if s]


def stop_auto_run(store: Store, idea_id: str | None = None) -> dict:
    """Stop an idea's auto run from outside its terminal (the `auto stop` command /
    a UI button): cancel its in-flight experiment job and SIGINT the orchestration
    process (like Ctrl+C) so it shuts down and records 'stopped'. Idempotent + safe if
    the process is already gone (just marks 'stopped'). With no *idea_id* it stops the
    single running run, if there's exactly one. Returns ``{ok, detail}``."""
    import signal
    from paperclaw import jobs
    if not idea_id:
        running = [r for r in store.list_auto_runs() if r.get("status") == "running" and r.get("ideaId")]
        if len(running) == 1:
            idea_id = running[0]["ideaId"]
        elif not running:
            return {"ok": False, "detail": "no running auto run found"}
        else:
            return {"ok": False, "detail": "multiple auto runs — specify which with --idea <id>"}
    st = store.get_idea_auto_run(idea_id)
    if not st:
        return {"ok": False, "detail": "no auto run for this idea"}
    if st.get("status") != "running":
        return {"ok": False, "detail": f"auto run is already '{st.get('status')}'"}
    notes: list[str] = []
    # 1. cancel the in-flight detached experiment job for the current hypothesis
    hid = st.get("currentHypothesisId")
    if hid:
        try:
            if jobs.cancel_experiment_job(store, idea_id, hid):
                notes.append(f"cancelled experiment {hid}")
        except Exception:
            pass
    # 2. signal the orchestration process — SIGINT runs its finally (→ 'stopped')
    pid = st.get("pid")
    if jobs._pid_alive(pid):
        try:
            os.kill(int(pid), signal.SIGINT)
            notes.append(f"signalled auto process (pid {pid})")
        except (ProcessLookupError, PermissionError, OSError, ValueError, TypeError):
            pass
    # 3. mark stopped now (immediate, even if the process already exited)
    st["status"], st["label"], st["updatedAt"] = "stopped", "Stopped", time.time()
    store.put_idea_auto_run(idea_id, st)
    return {"ok": True, "detail": "; ".join(notes) or "marked stopped"}


def launch_auto_run(store: Store, home, topic: str = "", *, idea_id: str | None = None,
                    target_positive: int = 2, max_hypotheses: int = 6, page_limit: int = 8,
                    max_depth: int = 3,
                    experiment_mode: str | None = None, ssh_target_id: str | None = None,
                    writing_style: str | None = None, benchmark: str | None = None,
                    use_reference_codebase: bool = True,
                    fill_page: bool = False) -> dict:
    """Start an auto run as a DETACHED process (so the web UI can launch one without
    holding a connection) and seed its status snapshot — the banner/`auto status` then
    track it, and `auto stop` stops it. With *idea_id* it runs the pipeline on that
    EXISTING idea (no domain/idea creation); otherwise it creates one from *topic*.
    Ideas run in PARALLEL — only refuses if THIS idea is already auto-running. The
    per-run overrides become `auto run` flags inherited by the detached child."""
    import subprocess
    import sys
    from paperclaw import jobs
    common = ["--positive", str(target_positive),
              "--max-hypotheses", str(max_hypotheses), "--page-limit", str(page_limit),
              "--max-depth", str(max_depth)]
    if experiment_mode:
        common += ["--experiment-mode", experiment_mode]
    if ssh_target_id:
        common += ["--ssh-target", ssh_target_id]
    if writing_style:
        common += ["--style", writing_style]
    if benchmark:
        common += ["--benchmark", benchmark]
    if not use_reference_codebase:
        common += ["--no-codebase"]
    if fill_page:
        common += ["--fill-page"]
    if idea_id:
        idea = next((i for i in store.list_ideas() if i.id == idea_id), None)
        if idea is None:
            return {"ok": False, "detail": f"idea {idea_id} not found"}
        prev = store.get_idea_auto_run(idea_id)
        if prev and prev.get("status") == "running" and jobs._pid_alive(prev.get("pid")):
            return {"ok": False, "detail": "this idea is already auto-running"}
        cmd = ["run", "--idea", idea_id, *common]
        seed = {"topic": idea.title, "phase": "hypotheses", "ideaId": idea_id, "ideaTitle": idea.title}
    else:
        topic = (topic or "").strip()
        if not topic:
            return {"ok": False, "detail": "topic is required"}
        cmd = ["run", topic, *common]
        seed = {"topic": topic, "phase": "doctor", "ideaId": None, "ideaTitle": None}

    # ABSOLUTE home: the child runs with this as cwd AND inherits it as PAPERCLAW_HOME —
    # a relative path would re-resolve against the new cwd (→ saves/saves) and the
    # child wouldn't find the existing idea.
    from pathlib import Path
    home_abs = Path(home).resolve()
    # Per-idea log (idea runs) so the live "agent feedback" panel can tail THIS run's
    # output without mixing parallel runs; topic runs (no idea yet) use the home log.
    idir = store.idea_path(idea_id) if idea_id else None
    log_path = (idir / "auto_run.log") if idir else (home_abs / "auto_run.log")
    log = open(log_path, "wb")  # noqa: SIM115 — owned by the detached child
    proc = subprocess.Popen(
        [sys.executable, "-m", "paperclaw.cli", *cmd],
        cwd=str(home_abs), env={**os.environ, "PAPERCLAW_HOME": str(home_abs)},
        stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    log.close()
    now = time.time()
    st = {
        "status": "running", "label": "Starting…", "domainId": None, "domainName": None,
        "round": 0, "positives": 0, "targetPositive": target_positive,
        "maxHypotheses": max_hypotheses, "pageLimit": page_limit, "maxDepth": max_depth,
        "currentHypothesisId": None, "paperReady": False, "pid": proc.pid,
        "startedAt": now, "updatedAt": now, "error": None, **seed,
    }
    # Seed the per-idea snapshot now (idea launch). Topic runs have no idea yet — the
    # detached child persists once it creates the idea (`idea_created`).
    if idea_id:
        store.put_idea_auto_run(idea_id, st)
    return {"ok": True, "detail": "started", "pid": proc.pid}


async def _auto_research_events(
    store: Store,
    settings: LLMSettings,
    home,
    topic: str,
    *,
    target_positive: int = 2,
    max_hypotheses: int = 6,
    page_limit: int = 8,
    max_depth: int = 3,
    experiment_mode: str | None = None,
    ssh_target_id: str | None = None,
    writing_style: str | None = None,
    benchmark: str | None = None,
    use_reference_codebase: bool = True,
    fill_page: bool = False,
) -> AsyncIterator[dict]:
    """Fully autonomous research from a one-line topic: doctor → create domain →
    draft idea → test hypotheses → write the paper (figures + overview). The
    hypothesis loop stops once *target_positive* hypotheses are SUPPORTED, or after
    *max_hypotheses* tested. Yields the iterative-pipeline events plus auto-specific
    ones: ``doctor`` / ``domain_created`` / ``idea_created``."""
    from paperclaw import iterative_pipeline as ip
    from paperclaw import images

    # 1. Doctor — verify the environment + LLM + figure (image) API up front, so we
    #    don't burn a long run on a misconfigured setup.
    yield {"type": "phase", "phase": "doctor", "label": "Checking environment (doctor)…"}
    report = environment_report(settings, home)
    yield {"type": "doctor", "ok": report.ok, "checks": [
        {"label": c.label, "status": c.status, "detail": c.detail} for c in report.checks]}
    if not report.ok:
        bad = "; ".join(f"{c.label}: {c.detail}" for c in report.checks if c.status == "fail")
        yield {"type": "error", "message": f"Environment not ready — fix first: {bad}"}
        return
    yield {"type": "delta", "text": (
        "🖼️  figure/image API configured — an overview figure will be generated into the paper.\n"
        if images.is_configured(settings) else
        "🖼️  no image API configured — figures fall back to matplotlib/TikZ diagrams "
        "(set one in Settings → Image generation for AI overview figures).\n")}

    # 2. Domain — reuse a very-similar existing domain (enriching it with recent
    #    advances) instead of creating a duplicate; otherwise survey a fresh one.
    yield {"type": "phase", "phase": "domain", "label": f"Surveying the domain for “{topic}”…"}
    try:
        existing = await find_similar_domain(store, settings, topic)
        if existing is not None:
            yield {"type": "delta",
                   "text": f"♻️ Reusing existing domain “{existing.name}” — enriching with recent advances…\n"}
            domain = await enrich_domain(store, settings, existing)
            reused = True
        else:
            domain = await auto_create_domain(store, settings, topic)
            reused = False
    except (llm.LLMNotConfigured, llm.LLMError) as exc:
        yield {"type": "error", "message": f"domain creation failed: {exc}"}
        return
    yield {"type": "domain_created", "domainId": domain.id, "name": domain.name, "reused": reused}

    # 3. Idea (default algorithm + performance baselines)
    yield {"type": "phase", "phase": "idea", "label": "Drafting the research idea…"}
    try:
        idea = await auto_create_idea(store, settings, domain)
    except (llm.LLMNotConfigured, llm.LLMError) as exc:
        yield {"type": "error", "message": f"idea creation failed: {exc}"}
        return
    yield {"type": "idea_created", "ideaId": idea.id, "title": idea.title}

    # 4. Hypothesis loop (stop at N positives or M tested) → paper with figures.
    async for ev in ip.stream_iterative_research_events(
        store, settings, idea.id,
        max_hypotheses=max_hypotheses, page_limit=page_limit, target_positive=target_positive,
        max_depth=max_depth,
        experiment_mode=experiment_mode, ssh_target_id=ssh_target_id,
        writing_style=writing_style, benchmark=benchmark, use_reference_codebase=use_reference_codebase,
        fill_page=fill_page,
    ):
        yield ev


# ── SSE event generators ─────────────────────────────────────────────────────

def _msg_dump(msg) -> dict:
    return msg.model_dump(by_alias=True)


async def stream_auto_create_domain_events(
    store: Store, settings: LLMSettings, prompt: str
) -> AsyncIterator[dict]:
    """Async generator of SSE event dicts for domain auto-creation.

    Events:
      {"type": "status",   "message": "..."}        — progress label
      {"type": "search",   "query", "broad", "sota"} — OpenAlex results (paper labels)
      {"type": "thinking", "text": "..."}            — model reasoning chunk (Anthropic)
      {"type": "delta",    "text": "..."}            — DOMAIN.md text chunk (live preview)
      {"type": "done",     "result": {...domain...}} — final saved domain
      {"type": "error",    "message": "..."}         — failure
    """
    yield {"type": "status", "message": "Searching OpenAlex — recent & SOTA papers…"}
    broad, sota = await literature.search_for_domain(prompt)
    n = len(broad) + len(sota)
    yield {
        "type": "search",
        "query": prompt,
        "broad": [literature.paper_label(p) for p in broad],
        "sota": [literature.paper_label(p) for p in sota],
    }
    if n == 0 and literature.last_error():
        yield {"type": "status",
               "message": f"{literature.last_error()} — generating domain spec from the model's knowledge…"}
    else:
        yield {"type": "status", "message": f"Found {n} papers. Generating domain spec…"}

    paper_ctx = literature.format_domain_papers_for_prompt(broad, sota)
    user_content = f"{prompt}\n\n{paper_ctx}" if paper_ctx else prompt

    full_text = ""
    try:
        async for ev in llm.stream_chat_thinking(
            settings, AUTO_DOMAIN_SYSTEM,
            [{"role": "user", "content": user_content}],
        ):
            if ev["type"] == "thinking":
                yield {"type": "thinking", "text": ev["text"]}
            else:
                full_text += ev["text"]
                yield {"type": "delta", "text": ev["text"]}
    except llm.LLMNotConfigured as exc:
        yield {"type": "error", "message": str(exc)}
        return
    except llm.LLMError as exc:
        yield {"type": "error", "message": str(exc)}
        return

    match = NEW_DOMAIN_BLOCK.search(full_text)
    spec = match.group(1) if match else full_text
    name = title_from_spec(spec, prompt[:60])
    domain = store.add_domain(name, spec=spec)

    cb_url = codebase_url_from_spec(spec)
    if cb_url:
        yield {"type": "status", "message": f"Downloading reference codebase {cb_url}…"}
        # off-thread: the tarball download + extraction is BLOCKING; running it on the
        # event loop would freeze every other request (chat, parallel runs) until done.
        files = await asyncio.to_thread(_auto_fetch_codebase, store, domain, spec)
        domain = next((d for d in store.list_domains() if d.id == domain.id), domain)
        yield {"type": "codebase", "url": cb_url, "files": files,
               "message": (f"⬇ downloaded {files} files" if files
                           else "codebase download skipped")}

    yield {"type": "done", "result": domain.model_dump(by_alias=True)}


async def stream_generate_seeds_events(
    store: Store,
    settings: LLMSettings,
    hint: str | None = None,
    idea_types: list[str] | None = None,
    emphasis: list[str] | None = None,
    count: int | None = None,
) -> AsyncIterator[dict]:
    """Async generator of SSE event dicts for brainstorm generation.

    Streams the model live so the UI shows progress: ``status`` (search/brainstorm
    phase), ``thinking`` (reasoning), ``delta`` (the drafts as they're written), then
    ``done`` (the saved seeds) or ``error``."""
    system, prompt, max_tokens, mode, statuses = await _build_brainstorm(
        store, settings, hint, idea_types, emphasis, count)
    for msg in statuses:
        yield {"type": "status", "message": msg}

    text = ""
    try:
        async for ev in llm.stream_chat_thinking(
            settings, system, [{"role": "user", "content": prompt}], max_tokens=max_tokens,
        ):
            if ev["type"] == "thinking":
                yield {"type": "thinking", "text": ev["text"]}
            else:
                text += ev["text"]
                yield {"type": "delta", "text": ev["text"]}
    except (llm.LLMNotConfigured, llm.LLMError) as exc:
        yield {"type": "error", "message": str(exc)}
        return

    created = _persist_brainstorm(store, text, mode)
    if not created:
        yield {"type": "error", "message": (
            "Model returned no idea drafts — try again" if mode == "draft"
            else "Model returned no idea seeds — try again")}
        return
    yield {"type": "done", "results": [s.model_dump(by_alias=True) for s in created]}


async def stream_chat_events(
    store: Store,
    settings: LLMSettings,
    content: str,
    idea_id: str | None = None,
    seed_id: str | None = None,
    domain_id: str | None = None,
) -> AsyncIterator[dict]:
    """Streaming version of send_chat — yields SSE event dicts.

    Events:
      {"type": "delta", "text": "<chunk>"}
      {"type": "done", "messages": [<user_msg>, <assistant_msg>]}
      {"type": "error", "message": "<reason>"}
    """
    # ── Resolve context (mirrors send_chat) ──────────────────────────────────
    seed = store.get_seed(seed_id) if seed_id else None
    if seed_id and seed is None:
        yield {"type": "error", "message": "Seed not found"}
        return
    domain_spec = store.get_domain_spec(domain_id) if domain_id else None
    if domain_id and domain_spec is None:
        yield {"type": "error", "message": "Domain not found"}
        return

    if domain_spec is not None:
        context_id = f"{DOMAIN_PREFIX}{domain_id}"
        system = DOMAIN_CHAT_SYSTEM.format(spec=domain_spec)
    elif seed is not None:
        context_id = f"{SEED_PREFIX}{seed.id}"
        system = SEED_CHAT_SYSTEM.format(
            draft=seed.draft or f"# {seed.text}\n\n_(no draft yet)_"
        )
    elif idea_id:
        spec = store.get_spec(idea_id)
        if spec is None:
            yield {"type": "error", "message": "Idea not found"}
            return
        context_id = idea_id
        system = _idea_chat_system(store, idea_id, spec)
    else:
        context_id = SCRATCH_ID
        system = SCRATCH_SYSTEM + "\n" + DOMAIN_WIZARD_RULE
    system = system + "\n" + QUESTION_RULE

    stripped = content.strip()

    # /pin_idea, /setup_codebase: no LLM/streaming needed — delegate to send_chat
    if (stripped.lower().startswith(PIN_IDEA_COMMAND)
            or stripped.lower().startswith(SETUP_CODEBASE_COMMAND)):
        try:
            msgs = await send_chat(store, settings, content,
                                   idea_id=idea_id, seed_id=seed_id, domain_id=domain_id)
            yield {"type": "done", "messages": [_msg_dump(m) for m in msgs]}
        except Exception as exc:
            yield {"type": "error", "message": str(exc)}
        return

    # /validate_references: deterministic (no LLM) but STREAMS per-entry status to chat
    if stripped.lower().startswith(VALIDATE_REFERENCES_COMMAND):
        user_msg = store.add_message(context_id, "user", content)
        if not idea_id or seed is not None or domain_spec is not None:
            assistant = store.add_message(
                context_id, "assistant",
                "⚙️ /validate_references works on an IDEA's `ref.bib` — select an idea first.")
            yield {"type": "done", "messages": [_msg_dump(user_msg), _msg_dump(assistant)]}
            return
        report = ""
        try:
            async for chunk in stream_validate_references(store, idea_id):
                report += chunk
                yield {"type": "delta", "text": chunk}
        except Exception as exc:  # network etc. — don't crash the chat
            report += f"\n⚠️ Validation error: {exc}"
            yield {"type": "delta", "text": f"\n⚠️ Validation error: {exc}"}
        assistant = store.add_message(context_id, "assistant", report)
        yield {"type": "done", "messages": [_msg_dump(user_msg), _msg_dump(assistant)]}
        return

    llm_content = content
    if stripped.lower().startswith(IDEA_GENERATION_COMMAND):
        rest = stripped[len(IDEA_GENERATION_COMMAND):].strip()
        llm_content = (rest or "Create the idea from our conversation.") + IDEA_GENERATION_DIRECTIVE
    elif stripped.lower().startswith(HYPOTHESIS_MAP_COMMAND):
        rest = stripped[len(HYPOTHESIS_MAP_COMMAND):].strip()
        llm_content = (rest or "Generate the hypothesis map for this idea.") + HYPOTHESIS_MAP_DIRECTIVE
    elif stripped.lower().startswith(GENERATE_PLAN_COMMAND):
        hid = stripped[len(GENERATE_PLAN_COMMAND):].strip() or "H1"
        llm_content = f"Generate the testing plan for {hid}." + GENERATE_PLAN_DIRECTIVE.replace("{hid}", hid)
    elif stripped.lower().startswith(GENERATE_REPORT_COMMAND):
        hid = stripped[len(GENERATE_REPORT_COMMAND):].strip() or "H1"
        llm_content = f"Generate the report for {hid}." + GENERATE_REPORT_DIRECTIVE.replace("{hid}", hid)
    elif stripped.lower().startswith(WRITE_PAPER_COMMAND):
        from paperclaw.prompts.ideas import PAGE_FILL_NOTE
        from paperclaw.prompts.pipeline import PAPER_RIGOR_RULES
        rest = stripped[len(WRITE_PAPER_COMMAND):].strip()
        fill_page = "--fill-page" in rest
        rest = rest.replace("--fill-page", "").strip()
        rest, style_name = _extract_style_arg(rest)
        ids = rest.strip() or "all supported hypotheses"
        paper_file = store.next_paper_name(idea_id) if idea_id else "paper.md"  # v2, v3… if one exists
        # A named style REPLACES the house default; with none chosen, inject DEFAULT_STYLE
        # so BOTH the narrative structure and the prose voice come from a writing style,
        # not the system prompt.
        style_md = resolve_writing_style(
            store, resolve_domain_id_for_idea(store, idea_id), style_name)
        style_block = ""
        if style_md:
            style_block = ("\n- WRITING STYLE — follow this prose-style guide for the paper's "
                           f"narrative structure, voice, and phrasing throughout:\n{style_md}")
        # The /write_paper task spec is a SYSTEM-level instruction (the writing-style
        # guide is injected into it); the user message is just the bare ask.
        system = system + "\n" + (WRITE_PAPER_DIRECTIVE
                                  .replace("{hypotheses}", ids)
                                  .replace("{paper_file}", paper_file)
                                  .replace("{writing_style}", style_block)
                                  .replace("{rigor_rules}", PAPER_RIGOR_RULES)
                                  .replace("{page_fill}", PAGE_FILL_NOTE if fill_page else ""))
        llm_content = f"Write the paper for {ids}."
    elif stripped.lower().startswith(SETUP_VENUE_COMMAND):
        rest = stripped[len(SETUP_VENUE_COMMAND):].strip()
        llm_content = "Set up the venue template." + SETUP_VENUE_DIRECTIVE.replace("{venue}", f" for {rest}" if rest else "")
    elif stripped.lower().startswith(SETUP_BENCHMARK_COMMAND):
        rest = stripped[len(SETUP_BENCHMARK_COMMAND):].strip()
        llm_content = "Set up the benchmark template." + SETUP_BENCHMARK_DIRECTIVE.replace("{paper}", f" from {rest}" if rest else "")

    history = store.list_messages(context_id)[-HISTORY_LIMIT:]
    llm_messages = [
        {"role": m.role, "content": m.content} for m in history if m.role != "system"
    ]
    llm_messages.append({"role": "user", "content": llm_content})

    user_msg = store.add_message(context_id, "user", content)

    # Resolve base_dir for file tools (idea or domain workspace).
    base_dir = None
    if idea_id and seed is None and domain_spec is None:
        base_dir = store.idea_path(idea_id)
    elif domain_id and domain_spec is not None:
        base_dir = store.domain_path(domain_id)

    # deepagents chat editor (DEFAULT for any workspace chat — idea or domain): its
    # built-in file tools edit real files reliably, instead of the diff-based
    # apply_patch. The self-defined tool loop stays defined but unused here; force it
    # back with chat_agent="builtin". (Seed/scratch have no workspace → plain chat.)
    use_deep = (base_dir is not None
                and (settings.chat_agent or "").lower() != "builtin" and deep_chat.available())

    # Addenda go to every provider — chat_with_tools wires tools to both Anthropic
    # and OpenAI-compatible endpoints, so both need the usage instructions.
    if base_dir is not None:
        if use_deep:
            system = system + "\n" + DEEP_CHAT_ADDENDUM
        elif domain_spec is not None:
            system = system + "\n" + DOMAIN_TOOL_ADDENDUM
        elif idea_id and seed is None:
            system = system + "\n" + CHAT_TOOL_ADDENDUM

    # ── Stream LLM response ──────────────────────────────────────────────────
    # With a workspace dir the assistant can call tools, so we run the streaming
    # tool loop: text streams to the UI chunk by chunk while tool calls execute
    # between rounds. The terminal "final" event carries the canonical last-round
    # text (used for block parsing/persistence) plus the files the tools wrote.
    full_text = ""
    stream_files_modified: frozenset[str] = frozenset()
    # Accumulate the streamed activity feed so it can be PERSISTED with the message
    # (otherwise the tool chain / thinking / plan vanish on reload — they're rebuilt
    # only from live stream events).
    acc_parts: list[dict] = []
    acc_thinking = ""
    acc_todos: list[dict] = []

    def _acc_text(t: str) -> None:
        if acc_parts and acc_parts[-1].get("kind") == "text":
            acc_parts[-1]["text"] += t
        else:
            acc_parts.append({"kind": "text", "text": t})

    try:
        if use_deep:
            try:
                async for ev in deep_chat.stream_deep_chat(settings, base_dir, system, llm_messages):
                    t = ev["type"]
                    if t == "delta":
                        _acc_text(ev["text"])
                        yield {"type": "delta", "text": ev["text"]}
                    elif t == "thinking":
                        acc_thinking += ev["text"]
                        yield {"type": "thinking", "text": ev["text"]}
                    elif t == "tool":  # chained tool-call feed for the UI
                        acc_parts.append({"kind": "tool", "name": ev["name"],
                                          "arg": ev.get("arg", ""), "detail": ev.get("detail", "")})
                        yield {"type": "tool", "name": ev["name"],
                               "arg": ev.get("arg", ""), "detail": ev.get("detail", "")}
                    elif t == "todos":  # the agent's write_todos plan/checklist
                        acc_todos = list(ev["todos"])
                        yield {"type": "todos", "todos": ev["todos"]}
                    elif t == "final":
                        full_text = ev["text"]
                        stream_files_modified = frozenset(ev["paths"])
            except (llm.LLMNotConfigured, llm.LLMError):
                raise  # handled below with a friendly message
            except Exception as exc:  # deepagents/LangChain failure — don't crash chat
                # KEEP the partial work (text + tool feed + thinking) + a resume-oriented note,
                # so the conversation isn't wiped to a one-line error and "continue" can resume.
                assistant = store.add_message(
                    context_id, "assistant",
                    _interrupted_chat_message(acc_parts, exc, deep=True),
                    thinking=acc_thinking or None,
                    parts=acc_parts or None,
                    todos=acc_todos or None,
                )
                yield {"type": "done", "messages": [_msg_dump(user_msg), _msg_dump(assistant)]}
                return
        elif base_dir is not None:
            async for ev in llm.stream_chat_with_tools(
                settings, system, llm_messages,
                tools=_tools.ALL_TOOLS,
                executor=_make_tool_executor(base_dir),
            ):
                if ev["type"] == "delta":
                    yield {"type": "delta", "text": ev["text"]}
                elif ev["type"] == "final":
                    full_text = ev["text"]
                    stream_files_modified = frozenset(ev["paths"])
        else:
            async for chunk in llm.stream_chat(settings, system, llm_messages):
                full_text += chunk
                yield {"type": "delta", "text": chunk}
    except llm.LLMNotConfigured as exc:
        assistant = store.add_message(context_id, "assistant", f"⚙️ {exc}")
        yield {"type": "done", "messages": [_msg_dump(user_msg), _msg_dump(assistant)]}
        return
    except llm.LLMError as exc:
        assistant = store.add_message(
            context_id, "assistant",
            _interrupted_chat_message(acc_parts, exc, deep=False),  # partial work + resume note
            thinking=acc_thinking or None,   # KEEP the partial feed so it survives a reload
            parts=acc_parts or None, todos=acc_todos or None,
        )
        yield {"type": "done", "messages": [_msg_dump(user_msg), _msg_dump(assistant)]}
        return

    # ── Post-process blocks (mirrors send_chat) ──────────────────────────────
    reply = full_text
    spec_updated = False
    map_updated = False
    paper_updated = False
    created_idea_id: str | None = None
    created_domain_id: str | None = None

    new_domain = NEW_DOMAIN_BLOCK.search(reply)
    if new_domain:
        block = new_domain.group(1)
        name = title_from_spec(block)
        domain = store.add_domain(name, spec=block)
        created_domain_id = domain.id
        reply = NEW_DOMAIN_BLOCK.sub(f"🌐 *(Domain created: {name})*", reply).strip()

    if domain_spec is not None:
        match = DOMAIN_SPEC_BLOCK.search(reply)
        if match:
            store.put_domain_spec(domain_id, match.group(1))
            reply = DOMAIN_SPEC_BLOCK.sub("🌐 *(DOMAIN.md updated)*", reply).strip()
            spec_updated = True
        elif "DOMAIN.md" in stream_files_modified:
            spec_updated = True

    new_idea = NEW_IDEA_BLOCK.search(reply)
    if new_idea:
        block = new_idea.group(1)
        title = title_from_spec(block)
        idea = store.add_idea(title)
        store.put_spec(idea.id, block)
        created_idea_id = idea.id
        reply = NEW_IDEA_BLOCK.sub(f"💡 *(Idea created: {title})*", reply).strip()

    if seed is not None:
        match = SEED_DRAFT_BLOCK.search(reply)
        if match:
            draft = match.group(1)
            store.put_seed_draft(seed.id, draft, title=title_from_spec(draft))
            reply = SEED_DRAFT_BLOCK.sub("📝 *(draft updated)*", reply).strip()
            spec_updated = True

    if idea_id and seed is None and domain_spec is None:
        match = SPEC_BLOCK.search(reply)
        if match:
            store.put_spec(idea_id, match.group(1))
            reply = SPEC_BLOCK.sub("📋 *(IDEA.md updated)*", reply).strip()
            spec_updated = True
        elif "IDEA.md" in stream_files_modified:
            spec_updated = True
        # agent wrote the hypothesis map (e.g. via /generate_hypothesis_map)
        report_hids = [m.group(1) for p in stream_files_modified
                       if (m := re.fullmatch(r"hypotheses/([^/]+)/report\.md", p))]
        if ".hypothesis_map.json" in stream_files_modified:
            map_updated = _normalize_written_map(store, idea_id)
        # agent wrote a hypothesis plan/report (/generate_plan, /generate_report) —
        # refresh the Hypotheses tab (map + the open hypothesis detail)
        elif report_hids or any(re.fullmatch(r"hypotheses/[^/]+/plan\.md", p)
                                for p in stream_files_modified):
            map_updated = True
        # /generate_report auto-grows the map: set node status from the verdict and
        # generate follow-up sub-hypotheses deterministically (don't rely on the agent
        # editing .hypothesis_map.json, which it does unreliably).
        for hid in report_hids:
            if await autoexpand_from_report(store, settings, idea_id, hid):
                map_updated = True
        # agent wrote/compiled a paper version (paper.tex/.pdf/.md, paper_v2.* …)
        if any(re.fullmatch(r"paper(?:_v\d+)?\.(tex|pdf|md)", p) for p in stream_files_modified):
            paper_updated = True

    reply, question = _extract_question(reply)

    # Persist the streamed activity feed so it survives a reload: keep the tool TIMELINE
    # only when there actually were tool calls (else the plain text in `content` suffices).
    persist_parts = acc_parts if any(p.get("kind") == "tool" for p in acc_parts) else None
    assistant = store.add_message(
        context_id, "assistant", reply,
        spec_updated=spec_updated,
        map_updated=map_updated,
        paper_updated=paper_updated,
        served_model=settings.model,
        created_idea_id=created_idea_id,
        created_domain_id=created_domain_id,
        question=question,
        thinking=acc_thinking or None,
        parts=persist_parts,
        todos=acc_todos or None,
    )

    if created_domain_id and context_id == SCRATCH_ID:
        store.move_scratch_to_domain(created_domain_id)

    yield {"type": "done", "messages": [_msg_dump(user_msg), _msg_dump(assistant)]}
