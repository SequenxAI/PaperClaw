"""Tests for the LLM workspace tools (paperclaw/tools)."""

from pathlib import Path

import pytest

from paperclaw import literature
from paperclaw import tools as _tools
from paperclaw.tools import apply_patch, list_files, openalex_search, read_image, write_file

# A valid 1×1 PNG (exercises the IHDR size parse).
_PNG_1x1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
    "0000000c49444154789c63f8ffff3f0005fe02fe0def46b80000000049454e44ae426082"
)


def test_openalex_search_registered():
    """The tool is exposed to both provider tool-loops via the registry."""
    names = {t["name"] for t in _tools.ALL_TOOLS}
    assert "openalex_search" in names
    assert _tools.EXECUTORS["openalex_search"] is openalex_search.execute


def test_openalex_search_formats_results(monkeypatch):
    captured = {}

    def fake_search(query, limit=8, recent_only=False):
        captured.update(query=query, limit=limit, recent_only=recent_only)
        return [{
            "title": "Diffusion Models for Time Series",
            "authors": ["Ada Lovelace", "Alan Turing", "Grace Hopper", "Ken Thompson"],
            "year": 2025,
            "venue": "NeurIPS",
            "citations": 1234,
            "abstract": "We present a diffusion approach",
            "doi": "10.1234/abcd",
        }]

    monkeypatch.setattr(literature, "search_papers_sync", fake_search)

    out = openalex_search.execute(Path("."), {"query": "diffusion time series", "recent_only": True})

    assert captured == {"query": "diffusion time series", "limit": 8, "recent_only": True}
    assert "Diffusion Models for Time Series" in out
    assert "10.1234/abcd" in out
    assert "1,234" in out
    assert "et al." in out  # >3 authors collapsed


def test_openalex_search_requires_query():
    assert openalex_search.execute(Path("."), {"query": "  "}).startswith("Error")


def test_openalex_search_no_results(monkeypatch):
    monkeypatch.setattr(literature, "search_papers_sync", lambda *a, **k: [])
    out = openalex_search.execute(Path("."), {"query": "nonexistent topic xyz"})
    assert "No OpenAlex results" in out
    assert "invent" in out.lower()


def test_openalex_search_limit_clamped(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        literature, "search_papers_sync",
        lambda query, limit=8, recent_only=False: captured.update(limit=limit) or [],
    )
    openalex_search.execute(Path("."), {"query": "x", "limit": 999})
    assert captured["limit"] == openalex_search._MAX_LIMIT


def test_search_papers_sync_empty_query():
    assert literature.search_papers_sync("   ") == []


# ── write_file / list_files ────────────────────────────────────────────────


def test_write_file_creates_and_tracks(tmp_path):
    assert "write_file" in {t["name"] for t in _tools.ALL_TOOLS}
    assert "write_file" in _tools.WRITE_TOOLS

    out = write_file.execute(tmp_path, {"path": "paper.md", "content": "# Paper\n\nBody"})
    assert "Created paper.md" in out
    assert (tmp_path / "paper.md").read_text() == "# Paper\n\nBody"

    # Overwriting reports the right verb and replaces content.
    out2 = write_file.execute(tmp_path, {"path": "paper.md", "content": "new"})
    assert "Overwrote paper.md" in out2
    assert (tmp_path / "paper.md").read_text() == "new"


def test_write_file_creates_parent_dirs(tmp_path):
    write_file.execute(tmp_path, {"path": "sub/notes.md", "content": "x"})
    assert (tmp_path / "sub" / "notes.md").read_text() == "x"


def test_write_file_rejects_escape(tmp_path):
    out = write_file.execute(tmp_path, {"path": "../evil.md", "content": "x"})
    assert out.startswith("Error")
    assert not (tmp_path.parent / "evil.md").exists()


def test_write_file_requires_args(tmp_path):
    assert write_file.execute(tmp_path, {"content": "x"}).startswith("Error")
    assert write_file.execute(tmp_path, {"path": "a.md"}).startswith("Error")


def test_list_files_lists_workspace(tmp_path):
    (tmp_path / "IDEA.md").write_text("spec")
    (tmp_path / "paper.md").write_text("paper body")
    (tmp_path / ".research_plan.md").write_text("plan")

    out = list_files.execute(tmp_path, {})
    assert "IDEA.md" in out
    assert "paper.md" in out
    assert ".research_plan.md" in out  # hidden artifacts are listed too


def test_list_files_empty(tmp_path):
    assert "empty" in list_files.execute(tmp_path, {}).lower()


# ── read_image: hand a figure to the vision model ─────────────────────────────


def test_read_image_registered():
    assert "read_image" in {t["name"] for t in _tools.ALL_TOOLS}
    assert _tools.EXECUTORS["read_image"] is read_image.execute
    assert "read_image" not in _tools.WRITE_TOOLS  # it only reads


def test_read_image_returns_image_block(tmp_path):
    (tmp_path / "loss.png").write_bytes(_PNG_1x1)
    out = read_image.execute(tmp_path, {"path": "loss.png"})
    assert isinstance(out, list)
    text, image = out
    assert text["type"] == "text" and "loss.png" in text["text"]
    assert "1×1px" in text["text"]  # IHDR dimensions parsed
    assert image["type"] == "image"
    assert image["source"]["media_type"] == "image/png"
    assert image["source"]["data"]  # base64 payload present


def test_read_image_rejects_non_image(tmp_path):
    (tmp_path / "notes.md").write_text("hi")
    out = read_image.execute(tmp_path, {"path": "notes.md"})
    assert isinstance(out, str) and out.startswith("Error")


def test_read_image_missing_and_escape(tmp_path):
    assert read_image.execute(tmp_path, {"path": "nope.png"}).startswith("Error")
    assert read_image.execute(tmp_path, {"path": "../x.png"}).startswith("Error")
    assert read_image.execute(tmp_path, {}).startswith("Error")


def test_read_pdf_registered():
    assert "read_pdf" in {t["name"] for t in _tools.ALL_TOOLS}
    assert "read_pdf" not in _tools.WRITE_TOOLS


def test_read_pdf_summary_and_page(tmp_path, monkeypatch):
    from paperclaw import paper_review
    from paperclaw.tools import read_pdf
    (tmp_path / "paper.pdf").write_bytes(b"%PDF-1.4 stub")  # existence + .pdf suffix
    # Mock the rendered text: 4 main pages, References on p.5, Appendix on p.6.
    pages = ["Intro", "Method", "Experiments", "Conclusion text",
             "References\n[1] Foo", "Appendix\nA Proofs"]
    monkeypatch.setattr(paper_review, "_pdf_pages_text", lambda p: pages)

    summary = read_pdf.execute(tmp_path, {"path": "paper.pdf"})
    assert "6 page(s)" in summary
    assert "Main content: ~4 page(s) — ends on p.4" in summary
    assert "References begin on p.5" in summary and "Appendix begins on p.6" in summary

    one = read_pdf.execute(tmp_path, {"path": "paper.pdf", "page": 5})
    assert "page 5/6" in one and "References" in one


def test_read_pdf_errors(tmp_path):
    from paperclaw.tools import read_pdf
    (tmp_path / "notes.txt").write_text("x")
    assert read_pdf.execute(tmp_path, {"path": "notes.txt"}).startswith("Error: not a PDF")
    assert read_pdf.execute(tmp_path, {"path": "missing.pdf"}).startswith("Error: file not found")
    assert read_pdf.execute(tmp_path, {"path": "../x.pdf"}).startswith("Error: path escapes")
    assert read_pdf.execute(tmp_path, {}).startswith("Error: 'path' is required")


def test_read_image_flattened_for_openai():
    """The OpenAI-compatible path can't carry images — it flattens to the label."""
    from paperclaw.llm import _flatten_tool_output
    blocks = [
        {"type": "text", "text": "Image loss.png (1×1px, 0 KB):"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAA"}},
    ]
    flat = _flatten_tool_output(blocks)
    assert "loss.png" in flat
    assert "[image not viewable on this provider]" in flat
    assert _flatten_tool_output("plain") == "plain"


# ── apply_patch: locate edits by content, not by (wrong) line numbers ──────────

_IDEA = (
    "# Idea\n"
    "\n"
    "## Target Venue\n"
    "AAAI 2026 https://aaai.org/x\n"
    "NeurIPS\n"
    "\n"
    "## Keywords\n"
    "kw1, kw2\n"
)


def test_apply_diff_matches_by_content_despite_wrong_line_numbers():
    # The @@ header says line 1, which is WRONG — but the context locates the
    # real spot, so only the NeurIPS line is removed (the bug: it used to delete
    # whatever sat at the stated line number).
    diff = "@@ -1,2 +1,1 @@\n AAAI 2026 https://aaai.org/x\n-NeurIPS\n"
    out = apply_patch.apply_diff(_IDEA, diff)
    assert "NeurIPS" not in out
    assert "AAAI 2026 https://aaai.org/x" in out
    assert "## Keywords" in out and "kw1, kw2" in out  # untouched


def test_apply_diff_replace_line_by_content():
    diff = "@@ -99,1 +99,1 @@\n-## Target Venue\n+## Venue\n"  # bogus line number
    out = apply_patch.apply_diff(_IDEA, diff)
    assert "## Venue\n" in out and "## Target Venue" not in out


def test_apply_diff_unmatched_context_raises_not_corrupts():
    diff = "@@ -3,2 +3,2 @@\n a line that does not exist\n-also missing\n+new\n"
    with pytest.raises(ValueError):
        apply_patch.apply_diff(_IDEA, diff)


def test_apply_diff_insertion_after_context():
    diff = "@@ -7,0 +8,1 @@\n ## Keywords\n+## Background\n"
    out = apply_patch.apply_diff(_IDEA, diff)
    assert "## Background\n" in out and "## Keywords\n" in out


def test_bash_tool_runs_in_workspace_and_caps_output(tmp_path):
    from paperclaw.tools import bash, ALL_TOOLS, EXECUTORS
    assert any(t["name"] == "bash" for t in ALL_TOOLS) and "bash" in EXECUTORS  # registered
    (tmp_path / "a.txt").write_text("x")
    out = bash.execute(tmp_path, {"command": "echo hi && ls"})
    assert "(exit 0)" in out and "hi" in out and "a.txt" in out   # ran in the workspace dir
    assert bash.execute(tmp_path, {"command": ""}).startswith("Error")
    assert "exit 1" in bash.execute(tmp_path, {"command": "exit 1"})


def test_bash_background_job_launches_polls_and_finishes(tmp_path):
    """bash(run_in_background) launches a DETACHED job (returns a job id immediately) that
    bash_output polls: running → done+exit-code, with output captured. Survives the turn."""
    import time
    from paperclaw.tools import bash, bash_output, ALL_TOOLS, EXECUTORS
    assert any(t["name"] == "bash_output" for t in ALL_TOOLS) and "bash_output" in EXECUTORS
    note = bash.execute(tmp_path, {"command": "echo go; sleep 1; echo done; exit 0",
                                   "run_in_background": True})
    assert "Started background job" in note
    jid = note.split("`")[1]
    assert "running" in bash_output.execute(tmp_path, {"job_id": jid})       # not blocking
    time.sleep(1.4)
    final = bash_output.execute(tmp_path, {"job_id": jid})
    assert "done (exit 0)" in final and "go" in final and "done" in final    # exit code + output
    # explicit non-zero exit is recorded (subshell isolates `exit`)
    n2 = bash.execute(tmp_path, {"command": "exit 7", "run_in_background": True})
    time.sleep(0.6)
    assert "exit 7" in bash_output.execute(tmp_path, {"job_id": n2.split("`")[1]})
    assert "No such" in bash_output.execute(tmp_path, {"job_id": "nope"})


def test_list_exp_resources_reports_compute_and_masks_keys(tmp_path):
    """list_exp_resources reports compute (local + SSH), experiment mode + remotes, and the LLM
    — but NEVER the raw API key value."""
    import json
    from paperclaw.tools import list_exp_resources as ler, ALL_TOOLS, EXECUTORS
    assert any(t["name"] == "list_exp_resources" for t in ALL_TOOLS) and "list_exp_resources" in EXECUTORS
    home = tmp_path
    (home / "HARDWARE.md").write_text("# Hardware\n- local: 4x A100\n")
    (home / "hardware.json").write_text(json.dumps(
        {"runConfig": {"experimentMode": "ssh", "sshTargetId": "gpu1"},
         "sshTargets": [{"id": "gpu1", "host": "u@h"}]}))
    (home / "settings.yaml").write_text(
        "LLM:\n  provider: openai\n  api_key: sk-SECRET\n  model: gpt-5.5\n")
    idea = home / "ideas" / "abc"; idea.mkdir(parents=True)
    out = ler.execute(idea, {})
    assert "4x A100" in out and "ssh" in out and "gpu1" in out      # compute + mode + remote
    assert "gpt-5.5" in out and "configured" in out                  # LLM reported
    assert "sk-SECRET" not in out                                    # key value NEVER leaked


def test_chat_agent_has_bash_tool():
    """The deepagents chat agent bridges a `bash` tool (so it can run shell, not just edit)."""
    from paperclaw.agents import deep_chat
    from paperclaw.config import LLMSettings
    from pathlib import Path
    names = [getattr(t, "__name__", getattr(t, "name", "")) for t in
             deep_chat._workspace_tools(LLMSettings(), Path("/tmp"))]
    assert "bash" in names
