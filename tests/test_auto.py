"""Tests for the autonomous `auto` mode (service.stream_auto_research + CLI wiring)."""

import asyncio
from types import SimpleNamespace

from paperclaw import service
from paperclaw.config import LLMSettings
from paperclaw.server.store import Store


def _collect(agen):
    out = []

    async def go():
        async for ev in agen:
            out.append(ev)

    asyncio.run(go())
    return out


def _ok_report():
    return SimpleNamespace(ok=True, checks=[SimpleNamespace(label="LLM", status="ok", detail="configured")])


def test_stream_auto_research_orchestration(tmp_path, monkeypatch):
    """doctor → domain → idea → pipeline, forwarding the configured stop params."""
    store = Store(tmp_path)
    domain = store.add_domain("Generative Modeling", spec="# Generative Modeling\n")
    idea = store.add_idea("Auto idea")
    monkeypatch.setattr(service, "environment_report", lambda settings, home=None: _ok_report())

    async def fake_domain(s, st, prompt):
        assert prompt == "generative modeling"
        return domain

    async def fake_idea(s, st, dom):
        assert dom is domain
        return idea

    async def fake_pipeline(s, st, idea_id, *, max_hypotheses, page_limit, target_positive, **over):
        assert idea_id == idea.id
        assert (target_positive, max_hypotheses, page_limit) == (2, 6, 8)
        assert over["use_reference_codebase"] is True  # default when no override given
        yield {"type": "round", "round": 1, "hypothesisId": "H1"}
        yield {"type": "paper_ready", "download_url": f"/api/ideas/{idea_id}/paper"}
        yield {"type": "done"}

    async def _no_similar(*a, **k):
        return None
    monkeypatch.setattr(service, "find_similar_domain", _no_similar)  # → fresh domain
    monkeypatch.setattr(service, "auto_create_domain", fake_domain)
    monkeypatch.setattr(service, "auto_create_idea", fake_idea)
    from paperclaw import iterative_pipeline as ip
    monkeypatch.setattr(ip, "stream_iterative_research_events", fake_pipeline)

    evs = _collect(service.stream_auto_research(store, LLMSettings(), tmp_path, "generative modeling"))
    types = [e["type"] for e in evs]
    assert "doctor" in types
    assert any(e["type"] == "domain_created" and e["name"] == "Generative Modeling" for e in evs)
    assert any(e["type"] == "idea_created" and e["ideaId"] == idea.id for e in evs)
    # ordering: domain before idea before the pipeline rounds
    assert types.index("domain_created") < types.index("idea_created") < types.index("round")
    assert "paper_ready" in types and evs[-1]["type"] == "done"

    # the run persisted a PER-IDEA status snapshot watchable from outside the terminal
    st = store.get_idea_auto_run(idea.id)
    assert st["status"] == "done" and st["phase"] == "done"
    assert st["ideaId"] == idea.id and st["domainName"] == "Generative Modeling"
    assert st["round"] == 1 and st["paperReady"] is True


def test_auto_reuses_similar_domain(tmp_path, monkeypatch):
    """A very-similar existing domain is enriched + reused, not duplicated."""
    store = Store(tmp_path)
    existing = store.add_domain("Generative Modeling", spec="# Generative Modeling\n")
    idea = store.add_idea("Auto idea")
    monkeypatch.setattr(service, "environment_report", lambda settings, home=None: _ok_report())

    async def _similar(s, st, topic):
        return existing

    enriched = []

    async def _enrich(s, st, dom):
        enriched.append(dom.id)
        return dom

    async def _no_create(*a, **k):
        raise AssertionError("should not create a domain when a similar one exists")

    async def _idea(s, st, dom):
        assert dom is existing
        return idea

    async def _pipeline(s, st, idea_id, **kw):
        yield {"type": "done"}

    monkeypatch.setattr(service, "find_similar_domain", _similar)
    monkeypatch.setattr(service, "enrich_domain", _enrich)
    monkeypatch.setattr(service, "auto_create_domain", _no_create)
    monkeypatch.setattr(service, "auto_create_idea", _idea)
    from paperclaw import iterative_pipeline as ip
    monkeypatch.setattr(ip, "stream_iterative_research_events", _pipeline)

    evs = _collect(service.stream_auto_research(store, LLMSettings(), tmp_path, "generative models"))
    assert enriched == [existing.id]
    dc = next(e for e in evs if e["type"] == "domain_created")
    assert dc["reused"] is True and dc["domainId"] == existing.id


def test_find_and_enrich_domain(tmp_path, monkeypatch):
    from paperclaw import literature, llm
    store = Store(tmp_path)
    assert asyncio.run(service.find_similar_domain(store, LLMSettings(), "x")) is None  # no domains

    d1 = store.add_domain("Generative Modeling", spec="# Generative Modeling\n\n## General Target\nimage synthesis\n")
    store.add_domain("Time Series", spec="# Time Series\n")

    async def says1(*a, **k):
        return SimpleNamespace(text="1", model="x")
    monkeypatch.setattr(llm, "chat", says1)
    assert asyncio.run(service.find_similar_domain(store, LLMSettings(), "generative models")).id == d1.id

    async def says0(*a, **k):
        return SimpleNamespace(text="0", model="x")
    monkeypatch.setattr(llm, "chat", says0)
    assert asyncio.run(service.find_similar_domain(store, LLMSettings(), "robotics")) is None

    # enrich rewrites the spec from a domain.md block
    async def no_papers(q):
        return ([], [])
    monkeypatch.setattr(literature, "search_for_domain", no_papers)

    async def fake_enrich(settings, system, messages, max_tokens=2048):
        return SimpleNamespace(
            text="```domain.md\n# Generative Modeling\n\n## General Target\nNEW recent advances\n```", model="x")
    monkeypatch.setattr(llm, "chat", fake_enrich)
    out = asyncio.run(service.enrich_domain(store, LLMSettings(), d1))
    assert "NEW recent advances" in store.get_domain_spec(out.id)


def test_stream_auto_research_aborts_when_doctor_fails(tmp_path, monkeypatch):
    """A failing doctor stops the run BEFORE spending tokens on a domain."""
    store = Store(tmp_path)
    monkeypatch.setattr(service, "environment_report", lambda settings, home=None: SimpleNamespace(
        ok=False, checks=[SimpleNamespace(label="LLM", status="fail", detail="no API key")]))
    reached = []

    async def fake_domain(*a, **k):
        reached.append(1)
        return None

    monkeypatch.setattr(service, "auto_create_domain", fake_domain)

    evs = _collect(service.stream_auto_research(store, LLMSettings(), tmp_path, "x"))
    assert any(e["type"] == "doctor" and e["ok"] is False for e in evs)
    assert evs[-1]["type"] == "error" and "no API key" in evs[-1]["message"]
    assert not reached  # never proceeded to domain creation
    # A topic run that fails at doctor has no idea yet → nothing to persist per-idea;
    # the error surfaces only in the stream (the foreground terminal shows it).
    assert store.list_auto_runs() == []


def test_auto_run_route_and_status(tmp_path):
    """GET /api/ideas/{id}/auto-run returns that idea's snapshot (web UI + CLI status)."""
    from fastapi.testclient import TestClient
    from paperclaw.server.app import create_app
    from paperclaw.server.store import Store

    app = create_app(home=tmp_path)
    client = TestClient(app)
    store = Store(tmp_path)
    idea = store.add_idea("T")
    assert client.get(f"/api/ideas/{idea.id}/auto-run").json() is None  # nothing yet

    store.put_idea_auto_run(idea.id, {
        "topic": "gen modeling", "status": "running", "phase": "hypotheses",
        "label": "Testing hypothesis 2…", "ideaId": idea.id, "ideaTitle": "T",
        "round": 2, "positives": 1, "targetPositive": 2, "maxHypotheses": 6,
        "currentHypothesisId": "H1.1", "paperReady": False, "pid": 999999999,
        "startedAt": 1.0, "updatedAt": 2.0, "error": None,
    })
    body = client.get(f"/api/ideas/{idea.id}/auto-run").json()
    # pid is dead → reconciled to 'interrupted', but the loop fields are preserved
    assert body["phase"] == "hypotheses" and body["currentHypothesisId"] == "H1.1"
    assert body["round"] == 2 and body["positives"] == 1
    # and it shows up in the global list
    assert any(r["ideaId"] == idea.id for r in client.get("/api/auto-runs").json())


def test_stream_auto_resume_continues_existing_idea(tmp_path, monkeypatch):
    store = Store(tmp_path)
    idea = store.add_idea("T")
    store.put_idea_auto_run(idea.id, {
        "topic": "gen", "status": "stopped", "phase": "hypotheses", "label": "stopped",
        "ideaId": idea.id, "ideaTitle": "T", "round": 1, "positives": 0,
        "targetPositive": 2, "maxHypotheses": 6, "pageLimit": 8,
    })
    captured = {}

    async def fake_pipeline(s, st, idea_id, *, restart, max_hypotheses, page_limit,
                            target_positive, max_depth):
        captured.update(idea_id=idea_id, restart=restart, max_hypotheses=max_hypotheses,
                        page_limit=page_limit, target_positive=target_positive, max_depth=max_depth)
        yield {"type": "round", "round": 2, "hypothesisId": "H2"}
        yield {"type": "paper_ready", "download_url": "/x"}
        yield {"type": "done"}

    from paperclaw import iterative_pipeline as ip
    monkeypatch.setattr(ip, "stream_iterative_research_events", fake_pipeline)

    # no idea_id → resumes the single stopped run
    evs = _collect(service.stream_auto_resume(store, LLMSettings(), tmp_path))
    # resumes the SAME idea, restart=False, reusing the stored stop settings (maxDepth
    # absent from the older snapshot → default 3)
    assert captured == {"idea_id": idea.id, "restart": False, "max_hypotheses": 6,
                        "page_limit": 8, "target_positive": 2, "max_depth": 3}
    assert evs[-1]["type"] == "done"
    st = store.get_idea_auto_run(idea.id)
    assert st["status"] == "done" and st["round"] == 2 and st["ideaId"] == idea.id


def test_stream_auto_resume_without_prior_run(tmp_path):
    store = Store(tmp_path)
    evs = _collect(service.stream_auto_resume(store, LLMSettings(), tmp_path))
    assert evs[-1]["type"] == "error" and "No auto run to resume" in evs[-1]["message"]


def test_stop_auto_run(tmp_path, monkeypatch):
    from paperclaw import jobs
    store = Store(tmp_path)
    assert service.stop_auto_run(store)["ok"] is False  # nothing running

    idea = store.add_idea("T")
    store.put_idea_auto_run(idea.id, {"topic": "t", "status": "running", "ideaId": idea.id,
                                      "currentHypothesisId": "H1"})  # no pid → no real signal
    cancelled = []
    monkeypatch.setattr(jobs, "cancel_experiment_job",
                        lambda s, i, h: cancelled.append((i, h)) or {"jobId": "x"})

    res = service.stop_auto_run(store)  # no idea_id → the single running run
    assert res["ok"] is True
    assert cancelled == [(idea.id, "H1")]                     # cancelled the experiment
    assert store.get_idea_auto_run(idea.id)["status"] == "stopped"  # marked stopped
    assert service.stop_auto_run(store)["ok"] is False        # nothing running now


def test_launch_auto_run(tmp_path, monkeypatch):
    from paperclaw import jobs
    store = Store(tmp_path)

    class FakeProc:
        pid = 4242

    captured = {}
    monkeypatch.setattr("subprocess.Popen",
                        lambda cmd, **kw: captured.update(cmd=cmd, env=kw.get("env")) or FakeProc())

    res = service.launch_auto_run(store, tmp_path, "gen modeling",
                                  target_positive=3, max_hypotheses=5, page_limit=7)
    assert res["ok"] is True and res["pid"] == 4242
    assert "run" in captured["cmd"] and "gen modeling" in captured["cmd"]
    assert "--positive" in captured["cmd"] and "3" in captured["cmd"]
    assert captured["env"]["PAPERCLAW_HOME"] == str(tmp_path.resolve())  # ABSOLUTE home for the child

    # topic runs create a NEW idea each time → no per-idea seed yet, and parallel runs
    # are allowed (no global "already in progress" guard anymore)
    monkeypatch.setattr(jobs, "_pid_alive", lambda p: True)
    assert service.launch_auto_run(store, tmp_path, "x")["ok"] is True


def test_launch_auto_run_for_idea(tmp_path, monkeypatch):
    """The per-idea ⚡ Auto run spawns `auto --idea <id>` on an EXISTING idea and
    seeds the banner status with that idea (phase = hypotheses, no creation)."""
    store = Store(tmp_path)
    idea = store.add_idea("Diffusion schedules")

    class FakeProc:
        pid = 777

    captured = {}
    monkeypatch.setattr("subprocess.Popen",
                        lambda cmd, **kw: captured.update(cmd=cmd) or FakeProc())

    res = service.launch_auto_run(store, tmp_path, idea_id=idea.id, target_positive=2)
    assert res["ok"] is True and res["pid"] == 777
    assert "--idea" in captured["cmd"] and idea.id in captured["cmd"]
    st = store.get_idea_auto_run(idea.id)
    assert st["ideaId"] == idea.id and st["phase"] == "hypotheses"
    assert st["ideaTitle"] == "Diffusion schedules"

    # the SAME idea refuses a concurrent run while its process is alive
    from paperclaw import jobs
    monkeypatch.setattr(jobs, "_pid_alive", lambda p: True)
    res2 = service.launch_auto_run(store, tmp_path, idea_id=idea.id)
    assert res2["ok"] is False and "already auto-running" in res2["detail"]

    # unknown idea → friendly failure, no process
    assert service.launch_auto_run(store, tmp_path, idea_id="nope")["ok"] is False


def test_launch_auto_run_threads_per_run_overrides(tmp_path, monkeypatch):
    """The Auto settings (experiment execution / style / codebase) become `run` flags."""
    store = Store(tmp_path)
    idea = store.add_idea("X")

    class FakeProc:
        pid = 5

    captured = {}
    monkeypatch.setattr("subprocess.Popen", lambda cmd, **kw: captured.update(cmd=cmd) or FakeProc())
    res = service.launch_auto_run(store, tmp_path, idea_id=idea.id, experiment_mode="ssh",
                                  ssh_target_id="gpu1", writing_style="narrative",
                                  use_reference_codebase=False)
    assert res["ok"] is True
    cmd = captured["cmd"]
    assert cmd[3] == "run" and "--idea" in cmd  # python -m paperclaw.cli run --idea …
    assert "--experiment-mode" in cmd and "ssh" in cmd
    assert "--ssh-target" in cmd and "gpu1" in cmd
    assert "--style" in cmd and "narrative" in cmd
    assert "--no-codebase" in cmd


def test_auto_run_start_route(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from paperclaw.server.app import create_app
    called = {}
    monkeypatch.setattr(service, "launch_auto_run",
                        lambda store, home, topic, **kw: called.update(topic=topic, **kw) or {"ok": True, "detail": "started"})
    app = create_app(home=tmp_path)
    r = TestClient(app).post("/api/auto-run/start",
                             json={"topic": "gen", "positive": 3, "maxHypotheses": 5, "pageLimit": 7})
    assert r.json()["ok"] is True
    assert called == {"topic": "gen", "idea_id": None,
                      "target_positive": 3, "max_hypotheses": 5, "page_limit": 7, "max_depth": 3,
                      "experiment_mode": None, "ssh_target_id": None,
                      "writing_style": None, "benchmark": None,
                      "use_reference_codebase": True, "fill_page": False}

    # the per-idea variant forwards ideaId + the per-run overrides
    called.clear()
    r2 = TestClient(app).post("/api/auto-run/start", json={
        "ideaId": "i9", "positive": 2, "experimentMode": "cli", "writingStyle": "narrative",
        "useReferenceCodebase": False})
    assert r2.json()["ok"] is True
    assert called["idea_id"] == "i9" and called["topic"] == ""
    assert called["experiment_mode"] == "cli" and called["writing_style"] == "narrative"
    assert called["use_reference_codebase"] is False


def test_auto_run_log_tail(tmp_path):
    """The agent-feedback panel tails the idea's detached auto-run log from an offset."""
    from fastapi.testclient import TestClient
    from paperclaw.server.app import create_app
    from paperclaw.server.store import Store as _Store
    app = create_app(home=tmp_path)
    store = _Store(tmp_path)
    idea = store.add_idea("T")
    (tmp_path / "ideas" / idea.id / "auto_run.log").write_bytes(b"hello\nworld\n")  # the child's output
    c = TestClient(app)

    r = c.get(f"/api/ideas/{idea.id}/auto-run/log").json()
    assert r["text"] == "hello\nworld\n" and r["next"] == 12 and r["running"] is False
    r2 = c.get(f"/api/ideas/{idea.id}/auto-run/log?from=6").json()  # tail only new bytes
    assert r2["text"] == "world\n" and r2["next"] == 12

    idea2 = store.add_idea("T2")  # no log yet → empty
    r3 = c.get(f"/api/ideas/{idea2.id}/auto-run/log").json()
    assert r3["text"] == "" and r3["next"] == 0 and r3["running"] is False


def test_auto_run_stop_route(tmp_path):
    from fastapi.testclient import TestClient
    from paperclaw.server.app import create_app
    from paperclaw.server.store import Store as _Store
    app = create_app(home=tmp_path)
    store = _Store(tmp_path)
    idea = store.add_idea("T")
    store.put_idea_auto_run(idea.id, {"topic": "t", "status": "running", "ideaId": idea.id})
    body = TestClient(app).post(f"/api/ideas/{idea.id}/auto-run/stop").json()
    assert body["ok"] is True
    assert store.get_idea_auto_run(idea.id)["status"] == "stopped"


def test_persist_auto_status_records_stopped_on_early_close(tmp_path):
    """Closing the stream early (Ctrl+C) records 'stopped' — so it can be resumed."""
    store = Store(tmp_path)
    idea = store.add_idea("T")
    st = {"topic": "t", "status": "running", "phase": "doctor", "ideaId": idea.id}

    async def inner():
        yield {"type": "phase", "phase": "doctor", "label": "checking"}
        yield {"type": "phase", "phase": "domain", "label": "never reached"}

    async def go():
        gen = service._persist_auto_status(store, st, inner())
        async for _ev in gen:
            break               # take one event…
        await gen.aclose()      # …then interrupt, like Ctrl+C
    asyncio.run(go())
    assert store.get_idea_auto_run(idea.id)["status"] == "stopped"


def test_auto_create_idea_pins_domain(tmp_path, monkeypatch):
    from paperclaw import llm
    store = Store(tmp_path)
    domain = store.add_domain("Generative Modeling", spec="# Generative Modeling\n\n## General Target\nx\n")

    async def fake_chat(settings, system, messages, max_tokens=2048):
        return SimpleNamespace(text=(
            "```idea-draft\n# Better Diffusion\n\n## Domain & Literature (domain pin)\n"
            "Generative Modeling — builds on Ho 2020.\n\n## Root Hypotheses\nMethod beats baseline.\n```"
        ), model="x")
    monkeypatch.setattr(llm, "chat", fake_chat)

    idea = asyncio.run(service.auto_create_idea(store, LLMSettings(), domain))
    spec = store.get_spec(idea.id)
    from paperclaw.research_pipeline import domain_is_pinned
    assert domain_is_pinned(spec)                 # idea is pinned to a real domain
    assert "Generative Modeling" in spec
    assert idea.title == "Better Diffusion"


def test_domain_is_pinned_tolerates_incidental_tbd():
    """A real, substantive pin that merely mentions '(authors/year TBD)' about one paper
    is still PINNED — only a bare short placeholder counts as not pinned."""
    from paperclaw.research_pipeline import domain_is_pinned

    real = ("# Idea\n\n## Domain & Literature (domain pin)\n"
            "Diffusion Models for Probabilistic Time Series Forecasting; builds on "
            "TimeGrad (Rasul 2021) and NsDiff (authors/year TBD from DOMAIN.md) as a key baseline.\n")
    assert domain_is_pinned(real)  # incidental 'TBD' no longer trips the check

    for placeholder in ("TBD", "_TBD_", "N/A", "Not pinned yet", "TBD — pin a domain"):
        spec = f"# Idea\n\n## Domain & Literature (domain pin)\n{placeholder}\n"
        assert not domain_is_pinned(spec), placeholder

    # empty / template-comment-only body → not pinned
    assert not domain_is_pinned("# Idea\n\n## Domain & Literature\n<!-- fill me -->\n")
