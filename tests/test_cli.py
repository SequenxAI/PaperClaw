"""CLI client tests — local mode (in-process) and env-based configuration."""

import pytest

from paperclaw import llm
from paperclaw.client import ClientError, LocalClient
from paperclaw.config import load_settings
from paperclaw.llm import ChatResult


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    for var in ("PAPERCLAW_PROVIDER", "PAPERCLAW_BASE_URL", "PAPERCLAW_MODEL", "PAPERCLAW_API_KEY",
                "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("PAPERCLAW_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)


def test_local_client_crud_without_llm(tmp_path):
    client = LocalClient()

    d = client.domain_create("TS Forecasting")
    assert client.domains_list()[0]["name"] == "TS Forecasting"
    assert "# TS Forecasting" in client.domain_spec(d["id"])
    client.domain_select(d["id"], False)
    assert client.domains_list()[0]["isSelected"] is False

    s = client.seed_add("a quick spark")
    assert client.seeds_list()[0]["text"] == "a quick spark"

    i = client.idea_create("My idea")
    spec = client.idea_spec(i["id"])
    assert "# My idea" in spec
    # new ideas carry the Main Result scaffold (baselines + main experiment)
    assert "## Main Result" in spec and "### Baselines" in spec and "### Main Experiment" in spec

    dup = client.idea_duplicate(i["id"])  # fork the idea
    assert dup["id"] != i["id"] and dup["title"] == "My idea (copy)"
    assert "# My idea" in client.idea_spec(dup["id"])
    client.idea_delete(dup["id"])

    client.seed_delete(s["id"])
    client.idea_delete(i["id"])
    client.domain_delete(d["id"])
    assert client.domains_list() == [] and client.ideas_list() == []

    with pytest.raises(ClientError):
        client.domain_spec("nope")


def test_local_client_idea_domains(tmp_path):
    """`paperclaw idea domains` connects an idea to one or more domains (CLI client path)."""
    client = LocalClient()
    d1 = client.domain_create("Time Series")
    d2 = client.domain_create("Diffusion")
    i = client.idea_create("My idea")
    assert client.idea_domains_get(i["id"]) == []                      # none connected yet
    assert set(client.idea_domains_set(i["id"], [d1["id"], d2["id"]])) == {d1["id"], d2["id"]}
    assert set(client.idea_domains_get(i["id"])) == {d1["id"], d2["id"]}
    assert client.idea_domains_set(i["id"], [d1["id"], "bogus"]) == [d1["id"]]   # unknown dropped
    assert client.idea_domains_set(i["id"], []) == []                  # disconnect all


def test_local_client_idea_color(tmp_path):
    """`paperclaw idea color` flags an idea (green/yellow/grey) and clears it; bad colors reject."""
    client = LocalClient()
    i = client.idea_create("My idea")
    assert client.idea_set_color(i["id"], "green")["color"] == "green"
    assert client.idea_set_color(i["id"], "")["color"] is None        # cleared
    with pytest.raises(ClientError):
        client.idea_set_color(i["id"], "purple")                      # invalid
    with pytest.raises(ClientError):
        client.idea_set_color("nope", "green")                        # missing idea


def test_local_client_idea_resources(tmp_path):
    """`paperclaw idea resources` allocates an idea's compute, and the allocation becomes the
    EFFECTIVE run config every experiment of that idea uses (manual + auto)."""
    client = LocalClient()
    st = client.store.get_hardware_state(); st["sshTargets"] = [{"id": "gpu1", "host": "h", "user": "u"}]
    client.store.save_hardware_state(st)
    i = client.idea_create("My idea")
    r = client.idea_resources_get(i["id"])
    assert "experimentMode" in r and r["llmKeyConfigured"] in (True, False)   # view shape
    client.idea_resources_set(i["id"], experiment_mode="ssh", ssh_target_id="gpu1")
    cfg = client.store.effective_run_config(i["id"])                          # what runs will use
    assert cfg.experiment_mode == "ssh" and cfg.ssh_target_id == "gpu1"
    # raw key is never surfaced in the view
    assert "apiKey" not in r and "api_key" not in r


def test_local_client_domain_codebase(tmp_path, monkeypatch):
    from paperclaw import codebase
    from pathlib import Path
    client = LocalClient()
    d = client.domain_create("TS Forecasting")

    def fake_dl(url, dest):
        Path(dest).mkdir(parents=True, exist_ok=True)
        (Path(dest) / "run.py").write_text("x")
        return {"url": url, "ref": "default", "fileCount": 2}
    monkeypatch.setattr(codebase, "download_codebase", fake_dl)

    out = client.domain_set_codebase(d["id"], "https://github.com/o/r")
    assert out["codebaseUrl"] == "https://github.com/o/r" and out["codebaseFiles"] == 2
    # the experiment runner resolves it (name-match fallback when no explicit connection)
    from paperclaw import iterative_pipeline as ip
    cb = ip._resolve_domain_codebase(client.store, None,
                                     "Domain & Literature: TS Forecasting is the field.")
    assert cb is not None and cb.name == "codebase"
    # clear
    assert client.domain_clear_codebase(d["id"])["codebaseFiles"] == 0


def test_local_client_writing_styles(tmp_path):
    client = LocalClient()
    names = {s["name"] for s in client.writing_styles_list()}
    assert {"technical-concise", "narrative", "formal-theoretical"} <= names
    assert client.writing_style_get("narrative")["content"].startswith("# ")
    out = client.writing_style_save("house-style", "# House\nterse")
    assert out["name"] == "house-style"
    assert "house-style" in {s["name"] for s in client.writing_styles_list()}
    with pytest.raises(ClientError):
        client.writing_style_get("does-not-exist")


def test_local_client_doctor(tmp_path, monkeypatch):
    """doctor reports structured checks; fails (no exception) when the LLM key is absent."""
    client = LocalClient()
    rep = client.doctor()
    keys = {c["key"] for c in rep["checks"]}
    assert {"home", "llm", "chat_agent", "latex", "images"} <= keys
    # No API key configured in this isolated home → llm check fails → overall not ok.
    llm_check = next(c for c in rep["checks"] if c["key"] == "llm")
    assert llm_check["status"] == "fail" and rep["ok"] is False
    # With a key set, the llm check passes.
    monkeypatch.setenv("PAPERCLAW_API_KEY", "sk-test")
    rep2 = LocalClient().doctor()
    assert next(c for c in rep2["checks"] if c["key"] == "llm")["status"] == "ok"


def test_local_client_workspace_files(tmp_path):
    client = LocalClient()
    i = client.idea_create("My idea")
    hdir = tmp_path / "ideas" / i["id"] / "hypotheses" / "H1"
    hdir.mkdir(parents=True)
    (hdir / "run.py").write_text("print('hi')\n", encoding="utf-8")
    (hdir / "results.json").write_text('{"summary":"ok"}', encoding="utf-8")

    listing = client.workspace_files(i["id"], "hypotheses/H1")
    paths = {e["path"] for e in listing["entries"]}
    assert "hypotheses/H1/run.py" in paths and "hypotheses/H1/results.json" in paths

    assert client.workspace_file(i["id"], "hypotheses/H1/run.py").decode() == "print('hi')\n"

    with pytest.raises(ClientError):
        client.workspace_file(i["id"], "../../etc/passwd")  # path-escape guarded


def test_local_client_chat_uses_service(monkeypatch):
    async def fake_chat(settings, system, messages, max_tokens=4096):
        return ChatResult(text="hello from cli", model="test-model")

    monkeypatch.setattr(llm, "chat", fake_chat)
    client = LocalClient()
    client.settings.api_key = "test-key"

    user, reply = client.chat_send("hi")
    assert user["role"] == "user"
    assert reply["content"] == "hello from cli"
    assert client.contexts()[0]["kind"] == "scratch"


def test_env_config_precedence(tmp_path, monkeypatch):
    # settings.json says anthropic; .env overrides; env var overrides .env
    (tmp_path / ".env").write_text("PAPERCLAW_MODEL=env-file-model\nPAPERCLAW_API_KEY=sk-envfile\n")
    settings = load_settings(tmp_path)
    assert settings.model == "env-file-model"
    assert settings.api_key == "sk-envfile"

    monkeypatch.setenv("PAPERCLAW_MODEL", "env-var-model")
    settings = load_settings(tmp_path)
    assert settings.model == "env-var-model"
    assert settings.api_key == "sk-envfile"  # .env still supplies the key


def test_provider_key_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fallback")
    settings = load_settings(tmp_path)
    assert settings.provider == "anthropic"
    assert settings.api_key == "sk-ant-fallback"


def test_home_settings_yaml_overrides_project_default(tmp_path, monkeypatch):
    """The persisted $PAPERCLAW_HOME/settings.yaml (written by the UI / `settings set`)
    OVERRIDES the project-dir ./settings.yaml (a no-command DEFAULT) — so a Settings-UI
    change actually takes effect on reload. The project file is the fallback when home
    has none. (Per-key: home overrides only the keys it sets; cwd fills the rest.)"""
    for k in ("PAPERCLAW_PROVIDER", "PAPERCLAW_MODEL", "PAPERCLAW_API_KEY",
              "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    home = tmp_path / "home"; home.mkdir()
    # the autouse fixture chdir'd us into tmp_path, so this IS ./settings.yaml (the default)
    (tmp_path / "settings.yaml").write_text(
        "LLM:\n  provider: openai\n  base_url: https://OLD.example/v1\n  model: proj-model\n  api_key: sk-proj\n")
    # UI saved here → wins
    (home / "settings.yaml").write_text(
        "LLM:\n  provider: openai\n  base_url: https://NEW.example/v1\n  model: home-model\n  api_key: sk-home\n")
    s = load_settings(home)
    assert s.base_url == "https://NEW.example/v1" and s.model == "home-model" and s.api_key == "sk-home"

    # with no home file, the project-dir default is used (fresh setup / CLI from a repo)
    (home / "settings.yaml").unlink()
    s2 = load_settings(home)
    assert s2.base_url == "https://OLD.example/v1" and s2.model == "proj-model"


def test_legacy_settings_json_still_read(tmp_path, monkeypatch):
    """A legacy flat settings.json (JSON is valid YAML) is still honoured when no
    settings.yaml exists."""
    import json
    for k in ("PAPERCLAW_PROVIDER", "PAPERCLAW_MODEL", "PAPERCLAW_API_KEY",
              "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    home = tmp_path / "home"; home.mkdir()
    (home / "settings.json").write_text(json.dumps(
        {"provider": "openai", "model": "legacy", "api_key": "sk-legacy"}))
    s = load_settings(home)
    assert s.provider == "openai" and s.model == "legacy" and s.api_key == "sk-legacy"


def test_nested_yaml_settings_with_comments(tmp_path, monkeypatch):
    """settings.yaml groups keys into sub-dicts (LLM / image_generation /
    academic_search.open_alex), supports # comments, and chat_agent is not a file key."""
    for k in ("PAPERCLAW_PROVIDER", "PAPERCLAW_MODEL", "PAPERCLAW_API_KEY", "PAPERCLAW_CHAT_AGENT",
              "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    home = tmp_path / "home"; home.mkdir()
    (tmp_path / "settings.yaml").write_text(
        "# my config\n"
        "LLM:\n"
        "  provider: openai          # anthropic | openai\n"
        "  base_url: https://api.example/v1\n"
        "  api_key: sk-llm\n"
        "  model: m-1\n"
        "image_generation:\n"
        "  base_url: https://img.example/v1\n"
        "  api_key: sk-img\n"
        "  model: img-1              # e.g. gpt-image-1\n"
        "academic_search:\n"
        "  open_alex:\n"
        "    api_key: oa-1\n")
    s = load_settings(home)
    assert s.provider == "openai" and s.base_url == "https://api.example/v1"
    assert s.api_key == "sk-llm" and s.model == "m-1"
    assert s.image_base_url == "https://img.example/v1" and s.image_api_key == "sk-img"
    assert s.image_model == "img-1" and s.openalex_api_key == "oa-1"
    assert s.chat_agent == "deepagents"  # always the default — not read from the file


def test_save_settings_writes_nested_yaml_without_chat_agent(tmp_path):
    """save_settings persists the nested YAML layout and never writes chat_agent."""
    import yaml
    from paperclaw.config import LLMSettings, save_settings, settings_path
    save_settings(tmp_path, LLMSettings(provider="openai", api_key="sk-x", model="m",
                                        openalex_api_key="oa", chat_agent="builtin"))
    path = settings_path(tmp_path)
    assert path.name == "settings.yaml"
    raw = yaml.safe_load(path.read_text())
    assert set(raw) == {"LLM", "image_generation", "academic_search"}
    assert raw["LLM"]["api_key"] == "sk-x" and raw["LLM"]["model"] == "m"
    assert raw["academic_search"]["open_alex"]["api_key"] == "oa"
    assert "chat_agent" not in raw and "chat_agent" not in raw["LLM"]


def test_local_client_auto_research_stream(tmp_path, monkeypatch):
    """`auto` (local) forwards the topic + stop params and streams events through."""
    from paperclaw import service
    captured = {}

    async def fake_auto(store, settings, home, topic, *, target_positive, max_hypotheses, page_limit):
        captured.update(topic=topic, target_positive=target_positive,
                        max_hypotheses=max_hypotheses, page_limit=page_limit)
        yield {"type": "domain_created", "name": "D"}
        yield {"type": "done"}

    monkeypatch.setattr(service, "stream_auto_research", fake_auto)
    seen = []
    LocalClient().auto_research_stream(
        "gen modeling", seen.append, target_positive=3, max_hypotheses=5, page_limit=7)
    assert [e["type"] for e in seen] == ["domain_created", "done"]
    assert captured == {"topic": "gen modeling", "target_positive": 3,
                        "max_hypotheses": 5, "page_limit": 7}


def test_remote_client_auto_runs_locally_message():
    from paperclaw.client import RemoteClient
    rc = RemoteClient.__new__(RemoteClient)  # no real HTTP needed for this guard
    with pytest.raises(ClientError) as ei:
        rc.auto_research_stream("topic", lambda ev: None)
    assert "runs locally" in str(ei.value)


def test_local_client_auto_status(tmp_path):
    client = LocalClient()
    assert client.auto_status() == []                          # nothing yet (list — parallel runs)
    idea = client.store.add_idea("X")
    client.store.put_idea_auto_run(idea.id, {"topic": "x", "status": "running", "phase": "hypotheses",
                                             "ideaId": idea.id, "round": 2, "positives": 1})
    runs = client.auto_status()
    assert len(runs) == 1 and runs[0]["phase"] == "hypotheses" and runs[0]["round"] == 2


@pytest.mark.real_run_config
def test_default_run_config_is_cli_agent():
    from paperclaw.server import store
    rc = store.default_run_config()  # default = CLI agent (real), never simulated
    assert rc.experiment_mode == "cli" and "claude -p" in (rc.agent_command or "")


def test_doctor_has_coding_agent_row(tmp_path, monkeypatch):
    from paperclaw import config, service
    from paperclaw.config import LLMSettings
    monkeypatch.setattr(config, "claude_cli_available", lambda: True)
    rep = service.environment_report(LLMSettings(api_key="sk-x"), home=tmp_path)
    coding = [c for c in rep.checks if c.key == "coding_agent"]
    assert coding and "claude" in coding[0].detail.lower()


def test_cmd_run_config_yaml(tmp_path):
    """`run --config f.yaml` takes settings from YAML; explicit CLI flags override."""
    import yaml
    from types import SimpleNamespace
    from paperclaw import cli
    cfg = tmp_path / "run.yaml"
    cfg.write_text(yaml.safe_dump({"topic": "gen modeling", "positive": 3,
                                   "max_hypotheses": 5, "page_limit": 7}))
    captured = {}

    class FakeClient:
        def auto_research_stream(self, topic, on_event, target_positive, max_hypotheses,
                                 page_limit, **over):  # **over = the per-run override flags
            captured.update(topic=topic, target_positive=target_positive,
                            max_hypotheses=max_hypotheses, page_limit=page_limit)

    cli.cmd_run(FakeClient(), SimpleNamespace(
        topic=None, idea=None, config=str(cfg),
        positive=None, max_hypotheses=None, page_limit=None, max_depth=None))
    assert captured == {"topic": "gen modeling", "target_positive": 3,
                        "max_hypotheses": 5, "page_limit": 7}

    captured.clear()  # explicit --positive 9 overrides the YAML's 3
    cli.cmd_run(FakeClient(), SimpleNamespace(
        topic=None, idea=None, config=str(cfg),
        positive=9, max_hypotheses=None, page_limit=None, max_depth=None))
    assert captured["target_positive"] == 9 and captured["max_hypotheses"] == 5
