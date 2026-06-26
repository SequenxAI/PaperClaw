"""Detached background jobs for a workspace.

Lets the chat agent launch a LONG command (an hours-long experiment rerun) that SURVIVES the
chat turn ending — and even the backend restarting — then poll it on a later turn. This is the
agent's analogue of Claude Code's ``Bash(run_in_background)`` + ``BashOutput``: a job that can't
fit a synchronous ``bash`` call (10-min cap) is launched detached and checked with ``bash_output``.

Each job lives under ``<workspace>/.bgjobs/<id>/``: ``meta.json`` (id/pid/command/startedAt),
``output.log`` (combined stdout+stderr), and ``exit_code`` (written by a wrapper when the command
finishes, so status can report done + code). The process runs in its OWN session
(``start_new_session=True``) with output to a file (not a pipe), so it is fully detached.

SECURITY: arbitrary shell, same trusted-self-hosted model as the experiment coding agent.
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

_DIR = ".bgjobs"          # job records live under <workspace>/.bgjobs/
_TAIL_BYTES = 65_536      # read at most the last 64 KB of a (possibly huge) log
_TAIL_CHARS = 6_000       # cap returned output


def _jobs_dir(base_dir: Path) -> Path:
    return Path(base_dir) / _DIR


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True       # exists but not ours
    return True


def launch(base_dir: Path, command: str) -> str:
    """Start *command* detached; return a human note with the job id + log path (returns
    IMMEDIATELY — does not wait)."""
    base_dir = Path(base_dir)
    jid = uuid.uuid4().hex[:8]
    jdir = _jobs_dir(base_dir) / jid
    jdir.mkdir(parents=True, exist_ok=True)
    log = jdir / "output.log"
    rc_file = jdir / "exit_code"
    # Wrapper records the exit code so status() can report "done (exit N)" without having waited.
    # Run the command in a SUBSHELL `( … )` so an `exit` inside it doesn't skip the rc-recording.
    wrapped = (f"( {command}\n)\n__rc=$?\n"
               f"printf %s \"$__rc\" > {shlex.quote(str(rc_file))}\nexit $__rc")
    try:
        with open(log, "wb") as out:
            p = subprocess.Popen(
                ["bash", "-c", wrapped], cwd=str(base_dir),
                stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                start_new_session=True,   # own process group → survives the turn + backend restart
            )
    except Exception as exc:  # bash missing, cwd gone, …
        return f"[failed to launch background job: {exc}]"
    (jdir / "meta.json").write_text(json.dumps(
        {"id": jid, "pid": p.pid, "command": command, "startedAt": time.time()}))
    return (f"Started background job `{jid}` (pid {p.pid}). It runs detached and survives this turn.\n"
            f"Output streams to `.bgjobs/{jid}/output.log`. Poll it with the `bash_output` tool "
            f"(job_id={jid}) — do NOT block waiting; end your turn and check back, or tell the user "
            f"to type 'continue' to check progress.")


def _tail(log: Path, lines: int) -> str:
    try:
        size = log.stat().st_size
        with open(log, "rb") as f:
            f.seek(max(0, size - _TAIL_BYTES))
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    return "\n".join(data.splitlines()[-lines:])[-_TAIL_CHARS:]


def _kill(jdir: Path) -> None:
    try:
        meta = json.loads((jdir / "meta.json").read_text())
        os.killpg(os.getpgid(meta["pid"]), signal.SIGTERM)
    except (OSError, ValueError, KeyError):
        pass


def _state(jdir: Path) -> str:
    meta = json.loads((jdir / "meta.json").read_text())
    if (jdir / "exit_code").is_file():
        return f"done (exit {(jdir / 'exit_code').read_text().strip()})"
    return f"running (pid {meta['pid']})" if _alive(meta["pid"]) \
        else "stopped (process gone, no exit code — killed or crashed)"


def _one(jdir: Path, lines: int) -> str:
    meta = json.loads((jdir / "meta.json").read_text())
    tail = _tail(jdir / "output.log", lines)
    head = f"[{jdir.name}] {_state(jdir)} — `{meta['command'][:140]}`"
    return f"{head}\n{tail}".rstrip() if tail else head


def status(base_dir: Path, job_id: str | None = None, lines: int = 40, kill: bool = False) -> str:
    """Status + recent output of a background job (or a one-line summary of ALL jobs when
    *job_id* is omitted). *kill* SIGTERMs the job's process group."""
    jobs = _jobs_dir(Path(base_dir))
    if not jobs.is_dir() or not any(jobs.iterdir()):
        return "No background jobs."
    if job_id:
        jdir = jobs / job_id
        if not (jdir / "meta.json").is_file():
            return f"No such background job: {job_id}"
        if kill:
            _kill(jdir)
        return _one(jdir, lines)
    out = []
    for jdir in sorted((d for d in jobs.iterdir() if (d / "meta.json").is_file()),
                       key=lambda p: p.stat().st_mtime):
        if kill:
            _kill(jdir)
        out.append(_one(jdir, lines if (job_id or kill) else 3))
    return "\n\n".join(out) or "No background jobs."
