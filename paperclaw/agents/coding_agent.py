"""Experiment coding agent — a real multi-file read→write→edit→run→fix loop.

Drives a small text-block ACTION protocol over ``llm.stream_chat_thinking`` so it
streams thinking + text + command output for BOTH providers (native tool-use is
non-streaming on OpenAI-compatible endpoints, hence the text-block protocol).

Unlike a single-``run.py`` runner, the agent manages a real codebase: it can
create/overwrite ANY file (``write <path>``, subdirs included), make targeted
edits to existing files (``patch <path>`` — a content-matched unified diff), and
run/inspect/read/list/grep via the shell (``bash``). It may emit several actions
per step; they execute in order and their combined output is fed back so the agent
iterates like a careful research engineer until it writes ``results.json``.

It reuses the result parsing/rendering helpers in ``experiments.py`` (the
runner-strategy + rendering module) and the content-matched diff applier in
``tools/apply_patch.py``.

SECURITY: executes arbitrary model-written code/shell as a subprocess with the
backend's permissions — for a trusted, self-hosted deployment (no container).
"""

import asyncio
import posixpath
import re
import shlex
import subprocess
import uuid
from pathlib import Path
from typing import AsyncIterator

from paperclaw import llm
from paperclaw.config import LLMSettings
from paperclaw.experiments import (
    _STDERR_TAIL,
    _load_results,
    _render_failure_md,
    _render_results_md,
    _ssh_base,
)
from paperclaw.prompts.pipeline import AGENT_EXPERIMENT_SYSTEM
from paperclaw.server.models import RunConfig
from paperclaw.tools import apply_patch as _apply_patch

# A fenced block: capture its info string (first line after ```) and its body.
_FENCE = re.compile(r"```([^\n]*)\n(.*?)```", re.DOTALL)
_AGENT_MAX_ROUNDS = 24
_OUTPUT_TAIL = 2000  # chars of command output streamed back to the UI per step


def _parse_actions(raw: str) -> list[tuple[str, str | None, str]]:
    """Parse model output into ordered ``(kind, path, body)`` actions.

    kinds: ``bash`` (path None), ``write`` (overwrite/create <path>), ``patch``
    (unified diff on <path>). ``python`` is shorthand for ``write run.py``.
    Unrecognised fences (```json, plain ```text, …) are ignored, so the agent can
    quote things without triggering an action.
    """
    actions: list[tuple[str, str | None, str]] = []
    for m in _FENCE.finditer(raw):
        info = m.group(1).strip()
        body = m.group(2)
        if not info:
            continue
        info = info.replace(":", " ", 1)  # accept write:path as well as write path
        tokens = info.split()
        verb, rest = tokens[0].lower(), info[len(tokens[0]):].strip()
        if verb in ("bash", "sh", "shell", "console"):
            actions.append(("bash", None, body))
        elif verb == "python":
            actions.append(("write", rest or "run.py", body))
        elif verb in ("write", "file", "create") and rest:
            actions.append(("write", rest, body))
        elif verb in ("patch", "diff") and rest:
            actions.append(("patch", rest, body))
    return actions


def _exec_bash(command: str, out_dir: Path, timeout: int) -> tuple[int, str]:
    """Run a shell command in out_dir; return (returncode, combined_output).
    ``timeout <= 0`` means NO timeout (experiments may run for hours)."""
    to = timeout if timeout and timeout > 0 else None
    try:
        p = subprocess.run(["bash", "-c", command], cwd=str(out_dir),
                           capture_output=True, text=True, timeout=to)
        out = p.stdout or ""
        if p.stderr:
            out += ("\n[stderr]\n" + p.stderr)
        return p.returncode, out
    except subprocess.TimeoutExpired:
        return 124, f"[command timed out after {timeout}s]"
    except Exception as exc:
        return 1, f"[failed to run: {exc}]"


async def _stream_argv(argv: list[str], cwd: Path | None, log: Path, header: str):
    """Async-run *argv* with NO timeout, appending output to *log* live and yielding
    ("chunk", text) as each line arrives, then ("done", rc, full_output). The same
    streaming logic drives either a local ``bash -c …`` or a remote ``ssh … bash -lc …``,
    so a long-running command (e.g. multi-hour training) stays monitorable either way.
    *header* is the human-readable command echoed into the log."""
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=str(cwd) if cwd is not None else None,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    chunks: list[str] = []
    with log.open("a", encoding="utf-8") as f:
        f.write(f"$ {header}\n"); f.flush()
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace")
            chunks.append(text)
            f.write(text); f.flush()
            yield ("chunk", text)
    rc = await proc.wait()
    yield ("done", rc, "".join(chunks))


class _LocalExec:
    """Run the agent's bash/file actions on the LOCAL host (the default)."""

    provenance = "agent"

    def __init__(self, out_dir: Path):
        self.out_dir = out_dir

    def setup(self) -> str | None:
        return None  # always ready locally

    def sync_file(self, rel: str) -> None:
        pass  # files already live in out_dir

    def collect(self) -> None:
        pass  # results.json + figures already in out_dir

    def stream(self, command: str, log: Path):
        return _stream_argv(["bash", "-c", command], self.out_dir, log, command)


class _RemoteExec:
    """Run the agent's bash on an SSH remote, mirroring authored files up and pulling
    ``results.json`` + figures back. The agent writes code LOCALLY (so it shows in the
    Files tab); each write is pushed to a fresh remote working dir, bash runs there in a
    login shell (picking up the remote's conda/PATH — no configured interpreter), and the
    deliverables are scp'd back. The remote dir is left in place for inspection/re-pull."""

    provenance = "ssh"

    def __init__(self, out_dir: Path, target):
        self.out_dir = out_dir
        self.target = target
        self.label = f"{target.user}@{target.host}"
        self.remote = f"/tmp/paperclaw_{uuid.uuid4().hex[:10]}"
        self._ssh = _ssh_base(target)
        self._scp = _ssh_base(target, scp=True)

    @staticmethod
    def _run(argv: list[str], timeout: int):
        """Best-effort ssh/scp: never raise (a missing ssh/scp binary → FileNotFoundError,
        a hung transfer → TimeoutExpired); return the CompletedProcess or None on failure."""
        try:
            return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError):
            return None

    def setup(self) -> str | None:
        """Create the remote working dir; return a human error string if the remote is
        unreachable (so the runner can fail cleanly up front instead of burning rounds)."""
        p = self._run(self._ssh + [self.label, f"mkdir -p {shlex.quote(self.remote)}"], 30)
        if p is None:
            return (f"could not reach the SSH remote {self.label} — is `ssh` installed and "
                    "the host reachable with the configured key?")
        if p.returncode != 0:
            return f"SSH remote {self.label} refused the connection: {(p.stderr or p.stdout or '').strip()[:300]}"
        return None

    def sync_file(self, rel: str) -> None:
        local = self.out_dir / rel
        if not local.is_file():
            return
        remote_path = posixpath.join(self.remote, rel.replace("\\", "/"))
        parent = posixpath.dirname(remote_path)
        if parent and parent != self.remote:
            self._run(self._ssh + [self.label, f"mkdir -p {shlex.quote(parent)}"], 30)
        self._run(self._scp + [str(local), f"{self.label}:{remote_path}"], 300)

    def collect(self) -> None:
        # best-effort — the deliverables may not exist yet
        self._run(self._scp + [f"{self.label}:{self.remote}/results.json", f"{self.out_dir}/"], 120)
        self._run(self._scp + [f"{self.label}:{self.remote}/*.png", f"{self.out_dir}/"], 300)

    def stream(self, command: str, log: Path):
        argv = self._ssh + [self.label,
                            f"cd {shlex.quote(self.remote)} && bash -lc {shlex.quote(command)}"]
        return _stream_argv(argv, None, log, command)


def _write_file(out_dir: Path, rel: str, content: str) -> str:
    """Create/overwrite a workspace file (mkdir parents); return a result line."""
    target = (out_dir / rel).resolve()
    try:
        target.relative_to(out_dir.resolve())
    except ValueError:
        return f"refused to write outside the workspace: {rel}"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        body = content if content.endswith("\n") else content + "\n"
        target.write_text(body, encoding="utf-8")
        return f"Wrote {rel} ({body.count(chr(10))} lines)."
    except OSError as exc:
        return f"Error writing {rel}: {exc}"


async def run_agentic_experiment(
    settings: LLMSettings,
    idea_ctx: str,
    plan: str,
    out_dir: Path,
    run_config: RunConfig,
    target=None,
) -> AsyncIterator[dict]:
    """Multi-file agentic experiment runner. Yields delta/thinking/status events +
    a terminal ``result`` (markdown / provenance / figures / error), like the other
    runners.

    When *target* (an ``SSHTarget``) is given, the agent still authors code LOCALLY (so
    it shows in the Files tab), but every ``bash`` step runs on the remote and the
    authored files are pushed up / ``results.json`` + figures pulled back — i.e. ``ssh``
    mode is just this loop with a remote executor (no interpreter / extra settings)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    log = out_dir / "stdout.log"
    log.write_text("", encoding="utf-8")
    executor = _RemoteExec(out_dir, target) if target is not None else _LocalExec(out_dir)
    setup_err = await asyncio.to_thread(executor.setup)
    if setup_err:  # remote unreachable — fail cleanly instead of crashing / burning rounds
        yield {"type": "result", "result": {
            "markdown": _render_failure_md(setup_err, 0), "provenance": executor.provenance,
            "figures": [], "attempts": 0, "error": setup_err}}
        return
    remote_note = (
        f"\n\nNOTE: you are operating on a REMOTE machine ({executor.label}) over SSH; the "
        "working directory lives on that host. Set up the environment and run everything with "
        "bash there (a login shell, so conda/venv are available), and write results.json in it."
        if target is not None else "")
    conversation = [{"role": "user", "content":
                     f"IDEA spec:\n{idea_ctx}\n\nResearch plan:\n{plan}\n\n"
                     "Explore the working directory, build the experiment as a clean "
                     "multi-file codebase, run it on the REAL data, and write results.json."
                     + remote_note}]

    rnd = 0
    for rnd in range(1, _AGENT_MAX_ROUNDS + 1):
        yield {"type": "status", "text": f"\n🤖 agent — step {rnd}/{_AGENT_MAX_ROUNDS}\n"}
        raw = ""
        try:
            async for ev in llm.stream_chat_thinking(settings, AGENT_EXPERIMENT_SYSTEM, conversation, max_tokens=4096):
                if ev["type"] == "thinking":
                    yield {"type": "thinking", "text": ev["text"]}
                else:
                    raw += ev["text"]
                    yield {"type": "delta", "text": ev["text"]}
        except (llm.LLMNotConfigured, llm.LLMError) as exc:
            yield {"type": "result", "result": {
                "markdown": _render_failure_md(str(exc), rnd), "provenance": executor.provenance,
                "figures": [], "attempts": rnd, "error": str(exc)}}
            return
        conversation.append({"role": "assistant", "content": raw})

        actions = _parse_actions(raw)
        if not actions:
            break  # no action → the agent is done (DONE)

        observations: list[str] = []
        for kind, path, body in actions:
            if kind == "write":
                msg = _write_file(out_dir, path, body.strip("\n") + "\n")
                await asyncio.to_thread(executor.sync_file, path)
                yield {"type": "status", "text": f"📝 {msg}\n"}
                observations.append(msg)
            elif kind == "patch":
                try:
                    msg = _apply_patch.apply_patch(out_dir, path, body)
                    await asyncio.to_thread(executor.sync_file, path)
                except (ValueError, FileNotFoundError, OSError) as exc:
                    msg = f"Error patching {path}: {exc}"
                yield {"type": "status", "text": f"🩹 {msg}\n"}
                observations.append(msg)
            else:  # bash — stream output live (no timeout) so long runs are monitorable
                cmd = body.strip()
                first = cmd.splitlines()[0] if cmd else ""
                yield {"type": "status", "text": f"▶ $ {first}\n"}
                rc, out = 0, ""
                async for kind, *rest in executor.stream(cmd, log):
                    if kind == "chunk":
                        yield {"type": "delta", "text": rest[0]}
                    else:
                        rc, out = rest[0], rest[1]
                observations.append(f"$ {cmd}\n(exit {rc})\n{out[-_STDERR_TAIL:]}")
        conversation.append({"role": "user", "content": "\n\n".join(observations)})

    await asyncio.to_thread(executor.collect)  # pull results.json + figures (no-op locally)
    results = _load_results(out_dir)
    figures = sorted(p.name for p in out_dir.glob("*.png"))
    stdout = log.read_text(encoding="utf-8") if log.is_file() else ""
    yield {"type": "result", "result": {
        "markdown": _render_results_md(results, stdout, figures),
        "provenance": executor.provenance, "figures": figures, "attempts": rnd,
        "error": None if results else "agent finished without writing results.json"}}
