import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from paperclaw import llm
from paperclaw.llm import ChatResult, _error_snippet
from paperclaw.server.app import create_app


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    """Keep tests hermetic: no ambient API keys or .env files leak in."""
    for var in ("PAPERCLAW_PROVIDER", "PAPERCLAW_BASE_URL", "PAPERCLAW_MODEL", "PAPERCLAW_API_KEY",
                "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)


def make_client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(home=tmp_path))


def test_health(tmp_path):
    client = make_client(tmp_path)
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_seed_crud(tmp_path):
    client = make_client(tmp_path)
    r = client.post("/api/brainstorm", json={"text": "diffusion for irregular time series"})
    assert r.status_code == 201
    seed_id = r.json()["id"]

    assert len(client.get("/api/brainstorm").json()) == 1

    # persists across app restarts (re-read from disk)
    client2 = make_client(tmp_path)
    assert len(client2.get("/api/brainstorm").json()) == 1

    assert client.delete(f"/api/brainstorm/{seed_id}").status_code == 204
    assert client.get("/api/brainstorm").json() == []


def test_idea_lifecycle_and_spec(tmp_path):
    client = make_client(tmp_path)
    r = client.post("/api/ideas", json={"title": "Neural ODE Forecasting"})
    assert r.status_code == 201
    idea_id = r.json()["id"]

    r = client.put(f"/api/ideas/{idea_id}/activate")
    assert r.json()["isActive"] is True

    # IDEA.md created from template
    r = client.get(f"/api/ideas/{idea_id}/spec")
    assert r.status_code == 200
    assert "# Neural ODE Forecasting" in r.json()["content"]
    assert "Domain & Literature" in r.json()["content"]

    # spec is editable
    new_content = "# Neural ODE Forecasting\n\n## Domain & Literature\nML, time series.\n"
    r = client.put(f"/api/ideas/{idea_id}/spec", json={"content": new_content})
    assert r.status_code == 200
    assert client.get(f"/api/ideas/{idea_id}/spec").json()["content"] == new_content

    # spec file exists on disk
    assert (tmp_path / "ideas" / idea_id / "IDEA.md").read_text() == new_content

    assert client.delete(f"/api/ideas/{idea_id}").status_code == 204
    assert client.get("/api/ideas").json() == []


def test_duplicate_idea(tmp_path):
    client = make_client(tmp_path)
    idea_id = client.post("/api/ideas", json={"title": "Diffusion Forecasting"}).json()["id"]
    client.put(f"/api/ideas/{idea_id}/spec",
               json={"content": "# Diffusion Forecasting\n\n## Research Gap\nthe gap.\n"})
    (tmp_path / "ideas" / idea_id / "ref.bib").write_text("@article{x2024, title={X}}\n")

    r = client.post(f"/api/ideas/{idea_id}/duplicate")
    assert r.status_code == 201
    copy = r.json()
    assert copy["id"] != idea_id
    assert copy["title"] == "Diffusion Forecasting (copy)"
    assert copy["isActive"] is False  # fork doesn't steal active status

    # spec + ref.bib copied into the new workspace
    assert "## Research Gap\nthe gap." in client.get(f"/api/ideas/{copy['id']}/spec").json()["content"]
    assert (tmp_path / "ideas" / copy["id"] / "ref.bib").read_text().startswith("@article{x2024")

    # both ideas now exist; the original is untouched
    ids = {i["id"] for i in client.get("/api/ideas").json()}
    assert ids == {idea_id, copy["id"]}

    assert client.post("/api/ideas/nonexistent/duplicate").status_code == 404


def test_chat_without_api_key_returns_config_hint(tmp_path):
    client = make_client(tmp_path)
    r = client.post("/api/ideas", json={"title": "Test idea"})
    idea_id = r.json()["id"]

    r = client.post("/api/chat/send", json={"ideaId": idea_id, "content": "hello"})
    assert r.status_code == 200
    msgs = r.json()
    assert msgs[0]["role"] == "user"
    assert msgs[1]["role"] == "assistant"
    assert "Settings" in msgs[1]["content"]  # LLMNotConfigured hint

    assert len(client.get(f"/api/chat/{idea_id}/messages").json()) == 2


def test_skills_listing(tmp_path):
    client = make_client(tmp_path)
    skills = client.get("/api/skills").json()
    assert any(s["command"] == "/idea_generation" for s in skills)
    assert all(s["description"] for s in skills)
    # the hypothesis step-generation + reference skills are registered
    cmds = {s["command"] for s in skills}
    assert {"/generate_plan", "/generate_report", "/validate_references"} <= cmds
    # idea-only skills are flagged so the UI can hide them when no idea is active
    by_cmd = {s["command"]: s for s in skills}
    assert by_cmd["/write_paper"]["requiresIdea"] is True
    assert by_cmd["/create_domain"].get("requiresIdea") in (False, None)


def test_generate_plan_report_directives():
    """The plan/report directives + post-process regex use the convention paths so
    the frontend tabs pick them up (no freeform testing_plan.json)."""
    import re
    from paperclaw.prompts.ideas import GENERATE_PLAN_DIRECTIVE, GENERATE_REPORT_DIRECTIVE
    assert "hypotheses/{hid}/plan.md" in GENERATE_PLAN_DIRECTIVE
    assert "testing_plan.json" in GENERATE_PLAN_DIRECTIVE  # explicitly forbidden
    assert "hypotheses/{hid}/report.md" in GENERATE_REPORT_DIRECTIVE
    assert "Proposed Hypotheses" in GENERATE_REPORT_DIRECTIVE
    # the refresh regex matches a written plan/report path
    pat = re.compile(r"hypotheses/[^/]+/(plan|report)\.md")
    assert pat.fullmatch("hypotheses/H1.2/plan.md") and pat.fullmatch("hypotheses/H3/report.md")


def test_reveal_idea_returns_path(tmp_path):
    client = make_client(tmp_path)
    idea_id = client.post("/api/ideas", json={"title": "Reveal me"}).json()["id"]

    r = client.post(f"/api/ideas/{idea_id}/reveal")
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == str(tmp_path / "ideas" / idea_id)

    assert client.post("/api/ideas/nonexistent/reveal").status_code == 404


def test_domain_crud_and_spec(tmp_path):
    client = make_client(tmp_path)
    r = client.post("/api/domains", json={"name": "Time Series Forecasting"})
    assert r.status_code == 201
    dom = r.json()
    assert dom["isSelected"] is True  # selected by default

    spec = client.get(f"/api/domains/{dom['id']}/spec").json()["content"]
    assert "# Time Series Forecasting" in spec
    assert "General Target" in spec and "Crucial Papers" in spec
    assert "Crucial Datasets / Benchmarks" in spec and "Crucial GitHub Libraries" in spec
    assert "Submission Venues" in spec
    assert "Conferences" in spec and "Journals / Transactions" in spec
    assert "| Venue | Full Name | Tier | Deadline |" in spec  # table format

    r = client.put(f"/api/domains/{dom['id']}/select", json={"selected": False})
    assert r.json()["isSelected"] is False

    assert client.delete(f"/api/domains/{dom['id']}").status_code == 204
    assert client.get("/api/domains").json() == []


def test_domain_auto_mode(tmp_path, monkeypatch):
    spec = "# Sparse Attention\n\n## General Target\nEfficient long-context models.\n"

    async def fake_chat(settings, system, messages, max_tokens=4096):
        return ChatResult(text=f"```new-domain\n{spec}```", model="test-model")

    monkeypatch.setattr(llm, "chat", fake_chat)
    client = make_client(tmp_path)
    client.put("/api/settings", json={"apiKey": "test-key"})

    r = client.post("/api/domains/auto", json={"prompt": "sparse attention"})
    assert r.status_code == 201
    assert r.json()["name"] == "Sparse Attention"
    assert client.get(f"/api/domains/{r.json()['id']}/spec").json()["content"] == spec


def test_domain_suggestions_generated_and_cached(tmp_path, monkeypatch):
    calls = {"n": 0}

    async def fake_chat(settings, system, messages, max_tokens=4096):
        calls["n"] += 1
        return ChatResult(text="Compare PatchTST vs iTransformer on ETT\nProbe distribution shift\n", model="m")

    monkeypatch.setattr(llm, "chat", fake_chat)
    client = make_client(tmp_path)
    client.put("/api/settings", json={"apiKey": "test-key"})
    domain_id = client.post("/api/domains", json={"name": "TS"}).json()["id"]

    s1 = client.get(f"/api/domains/{domain_id}/suggestions").json()
    assert s1[0] == "Compare PatchTST vs iTransformer on ETT"
    s2 = client.get(f"/api/domains/{domain_id}/suggestions").json()
    assert s2 == s1
    assert calls["n"] == 1  # second hit served from cache

    # editing the spec invalidates the cache
    client.put(f"/api/domains/{domain_id}/spec", json={"content": "# TS\nnew"})
    client.get(f"/api/domains/{domain_id}/suggestions")
    assert calls["n"] == 2


def test_domain_suggestions_fallback_without_key(tmp_path):
    client = make_client(tmp_path)
    domain_id = client.post("/api/domains", json={"name": "Neural ODEs"}).json()["id"]
    s = client.get(f"/api/domains/{domain_id}/suggestions").json()
    assert len(s) == 4
    assert any("Neural ODEs" in x for x in s)


def test_domain_grounded_brainstorm_creates_drafts(tmp_path, monkeypatch):
    draft = (
        "# Diffusion Heads for Probabilistic Forecasts\n\n"
        "## Domain & Literature (domain pin)\nTS forecasting.\n\n"
        "## Background\nKnown.\n\n## Motivation\nGap.\n\n"
        "## Possible Findings & Verification\nTest on ETT.\n\n"
        "## Current Findings\n_None yet._\n\n## Open Questions\nScaling.\n"
    )

    async def fake_chat(settings, system, messages, max_tokens=4096):
        assert "Domain specs follow" in system  # domain-grounded path used
        return ChatResult(text=f"```idea-draft\n{draft}```", model="test-model")

    monkeypatch.setattr(llm, "chat", fake_chat)
    client = make_client(tmp_path)
    client.put("/api/settings", json={"apiKey": "test-key"})
    client.post("/api/domains", json={"name": "TS Forecasting"})

    r = client.post("/api/brainstorm/generate", json={})
    assert r.status_code == 200
    seeds = r.json()
    assert len(seeds) == 1
    assert seeds[0]["text"] == "Diffusion Heads for Probabilistic Forecasts"
    assert seeds[0]["draft"].startswith("# Diffusion Heads")


def test_existing_idea_labels_includes_pinned_ideas_and_fuzzy_ranks(tmp_path):
    """Brainstorm dedup pool now includes PINNED IDEAS (not just seeds — that was the
    cause of regenerated duplicates) and is fuzzy-ranked by relevance to the query."""
    from paperclaw import service
    from paperclaw.server.store import Store

    store = Store(tmp_path)
    store.add_idea("Diffusion models for time series forecasting")
    store.add_idea("Graph neural networks for traffic")
    store.add_seed("Transformer time series anomaly detection")

    labels = service._existing_idea_labels(store, "time series diffusion", limit=20)
    assert "Diffusion models for time series forecasting" in labels      # pinned idea included
    assert "Transformer time series anomaly detection" in labels          # seed included
    assert labels[0] == "Diffusion models for time series forecasting"    # most relevant first
    assert len(service._existing_idea_labels(store, "anything", limit=1)) == 1  # cap respected


def test_brainstorm_stream_emits_thinking_and_drafts(tmp_path, monkeypatch):
    """The /generate-stream SSE endpoint streams the model live — thinking + the
    drafts as they're written — instead of only a status then the final list."""
    draft = "# Streamed Idea\n\n## Background\nx\n\n## Motivation\ny\n"

    async def fake_stream(settings, system, messages, max_tokens=4096):
        assert "Domain specs follow" in system  # domain-grounded draft path
        yield {"type": "thinking", "text": "weighing a few directions…"}
        yield {"type": "text", "text": f"```idea-draft\n{draft}"}
        yield {"type": "text", "text": "```"}

    monkeypatch.setattr(llm, "stream_chat_thinking", fake_stream)
    client = make_client(tmp_path)
    client.put("/api/settings", json={"apiKey": "test-key"})
    client.post("/api/domains", json={"name": "TS Forecasting"})

    r = client.post("/api/brainstorm/generate-stream", json={})
    assert r.status_code == 200
    events = _sse_events(r.text)
    assert any(e["type"] == "thinking" for e in events)        # reasoning surfaced
    assert any(e["type"] == "delta" for e in events)           # drafts streamed live
    done = next(e for e in events if e["type"] == "done")
    assert len(done["results"]) == 1
    assert done["results"][0]["text"] == "Streamed Idea"


def test_pin_idea_moves_draft_and_chat(tmp_path, monkeypatch):
    async def fake_chat(settings, system, messages, max_tokens=4096):
        return ChatResult(text="Looks solid — consider /pin_idea.", model="test-model")

    monkeypatch.setattr(llm, "chat", fake_chat)
    client = make_client(tmp_path)
    client.put("/api/settings", json={"apiKey": "test-key"})

    # create a draft seed directly via the store-backed API path
    from paperclaw.server.store import Store
    store = Store(tmp_path)
    seed = store.add_seed("Draft idea", draft="# Draft idea\n\n## Background\nx\n")

    # talk in the seed conversation, then pin
    r = client.post("/api/chat/send", json={"seedId": seed.id, "content": "thoughts?"})
    assert r.status_code == 200

    r = client.post("/api/chat/send", json={"seedId": seed.id, "content": "/pin_idea"})
    assert r.status_code == 200
    reply = r.json()[1]
    assert reply["createdIdeaId"]

    ideas = client.get("/api/ideas").json()
    assert len(ideas) == 1 and ideas[0]["title"] == "Draft idea"
    # spec carried over from the draft
    spec = client.get(f"/api/ideas/{ideas[0]['id']}/spec").json()["content"]
    assert spec.startswith("# Draft idea")
    # conversation moved to the idea
    msgs = client.get(f"/api/chat/{ideas[0]['id']}/messages").json()
    assert any(m["content"] == "thoughts?" for m in msgs)
    # seed gone
    assert client.get("/api/brainstorm").json() == []


def test_question_block_parsed(tmp_path, monkeypatch):
    async def fake_chat(settings, system, messages, max_tokens=4096):
        return ChatResult(
            text='Which scope?\n```question\n{"prompt": "Which scope?", "options": ["Broad", "Specific"], "allowFreeText": true}\n```',
            model="test-model",
        )

    monkeypatch.setattr(llm, "chat", fake_chat)
    client = make_client(tmp_path)
    client.put("/api/settings", json={"apiKey": "test-key"})

    r = client.post("/api/chat/send", json={"content": "/create_domain"})
    reply = r.json()[1]
    assert reply["question"]["prompt"] == "Which scope?"
    assert reply["question"]["options"] == ["Broad", "Specific"]
    assert "```question" not in reply["content"]


def test_wizard_new_domain_block_creates_domain(tmp_path, monkeypatch):
    spec = "# Neural ODEs\n\n## General Target\nContinuous-time models.\n"

    async def fake_chat(settings, system, messages, max_tokens=4096):
        return ChatResult(text=f"Done!\n```new-domain\n{spec}```", model="test-model")

    monkeypatch.setattr(llm, "chat", fake_chat)
    client = make_client(tmp_path)
    client.put("/api/settings", json={"apiKey": "test-key"})

    r = client.post("/api/chat/send", json={"content": "that is everything"})
    reply = r.json()[1]
    assert reply["createdDomainId"]
    assert "Domain created: Neural ODEs" in reply["content"]
    assert client.get("/api/domains").json()[0]["name"] == "Neural ODEs"


def test_wizard_history_moves_to_domain_chat(tmp_path, monkeypatch):
    spec = "# Neural ODEs\n\n## General Target\nContinuous-time models.\n"

    async def fake_chat(settings, system, messages, max_tokens=4096):
        return ChatResult(text=f"Done!\n```new-domain\n{spec}```", model="test-model")

    monkeypatch.setattr(llm, "chat", fake_chat)
    client = make_client(tmp_path)
    client.put("/api/settings", json={"apiKey": "test-key"})

    r = client.post("/api/chat/send", json={"content": "/create_domain neural odes"})
    domain_id = r.json()[1]["createdDomainId"]

    # conversation moved: domain chat has it, scratch is empty
    msgs = client.get(f"/api/chat/domain-{domain_id}/messages").json()
    assert any("/create_domain" in m["content"] for m in msgs)
    assert client.get("/api/chat/_scratch/messages").json() == []


def test_domain_chat_updates_spec(tmp_path, monkeypatch):
    new_spec_text = "# TS Forecasting\n\n## General Target\nBetter probabilistic forecasts.\n"

    # A domain conversation has a workspace dir, so chat routes through the
    # tool-call loop (chat_with_tools), not plain chat.
    async def fake_chat_with_tools(settings, system, messages, tools, executor,
                                   max_tokens=4096, max_rounds=8):
        assert "DOMAIN.md follows" in system  # domain conversation system prompt
        return ChatResult(text=f"Updated!\n```domain.md\n{new_spec_text}```", model="test-model")

    monkeypatch.setattr(llm, "chat_with_tools", fake_chat_with_tools)
    client = make_client(tmp_path)
    client.put("/api/settings", json={"apiKey": "test-key"})
    domain_id = client.post("/api/domains", json={"name": "TS Forecasting"}).json()["id"]

    r = client.post("/api/chat/send", json={"domainId": domain_id, "content": "improve the target"})
    reply = r.json()[1]
    assert reply["specUpdated"] is True
    assert "DOMAIN.md updated" in reply["content"]
    assert client.get(f"/api/domains/{domain_id}/spec").json()["content"] == new_spec_text


def _sse_events(text: str) -> list[dict]:
    """Parse an SSE body into a list of JSON event dicts."""
    out = []
    for line in text.splitlines():
        if line.startswith("data: "):
            out.append(json.loads(line[len("data: "):]))
    return out


def test_stream_chat_streams_tool_deltas(tmp_path, monkeypatch):
    """The SSE endpoint streams text chunk-by-chunk in a tool (domain) context,
    and the terminal 'final' event drives spec persistence."""
    new_spec_text = "# TS Forecasting\n\n## General Target\nStreamed update.\n"

    async def fake_stream(settings, system, messages, tools, executor,
                          max_tokens=4096, max_rounds=8):
        assert "DOMAIN.md follows" in system
        yield {"type": "delta", "text": "Updat"}
        yield {"type": "delta", "text": "ing the target…"}
        yield {"type": "final",
               "text": f"Updated!\n```domain.md\n{new_spec_text}```",
               "paths": []}

    monkeypatch.setenv("PAPERCLAW_CHAT_AGENT", "builtin")  # exercise the kept built-in tool loop
    monkeypatch.setattr(llm, "stream_chat_with_tools", fake_stream)
    client = make_client(tmp_path)
    client.put("/api/settings", json={"apiKey": "test-key"})
    domain_id = client.post("/api/domains", json={"name": "TS Forecasting"}).json()["id"]

    r = client.post("/api/chat/stream", json={"domainId": domain_id, "content": "improve the target"})
    assert r.status_code == 200
    events = _sse_events(r.text)

    deltas = [e["text"] for e in events if e["type"] == "delta"]
    assert deltas == ["Updat", "ing the target…"]  # separate chunks, not one blob
    assert len(deltas) > 1  # the whole point: progressive streaming

    done = next(e for e in events if e["type"] == "done")
    reply = done["messages"][1]
    assert reply["specUpdated"] is True
    assert "DOMAIN.md updated" in reply["content"]
    assert client.get(f"/api/domains/{domain_id}/spec").json()["content"] == new_spec_text


def test_generate_hypothesis_map_via_chat_skill(tmp_path, monkeypatch):
    """/generate_hypothesis_map runs in the chat: the agent writes .hypothesis_map.json,
    the reply flags mapUpdated, and the map is normalized (hierarchical ids, roots)."""
    monkeypatch.setenv("PAPERCLAW_CHAT_AGENT", "builtin")  # exercise without the deepagents dep
    client = make_client(tmp_path)
    client.put("/api/settings", json={"apiKey": "test-key"})
    iid = client.post("/api/ideas", json={"title": "TS"}).json()["id"]

    raw = ('{"nodes":[{"statement":"Adaptive schedules help at late horizons","rationale":"core"},'
           '{"statement":"Per-variable routing beats shared","rationale":"core"}]}')

    async def fake_stream(settings, system, messages, tools, executor, max_tokens=4096, max_rounds=8):
        assert any("hypothesis_map.json" in m["content"] for m in messages)  # directive injected
        (tmp_path / "ideas" / iid / ".hypothesis_map.json").write_text(raw)   # agent writes the file
        yield {"type": "delta", "text": "Wrote 2 root hypotheses."}
        yield {"type": "final", "text": "Wrote 2 root hypotheses.", "paths": [".hypothesis_map.json"]}

    monkeypatch.setattr(llm, "stream_chat_with_tools", fake_stream)

    r = client.post("/api/chat/stream", json={"ideaId": iid, "content": "/generate_hypothesis_map"})
    assert r.status_code == 200
    done = next(e for e in _sse_events(r.text) if e["type"] == "done")
    assert done["messages"][1]["mapUpdated"] is True

    nodes = client.get(f"/api/ideas/{iid}/hypothesis-map").json()["nodes"]
    assert [n["id"] for n in nodes] == ["H1", "H2"]       # normalized hierarchical ids
    assert nodes[0]["children"] == [] and nodes[0]["status"] == "untested"  # roots only


def test_write_paper_via_chat_skill(tmp_path, monkeypatch):
    """/write_paper H1 H3: the agent writes paper.tex and compiles paper.pdf; the
    reply flags paperUpdated and the Paper tab serves the compiled PDF."""
    monkeypatch.setenv("PAPERCLAW_CHAT_AGENT", "builtin")
    client = make_client(tmp_path)
    client.put("/api/settings", json={"apiKey": "test-key"})
    iid = client.post("/api/ideas", json={"title": "TS"}).json()["id"]

    async def fake_stream(settings, system, messages, tools, executor, max_tokens=4096, max_rounds=8):
        joined = system + " " + " ".join(m["content"] for m in messages)  # directive is system-level
        assert "paper.tex" in joined and "compile_latex" in joined and "H1 H3" in joined
        assert "EVIDENCE-BOUNDING" in joined  # shared rigor rules injected into the system prompt
        base = tmp_path / "ideas" / iid
        (base / "paper.tex").write_text(r"\documentclass{article}\begin{document}X\end{document}")
        (base / "paper.pdf").write_bytes(b"%PDF-1.4 fake")  # simulate compile output
        yield {"type": "final", "text": "Compiled paper.pdf (8 pages).",
               "paths": ["paper.tex", "paper.pdf"]}

    monkeypatch.setattr(llm, "stream_chat_with_tools", fake_stream)

    r = client.post("/api/chat/stream", json={"ideaId": iid, "content": "/write_paper H1 H3"})
    assert r.status_code == 200
    assert next(e for e in _sse_events(r.text) if e["type"] == "done")["messages"][1]["paperUpdated"] is True
    pc = client.get(f"/api/ideas/{iid}/paper-content").json()
    assert pc["hasPdf"] is True and pc["paperFile"] == "paper.pdf" and pc["versionCount"] == 1


def test_write_paper_versions(tmp_path, monkeypatch):
    """When a paper already exists, /write_paper targets the NEXT version
    (paper_v2.tex/.pdf) and the Paper tab serves the latest PDF; earlier preserved."""
    monkeypatch.setenv("PAPERCLAW_CHAT_AGENT", "builtin")
    client = make_client(tmp_path)
    client.put("/api/settings", json={"apiKey": "test-key"})
    iid = client.post("/api/ideas", json={"title": "TS"}).json()["id"]
    base = tmp_path / "ideas" / iid
    (base / "paper.pdf").write_bytes(b"%PDF v1")  # a compiled paper already exists

    seen = {}

    async def fake_stream(settings, system, messages, tools, executor, max_tokens=4096, max_rounds=8):
        seen["v2"] = "paper_v2.tex" in (system + " " + " ".join(m["content"] for m in messages))  # directive target (system-level)
        (base / "paper_v2.tex").write_text(r"\documentclass{article}\begin{document}v2\end{document}")
        (base / "paper_v2.pdf").write_bytes(b"%PDF v2")
        yield {"type": "final", "text": "Wrote v2.", "paths": ["paper_v2.tex", "paper_v2.pdf"]}

    monkeypatch.setattr(llm, "stream_chat_with_tools", fake_stream)

    r = client.post("/api/chat/stream", json={"ideaId": iid, "content": "/write_paper H1"})
    assert next(e for e in _sse_events(r.text) if e["type"] == "done")["messages"][1]["paperUpdated"] is True
    assert seen["v2"] is True                          # paper_v2.tex was the write target
    pc = client.get(f"/api/ideas/{iid}/paper-content").json()
    assert pc["paperFile"] == "paper_v2.pdf" and pc["versionCount"] == 2  # latest served
    assert (base / "paper.pdf").read_bytes() == b"%PDF v1"  # v1 preserved


def test_paper_pdf_served_inline_for_iframe(tmp_path):
    """The Paper-tab iframe needs an INLINE PDF (not attachment) to render it."""
    client = make_client(tmp_path)
    iid = client.post("/api/ideas", json={"title": "T"}).json()["id"]
    (tmp_path / "ideas" / iid / "paper.pdf").write_bytes(b"%PDF-1.4 fake")

    r = client.get(f"/api/ideas/{iid}/paper")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"].startswith("inline")          # renders in the iframe
    r2 = client.get(f"/api/ideas/{iid}/paper", params={"download": "1"})
    assert r2.headers["content-disposition"].startswith("attachment")      # download button


def test_paper_version_selection(tmp_path):
    """The Paper tab can list and view any version (latest by default, or ?version=N)."""
    client = make_client(tmp_path)
    iid = client.post("/api/ideas", json={"title": "T"}).json()["id"]
    base = tmp_path / "ideas" / iid
    base.joinpath("paper.md").write_text("# v1 markdown\n")        # version 1 (markdown)
    base.joinpath("paper_v2.pdf").write_bytes(b"%PDF-1.4 v2")      # version 2 (compiled)

    pc = client.get(f"/api/ideas/{iid}/paper-content").json()      # latest
    assert pc["versions"] == [1, 2] and pc["hasPdf"] is True and pc["paperFile"] == "paper_v2.pdf"

    pc1 = client.get(f"/api/ideas/{iid}/paper-content", params={"version": 1}).json()
    assert pc1["hasPdf"] is False and "v1 markdown" in pc1["content"] and pc1["paperFile"] == "paper.md"

    assert client.get(f"/api/ideas/{iid}/paper", params={"version": 2}).headers["content-type"] == "application/pdf"
    assert "v1 markdown" in client.get(f"/api/ideas/{iid}/paper", params={"version": 1}).text


def test_paper_latest_viewable_when_newest_is_tex_only(tmp_path):
    """If the newest version is .tex-only (a failed compile), the Paper tab still
    shows the latest VIEWABLE version (a PDF) — so the Paper button never vanishes —
    and the .tex source is viewable by selecting that version."""
    client = make_client(tmp_path)
    iid = client.post("/api/ideas", json={"title": "T"}).json()["id"]
    base = tmp_path / "ideas" / iid
    base.joinpath("paper.md").write_text("# v1\n")
    base.joinpath("paper_v2.pdf").write_bytes(b"%PDF-1.4 v2")
    base.joinpath("paper_v3.tex").write_text(r"\documentclass{article}\begin{document}v3\end{document}")

    pc = client.get(f"/api/ideas/{iid}/paper-content").json()         # default
    assert pc["versions"] == [1, 2, 3]
    assert pc["hasPdf"] is True and pc["paperFile"] == "paper_v2.pdf"  # NOT the tex-only v3

    pc3 = client.get(f"/api/ideas/{iid}/paper-content", params={"version": 3}).json()
    assert pc3["hasPdf"] is False and pc3["paperFile"] == "paper_v3.tex"
    assert "documentclass" in pc3["content"]                          # latex source shown


def test_setup_venue_via_chat_skill(tmp_path, monkeypatch):
    """/setup_venue NeurIPS 2025 runs in the chat: the agent writes venue/STYLE.md
    (browsable via the workspace files route)."""
    monkeypatch.setenv("PAPERCLAW_CHAT_AGENT", "builtin")
    client = make_client(tmp_path)
    client.put("/api/settings", json={"apiKey": "test-key"})
    iid = client.post("/api/ideas", json={"title": "TS"}).json()["id"]

    async def fake_stream(settings, system, messages, tools, executor, max_tokens=4096, max_rounds=8):
        joined = " ".join(m["content"] for m in messages)
        assert "STYLE.md" in joined and "NeurIPS 2025" in joined  # directive + venue arg
        vdir = tmp_path / "ideas" / iid / "venue"
        vdir.mkdir(parents=True, exist_ok=True)
        (vdir / "STYLE.md").write_text("# NeurIPS 2025\nPage limit: 9 pages.\n")
        yield {"type": "delta", "text": "Set up the NeurIPS template."}
        yield {"type": "final", "text": "Set up the NeurIPS template.", "paths": ["venue/STYLE.md"]}

    monkeypatch.setattr(llm, "stream_chat_with_tools", fake_stream)

    r = client.post("/api/chat/stream", json={"ideaId": iid, "content": "/setup_venue NeurIPS 2025"})
    assert r.status_code == 200
    assert next(e for e in _sse_events(r.text) if e["type"] == "done")
    files = client.get(f"/api/ideas/{iid}/files").json()["entries"]
    assert any(e["path"] == "venue/STYLE.md" for e in files)


def test_upload_venue_template(tmp_path):
    """A LaTeX template uploaded into venue/ (single file or zip) makes the pipeline
    see a usable venue skeleton (so the auto paper is based on it)."""
    import base64
    import io
    import zipfile
    from paperclaw import iterative_pipeline as ip
    client = make_client(tmp_path)
    iid = client.post("/api/ideas", json={"title": "TS"}).json()["id"]

    # single .sty file
    b64 = base64.b64encode(b"%% fake style\n").decode()
    r = client.post(f"/api/ideas/{iid}/venue/upload",
                    json={"filename": "aaai2026.sty", "contentBase64": b64})
    assert r.status_code == 200 and "aaai2026.sty" in r.json()["files"]
    assert (tmp_path / "ideas" / iid / "venue" / "aaai2026.sty").exists()

    # a zip with a skeleton .tex + class file → extracted
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("template.tex", "\\documentclass{aaai2026}\n\\begin{document}\\end{document}\n")
        zf.writestr("aaai2026.cls", "%% cls\n")
    zb64 = base64.b64encode(buf.getvalue()).decode()
    r2 = client.post(f"/api/ideas/{iid}/venue/upload",
                     json={"filename": "tmpl.zip", "contentBase64": zb64})
    assert r2.status_code == 200
    assert {"template.tex", "aaai2026.cls"} <= set(r2.json()["files"])
    assert ip._venue_skeleton(tmp_path / "ideas" / iid) is not None  # usable skeleton now

    # unknown idea → 404
    assert client.post("/api/ideas/nope/venue/upload",
                       json={"filename": "x.sty", "contentBase64": b64}).status_code == 404


def test_stream_chat_with_tools_openai_fallback(monkeypatch):
    """Non-Anthropic providers fall back to the non-streaming loop, surfaced as a
    single delta plus the terminal final event (text + apply_patch paths)."""
    import asyncio

    from paperclaw.config import LLMSettings

    async def fake_openai(*args, **kwargs):
        return ChatResult(text="done", model="m", files_modified=frozenset({"IDEA.md"}))

    monkeypatch.setattr(llm, "_chat_with_tools_openai", fake_openai)
    settings = LLMSettings(provider="openai", api_key="k", model="m", base_url="http://x/v1")

    async def collect():
        return [ev async for ev in llm.stream_chat_with_tools(
            settings, "sys", [{"role": "user", "content": "x"}],
            tools=[], executor=lambda n, i: "",
        )]

    events = asyncio.run(collect())
    assert {"type": "delta", "text": "done"} in events
    final = next(e for e in events if e["type"] == "final")
    assert final["text"] == "done"
    assert final["paths"] == ["IDEA.md"]


def test_contexts_listing(tmp_path, monkeypatch):
    async def fake_chat(settings, system, messages, max_tokens=4096):
        return ChatResult(text="ok", model="test-model")

    monkeypatch.setattr(llm, "chat", fake_chat)
    client = make_client(tmp_path)
    client.put("/api/settings", json={"apiKey": "test-key"})

    client.post("/api/chat/send", json={"content": "hello scratch"})
    domain_id = client.post("/api/domains", json={"name": "Dom"}).json()["id"]
    client.post("/api/chat/send", json={"domainId": domain_id, "content": "hello domain"})
    idea_id = client.post("/api/ideas", json={"title": "Idea A"}).json()["id"]
    client.post("/api/chat/send", json={"ideaId": idea_id, "content": "hello idea"})

    contexts = client.get("/api/chat/contexts").json()
    kinds = {c["kind"]: c for c in contexts}
    assert set(kinds) == {"scratch", "domain", "idea"}
    assert kinds["domain"]["title"] == "Dom"
    assert kinds["idea"]["title"] == "Idea A"
    assert all(c["messageCount"] >= 2 for c in contexts)
    # sorted by recency
    times = [c["lastTimestamp"] for c in contexts]
    assert times == sorted(times, reverse=True)


def test_question_parser_tolerates_sloppy_json(tmp_path, monkeypatch):
    sloppy = "Which?\n``` Question\n{'prompt': 'Which?', 'options': ['A', 'B',], 'allowFreeText': true}\n```"

    async def fake_chat(settings, system, messages, max_tokens=4096):
        return ChatResult(text=sloppy.replace("true", "True").replace("True", "true"), model="test-model")

    monkeypatch.setattr(llm, "chat", fake_chat)
    client = make_client(tmp_path)
    client.put("/api/settings", json={"apiKey": "test-key"})

    r = client.post("/api/chat/send", json={"content": "hi"})
    reply = r.json()[1]
    assert reply["question"] is not None
    assert reply["question"]["prompt"] == "Which?"
    assert reply["question"]["options"] == ["A", "B"]


def test_new_idea_block_creates_idea(tmp_path, monkeypatch):
    """LLM emits a ```new-idea block → idea + IDEA.md created, reply rewritten."""
    spec = "# Sparse Attention for Long Horizons\n\n## Domain & Literature\nML.\n"

    async def fake_chat(settings, system, messages, max_tokens=4096):
        return ChatResult(text=f"Here you go!\n```new-idea\n{spec}```", model="test-model")

    monkeypatch.setattr(llm, "chat", fake_chat)
    client = make_client(tmp_path)

    # settings need a key so the route reaches llm.chat (mock ignores it anyway)
    client.put("/api/settings", json={"apiKey": "test-key"})

    r = client.post("/api/chat/send", json={"content": "/idea_generation"})
    assert r.status_code == 200
    reply = r.json()[1]
    assert "Idea created: Sparse Attention for Long Horizons" in reply["content"]
    assert reply["createdIdeaId"]
    assert "```new-idea" not in reply["content"]

    ideas = client.get("/api/ideas").json()
    assert len(ideas) == 1
    assert ideas[0]["title"] == "Sparse Attention for Long Horizons"
    assert client.get(f"/api/ideas/{ideas[0]['id']}/spec").json()["content"] == spec


def test_chat_scratch_mode(tmp_path):
    client = make_client(tmp_path)
    r = client.post("/api/chat/send", json={"content": "I have a vague idea"})
    assert r.status_code == 200
    assert len(client.get("/api/chat/_scratch/messages").json()) == 2


def _fake_response(status: int, content: str, content_type: str = "text/plain") -> httpx.Response:
    return httpx.Response(
        status_code=status,
        content=content.encode(),
        headers={"content-type": content_type},
        request=httpx.Request("POST", "https://example.com/v1/chat/completions"),
    )


def test_error_snippet_hides_html_pages():
    resp = _fake_response(502, "<!DOCTYPE html><html><body>Cloudflare error</body></html>", "text/html")
    snippet = _error_snippet(resp)
    assert "<" not in snippet
    assert "transient" in snippet


def test_error_snippet_extracts_json_message():
    resp = _fake_response(400, '{"error": {"message": "model not found"}}', "application/json")
    assert _error_snippet(resp) == "model not found"


def test_error_snippet_plain_text_passthrough():
    assert _error_snippet(_fake_response(503, "upstream busy")) == "upstream busy"


def test_settings_roundtrip_masks_key(tmp_path):
    client = make_client(tmp_path)

    r = client.get("/api/settings")
    assert r.json()["provider"] == "anthropic"
    assert r.json()["hasKey"] is False

    r = client.put(
        "/api/settings",
        json={
            "provider": "openai",
            "baseUrl": "https://example.com/v1",
            "model": "some-model",
            "apiKey": "sk-secret-1234567890",
        },
    )
    body = r.json()
    assert body["provider"] == "openai"
    assert body["hasKey"] is True
    assert "secret" not in body["apiKeyMasked"]
    assert body["apiKeyMasked"].startswith("sk-s")

    # empty apiKey keeps the stored key
    r = client.put("/api/settings", json={"apiKey": ""})
    assert r.json()["hasKey"] is True

    # invalid provider rejected
    assert client.put("/api/settings", json={"provider": "bogus"}).status_code == 422


def test_settings_openalex_key_masked_and_applied(tmp_path):
    from paperclaw import literature
    client = make_client(tmp_path)

    assert client.get("/api/settings").json()["hasOpenalexKey"] is False

    body = client.put("/api/settings", json={
        "openalexApiKey": "oa-secret-abcdefgh",
    }).json()
    assert body["hasOpenalexKey"] is True
    assert "secret" not in body["openalexKeyMasked"]
    # the live literature client is reconfigured so requests carry the key
    assert literature._auth_params() == {"api_key": "oa-secret-abcdefgh"}

    # empty key keeps the stored one
    assert client.put("/api/settings", json={"openalexApiKey": ""}).json()["hasOpenalexKey"] is True
    literature.configure()  # reset module state so other tests aren't affected


def test_domain_codebase_route(tmp_path, monkeypatch):
    from paperclaw import codebase
    client = make_client(tmp_path)
    d = client.post("/api/domains", json={"name": "TS Diffusion"}).json()

    # mock the actual download so the route test needs no network
    def fake_dl(url, dest):
        from pathlib import Path
        Path(dest).mkdir(parents=True, exist_ok=True)
        (Path(dest) / "model.py").write_text("x")
        return {"url": url, "ref": "default", "fileCount": 3}
    monkeypatch.setattr(codebase, "download_codebase", fake_dl)

    r = client.post(f"/api/domains/{d['id']}/codebase",
                    json={"url": "https://github.com/o/r"})
    assert r.status_code == 200
    body = r.json()
    assert body["codebaseUrl"] == "https://github.com/o/r" and body["codebaseFiles"] == 3
    # it shows on the list too
    assert client.get("/api/domains").json()[0]["codebaseFiles"] == 3
    # clear it
    assert client.request("DELETE", f"/api/domains/{d['id']}/codebase").json()["codebaseFiles"] == 0
    # missing domain → 404
    assert client.post("/api/domains/nope/codebase",
                       json={"url": "https://github.com/o/r"}).status_code == 404


def test_writing_styles_route(tmp_path):
    client = make_client(tmp_path)
    styles = client.get("/api/writing-styles").json()
    names = {s["name"] for s in styles}
    assert "technical-concise" in names and all("title" in s for s in styles)
    # fetch one guide's content
    body = client.get("/api/writing-styles/narrative").json()
    assert body["content"].startswith("# ")
    assert client.get("/api/writing-styles/nope").status_code == 404
    # create one
    assert client.post("/api/writing-styles",
                       json={"name": "house", "content": "# House\nx"}).json()["name"] == "house"
    assert "house" in {s["name"] for s in client.get("/api/writing-styles").json()}


def test_experiments_monitor_route(tmp_path):
    import json as _json
    import time as _time
    client = make_client(tmp_path)
    iid = client.post("/api/ideas", json={"title": "Exp idea"}).json()["id"]
    # simulate a finished detached job on disk
    store = client.app.state.store
    hdir = store.idea_path(iid) / "hypotheses" / "H1"
    hdir.mkdir(parents=True)
    hdir.joinpath("job.json").write_text(_json.dumps(
        {"jobId": "j1", "pid": 999999, "status": "running", "startedAt": _time.time(),
         "updatedAt": _time.time(), "error": None}))
    jobs = client.get("/api/experiments").json()
    assert len(jobs) == 1
    j = jobs[0]
    assert j["hypothesisId"] == "H1" and j["ideaTitle"] == "Exp idea"
    assert j["status"] == "interrupted"  # PID 999999 isn't alive → reconciled
    # per-hypothesis job status route
    assert client.get(f"/api/ideas/{iid}/hypotheses/H1/experiment/job").json()["status"] == "interrupted"


def test_doctor_report(tmp_path):
    client = make_client(tmp_path)
    body = client.get("/api/doctor").json()
    assert "ok" in body and isinstance(body["checks"], list)
    keys = {c["key"] for c in body["checks"]}
    assert {"home", "llm", "chat_agent", "latex", "images"} <= keys
    # default home has no API key → llm check fails → overall not ok
    assert body["ok"] is False
    assert next(c for c in body["checks"] if c["key"] == "llm")["status"] == "fail"
