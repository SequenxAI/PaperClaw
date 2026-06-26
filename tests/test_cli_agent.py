"""Tests for the CLI experiment runner — streaming + stream-json log rendering."""

import asyncio
import json
import shlex

import pytest

from paperclaw.agents import cli_agent
from paperclaw.config import LLMSettings
from paperclaw.server.models import RunConfig


def _collect(run_config, out_dir):
    events = []

    async def go():
        async for ev in cli_agent.run_cli_agent(LLMSettings(), "idea ctx", "the plan", out_dir, run_config):
            events.append(ev)

    asyncio.run(go())
    return events


def test_ensure_streaming_enables_claude_print():
    # `claude -p` with no output format → streaming + token-level flags appended.
    out = cli_agent._ensure_streaming("claude -p {prompt} --dangerously-skip-permissions")
    assert "--output-format stream-json --verbose" in out
    assert "--include-partial-messages" in out  # token-by-token live output
    # Already configured → unchanged (no double flags).
    cmd = "claude -p {prompt} --output-format stream-json --verbose"
    assert cli_agent._ensure_streaming(cmd) == cmd
    # Non-claude commands are left alone.
    assert cli_agent._ensure_streaming("opencode run {prompt}") == "opencode run {prompt}"
    assert cli_agent._ensure_streaming("claude mcp list") == "claude mcp list"  # no -p


def test_stream_renderer_partials_token_by_token():
    """With partials on, assistant text/thinking stream live and aren't duplicated by
    the consolidated assistant event (which contributes only its tool calls)."""
    r = cli_agent._StreamRenderer(stream_partials=True)
    seq = [
        {"type": "stream_event", "event": {"type": "content_block_start", "content_block": {"type": "text"}}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Let me run "}}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "it."}}},
        {"type": "stream_event", "event": {"type": "content_block_stop"}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Let me run it."},
            {"type": "tool_use", "name": "Bash", "input": {"command": "python run.py"}}]}},
    ]
    out = "".join(r.feed_line(__import__("json").dumps(e)) for e in seq)
    assert out.count("Let me run it.") == 1     # streamed once, NOT duplicated
    assert "🔧 Bash: python run.py" in out      # tool call still shown from the complete event


def test_stream_renderer_without_partials_renders_complete_text():
    """No partials → the complete assistant event renders its text (and tools)."""
    r = cli_agent._StreamRenderer(stream_partials=False)
    out = r.feed_line(__import__("json").dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "hello there"},
        {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}}))
    assert "hello there" in out and "🔧 Bash: ls" in out


def test_render_stream_json_lines():
    assert "▶ session started" in cli_agent._render_agent_line(
        json.dumps({"type": "system", "subtype": "init", "model": "m", "tools": ["Bash"]}))
    r = cli_agent._render_agent_line(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "running it"},
        {"type": "tool_use", "name": "Bash", "input": {"command": "python run.py"}}]}}))
    assert "running it" in r and "🔧 Bash: python run.py" in r
    assert "↳" in cli_agent._render_agent_line(json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "content": "loss 0.1", "is_error": False}]}}))
    assert "✓ agent success" in cli_agent._render_agent_line(
        json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": 2}))
    # Non-JSON passes through; noisy partials drop.
    assert cli_agent._render_agent_line("plain text log") == "plain text log"
    assert cli_agent._render_agent_line(json.dumps({"type": "stream_event"})) is None


def test_run_cli_agent_no_command(tmp_path):
    events = _collect(RunConfig(experimentMode="cli", agentCommand=""), tmp_path)
    assert events[-1]["type"] == "result"
    assert "No agent command" in events[-1]["result"]["markdown"]


@pytest.mark.skipif(cli_agent.pty is None, reason="POSIX PTY only")
def test_run_cli_agent_renders_stream_json_live(tmp_path):
    """A command emitting Claude Code stream-json streams READABLE deltas (not raw JSON)."""
    events = [
        {"type": "system", "subtype": "init", "model": "claude-x", "tools": ["Bash"]},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "python run.py"}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "epoch1 loss 0.1", "is_error": False}]}},
        {"type": "result", "subtype": "success", "is_error": False, "num_turns": 1},
    ]
    script = (
        "import json\n"
        + "".join(f"print({json.dumps(json.dumps(e))}, flush=True)\n" for e in events)
        + "open('results.json','w').write('{\"metric\": 1.0}')\n"
    )
    cmd = "python3 -c " + shlex.quote(script)
    evs = _collect(RunConfig(experimentMode="cli", agentCommand=cmd), tmp_path)
    deltas = "".join(e["text"] for e in evs if e["type"] == "delta")

    assert "▶ session started" in deltas
    assert "🔧 Bash: python run.py" in deltas
    assert "epoch1 loss 0.1" in deltas
    assert "✓ agent success" in deltas
    assert '{"type"' not in deltas  # raw JSON is NOT shown to the user
    # raw stream kept for debugging; rendered text in stdout.log
    assert (tmp_path / "agent_stream.log").read_text().strip()
    assert "🔧 Bash" in (tmp_path / "stdout.log").read_text()
    assert evs[-1]["type"] == "result" and evs[-1]["result"]["error"] is None  # results.json read


@pytest.mark.skipif(cli_agent.pty is None, reason="POSIX PTY only")
def test_run_cli_agent_finalizes_on_deliverable_when_agent_hangs(tmp_path):
    """The agent writes results.json + emits its terminal result event, then HANGS
    (sleeps, like a rate-limited claude). The runner must finalize promptly via the
    result event instead of blocking on the stuck process."""
    import time as _time
    events = [
        {"type": "system", "subtype": "init", "model": "m", "tools": []},
        {"type": "result", "subtype": "success", "is_error": False, "num_turns": 1},
    ]
    script = (
        "import json, time\n"
        "open('results.json','w').write('{\"metric\": 1.0}')\n"          # deliverable first
        + "".join(f"print({json.dumps(json.dumps(e))}, flush=True)\n" for e in events)
        + "time.sleep(120)\n"                                             # then hang
    )
    cmd = "python3 -c " + shlex.quote(script)
    t0 = _time.time()
    # No deadline (experiments never time out), so only finalize-on-deliverable can end this.
    evs = _collect(RunConfig(experimentMode="cli", agentCommand=cmd), tmp_path)
    elapsed = _time.time() - t0

    assert elapsed < 30, f"runner waited on the hung process ({elapsed:.0f}s)"
    assert "results.json written) — finalizing" in "".join(e["text"] for e in evs if e["type"] == "delta")
    assert evs[-1]["type"] == "result" and evs[-1]["result"]["error"] is None  # results.json read


def test_run_cli_agent_waits_grace_for_late_results(tmp_path, monkeypatch):
    """Agent emits its terminal result event BEFORE results.json is on disk (a foreground
    run flushing it a moment later), then hangs. The runner must NOT kill instantly — it
    waits out the deliverable grace, picks up results.json when it appears, and succeeds."""
    monkeypatch.setattr(cli_agent, "_DELIVERABLE_GRACE", 10.0)
    events = [
        {"type": "system", "subtype": "init", "model": "m", "tools": []},
        {"type": "result", "subtype": "success", "is_error": False, "num_turns": 1},
    ]
    script = (
        "import json, time\n"
        + "".join(f"print({json.dumps(json.dumps(e))}, flush=True)\n" for e in events)
        + "time.sleep(1.5)\n"                                            # report done FIRST
        + "open('results.json','w').write('{\"metric\": 1.0}')\n"       # deliverable lands late
        + "time.sleep(120)\n"                                           # then hang
    )
    cmd = "python3 -c " + shlex.quote(script)
    evs = _collect(RunConfig(experimentMode="cli", agentCommand=cmd), tmp_path)

    deltas = "".join(e["text"] for e in evs if e["type"] == "delta")
    assert "results.json is not written yet" in deltas  # didn't kill instantly
    assert evs[-1]["type"] == "result" and evs[-1]["result"]["error"] is None  # late results.json read


@pytest.mark.skipif(cli_agent.pty is None, reason="POSIX PTY only")
def test_run_cli_agent_passthrough_plain_text(tmp_path):
    """A non-JSON CLI (plain stdout) streams through unchanged."""
    cmd = "printf 'line one\\nline two\\n'; echo '{\"m\":1}' > results.json"
    evs = _collect(RunConfig(experimentMode="cli", agentCommand=cmd), tmp_path)
    deltas = "".join(e["text"] for e in evs if e["type"] == "delta")
    assert "line one" in deltas and "line two" in deltas
