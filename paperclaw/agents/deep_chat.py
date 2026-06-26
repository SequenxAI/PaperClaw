"""deepagents-backed chat file-editing — an optional, more reliable editor.

The built-in chat tool loop edits files with a unified-diff `apply_patch`, which
LLMs frequently mis-format (so even a one-line delete can fail). This module
routes a chat turn through a `deepagents` agent whose built-in file tools
(`read_file` / `edit_file` / `write_file`) operate on the REAL idea workspace via a
`FilesystemBackend`, so edits are exact and reliable.

This is the DEFAULT chat editor (`config.chat_agent` defaults to "deepagents"),
but `deepagents` (+ `langchain-openai` / `langchain-anthropic`) is an OPTIONAL
dependency: if it isn't installed, `available()` is False and the chat
automatically falls back to the built-in tool loop. Force the old behavior with
`chat_agent="builtin"` (env `PAPERCLAW_CHAT_AGENT=builtin` or .env — not a settings file key).

`stream_deep_chat` yields the same event shape the chat path already consumes:
  {"type":"delta","text":…}  streamed assistant text
  {"type":"status","text":…} tool activity
  {"type":"final","text":…,"paths":[…]}  full reply + workspace files it changed
"""

from __future__ import annotations

from pathlib import Path
from typing import AsyncIterator

from paperclaw.config import LLMSettings

# Resilience for the chat model client (a turn makes many calls; endpoints can blip / 5xx).
_TIMEOUT = 180.0     # seconds per request — generous for slow reasoning models
_MAX_RETRIES = 4     # retry transient connection errors / 5xx with backoff


def available() -> bool:
    """True when the optional dependency is importable."""
    try:
        import deepagents  # noqa: F401
        return True
    except Exception:
        return False


def _build_model(settings: LLMSettings):
    """A LangChain chat model from our settings (Anthropic or OpenAI-compatible).

    A workspace chat turn makes MANY model calls (tool loop × rounds), so a single transient
    network blip / 5xx from the endpoint would otherwise fail the whole turn. We give the
    client a generous timeout and several retries (with backoff) to ride those out — matching
    the resilience of `llm.py`'s own httpx path."""
    if (settings.provider or "").lower() == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model=settings.model, api_key=settings.api_key, max_tokens=4096,
                             max_retries=_MAX_RETRIES, timeout=_TIMEOUT)
    from langchain_openai import ChatOpenAI
    kw = {"model": settings.model, "api_key": settings.api_key,
          "max_retries": _MAX_RETRIES, "timeout": _TIMEOUT}
    if settings.base_url:  # custom OpenAI-compatible endpoint
        kw["base_url"] = settings.base_url
    return ChatOpenAI(**kw)


def _snapshot(base_dir: Path) -> dict[str, float]:
    snap: dict[str, float] = {}
    for p in base_dir.rglob("*"):
        if p.is_file() and "__pycache__" not in p.parts:
            try:
                snap[p.relative_to(base_dir).as_posix()] = p.stat().st_mtime_ns
            except OSError:
                pass
    return snap


def _changed(base_dir: Path, before: dict[str, float]) -> frozenset[str]:
    after = _snapshot(base_dir)
    return frozenset(k for k, v in after.items() if before.get(k) != v)


def _is_ai(msg) -> bool:
    # NB: a STREAMED chunk is an AIMessageChunk whose .type is "AIMessageChunk",
    # NOT "ai" — checking only "ai" drops every token (empty reply).
    return getattr(msg, "type", "") in ("ai", "AIMessageChunk")


def _split_content(content) -> tuple[str, str]:
    """Return (answer_text, thinking_text) from a message chunk's content — a str,
    or a list of content blocks (Anthropic streams text + thinking blocks)."""
    if isinstance(content, str):
        return content, ""
    text_parts, think_parts = [], []
    if isinstance(content, list):
        for blk in content:
            if isinstance(blk, str):
                text_parts.append(blk)
            elif isinstance(blk, dict):
                t = blk.get("type")
                if t == "text":
                    text_parts.append(blk.get("text", ""))
                elif t in ("thinking", "reasoning"):
                    think_parts.append(blk.get("thinking") or blk.get("text") or "")
    return "".join(text_parts), "".join(think_parts)


def _hint(args: dict) -> str:
    """A short context string for a tool call (the file it touches, the query, …).
    Strips the virtual-root leading '/' so it reads `IDEA.md`, not `/IDEA.md`."""
    val = (args.get("file_path") or args.get("path") or args.get("query")
           or args.get("command") or args.get("url") or "")
    val = str(val).splitlines()[0] if val else ""
    return val.lstrip("/")[:80]


def _snippet(v, n: int = 48) -> str:
    line = str(v).strip().splitlines()
    s = line[0] if line else ""
    return (s[:n] + "…") if len(s) > n else s


def _detail(name: str, args: dict) -> str:
    """A one-line preview of the call's KEY parameters — what it actually does.
    (edit_file deliberately shows only the file, not an old→new preview.)"""
    n = (name or "").lower()
    if n in ("write_file", "write"):
        c = args.get("content") or ""
        return f"{len(str(c))} chars" if c else ""
    if n in ("run", "bash", "shell", "execute"):
        return _snippet(args.get("command") or args.get("input") or "", 60)
    return ""


def _tool_events(msg, seen: set, require_hint: bool = False) -> list[dict]:
    """A ``{"name", "arg", "detail"}`` dict per NEW tool call on *msg* (deduped by
    call id) — the chained-tool-call feed, as structured events for the UI. With
    ``require_hint`` (streamed chunks, whose args are still partial) a call with no
    resolvable arg is skipped and left UNMARKED, so the complete copy from the
    ``updates`` stream emits it (with the arg + detail)."""
    out = []
    for tc in (getattr(msg, "tool_calls", None) or []):
        cid = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
        name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
        if not name or name == "write_todos" or (cid and cid in seen):
            continue  # write_todos is shown as a checklist (see _todo_events), not a tool row
        args = (tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {})) or {}
        hint = _hint(args)
        if require_hint and not hint:
            continue  # partial streamed call — wait for the full one (don't mark seen)
        if cid:
            seen.add(cid)
        out.append({"name": name, "arg": hint, "detail": _detail(name, args)})
    return out


def _clean_todos(todos) -> list[dict]:
    """Normalise a write_todos list → [{content, status}] (status ∈ pending/in_progress/completed)."""
    out = []
    for t in todos or []:
        if isinstance(t, dict) and t.get("content"):
            status = t.get("status", "pending")
            out.append({"content": str(t["content"]),
                        "status": status if status in ("pending", "in_progress", "completed") else "pending"})
    return out


def _todo_events(msg, seen: set) -> list[list]:
    """The todos list from each NEW write_todos tool call on *msg* (deduped by id)."""
    out = []
    for tc in (getattr(msg, "tool_calls", None) or []):
        name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
        if name != "write_todos":
            continue
        cid = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
        if cid and ("todo", cid) in seen:
            continue
        args = (tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {})) or {}
        todos = _clean_todos(args.get("todos"))
        if not todos:
            continue  # partial/streaming — wait for the complete copy from the updates stream
        if cid:
            seen.add(("todo", cid))
        out.append(todos)
    return out


def _make_figure_tool(settings: LLMSettings, base_dir: Path):
    """A `generate_figure` tool (closure over settings + workspace) bridged into the
    deepagents agent so it can render paper figures. Uses the configured image API
    when set; otherwise falls back to generating a vector TikZ figure (.tex)."""
    from paperclaw import images

    def generate_figure(prompt: str, filename: str) -> str:
        """Generate a conceptual figure for the paper from a visual DESCRIPTION and
        save it into the workspace. Use it for an introduction/teaser or methodology
        diagram. `prompt` is the visual description; `filename` is the base name (e.g.
        'fig_method'). If an image-generation API is configured it renders a PNG; if
        NOT, it automatically generates a vector TikZ figure (.tex) instead — so this
        tool always produces a real figure (never fake one). The return message tells
        you exactly how to include the saved file in paper.tex."""
        if not images.is_configured(settings):
            return _generate_tikz_figure(settings, base_dir, prompt, filename)
        name = filename if filename.lower().endswith(".png") else filename + ".png"
        target = (base_dir / name).resolve()
        try:
            target.relative_to(base_dir.resolve())
        except ValueError:
            return f"Refused to write outside the workspace: {filename}"
        ok = images.generate_image(settings, prompt, target)
        if ok:
            return (f"Saved figure {name}. Include it in paper.tex inside a figure "
                    f"environment: \\includegraphics[width=\\linewidth]{{{name}}}.")
        # Image API configured but the call failed — fall back to TikZ rather than
        # leaving the paper figure-less.
        return _generate_tikz_figure(settings, base_dir, prompt, filename,
                                     note="Image generation failed; ")

    return generate_figure


def _generate_tikz_figure(settings: LLMSettings, base_dir: Path, prompt: str,
                          filename: str, note: str = "") -> str:
    """Generate a vector TikZ figure (.tex) from a description and save it under
    `figures/`. Returns guidance on how to `\\input` it in paper.tex (with the
    preamble lines it needs). Used when no image API is configured (or it failed)."""
    from paperclaw import llm
    from paperclaw.prompts.pipeline import TIKZ_FIGURE_SYSTEM

    stem = Path(filename).stem or "fig"
    name = f"{stem}.tex"
    target = (base_dir / "figures" / name).resolve()
    try:
        target.relative_to((base_dir / "figures").resolve())
    except ValueError:
        return f"Refused to write outside the workspace: {filename}"
    try:
        result = _run_coro_sync(llm.chat(
            settings, TIKZ_FIGURE_SYSTEM,
            [{"role": "user", "content": f"Figure description:\n{prompt}"}],
            max_tokens=1500,
        ))
        code = _extract_tikz(result.text)
    except Exception as exc:  # LLM not configured / network / parse — be explicit
        return (f"{note}TikZ fallback failed ({exc}). No figure was written; "
                "describe the figure in text instead, or configure an image API.")
    if "tikzpicture" not in code:
        return (f"{note}TikZ fallback produced no usable figure. Skip it, or "
                "configure an image API in Settings → Image generation.")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(code, encoding="utf-8")
    return (
        f"{note}No image API configured — generated a vector TikZ figure "
        f"figures/{name} instead. To use it: ensure the preamble has "
        "`\\usepackage{tikz}` and "
        "`\\usetikzlibrary{arrows.meta,positioning,shapes.geometric,fit,backgrounds,calc}`, "
        f"then inside a figure environment write `\\input{{figures/{name}}}` "
        "(add your own \\caption/\\label). Finally compile_latex(\"paper.tex\")."
    )


def _extract_tikz(raw: str) -> str:
    """Pull the tikzpicture out of an LLM reply — strip a ```latex/```tex fence if
    present, else return the raw text (trusting the prompt's ONLY-the-body rule)."""
    import re
    m = re.search(r"```(?:latex|tex)?\s*\n(.*?)```", raw, re.DOTALL | re.IGNORECASE)
    return (m.group(1) if m else raw).strip()


def _run_coro_sync(coro):
    """Run an async coroutine to completion from a synchronous tool body. deepagents
    tools run in a worker thread (no running loop) so asyncio.run is normally safe;
    if a loop IS running on this thread, run it in a fresh thread to avoid nesting."""
    import asyncio
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import threading
    box: dict = {}
    def _runner() -> None:
        box["v"] = asyncio.run(coro)
    t = threading.Thread(target=_runner)
    t.start()
    t.join()
    return box["v"]


def _workspace_tools(settings: LLMSettings, base_dir: Path) -> list:
    """Extra tools bridged into the deepagents agent: paper figures + web research
    + file download (so it can fetch a venue's LaTeX template / style guide)."""
    import httpx

    from paperclaw.tools import cite as _cite
    from paperclaw.tools import fetch_url as _fu
    from paperclaw.tools import openalex_search as _oa
    from paperclaw.tools import web_search as _ws

    def web_search(query: str) -> str:
        """Search the web (DuckDuckGo) for up-to-date info — e.g. a venue's LaTeX
        template / author kit / formatting instructions. Returns titles, URLs, snippets."""
        return _ws.execute(base_dir, {"query": query})

    def openalex_search(query: str) -> str:
        """Search OpenAlex for REAL academic papers by topic (title + abstract) —
        returns title / authors / year / venue / DOI for each. Use this to FIND real
        related work to cite (then `cite` each). NEVER invent papers; only cite ones
        a search actually returns."""
        return _oa.execute(base_dir, {"query": query})

    def cite(query: str = "", doi: str = "") -> str:
        """Find a REAL paper (by `doi` via Crossref, or by `query` via OpenAlex) and
        append its VERIFIED BibTeX entry to the idea's ref.bib, returning the Scholar-
        style cite key to use in \\cite{key}. This is the ONLY allowed way to add a
        citation — every reference must come from here, NEVER hand-written or invented.
        Provide `doi` OR `query`. Call it repeatedly to build ≥40 verified references."""
        return _cite.execute(base_dir, {"query": query, "doi": doi})

    def fetch_url(url: str) -> str:
        """Fetch an http(s) page and return its readable text (e.g. a venue's
        author-guidelines / call-for-papers page)."""
        return _fu.execute(base_dir, {"url": url})

    def download_file(url: str, filename: str) -> str:
        """Download a binary file (e.g. a LaTeX template .zip or .sty) from a URL
        into the workspace at `filename` (relative path; subdirs created)."""
        target = (base_dir / filename).resolve()
        try:
            target.relative_to(base_dir.resolve())
        except ValueError:
            return f"refused to write outside the workspace: {filename}"
        try:
            r = httpx.get(url, timeout=120, follow_redirects=True)
            r.raise_for_status()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(r.content)
            return f"Downloaded {len(r.content)} bytes → {filename}"
        except Exception as exc:
            return f"Download failed: {exc}"

    def read_image(path: str):
        """View an image file (PNG/JPG/GIF/WebP) from the workspace — the figure is
        shown to you directly so you can read its axis labels, legend, layout and any
        text baked into it. Use this to CHECK a generated result figure before
        referencing it in the paper (you cannot read binary images with read_file).
        `path` is relative, e.g. 'experiments/1/loss.png'."""
        import base64 as _b64

        from paperclaw.tools import read_image as _ri
        target = (base_dir / path).resolve()
        try:
            target.relative_to(base_dir.resolve())
        except ValueError:
            return f"refused to read outside the workspace: {path}"
        if not target.is_file():
            return f"not found: {path}"
        media = _ri._MEDIA.get(target.suffix.lower())
        if media is None:
            return f"not a supported image (PNG/JPG/GIF/WebP): {path}"
        try:
            raw = target.read_bytes()
        except OSError as exc:
            return f"could not read {path}: {exc}"
        if len(raw) > _ri.MAX_BYTES:
            return f"image too large to view ({len(raw) // 1024} KB): {path}"
        size = _ri._png_size(raw)
        dims = f"{size[0]}×{size[1]}px, " if size else ""
        b64 = _b64.b64encode(raw).decode("ascii")
        # LangChain v1 standard multimodal content blocks (text label + image), so a
        # vision model (ChatAnthropic / vision ChatOpenAI) actually sees the figure.
        return [
            {"type": "text", "text": f"Image {path} ({dims}{len(raw) // 1024} KB):"},
            {"type": "image", "source_type": "base64", "mime_type": media, "data": b64},
        ]

    def read_pdf(path: str, page: int | None = None) -> str:
        """Read a compiled PDF's RENDERED text (you CANNOT read a PDF with read_file).
        With just `path`, returns a page map and reports where the MAIN content ends —
        total pages, main-text page count, and the pages where References / Appendix
        begin (both are excluded from the page limit). Pass `page` (1-based) to dump
        that one page's full text. Use this to answer 'how long is the main text / which
        page does it end on' from the ACTUAL PDF, not the LaTeX source."""
        from paperclaw.tools import read_pdf as _rp
        inputs: dict = {"path": path}
        if page is not None:
            inputs["page"] = page
        return _rp.execute(base_dir, inputs)

    def compile_latex(tex_filename: str) -> str:
        """Compile a LaTeX `.tex` file in the workspace into a PDF (same base name,
        tectonic/pdflatex). Call this AFTER writing/editing the `.tex`. If it fails,
        READ the returned compiler log, FIX the `.tex`, and call again until it
        compiles. Returns success + page count, or the error log to fix."""
        from paperclaw.iterative_pipeline import compile_tex
        name = tex_filename if tex_filename.endswith(".tex") else tex_filename + ".tex"
        try:
            ok, pages, log = compile_tex(base_dir, name)
        except Exception as exc:  # a runner bug must NOT kill the whole chat turn
            return f"Compile ERROR for {name}: {type(exc).__name__}: {exc}. Check the path/filename and retry."
        if ok:
            return f"Compiled {name} → {name[:-4]}.pdf ({pages if pages is not None else '?'} pages)."
        return f"Compile FAILED for {name}. Fix the LaTeX and recompile. Compiler log (tail):\n{log[-1800:]}"

    def review_paper(tex_filename: str) -> str:
        """Compile `tex_filename` and CHECK it against the venue's submission rules,
        returning a compliance report. Use this in a loop after writing the paper:
        review → FIX the reported ✗ errors (page limit, disallowed packages/commands,
        margin overflow, undefined citations, missing figures) in the `.tex` →
        review again, until it reports COMPLIANT. Reads the page limit from
        venue/STYLE.md when present. Do not declare the paper done until this passes."""
        from paperclaw import paper_review
        name = tex_filename if tex_filename.endswith(".tex") else tex_filename + ".tex"
        style_md = None
        style_path = base_dir / "venue" / "STYLE.md"
        if style_path.is_file():
            style_md = style_path.read_text(encoding="utf-8", errors="ignore")
        venue_style = (base_dir / "venue").is_dir()
        try:
            report = paper_review.review_paper(
                base_dir, name,
                page_limit=paper_review.page_limit_from_style(style_md),
                venue_style=venue_style)
        except Exception as exc:  # a runner bug must NOT kill the whole chat turn
            return f"Review ERROR for {name}: {type(exc).__name__}: {exc}. Check the path/filename and retry."
        return paper_review.format_report(report, name)

    def bash(command: str, run_in_background: bool = False) -> str:
        """Run a shell command in the workspace (inherits the server's env, so python/conda are
        on PATH) and return its combined stdout+stderr and exit code. Use it to run scripts,
        inspect or re-run experiment code (e.g. `python run.py`), list/grep files, install a
        package, etc. A foreground command is capped at 10 min and CANNOT be interrupted by
        Stop. For a LONG job (training, a full rerun) pass run_in_background=true: it launches
        DETACHED, returns a job id immediately, keeps running after this turn / a restart, and
        you poll it with the `bash_output` tool. Never block waiting on a long job."""
        from paperclaw.tools import background as _bg
        if run_in_background:
            return _bg.launch(base_dir, command)
        import subprocess
        try:
            p = subprocess.run(["bash", "-c", command], cwd=str(base_dir),
                               capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            return ("[timed out after 600s — for a long job pass run_in_background=true "
                    "(detached, no timeout) and poll with bash_output]")
        except Exception as exc:
            return f"[failed to run: {exc}]"
        out = (p.stdout or "") + (("\n[stderr]\n" + p.stderr) if p.stderr else "")
        out = out[-8000:].strip()  # last 8k chars
        return f"(exit {p.returncode})\n{out}" if out else f"(exit {p.returncode}, no output)"

    def bash_output(job_id: str = "", lines: int = 40, kill: bool = False) -> str:
        """Check a background job started by bash(run_in_background=true): its status (running /
        done+exit-code / stopped) and recent output. Pass the job_id you got back; omit it to
        list ALL jobs; set kill=true to stop one. Report run progress with this instead of
        blocking — if a job is still running, end your turn and check again next time."""
        from paperclaw.tools import background as _bg
        return _bg.status(base_dir, job_id=job_id.strip() or None, lines=lines, kill=kill)

    def list_exp_resources() -> str:
        """List the experiment resources available BEFORE running one: detected compute (local +
        any SSH remotes — CPU/GPU/MEM/disk), the experiment-execution mode + coding-agent CLI,
        and the LLM provider/model. Call this FIRST when about to run an experiment, to decide
        where (local vs an SSH GPU host) and how to run it. (Keys shown as configured/missing.)"""
        from paperclaw.tools import list_exp_resources as _ler
        return _ler.execute(base_dir, {})

    return [_make_figure_tool(settings, base_dir), web_search, openalex_search, cite,
            fetch_url, download_file, read_image, read_pdf, compile_latex, review_paper,
            bash, bash_output, list_exp_resources]


def _update_messages(data):
    """Messages contained in a stream_mode='updates' payload ({node: state-delta})."""
    if isinstance(data, dict):
        for upd in data.values():
            if isinstance(upd, dict):
                for m in (upd.get("messages") or []):
                    yield m


async def stream_deep_chat(
    settings: LLMSettings,
    base_dir: Path,
    system: str,
    messages: list[dict],
) -> AsyncIterator[dict]:
    """Run one chat turn through a deepagents agent rooted at *base_dir*. Streams the
    assistant text, its **thinking**, and a **chained-tool-call** feed (🔧 read_file /
    edit_file …) — Claude-Code style — then a terminal ``final`` with the full reply
    and the workspace files the agent changed (for spec_updated / block parsing)."""
    from deepagents import create_deep_agent
    from deepagents.backends import FilesystemBackend

    base_dir = Path(base_dir)
    agent = create_deep_agent(
        model=_build_model(settings),
        system_prompt=system,
        backend=FilesystemBackend(root_dir=str(base_dir.resolve()), virtual_mode=True),
        tools=_workspace_tools(settings, base_dir),  # figures + web research + download
    )

    before = _snapshot(base_dir)
    full = ""
    seen_tools: set = set()
    # messages → token-level text/thinking; updates → completed tool calls per step.
    # Tool calls, thinking, and the write_todos plan are STRUCTURED events (rendered
    # in the UI), not folded into the answer; ``full`` is the answer only.
    async for mode, data in agent.astream({"messages": messages}, stream_mode=["messages", "updates"]):
        if mode == "messages":
            msg = data[0] if isinstance(data, tuple) and data else None
            if msg is None or not _is_ai(msg):
                continue
            text, thinking = _split_content(getattr(msg, "content", ""))
            if thinking:
                yield {"type": "thinking", "text": thinking}
            if text:
                full += text
                yield {"type": "delta", "text": text}
            for ev in _tool_events(msg, seen_tools, require_hint=True):  # partial on the chunk
                yield {"type": "tool", **ev}
        elif mode == "updates":
            for m in _update_messages(data):
                for todos in _todo_events(m, seen_tools):      # the agent's plan/checklist
                    yield {"type": "todos", "todos": todos}
                for ev in _tool_events(m, seen_tools):
                    yield {"type": "tool", **ev}

    yield {"type": "final", "text": full, "paths": list(_changed(base_dir, before))}
