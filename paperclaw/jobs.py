"""Experiment jobs — run a hypothesis experiment as a DETACHED OS process.

The experiment (the whole agent loop + code execution) runs in its own process so it
keeps going even if the backend dies. The backend is a thin monitor: it spawns the
process, then tracks it by tailing an on-disk event log and checking the PID — there
is no in-process job registry and no wall-clock timeout.

Per hypothesis dir (`hypotheses/<hid>/`):
  - ``job.json``      — {jobId, pid, status, startedAt, updatedAt, error}
  - ``events.jsonl``  — one JSON event per line (phase/thinking/delta/result/done…)
  - ``runner.log``    — the detached process's own stdout/stderr (crash diagnostics)

Status: running | done | error | cancelled | interrupted (running but PID gone).
"""

import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import AsyncIterator

_HID_RE = re.compile(r"^[A-Za-z0-9.]+$")
_TERMINAL = {"done", "error", "cancelled", "interrupted"}
_OVERRIDE_FILE = ".run_override.json"  # per-job EFFECTIVE run config (auto-run overrides)


def _safe_hid(hid: str) -> str:
    if not hid or not _HID_RE.match(hid) or ".." in hid:
        raise ValueError(f"invalid hypothesis id: {hid!r}")
    return hid


def _hyp_dir(store, idea_id: str, hid: str) -> Path | None:
    base = store.idea_path(idea_id)
    if base is None:
        return None
    return base / "hypotheses" / _safe_hid(hid)


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but not ours
    return True


def _read_job(hdir: Path) -> dict | None:
    p = hdir / "job.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _write_job(hdir: Path, job: dict) -> None:
    hdir.mkdir(parents=True, exist_ok=True)
    (hdir / "job.json").write_text(json.dumps(job, indent=2), encoding="utf-8")


def _write_run_override(hdir: Path, run_config, use_reference_codebase: bool) -> None:
    """Pin the EFFECTIVE run config for this job (per-run overrides from an auto run),
    or clear it when *run_config* is None so the child falls back to the global config."""
    p = hdir / _OVERRIDE_FILE
    if run_config is None:
        p.unlink(missing_ok=True)
        return
    p.write_text(json.dumps({
        "runConfig": run_config.model_dump(by_alias=True),
        "useReferenceCodebase": bool(use_reference_codebase),
    }, indent=2), encoding="utf-8")


def read_run_override(hdir: Path) -> dict | None:
    """The per-job effective run config pinned by :func:`start_experiment_job`, or None.

    Returns ``{"runConfig": RunConfig, "useReferenceCodebase": bool}`` so the detached
    experiment child uses exactly the mode / SSH target / codebase toggle the (auto-run)
    caller chose — not the global on-disk config it would otherwise re-read."""
    p = hdir / _OVERRIDE_FILE
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    from paperclaw.server.models import RunConfig
    try:
        rc = RunConfig.model_validate(data.get("runConfig") or {})
    except Exception:  # noqa: BLE001 — a malformed override degrades to the global config
        return None
    return {"runConfig": rc, "useReferenceCodebase": bool(data.get("useReferenceCodebase", True))}


def _reconcile(job: dict) -> dict:
    """A job marked running whose process is gone was interrupted (e.g. machine reboot)."""
    if job.get("status") == "running" and not _pid_alive(job.get("pid")):
        job = {**job, "status": "interrupted"}
    return job


def _view(store, idea_id: str, hid: str, job: dict) -> dict:
    idea = next((i for i in store.list_ideas() if i.id == idea_id), None)
    return {
        "jobId": job.get("jobId", ""),
        "ideaId": idea_id,
        "ideaTitle": idea.title if idea else idea_id,
        "hypothesisId": hid,
        "status": job.get("status", "interrupted"),
        "startedAt": job.get("startedAt", 0.0),
        "updatedAt": job.get("updatedAt", job.get("startedAt", 0.0)),
        "error": job.get("error"),
    }


# ── Spawn (backend side) ──────────────────────────────────────────────────────

def start_experiment_job(store, idea_id: str, hid: str, *,
                         run_config=None, use_reference_codebase: bool = True) -> dict:
    """Start (or re-attach to) a detached experiment process for one hypothesis.
    Returns the ExperimentJob view dict. Idempotent while a job is alive.

    *run_config* (with *use_reference_codebase*) pins the EFFECTIVE experiment config
    for THIS job — a per-run override from an auto run (experiment mode / SSH target /
    reference-codebase reuse) — into ``.run_override.json`` so the detached child uses
    it instead of re-reading the global config. None ⇒ use the IDEA's allocated resources
    (`store.effective_run_config`: the idea's pinned compute/GPU over the global config), so
    a manual run from the Hypotheses tab honours the idea's resource allocation too."""
    hdir = _hyp_dir(store, idea_id, hid)
    if hdir is None:
        raise FileNotFoundError("Idea not found")
    hdir.mkdir(parents=True, exist_ok=True)
    if run_config is None:  # honour the idea's allocated resources (else global)
        run_config = store.effective_run_config(idea_id)

    existing = _read_job(hdir)
    if existing and existing.get("status") == "running" and _pid_alive(existing.get("pid")):
        return _view(store, idea_id, hid, existing)  # already running — re-attach, don't duplicate

    # fresh run: clear the previous event log + pin/clear the effective run config
    (hdir / "events.jsonl").write_text("", encoding="utf-8")
    _write_run_override(hdir, run_config, use_reference_codebase)
    job_id = uuid.uuid4().hex[:12]

    runner_log = open(hdir / "runner.log", "wb")  # noqa: SIM115 — owned by the child
    proc = subprocess.Popen(
        [sys.executable, "-m", "paperclaw.cli", "experiment-run", idea_id, hid, "--job", job_id],
        cwd=str(store.home),
        env={**os.environ, "PAPERCLAW_HOME": str(store.home)},
        stdout=runner_log, stderr=subprocess.STDOUT,
        start_new_session=True,  # detach: own process group, survives the backend exiting
    )
    runner_log.close()
    now = time.time()
    job = {"jobId": job_id, "pid": proc.pid, "status": "running",
           "startedAt": now, "updatedAt": now, "error": None}
    _write_job(hdir, job)
    return _view(store, idea_id, hid, job)


def experiment_job(store, idea_id: str, hid: str) -> dict | None:
    hdir = _hyp_dir(store, idea_id, hid)
    if hdir is None:
        return None
    job = _read_job(hdir)
    return _view(store, idea_id, hid, _reconcile(job)) if job else None


def list_experiment_jobs(store) -> list[dict]:
    """Every experiment job across all ideas (newest first) — drives the monitor."""
    out: list[dict] = []
    for idea in store.list_ideas():
        base = store.idea_path(idea.id)
        if base is None:
            continue
        for jp in (base / "hypotheses").glob("*/job.json"):
            job = _read_job(jp.parent)
            if job:
                out.append(_view(store, idea.id, jp.parent.name, _reconcile(job)))
    out.sort(key=lambda j: j["startedAt"], reverse=True)
    return out


def cancel_experiment_job(store, idea_id: str, hid: str) -> dict | None:
    hdir = _hyp_dir(store, idea_id, hid)
    if hdir is None:
        return None
    job = _read_job(hdir)
    if not job:
        return None
    pid = job.get("pid")
    if job.get("status") == "running" and _pid_alive(pid):
        try:
            # Only signal a process group WE own: our detached children are session
            # leaders (start_new_session=True ⇒ pgid == pid). If pgid != pid the pid
            # was reused by something else — never risk SIGTERM-ing the wrong group
            # (including the backend's own).
            if pid and os.getpgid(pid) == pid:
                os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    job = {**job, "status": "cancelled", "updatedAt": time.time()}
    _write_job(hdir, job)
    return _view(store, idea_id, hid, job)


def _read_new(path: Path, offset: int) -> tuple[str, int]:
    """Bytes appended since *offset* (decoded) + the new offset. Reading only the tail
    keeps this O(new data), not O(file), so the event loop isn't blocked on a big log."""
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read()
        return data.decode("utf-8", "replace"), offset + len(data)
    except OSError:
        return "", offset


async def tail_experiment_events(store, idea_id: str, hid: str,
                                 from_line: int = 0) -> AsyncIterator[dict]:
    """Replay events.jsonl from *from_line*, then stream new lines live until the job
    is terminal — re-attachable across reloads AND backend restarts (pure file/PID I/O).
    Reads only newly-appended bytes (off-thread) so a large/growing log never blocks
    the event loop."""
    hdir = _hyp_dir(store, idea_id, hid)
    if hdir is None:
        yield {"type": "error", "message": "Idea not found"}
        return
    events = hdir / "events.jsonl"
    offset, line_no, pending, idle = 0, 0, "", 0.0
    while True:
        text, offset = await asyncio.to_thread(_read_new, events, offset)
        if text:
            pending += text
            parts = pending.split("\n")
            pending = parts.pop()  # last item is the (possibly partial) trailing line
            for raw in parts:
                if not raw.strip():
                    continue
                line_no += 1
                if line_no <= from_line:
                    continue
                try:
                    ev = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                yield {**ev, "line": line_no}
                if ev.get("type") in ("done", "error"):
                    return
            idle = 0.0
        job = _reconcile(_read_job(hdir) or {})
        if job.get("status") in _TERMINAL:
            yield {"type": "done", "status": job.get("status"), "error": job.get("error"), "line": line_no}
            return
        await asyncio.sleep(0.5)
        idle += 0.5
        if idle > 3600 * 12:  # safety: stop tailing a stuck/abandoned reader after 12h
            return


# ── Child side (the detached process body) ────────────────────────────────────

async def run_experiment_job_blocking(store, settings, idea_id: str, hid: str, job_id: str) -> int:
    """Body of the detached `experiment-run` process: run the experiment, append every
    event to events.jsonl, and keep job.json status current. Returns an exit code."""
    from paperclaw import service

    hdir = _hyp_dir(store, idea_id, hid)
    if hdir is None:
        return 2
    hdir.mkdir(parents=True, exist_ok=True)
    events = hdir / "events.jsonl"

    def _bump(status: str, error: str | None = None) -> None:
        job = _read_job(hdir) or {"jobId": job_id, "pid": os.getpid(), "startedAt": time.time()}
        job.update({"jobId": job_id, "status": status, "updatedAt": time.time(), "error": error})
        _write_job(hdir, job)

    _bump("running")
    last_bump = time.time()
    status, err = "done", None
    try:
        with events.open("a", encoding="utf-8") as fh:
            async for ev in service.stream_hypothesis_experiment(store, settings, idea_id, hid):
                fh.write(json.dumps(ev) + "\n")
                fh.flush()
                if ev.get("type") == "error":
                    status, err = "error", ev.get("message")
                now = time.time()
                if now - last_bump > 2:  # keep updatedAt fresh for the monitor
                    _bump("running")
                    last_bump = now
    except Exception as exc:  # noqa: BLE001 — record any crash for the monitor
        status, err = "error", f"{type(exc).__name__}: {exc}"
        try:
            with events.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"type": "error", "message": err}) + "\n")
        except OSError:
            pass
    _bump(status, err)
    return 0 if status == "done" else 1
