"""Tests for detached experiment jobs (no real subprocess / LLM — mocked)."""

import asyncio
import json
import time

from paperclaw import jobs, service
from paperclaw.server.store import Store


def _idea_with_hyp(tmp_path, hid="H1"):
    store = Store(tmp_path)
    idea = store.add_idea("Job test")
    (store.idea_path(idea.id) / "hypotheses" / hid).mkdir(parents=True)
    return store, idea.id


def test_run_job_blocking_writes_log_and_status(tmp_path, monkeypatch):
    store, idea_id = _idea_with_hyp(tmp_path)

    async def fake_stream(store, settings, iid, h):
        yield {"type": "phase", "phase": "experiment", "label": "Running…"}
        yield {"type": "delta", "text": "epoch 1\n"}
        yield {"type": "done"}
    monkeypatch.setattr(service, "stream_hypothesis_experiment", fake_stream)

    rc = asyncio.run(jobs.run_experiment_job_blocking(store, None, idea_id, "H1", "j1"))
    assert rc == 0
    hdir = store.idea_path(idea_id) / "hypotheses" / "H1"
    assert len(hdir.joinpath("events.jsonl").read_text().splitlines()) == 3
    assert json.loads(hdir.joinpath("job.json").read_text())["status"] == "done"


def test_run_job_records_error(tmp_path, monkeypatch):
    store, idea_id = _idea_with_hyp(tmp_path)

    async def boom(store, settings, iid, h):
        yield {"type": "error", "message": "no key"}
    monkeypatch.setattr(service, "stream_hypothesis_experiment", boom)
    rc = asyncio.run(jobs.run_experiment_job_blocking(store, None, idea_id, "H1", "j1"))
    assert rc == 1
    job = jobs.experiment_job(store, idea_id, "H1")
    assert job["status"] == "error" and job["error"] == "no key"


def test_tail_replays_then_ends(tmp_path, monkeypatch):
    store, idea_id = _idea_with_hyp(tmp_path)

    async def fake_stream(store, settings, iid, h):
        yield {"type": "delta", "text": "a"}
        yield {"type": "done"}
    monkeypatch.setattr(service, "stream_hypothesis_experiment", fake_stream)

    async def go():
        await jobs.run_experiment_job_blocking(store, None, idea_id, "H1", "j1")  # populate log
        return [ev["type"] async for ev in jobs.tail_experiment_events(store, idea_id, "H1")]
    types = asyncio.run(go())
    assert "delta" in types and types[-1] == "done"


def test_list_and_reconcile_interrupted(tmp_path):
    store, idea_id = _idea_with_hyp(tmp_path)
    hdir = store.idea_path(idea_id) / "hypotheses" / "H1"
    # a job marked running whose PID is gone → interrupted
    hdir.joinpath("job.json").write_text(json.dumps(
        {"jobId": "j1", "pid": 999999, "status": "running", "startedAt": time.time(),
         "updatedAt": time.time(), "error": None}))
    listed = jobs.list_experiment_jobs(store)
    assert len(listed) == 1
    assert listed[0]["status"] == "interrupted" and listed[0]["hypothesisId"] == "H1"
    assert listed[0]["ideaTitle"] == "Job test"


def test_start_spawns_detached(tmp_path, monkeypatch):
    store, idea_id = _idea_with_hyp(tmp_path)

    class _FakeProc:
        pid = 4242
    captured = {}

    def fake_popen(cmd, **kw):
        captured["cmd"] = cmd
        captured["detached"] = kw.get("start_new_session")
        return _FakeProc()
    monkeypatch.setattr(jobs.subprocess, "Popen", fake_popen)

    view = jobs.start_experiment_job(store, idea_id, "H1")
    assert view["status"] == "running" and view["hypothesisId"] == "H1"
    assert "experiment-run" in captured["cmd"] and captured["detached"] is True
    job = json.loads((store.idea_path(idea_id) / "hypotheses" / "H1" / "job.json").read_text())
    assert job["pid"] == 4242


def test_start_pins_run_override(tmp_path, monkeypatch):
    """A per-run override (auto-run experiment mode / SSH target / codebase toggle) is
    pinned into the job dir so the detached child uses it; a plain start pins the IDEA's
    allocated resources (its effective run config)."""
    from paperclaw.server.models import RunConfig
    store, idea_id = _idea_with_hyp(tmp_path)
    hdir = store.idea_path(idea_id) / "hypotheses" / "H1"

    class _FakeProc:
        pid = 4242
    monkeypatch.setattr(jobs.subprocess, "Popen", lambda *a, **k: _FakeProc())

    rc = RunConfig(experimentMode="executed", sshTargetId="gpu1")
    jobs.start_experiment_job(store, idea_id, "H1",
                              run_config=rc, use_reference_codebase=False)
    ov = jobs.read_run_override(hdir)
    assert ov is not None
    assert ov["runConfig"].experiment_mode == "executed"
    assert ov["runConfig"].ssh_target_id == "gpu1"
    assert ov["useReferenceCodebase"] is False

    # finish the job so the next start is a fresh run (not a re-attach). With NO explicit
    # override, a plain start now pins the idea's ALLOCATED resources (effective run config).
    job = json.loads((hdir / "job.json").read_text())
    (hdir / "job.json").write_text(json.dumps({**job, "status": "done"}))
    store.set_idea_resources(idea_id, {"experimentMode": "ssh", "sshTargetId": "gpu2"})
    jobs.start_experiment_job(store, idea_id, "H1")
    ov2 = jobs.read_run_override(hdir)
    assert ov2 is not None and ov2["runConfig"].experiment_mode == "ssh"  # idea allocation used
    assert ov2["runConfig"].ssh_target_id == "gpu2"
