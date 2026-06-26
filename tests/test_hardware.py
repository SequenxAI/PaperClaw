"""Tests for hardware/environment detection (module + routes)."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from paperclaw import hardware
from paperclaw.server.app import create_app


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    for var in ("PAPERCLAW_PROVIDER", "PAPERCLAW_BASE_URL", "PAPERCLAW_MODEL", "PAPERCLAW_API_KEY",
                "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)


def make_client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(home=tmp_path))


# ── Parsing (deterministic, no real hardware needed) ──────────────────────────

SAMPLE = """\
##OS##
Linux 5.15.0 x86_64
##CPU_MODEL##
Intel(R) Xeon(R) Silver 4210R CPU @ 2.40GHz
##CPU_CORES##
10
##CPU_SOCKETS##
2
##CPU_THREADS##
40
##MEM_KB##
394964216
##GPU##
NVIDIA RTX A6000, 49140
NVIDIA RTX A6000, 49140
##DISK##
NAME="sda" MODEL="WDC WUH721814AL" SIZE="14000519643136" ROTA="1" TRAN="sata"
NAME="nvme0n1" MODEL="KXG80ZNV2T04 KIOXIA" SIZE="2048408248320" ROTA="0" TRAN="nvme"
NAME="loop0" MODEL="" SIZE="65536" ROTA="0" TRAN=""
##END##
"""


def test_build_info_parses_all_sections():
    info = hardware._build_info("local", "local", SAMPLE)
    assert info.cpu_model.startswith("Intel(R) Xeon")
    assert info.cpu_cores == 20  # 10 cores/socket * 2 sockets
    assert info.cpu_threads == 40
    assert info.mem_total_gb == pytest.approx(376.6, abs=0.5)
    assert [g.name for g in info.gpus] == ["NVIDIA RTX A6000", "NVIDIA RTX A6000"]
    assert info.gpus[0].memory_total_mb == 49140
    kinds = {d.name: d.kind for d in info.disks}
    assert kinds == {"sda": "HDD", "nvme0n1": "NVMe"}  # loop0 filtered out


def test_render_markdown_has_machine_section():
    info = hardware._build_info("local", "local", SAMPLE)
    md = hardware.render_markdown([info])
    assert "# Hardware & Environment" in md
    assert "## local" in md
    assert "NVIDIA RTX A6000" in md
    assert "| Device | Model | Size | Type |" in md


def test_unreachable_remote_renders_gracefully():
    info = hardware.HardwareInfo(scope="remote", label="me@box", reachable=False, error="timeout")
    md = hardware.render_markdown([info])
    assert "Unreachable: timeout" in md


def test_detect_local_runs():
    """Real local probe — bash exists in the test env; fields are best-effort."""
    info = hardware.detect_local()
    assert info.scope == "local"
    assert info.os  # uname/platform always yields something


# ── Routes ────────────────────────────────────────────────────────────────────

def test_hardware_empty_then_ssh_then_persist(tmp_path):
    client = make_client(tmp_path)

    r = client.get("/api/hardware")
    assert r.status_code == 200
    body = r.json()
    assert body["machines"] == [] and body["sshTargets"] == []
    assert body["markdown"] is None and body["updatedAt"] == 0.0
    assert body["runConfig"]["experimentMode"] == "simulated"  # classic pipeline by default

    r = client.put("/api/hardware/ssh", json={"sshTargets": [
        {"id": "t1", "host": "1.2.3.4", "user": "me", "port": 2222,
         "keyPath": "~/.ssh/id_ed25519", "label": "gpu-box"},
    ]})
    assert r.status_code == 200
    targets = r.json()["sshTargets"]
    assert targets[0]["label"] == "gpu-box" and targets[0]["port"] == 2222

    # persisted across a fresh app on the same home
    client2 = make_client(tmp_path)
    assert client2.get("/api/hardware").json()["sshTargets"][0]["host"] == "1.2.3.4"


def test_hardware_detect_writes_files(tmp_path):
    client = make_client(tmp_path)
    r = client.post("/api/hardware/detect")
    assert r.status_code == 200
    body = r.json()
    labels = [m["label"] for m in body["machines"]]
    assert "local" in labels
    assert body["markdown"] and "# Hardware & Environment" in body["markdown"]
    assert (tmp_path / "HARDWARE.md").is_file()
    assert (tmp_path / "hardware.json").is_file()


def test_run_config_defaults_and_update(tmp_path):
    client = make_client(tmp_path)
    # default is simulated (classic pipeline preserved)
    assert client.get("/api/hardware").json()["runConfig"]["experimentMode"] == "simulated"

    r = client.put("/api/hardware/run-config", json={
        "experimentMode": "executed", "pythonPath": "/usr/bin/python3", "maxAttempts": 3,
    })
    assert r.status_code == 200
    rc = r.json()["runConfig"]
    assert rc["experimentMode"] == "executed" and rc["pythonPath"] == "/usr/bin/python3"

    # persisted across a fresh app
    assert make_client(tmp_path).get("/api/hardware").json()["runConfig"]["maxAttempts"] == 3


# ── Executed runner ──────────────────────────────────────────────────────────

def test_run_local_code_executes_and_parses(tmp_path, monkeypatch):
    """Stub the LLM to return a trivial script; assert it runs and results parse."""
    import asyncio
    from paperclaw import experiments, llm
    from paperclaw.config import LLMSettings
    from paperclaw.server.models import RunConfig

    script = (
        "```python\n"
        "import json\n"
        "open('results.json','w').write(json.dumps({\n"
        "  'experiments':[{'name':'toy','setup':'synthetic',\n"
        "    'metrics':{'baseline':{'acc':0.8},'ours':{'acc':0.9}},\n"
        "    'hypothesis':'ours>baseline','verdict':'SUPPORTED','status':'POSITIVE','observations':'gap'}],\n"
        "  'summary':'ours wins'}))\n"
        "print('done')\n"
        "```"
    )

    async def fake_stream(settings, system, messages, max_tokens=4096, **kw):
        yield {"type": "text", "text": script}

    monkeypatch.setattr(llm, "stream_chat_thinking", fake_stream)

    async def run():
        out = tmp_path / "experiments"
        result = None
        async for ev in experiments.run_local_code(
            LLMSettings(), "idea", "plan", out, RunConfig(experimentMode="executed", maxAttempts=2)
        ):
            if ev["type"] == "result":
                result = ev["result"]
        return result, out

    result, out = asyncio.run(run())
    assert result["provenance"] == "executed" and result["error"] is None
    assert "| Metric | baseline | ours |" in result["markdown"]
    assert "SUPPORTED" in result["markdown"]
    assert (out / "results.json").is_file() and (out / "run.py").is_file()


def test_run_agentic_experiment(tmp_path, monkeypatch):
    """Agentic loop: write run.py (python block) → run it (bash block) → DONE."""
    import asyncio
    from paperclaw import agents, llm
    from paperclaw.config import LLMSettings
    from paperclaw.server.models import RunConfig

    PY = ('```python\nimport json\n'
          'open("results.json","w").write(json.dumps({"experiments":[{"name":"t","setup":"s",'
          '"metrics":{"ours":{"acc":0.9}},"hypothesis":"h","verdict":"SUPPORTED",'
          '"status":"POSITIVE","observations":"o"}],"summary":"ok"}))\nprint("wrote results")\n```')
    calls = {"n": 0}

    async def fake_stream(settings, system, messages, max_tokens=4096, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            text = "I'll write the script.\n" + PY
        elif calls["n"] == 2:
            text = "Now run it.\n```bash\npython run.py\n```"
        else:
            text = "DONE"
        yield {"type": "text", "text": text}

    monkeypatch.setattr(llm, "stream_chat_thinking", fake_stream)

    async def run():
        result = None
        async for ev in agents.run_agentic_experiment(
            LLMSettings(), "idea", "plan", tmp_path / "exp",
            RunConfig(experimentMode="executed"),
        ):
            if ev["type"] == "result":
                result = ev["result"]
        return result

    result = asyncio.run(run())
    assert result["provenance"] == "agent" and result["error"] is None
    assert "SUPPORTED" in result["markdown"]
    assert (tmp_path / "exp" / "run.py").is_file()
    assert (tmp_path / "exp" / "results.json").is_file()
    assert (tmp_path / "exp" / "stdout.log").read_text()  # command output captured


def test_run_agentic_experiment_multifile(tmp_path, monkeypatch):
    """Agent manages a real codebase: write a module in a subdir, write run.py that
    imports it, PATCH the module, then run → results.json reflects the edit."""
    import asyncio
    import json
    from paperclaw import agents, llm
    from paperclaw.config import LLMSettings
    from paperclaw.server.models import RunConfig

    MOD = ("```write mylib/calc.py\n"
           "FACTOR = 1\n\ndef score():\n    return 0.5 * FACTOR\n```")
    RUN = ('```write run.py\nimport json\nfrom mylib.calc import score\n'
           'open("results.json","w").write(json.dumps({"experiments":[{"name":"t","setup":"s",'
           '"metrics":{"ours":{"acc":score()}},"hypothesis":"h","verdict":"SUPPORTED",'
           '"status":"POSITIVE","observations":"o"}],"summary":"ok"}))\nprint("ran")\n```')
    PATCH = ("```patch mylib/calc.py\n@@ -1,1 +1,1 @@\n-FACTOR = 1\n+FACTOR = 2\n```")
    steps = iter([
        "Create the library module.\n" + MOD,
        "Add the entry point.\n" + RUN,
        "Bump the factor.\n" + PATCH,
        "Run it.\n```bash\npython run.py\n```",
        "DONE",
    ])

    async def fake_stream(settings, system, messages, max_tokens=4096, **kw):
        yield {"type": "text", "text": next(steps)}

    monkeypatch.setattr(llm, "stream_chat_thinking", fake_stream)

    async def run():
        result = None
        async for ev in agents.run_agentic_experiment(
            LLMSettings(), "idea", "plan", tmp_path / "exp",
            RunConfig(experimentMode="executed"),
        ):
            if ev["type"] == "result":
                result = ev["result"]
        return result

    result = asyncio.run(run())
    exp = tmp_path / "exp"
    assert (exp / "mylib" / "calc.py").is_file()                  # multi-file + subdir
    assert (exp / "run.py").is_file()
    assert "FACTOR = 2" in (exp / "mylib" / "calc.py").read_text()  # patch applied
    assert result["error"] is None and "SUPPORTED" in result["markdown"]
    data = json.loads((exp / "results.json").read_text())
    assert data["experiments"][0]["metrics"]["ours"]["acc"] == 1.0  # 0.5 * 2


def test_run_agentic_experiment_remote(tmp_path, monkeypatch):
    """SSH mode: the SAME agentic loop, but bash is dispatched to the remote (here
    mocked to run locally) and authored files are pushed / results pulled. Asserts the
    ssh/scp command construction and a normal ssh-provenance result."""
    import asyncio
    import subprocess as real_sp
    from types import SimpleNamespace
    from paperclaw import agents, llm
    from paperclaw.agents import coding_agent
    from paperclaw.config import LLMSettings
    from paperclaw.server.models import RunConfig

    PY = ('```python\nimport json\n'
          'open("results.json","w").write(json.dumps({"experiments":[{"name":"t","setup":"s",'
          '"metrics":{"ours":{"acc":0.9}},"hypothesis":"h","verdict":"SUPPORTED",'
          '"status":"POSITIVE","observations":"o"}],"summary":"ok"}))\nprint("wrote results")\n```')
    steps = iter(["Write the script.\n" + PY, "Run it.\n```bash\npython run.py\n```", "DONE"])

    async def fake_llm(settings, system, messages, max_tokens=4096, **kw):
        yield {"type": "text", "text": next(steps)}
    monkeypatch.setattr(llm, "stream_chat_thinking", fake_llm)

    orig_run = real_sp.run  # the real subprocess.run, before we patch the module attr

    # record ssh/scp invocations (setup / sync_file / collect) — no real remote
    ssh_calls = []
    def fake_run(argv, *a, **kw):
        ssh_calls.append(argv)
        return real_sp.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(coding_agent.subprocess, "run", fake_run)

    # the "remote" bash actually runs locally in the workspace (stands in for the box)
    bash_argvs = []
    async def fake_stream_argv(argv, cwd, log, header):
        bash_argvs.append(argv)
        p = orig_run(["bash", "-c", header], cwd=str(log.parent), capture_output=True, text=True)
        out = (p.stdout or "") + (p.stderr or "")
        with log.open("a", encoding="utf-8") as f:
            f.write(out)
        yield ("chunk", out)
        yield ("done", p.returncode, out)
    monkeypatch.setattr(coding_agent, "_stream_argv", fake_stream_argv)

    target = SimpleNamespace(user="me", host="gpu.box", port=22, key_path=None)

    async def run():
        result = None
        async for ev in agents.run_agentic_experiment(
            LLMSettings(), "idea", "plan", tmp_path / "exp",
            RunConfig(experimentMode="ssh"), target=target):
            if ev["type"] == "result":
                result = ev["result"]
        return result
    result = asyncio.run(run())

    exp = tmp_path / "exp"
    assert result["provenance"] == "ssh" and result["error"] is None
    assert "SUPPORTED" in result["markdown"]
    assert (exp / "run.py").is_file() and (exp / "results.json").is_file()  # authored locally
    # bash dispatched over ssh into the remote working dir (login shell)
    assert bash_argvs and bash_argvs[0][0] == "ssh"
    assert any("me@gpu.box" == part for part in bash_argvs[0])
    assert any("bash -lc" in part and "/tmp/paperclaw_" in part for part in bash_argvs[0])
    # setup mkdir + scp push of run.py + scp pull of results.json
    flat = [" ".join(c) for c in ssh_calls]
    assert any(c.startswith("ssh") and "mkdir -p /tmp/paperclaw_" in c for c in flat)
    assert any(c.startswith("scp") and "run.py" in c for c in flat)
    assert any(c.startswith("scp") and "results.json" in c for c in flat)


def test_run_cli_agent_streams_and_parses(tmp_path):
    """CLI mode: a fake headless 'agent' command writes results.json; the runner
    streams its stdout live and parses the deliverable. No real CLI / LLM needed."""
    import asyncio
    from paperclaw import agents
    from paperclaw.config import LLMSettings
    from paperclaw.server.models import RunConfig

    cmd = (
        "echo 'agent: starting'; "
        """printf '%s' '{"experiments":[{"name":"t","setup":"s","metrics":{"ours":{"acc":0.9}},"hypothesis":"h","verdict":"SUPPORTED","status":"POSITIVE","observations":"o"}],"summary":"ok"}' > results.json; """
        "echo 'agent: done'"
    )

    async def run():
        deltas, result = [], None
        async for ev in agents.run_cli_agent(
            LLMSettings(), "idea", "plan", tmp_path / "exp",
            RunConfig(experimentMode="cli", agentCommand=cmd),
        ):
            if ev["type"] == "delta":
                deltas.append(ev["text"])
            elif ev["type"] == "result":
                result = ev["result"]
        return result, "".join(deltas)

    result, streamed = asyncio.run(run())
    assert result["provenance"] == "cli" and result["error"] is None
    assert "SUPPORTED" in result["markdown"]
    assert "agent: starting" in streamed and "agent: done" in streamed  # stdout streamed live
    exp = tmp_path / "exp"
    assert (exp / "results.json").is_file()
    assert (exp / "task.md").is_file()              # task handed to the external agent
    assert "FOLLOW IT EXACTLY" in (exp / "task.md").read_text()
    assert (exp / "stdout.log").read_text()         # output captured to log


def test_run_cli_agent_requires_command(tmp_path):
    """cli mode with no agent_command yields a friendly, non-crashing result."""
    import asyncio
    from paperclaw import agents
    from paperclaw.config import LLMSettings
    from paperclaw.server.models import RunConfig

    async def run():
        result = None
        async for ev in agents.run_cli_agent(
            LLMSettings(), "i", "p", tmp_path / "e",
            RunConfig(experimentMode="cli"),
        ):
            if ev["type"] == "result":
                result = ev["result"]
        return result

    result = asyncio.run(run())
    assert result["provenance"] == "cli"
    assert result["error"] and "no agent_command" in result["error"].lower()


def test_run_config_cli_mode_persists(tmp_path):
    """cli mode + agentCommand round-trips through the run-config route."""
    client = make_client(tmp_path)
    r = client.put("/api/hardware/run-config", json={
        "experimentMode": "cli", "agentCommand": "opencode run {prompt}",
    })
    assert r.status_code == 200
    rc = r.json()["runConfig"]
    assert rc["experimentMode"] == "cli" and rc["agentCommand"] == "opencode run {prompt}"
    # persisted across a fresh app
    rc2 = make_client(tmp_path).get("/api/hardware").json()["runConfig"]
    assert rc2["agentCommand"] == "opencode run {prompt}"


def test_run_local_code_retries_on_failure(tmp_path, monkeypatch):
    """First script crashes, second writes results — runner should recover."""
    import asyncio
    from paperclaw import experiments, llm
    from paperclaw.config import LLMSettings
    from paperclaw.server.models import RunConfig

    good = (
        "```python\n"
        "import json\n"
        "open('results.json','w').write(json.dumps({'experiments':[],'summary':'ok'}))\n"
        "```"
    )
    bad = "```python\nraise RuntimeError('boom')\n```"
    scripts = iter([bad, good])

    async def fake_stream(settings, system, messages, max_tokens=4096, **kw):
        yield {"type": "text", "text": next(scripts)}

    monkeypatch.setattr(llm, "stream_chat_thinking", fake_stream)

    async def run():
        result = None
        async for ev in experiments.run_local_code(
            LLMSettings(), "idea", "plan", tmp_path / "exp",
            RunConfig(experimentMode="executed", maxAttempts=3),
        ):
            if ev["type"] == "result":
                result = ev["result"]
        return result

    result = asyncio.run(run())
    assert result["error"] is None and result["attempts"] == 2
