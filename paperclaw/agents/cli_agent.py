"""CLI experiment runner — shell out to an external headless coding-agent CLI.

Instead of maintaining our own agent loop (see ``coding_agent.py``), this runner
delegates the whole experiment to a mature, headless coding agent — ``claude``
(Claude Code ``-p``), ``opencode run``, ``openhands`` headless, or anything else
— configured as a shell command template, and simply **streams all of its stdout**
to the UI/CLI. When the process exits we read ``results.json`` (the deliverable)
just like the other runners, so the rest of the pipeline is unchanged.

The command template (``RunConfig.agent_command``) supports placeholders:

  * ``{prompt}``    — the full, shell-quoted task text (idea + plan + schema)
  * ``{task_file}`` — ``task.md`` (the same task written into the working dir)
  * ``{dir}``       — the absolute working directory

The external agent uses ITS OWN auth/model config (inherited from the backend's
environment) — that is the whole point: we don't drive the LLM, the CLI does.

SECURITY: runs the configured command (which itself runs arbitrary model-written
code) as a subprocess with the backend's permissions — for a trusted, self-hosted
deployment (no container), consistent with the other executed runners.
"""

import asyncio
import json
import os
import re
import shlex
import signal
import threading
from pathlib import Path
from typing import AsyncIterator

_DRAIN_GRACE = 2.0  # secs to flush trailing output after the agent is done before we stop
_DELIVERABLE_GRACE = 120.0  # base secs to wait for results.json after the agent reports done
                            # (a foreground run flushing it) before finalizing
_STALL_GRACE = 600.0        # ...but KEEP waiting while the experiment is still writing files
                            # (the agent backgrounded training); give up after this much QUIET

# Files the runner itself writes — excluded when checking whether the EXPERIMENT is still
# producing output, so our own logging doesn't look like progress.
_RUNNER_FILES = frozenset({"events.jsonl", "stdout.log", "runner.log", "agent_stream.log",
                           "job.json", ".run_override.json", "task.md"})


def _latest_work_mtime(out_dir: Path) -> float:
    """Newest mtime among the experiment's OWN output files (ignores the runner's bookkeeping),
    so a backgrounded training job that keeps writing checkpoints/logs reads as 'still working'."""
    latest = 0.0
    try:
        for p in out_dir.rglob("*"):
            if p.is_file() and p.name not in _RUNNER_FILES:
                try:
                    latest = max(latest, p.stat().st_mtime)
                except OSError:
                    pass
    except OSError:
        pass
    return latest

try:
    import pty  # POSIX only — gives the agent a real terminal so it streams
except ImportError:  # pragma: no cover - Windows
    pty = None  # type: ignore

from paperclaw.config import LLMSettings
from paperclaw.experiments import _load_results, _render_failure_md, _render_results_md
from paperclaw.prompts.pipeline import CLI_AGENT_TASK
from paperclaw.server.models import RunConfig


def _killpg(proc) -> None:
    """SIGTERM the whole process group (the agent + its children — e.g. a `sleep` it
    spawned), falling back to killing just the leader. Started with start_new_session
    so the group is the agent's, never ours."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


async def _stream_command(cmd: str, cwd: str, deadline: float | None,
                          stop_event: "asyncio.Event | None" = None):
    """Run *cmd* and yield ('chunk', text) as output arrives, then ('rc', code) —
    plus ('timeout', None) if *deadline* passes.

    Runs under a PTY when available so headless agent CLIs (claude -p, opencode…)
    stream live instead of block-buffering on a pipe. Crucially it does NOT hang
    waiting for the PTY to EOF: a control task stops the read loop once the process
    exits (a grandchild can keep the PTY open) OR *stop_event* is set (the caller has
    its deliverable and wants to finalize — see finalize-on-deliverable in
    run_cli_agent), then SIGTERMs the whole process group."""
    loop = asyncio.get_running_loop()
    master_fd, slave_fd = (pty.openpty() if pty is not None else (None, None))
    queue: asyncio.Queue = asyncio.Queue()

    if master_fd is not None:
        proc = await asyncio.create_subprocess_shell(
            cmd, cwd=cwd, stdin=asyncio.subprocess.DEVNULL,
            stdout=slave_fd, stderr=slave_fd, close_fds=True, start_new_session=True)
        os.close(slave_fd)

        def _pump():  # read the PTY master off-thread; EIO on close == EOF
            try:
                while True:
                    data = os.read(master_fd, 4096)
                    if not data:
                        break
                    loop.call_soon_threadsafe(queue.put_nowait, ("data", data))
            except OSError:
                pass
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, ("eof", None))
        threading.Thread(target=_pump, daemon=True).start()
    else:  # pipe fallback (no PTY, e.g. Windows)
        proc = await asyncio.create_subprocess_shell(
            cmd, cwd=cwd, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True)

        async def _pump_pipe():
            while True:
                data = await proc.stdout.read(4096)  # type: ignore[union-attr]
                queue.put_nowait(("data", data) if data else ("eof", None))
                if not data:
                    break
        loop.create_task(_pump_pipe())

    async def _control():
        # Stop the read loop when the process exits (+grace to flush trailing output)
        # OR the caller asks to finalize early — so we never block forever on the PTY.
        waiters = [loop.create_task(proc.wait())]
        if stop_event is not None:
            waiters.append(loop.create_task(stop_event.wait()))
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            await asyncio.sleep(_DRAIN_GRACE)
        finally:
            for t in waiters:
                t.cancel()
            queue.put_nowait(("stop", None))
    control = loop.create_task(_control())

    timed_out = False
    while True:
        try:
            if deadline is not None:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    timed_out = True
                    break
                kind, data = await asyncio.wait_for(queue.get(), timeout=remaining)
            else:
                kind, data = await queue.get()
        except asyncio.TimeoutError:
            timed_out = True
            break
        if kind == "data":
            yield ("chunk", data.decode("utf-8", "replace") if isinstance(data, bytes) else data)
        elif kind in ("eof", "stop"):
            break

    control.cancel()
    _killpg(proc)  # also unblocks a PTY a lingering child kept open
    if master_fd is not None:
        try:
            os.close(master_fd)
        except OSError:
            pass
    if timed_out:
        yield ("timeout", None)
    rc = await proc.wait()
    yield ("rc", rc)

_NO_COMMAND_HINT = (
    "_No agent command configured. Set one in **Settings → Experiment execution → "
    "CLI agent** (or `paperclaw run-config --agent-command …`). Examples:_\n\n"
    "```\n"
    "claude -p {prompt} --dangerously-skip-permissions\n"
    "opencode run {prompt}\n"
    "```\n"
)

# ── live-log rendering for headless agents that emit Claude Code stream-json ──────
# `claude -p` in the default TEXT mode prints ONLY the final result (nothing during
# the run). Adding `--output-format stream-json --verbose` makes it emit one JSON
# event per step (assistant text, tool call, tool result, result) which we render
# into readable lines so the user sees the whole run live. Non-JSON output (opencode,
# openhands, plain text) is auto-detected and streamed through unchanged.
_STREAM_JSON_TYPES = {"system", "assistant", "user", "result", "stream_event"}
_TOOL_RESULT_MAX = 4000


def _ensure_streaming(template: str) -> str:
    """Enable live streaming for a `claude` print command that doesn't set an output
    format — otherwise `-p` prints only the final answer at the very end. We also add
    `--include-partial-messages` so assistant text/thinking streams TOKEN-BY-TOKEN
    (so you see something WHILE it generates, not just after each turn). Leaves any
    other command (opencode, openhands, a custom script) untouched."""
    t = template.strip()
    is_claude = re.search(r"(^|[\s/])claude(\s|$)", t) is not None
    has_print = re.search(r"(^|\s)(-p|--print)(\s|$)", t) is not None
    has_fmt = "--output-format" in t or "--print-format" in t
    if is_claude and has_print and not has_fmt:
        return t + " --output-format stream-json --verbose --include-partial-messages"
    return t


def _looks_like_stream_json(line: str) -> bool:
    s = line.strip().rstrip("\r")
    if not (s.startswith("{") and s.endswith("}")):
        return False
    try:
        obj = json.loads(s)
    except ValueError:
        return False
    return isinstance(obj, dict) and obj.get("type") in _STREAM_JSON_TYPES


def _tool_input_summary(name: str, inp) -> str:
    """A one-line preview of a tool call's key argument (the command, file, query…)."""
    if not isinstance(inp, dict):
        return str(inp)[:400]
    if (name or "").lower() in ("bash", "shell", "execute"):
        return str(inp.get("command", ""))[:400]
    for k in ("file_path", "path", "notebook_path", "pattern", "url", "query", "prompt"):
        if inp.get(k):
            return str(inp[k])[:200]
    for v in inp.values():
        if isinstance(v, (str, int, float)):
            return str(v)[:200]
    return ""


def _content_to_text(content) -> str:
    """Flatten a message/tool-result content field (str or list of blocks) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict):
                if b.get("type") == "text":
                    parts.append(b.get("text", ""))
                elif "content" in b:
                    parts.append(_content_to_text(b["content"]))
        return "\n".join(p for p in parts if p)
    return str(content)


def _render_stream_json(obj: dict) -> str | None:
    """Render one Claude Code stream-json event as a readable log line (or None to
    drop noisy events like token-level partials)."""
    t = obj.get("type")
    if t == "system":
        if obj.get("subtype") == "init":
            return f"▶ session started · model {obj.get('model') or '?'} · {len(obj.get('tools') or [])} tools"
        return None
    if t == "assistant":
        out = []
        for blk in (obj.get("message") or {}).get("content") or []:
            bt = blk.get("type")
            if bt == "text" and (blk.get("text") or "").strip():
                out.append(blk["text"].strip())
            elif bt == "thinking" and (blk.get("thinking") or "").strip():
                out.append(f"💭 {blk['thinking'].strip()}")
            elif bt == "tool_use":
                out.append(f"🔧 {blk.get('name', 'tool')}: {_tool_input_summary(blk.get('name'), blk.get('input'))}")
        return "\n".join(out) or None
    if t == "user":
        out = []
        for blk in (obj.get("message") or {}).get("content") or []:
            if isinstance(blk, dict) and blk.get("type") == "tool_result":
                text = _content_to_text(blk.get("content")).rstrip()
                if not text:
                    continue
                if len(text) > _TOOL_RESULT_MAX:
                    text = text[:_TOOL_RESULT_MAX] + f"\n   … [+{len(text) - _TOOL_RESULT_MAX} more chars]"
                mark = "⚠ " if blk.get("is_error") else ""
                out.append("   ↳ " + mark + text.replace("\n", "\n     "))
        return "\n".join(out) or None
    if t == "result":
        bits = []
        if obj.get("num_turns") is not None:
            bits.append(f"{obj['num_turns']} turns")
        if obj.get("duration_ms") is not None:
            bits.append(f"{obj['duration_ms'] / 1000:.0f}s")
        if obj.get("total_cost_usd") is not None:
            bits.append(f"${obj['total_cost_usd']:.4f}")
        head = "✗" if obj.get("is_error") else "✓"
        return f"{head} agent {obj.get('subtype') or 'finished'}" + (" · " + " · ".join(bits) if bits else "")
    return None  # stream_event partials etc.


def _render_agent_line(line: str) -> str | None:
    """A stream-json event → readable line; any non-JSON line passes through raw.
    Stateless: renders COMPLETE events only (no token-level partials)."""
    s = line.rstrip("\r")
    if not s.strip():
        return None
    try:
        obj = json.loads(s.strip())
    except ValueError:
        return s
    if isinstance(obj, dict) and obj.get("type") in _STREAM_JSON_TYPES:
        return _render_stream_json(obj)
    return s


class _StreamRenderer:
    """Stateful renderer for a stream-json line sequence. Each ``feed_line`` returns
    text to emit (possibly ``""``). When ``stream_partials`` is on it streams assistant
    text/thinking TOKEN-BY-TOKEN from ``stream_event`` deltas (so output appears WHILE
    the model generates), and suppresses the now-duplicate text in the consolidated
    ``assistant`` event — showing only its clean, fully-formed tool calls there."""

    def __init__(self, stream_partials: bool):
        self.stream_partials = stream_partials
        self._open: str | None = None  # an in-progress streamed block: 'text'|'thinking'
        self.saw_result = False  # the agent emitted its terminal `result` event (session done)

    def _close(self) -> str:
        """Terminate a streamed text/thinking line before a discrete line follows."""
        if self._open:
            self._open = None
            return "\n"
        return ""

    def feed_line(self, line: str) -> str:
        s = line.rstrip("\r")
        if not s.strip():
            return ""
        try:
            obj = json.loads(s.strip())
        except ValueError:
            return self._close() + s + "\n"  # non-JSON passthrough
        if not (isinstance(obj, dict) and obj.get("type") in _STREAM_JSON_TYPES):
            return self._close() + s + "\n"
        t = obj["type"]
        if t == "result":
            self.saw_result = True  # claude -p's terminal event — the session is over
        if t == "stream_event":
            return self._feed_partial(obj.get("event") or {})
        if t == "assistant" and self.stream_partials:
            tools = [f"🔧 {b.get('name', 'tool')}: {_tool_input_summary(b.get('name'), b.get('input'))}"
                     for b in (obj.get("message") or {}).get("content") or [] if b.get("type") == "tool_use"]
            return self._close() + ("\n".join(tools) + "\n" if tools else "")
        rendered = _render_stream_json(obj)
        return self._close() + rendered + "\n" if rendered else self._close()

    def _feed_partial(self, ev: dict) -> str:
        et = ev.get("type")
        if et == "content_block_start":
            cb = ev.get("content_block") or {}
            if cb.get("type") == "thinking":
                out = self._close() + "🧠 "; self._open = "thinking"; return out
            if cb.get("type") == "text":
                out = self._close(); self._open = "text"; return out
            return ""  # tool_use input streams as JSON — show it clean from the complete event
        if et == "content_block_delta":
            d = ev.get("delta") or {}
            if d.get("type") == "text_delta":
                return d.get("text", "")
            if d.get("type") == "thinking_delta":
                return d.get("thinking", "")
            return ""  # input_json_delta — skip
        if et == "content_block_stop":
            return self._close()
        return ""  # message_start / message_delta / message_stop / ping


def agent_command_available(run_config: RunConfig) -> bool:
    """True if the configured cli-agent command's binary is on PATH — so `cli` mode
    can actually run. When False the pipeline falls back to the in-process agentic
    runner (which uses our configured LLM instead of an external CLI)."""
    import shutil
    template = (run_config.agent_command or "").strip()
    if not template:
        return False
    try:
        binary = shlex.split(template)[0]
    except ValueError:
        parts = template.split()
        binary = parts[0] if parts else ""
    return bool(binary) and shutil.which(binary) is not None


async def run_cli_agent(
    settings: LLMSettings,
    idea_ctx: str,
    plan: str,
    out_dir: Path,
    run_config: RunConfig,
) -> AsyncIterator[dict]:
    """Run an external headless coding-agent CLI; stream its stdout. Yields
    delta/status events + a terminal ``result`` (markdown / provenance / figures /
    error), exactly like the other runners. ``settings`` is unused — the external
    CLI carries its own auth (kept for a uniform runner signature)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    template = (run_config.agent_command or "").strip()
    if not template:
        yield {"type": "result", "result": {
            "markdown": _NO_COMMAND_HINT, "provenance": "cli",
            "figures": [], "attempts": 0, "error": "no agent_command configured"}}
        return

    task = CLI_AGENT_TASK.replace("{idea}", idea_ctx).replace("{plan}", plan)
    (out_dir / "task.md").write_text(task, encoding="utf-8")
    eff_template = _ensure_streaming(template)  # add stream-json to `claude -p` so it streams
    cmd = (eff_template
           .replace("{prompt}", shlex.quote(task))
           .replace("{task_file}", "task.md")
           .replace("{dir}", str(out_dir)))

    log = out_dir / "stdout.log"
    log.write_text("", encoding="utf-8")
    raw_log = out_dir / "agent_stream.log"  # full raw stdout (stream-json), for debugging
    yield {"type": "status", "text": f"\n🖥️ running headless agent: {eff_template}\n"}

    deadline = None  # experiments run with NO wall-clock timeout (they can take hours)
    timed_out = False
    rc = 0
    renderer = _StreamRenderer(stream_partials="--include-partial-messages" in eff_template)
    stop_event = asyncio.Event()
    finalized = False  # tripped once the deliverable is in — see below
    try:
        f = log.open("a", encoding="utf-8")
        rf = raw_log.open("a", encoding="utf-8")
        buf = ""
        json_mode: bool | None = None  # None=undecided, True=render stream-json, False=raw

        def _emit(text: str) -> dict:
            f.write(text); f.flush()
            return {"type": "delta", "text": text}

        def _maybe_finalize():
            # FINALIZE-ON-DELIVERABLE: claude -p emits exactly ONE terminal `result`
            # event when its session ends. If results.json is ALREADY on disk, finalize
            # immediately — stop waiting on a process that can linger (a backgrounded
            # child holding the PTY, or a rate-limit sleep). If the agent reports done
            # but results.json is NOT there yet, do NOT kill instantly: give it a bounded
            # grace to flush/appear (a foreground run finishing its last write), then
            # finalize regardless so we never hang. This avoids cutting off a run whose
            # deliverable lands a moment after the terminal event.
            nonlocal finalized
            if finalized or not renderer.saw_result:
                return None
            finalized = True
            if (out_dir / "results.json").is_file():
                stop_event.set()
                return _emit("\n[agent reported done (results.json written) — finalizing.]\n")
            loop = asyncio.get_running_loop()

            async def _await_deliverable():
                # Wait the base grace; but if the experiment is STILL writing output (a
                # backgrounded training the agent launched), keep waiting until results.json
                # lands or the workspace goes quiet for _STALL_GRACE — the job has no timeout,
                # so we must not abandon a run that's still producing results.
                last_mtime = _latest_work_mtime(out_dir)
                deadline = loop.time() + _DELIVERABLE_GRACE
                while not (out_dir / "results.json").is_file():
                    await asyncio.sleep(3.0)
                    m = _latest_work_mtime(out_dir)
                    if m > last_mtime:                 # files still changing → work in progress
                        last_mtime = m
                        deadline = loop.time() + _STALL_GRACE
                    elif loop.time() >= deadline:      # quiet past the grace → give up
                        break
                stop_event.set()  # results.json appeared, or the workspace went quiet
            loop.create_task(_await_deliverable())
            return _emit("\n[agent reported done but results.json is not written yet — waiting "
                         "for it while the experiment keeps writing output (a backgrounded run); "
                         "finalizing once it appears or the workspace goes quiet.]\n")

        try:
            async for kind, payload in _stream_command(cmd, str(out_dir), deadline, stop_event):
                if kind == "timeout":
                    timed_out = True
                    continue
                if kind == "rc":
                    rc = payload
                    continue
                # kind == "chunk"
                rf.write(payload); rf.flush()
                if json_mode is False:           # plain text — stream through as-is
                    yield _emit(payload); continue
                buf += payload
                if json_mode is None:            # decide once the first line is complete
                    if "\n" not in buf and len(buf) < 8192:
                        continue
                    json_mode = _looks_like_stream_json(buf.split("\n", 1)[0])
                    if not json_mode:
                        yield _emit(buf); buf = ""; continue
                *complete, buf = buf.split("\n")  # render each finished stream-json line
                for ln in complete:
                    rendered = renderer.feed_line(ln)
                    if rendered:
                        yield _emit(rendered)
                fin = _maybe_finalize()
                if fin:
                    yield fin
            if buf.strip():                       # flush any trailing partial line
                rendered = renderer.feed_line(buf)
                if rendered:
                    yield _emit(rendered)
        finally:
            f.close(); rf.close()
    except Exception as exc:  # command not found, etc.
        yield {"type": "result", "result": {
            "markdown": _render_failure_md(f"failed to launch agent: {exc}", 1),
            "provenance": "cli", "figures": [], "attempts": 1, "error": str(exc)}}
        return

    if timed_out:
        msg = "\n[agent killed]\n"
        with log.open("a", encoding="utf-8") as f2:
            f2.write(msg)
        yield {"type": "status", "text": msg}
    results = _load_results(out_dir)
    figures = sorted(p.name for p in out_dir.glob("*.png"))
    stdout = log.read_text(encoding="utf-8") if log.is_file() else ""
    if results:
        yield {"type": "status", "text": f"✓ agent finished (exit {rc})\n"}
        error = None
    else:
        error = ("agent was killed" if timed_out
                 else f"agent exited ({rc}) without writing results.json")
    yield {"type": "result", "result": {
        "markdown": _render_results_md(results, stdout, figures),
        "provenance": "cli", "figures": figures, "attempts": 1, "error": error}}
