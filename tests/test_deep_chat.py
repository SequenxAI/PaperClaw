"""Tests for the deepagents chat editor (mocked — no real LLM/agent)."""

import asyncio
import os

from paperclaw.agents import deep_chat
from paperclaw.config import LLMSettings


def test_chat_agent_setting_precedence(tmp_path, monkeypatch):
    """chat_agent defaults to deepagents; PAPERCLAW_CHAT_AGENT can override it to builtin."""
    from paperclaw.config import load_settings

    for v in ("PAPERCLAW_PROVIDER", "PAPERCLAW_MODEL", "PAPERCLAW_API_KEY", "PAPERCLAW_CHAT_AGENT"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.chdir(tmp_path)
    assert load_settings(tmp_path).chat_agent == "deepagents"  # default

    (tmp_path / ".env").write_text("PAPERCLAW_CHAT_AGENT=builtin\n")
    assert load_settings(tmp_path).chat_agent == "builtin"  # override back to builtin


def test_interrupted_chat_message_keeps_work_and_is_resumable():
    """A connection error must NOT wipe the turn to a one-line error: the partial text is kept
    and a transient drop gets a soft 'type continue to resume' (vs. a real failure's hint)."""
    from paperclaw import service

    assert service._is_transient_chat_error(Exception("APIConnectionError: Connection error."))
    assert service._is_transient_chat_error(Exception("Error code: 503 - temporarily unavailable"))
    assert not service._is_transient_chat_error(KeyError("missing"))

    parts = [{"kind": "text", "text": "Found MSE 0.33"}, {"kind": "tool", "name": "bash"}]
    soft = service._interrupted_chat_message(parts, Exception("APIConnectionError: Connection error."), deep=True)
    assert "Found MSE 0.33" in soft and "continue" in soft.lower()        # work kept + resumable
    assert "set chat_agent" not in soft                                   # no scary config hint for a transient drop
    hard = service._interrupted_chat_message(parts, KeyError("boom"), deep=True)
    assert "Found MSE 0.33" in hard and "continue" in hard.lower()


def test_split_content_separates_text_and_thinking():
    assert deep_chat._split_content("hello") == ("hello", "")
    text, thinking = deep_chat._split_content(
        [{"type": "text", "text": "a"}, {"type": "thinking", "thinking": "x"}, "b"])
    assert text == "ab" and thinking == "x"
    assert deep_chat._split_content(None) == ("", "")


def test_is_ai_accepts_streamed_chunk_type():
    # the real streamed chunk type is "AIMessageChunk", not "ai" — both must pass
    assert deep_chat._is_ai(_FakeMsg("x", "AIMessageChunk"))
    assert deep_chat._is_ai(_FakeMsg("x", "ai"))
    assert not deep_chat._is_ai(_FakeMsg("r", "tool"))


class _FakeMsg:
    def __init__(self, content, mtype="AIMessageChunk", tool_calls=None):
        self.content = content
        self.type = mtype
        self.tool_calls = tool_calls or []


def test_stream_deep_chat_streams_text_thinking_and_tool_chain(tmp_path, monkeypatch):
    """Real streamed chunks (type 'AIMessageChunk') over the (mode, data) format:
    text + thinking stream, the tool call surfaces once (deduped), and the edited
    file is detected."""
    (tmp_path / "IDEA.md").write_text("# T\nold line\n", encoding="utf-8")

    class _FakeAgent:
        async def astream(self, inp, stream_mode=None):
            assert inp["messages"][-1]["content"] == "hi"
            assert stream_mode == ["messages", "updates"]
            yield ("messages", (_FakeMsg([{"type": "thinking", "thinking": "Let me read it"}]), {}))
            yield ("messages", (_FakeMsg("I'll edit "), {}))
            yield ("messages", (_FakeMsg("IDEA.md.", tool_calls=[{
                "name": "edit_file",
                "args": {"file_path": "/IDEA.md", "old_string": "old line", "new_string": "new line"},
                "id": "t1"}]), {}))
            p = tmp_path / "IDEA.md"
            p.write_text("# T\nnew line\n", encoding="utf-8")  # simulate the edit
            # Bump the mtime deterministically: real edits land seconds after the
            # snapshot, but this simulated write is microseconds away and can share
            # the snapshot's mtime tick on a coarse-granularity fs (a latent race).
            st = p.stat()
            os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
            yield ("updates", {"tools": {"messages": [_FakeMsg("ok", "tool")]}})     # tool result (no new call)
            yield ("messages", (_FakeMsg(" Done."), {}))

    monkeypatch.setattr(deep_chat, "_build_model", lambda s: object())
    import deepagents
    monkeypatch.setattr(deepagents, "create_deep_agent", lambda **kw: _FakeAgent())

    async def run():
        return [ev async for ev in deep_chat.stream_deep_chat(
            LLMSettings(model="m"), tmp_path, "system", [{"role": "user", "content": "hi"}])]

    evs = asyncio.run(run())
    deltas = "".join(e["text"] for e in evs if e["type"] == "delta")
    thinking = "".join(e["text"] for e in evs if e["type"] == "thinking")
    tools = [e for e in evs if e["type"] == "tool"]
    final = next(e for e in evs if e["type"] == "final")

    assert thinking == "Let me read it"                       # structured thinking event
    assert "I'll edit IDEA.md." in deltas and "Done." in deltas  # answer text (was empty before the fix)
    assert len(tools) == 1                                     # one tool call, deduped (chunk + update)
    assert tools[0]["name"] == "edit_file" and tools[0]["arg"] == "IDEA.md"  # arg hint, leading / stripped
    assert tools[0]["detail"] == ""                            # edit_file shows only the file, no old→new
    assert "IDEA.md" in final["paths"]                        # mtime change → spec_updated
    assert "Done." in final["text"]                           # answer only (no tool/thinking folded in)


def test_write_todos_surfaces_as_todos_event(tmp_path, monkeypatch):
    """A write_todos tool call becomes a structured 'todos' event (the plan/checklist),
    not a generic tool row."""
    class _FakeAgent:
        async def astream(self, inp, stream_mode=None):
            yield ("updates", {"agent": {"messages": [_FakeMsg("", "ai", tool_calls=[{
                "name": "write_todos", "id": "wt1", "args": {"todos": [
                    {"content": "Read the reports", "status": "completed"},
                    {"content": "Write paper.tex", "status": "in_progress"},
                    {"content": "Compile to PDF", "status": "pending"}]}}])]}})
            yield ("messages", (_FakeMsg("Working on it."), {}))

    monkeypatch.setattr(deep_chat, "_build_model", lambda s: object())
    import deepagents
    monkeypatch.setattr(deepagents, "create_deep_agent", lambda **kw: _FakeAgent())

    async def run():
        return [ev async for ev in deep_chat.stream_deep_chat(
            LLMSettings(model="m"), tmp_path, "system", [{"role": "user", "content": "hi"}])]

    evs = asyncio.run(run())
    todos_evs = [e for e in evs if e["type"] == "todos"]
    assert len(todos_evs) == 1
    todos = todos_evs[0]["todos"]
    assert [t["status"] for t in todos] == ["completed", "in_progress", "pending"]
    assert todos[0]["content"] == "Read the reports"
    assert not any(e["type"] == "tool" and e.get("name") == "write_todos" for e in evs)  # not a tool row
