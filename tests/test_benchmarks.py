"""Tests for benchmark templates — the library, ref.bib merge, service resolve, the
route, and the deep pipeline injection (the run is reframed around the cited table)."""

import asyncio
from types import SimpleNamespace

import pytest

from paperclaw import benchmarks, iterative_pipeline as ip, literature, llm, service
from paperclaw.config import LLMSettings
from paperclaw.server.store import Store

# Reuse the iterative-pipeline mock LLM so a full run is hermetic (bare module name —
# pytest puts the tests/ dir on sys.path; there is no tests package).
from test_iterative_pipeline import PINNED_SPEC, fake_chat, _make_stream, fake_search


_BENCH_MD = (
    "# My Benchmark\n\n"
    "## Protocol\n- Datasets: D1\n- Metric: MSE (PRIMARY)\n\n"
    "## Published results\n| Method | D1 (MSE) | cite |\n| Foo | 0.10 | foo2020bar |\n\n"
    "## References\n@article{foo2020bar,\n  title={Foo Bar},\n  author={Foo, A},\n  year={2020}\n}\n"
)

# Same shape, one DUP key (foo2020bar) + one NEW key (bar2021baz) — for the merge dedup test.
_BENCH_MD2 = (
    "# Other\n\n## References\n"
    "@article{foo2020bar,\n  title={Foo Bar},\n  author={Foo, A},\n  year={2020}\n}\n"
    "@article{bar2021baz,\n  title={Bar Baz},\n  author={Bar, B},\n  year={2021}\n}\n"
)


# ── library (disk I/O) ──────────────────────────────────────────────────────

def test_library_empty_by_default_then_domain_overrides_global(tmp_path):
    home = tmp_path / "home"
    assert benchmarks.list_benchmarks(home) == []                        # NO built-in default (empty)

    dom = tmp_path / "dom"
    benchmarks.save_benchmark(home, None, "shared", "# Global Shared\n")
    benchmarks.save_benchmark(home, dom, "shared", "# Domain Shared\n")   # same name, domain scope
    listed = benchmarks.list_benchmarks(home, dom)
    shared = [b for b in listed if b["name"] == "shared"]
    assert len(shared) == 1 and shared[0]["scope"] == "domain"           # domain wins
    assert benchmarks.get_benchmark(home, dom, "shared").startswith("# Domain Shared")


def test_save_sanitizes_name(tmp_path):
    assert benchmarks.save_benchmark(tmp_path, None, "My Bench!", "# x\n") == "my-bench"
    assert benchmarks.save_benchmark(tmp_path, None, "  ", "# x\n") is None


# ── ref.bib merge ───────────────────────────────────────────────────────────

def test_merge_into_refbib_dedups(tmp_path):
    ref = tmp_path / "ref.bib"
    assert benchmarks.merge_into_refbib(ref, _BENCH_MD) == 1
    assert "foo2020bar" in ref.read_text()
    assert benchmarks.merge_into_refbib(ref, _BENCH_MD) == 0           # already present → no dupe
    assert benchmarks.merge_into_refbib(ref, _BENCH_MD2) == 1          # one new key; foo2020bar skipped
    assert "bar2021baz" in ref.read_text()


def test_references_block_extracts_bibtex():
    block = benchmarks.references_block(_BENCH_MD)
    assert block.startswith("@article{foo2020bar")


# ── service resolve + note ──────────────────────────────────────────────────

def test_resolve_benchmark_name_and_domain_default(tmp_path):
    store = Store(tmp_path)
    d = store.add_domain("My Domain")
    assert service.resolve_benchmark(store, d.id, None) is None          # empty by default — no LTSF
    service.save_benchmark(store, "shared", "# Global\n")                # global, resolvable by name
    assert service.resolve_benchmark(store, None, "shared").startswith("# Global")
    assert service.resolve_benchmark(store, d.id, None) is None          # a global one is NOT a domain default
    service.save_benchmark(store, "mine", _BENCH_MD, domain_id=d.id)     # the SOLE domain-scoped one
    assert service.resolve_benchmark(store, d.id, None).startswith("# My Benchmark")
    assert service.resolve_benchmark(store, d.id, "nope") is None        # unknown name → None


def test_idea_benchmark_library_idea_domain_global(tmp_path):
    """The idea Benchmark view lists templates from idea scope (e.g. /setup_benchmark in
    the idea chat) + its domain + global, and can view each by name — so a benchmark just
    created shows up even if the domain has several."""
    store = Store(tmp_path)
    d = store.add_domain("My Domain")
    idea = store.add_idea("Idea")
    store.put_spec(idea.id, "# Idea\n\nBuilds on My Domain.\n")        # pins the domain by name
    service.save_benchmark(store, "glob", "# Global B\n")
    service.save_benchmark(store, "domb", "# Domain B\n", domain_id=d.id)
    (store.idea_path(idea.id) / "benchmarks").mkdir(parents=True, exist_ok=True)
    (store.idea_path(idea.id) / "benchmarks" / "ideab.md").write_text("# Idea B\n", encoding="utf-8")

    names = {b["name"]: b["scope"] for b in service.list_idea_benchmarks(store, idea.id)}
    assert names == {"ideab": "idea", "domb": "domain", "glob": "global"}   # all three scopes
    assert service.get_idea_benchmark(store, idea.id, "ideab")["content"].startswith("# Idea B")
    assert service.get_idea_benchmark(store, idea.id, "domb")["content"].startswith("# Domain B")


def test_idea_benchmark_note_reads_persisted_file(tmp_path):
    store = Store(tmp_path)
    idea = store.add_idea("Idea")
    assert service.idea_benchmark_note(store, idea.id) == ""            # none yet
    (store.idea_path(idea.id) / ".benchmark.md").write_text(_BENCH_MD, encoding="utf-8")
    note = service.idea_benchmark_note(store, idea.id)
    assert "FIXED protocol" in note and "My Benchmark" in note          # wrapped in BENCHMARK_NOTE


# ── route ───────────────────────────────────────────────────────────────────

def test_benchmark_route_list_get_save(tmp_path):
    from fastapi.testclient import TestClient
    from paperclaw.server.app import create_app
    c = TestClient(create_app(home=tmp_path))
    assert c.get("/api/benchmarks").json() == []                         # empty by default
    assert c.post("/api/benchmarks", json={"name": "B 1", "content": "# B\n"}).json() == {"name": "b-1"}
    assert "b-1" in [b["name"] for b in c.get("/api/benchmarks").json()]
    assert c.get("/api/benchmarks/b-1").json()["content"].startswith("# B")
    assert c.get("/api/benchmarks/nope").status_code == 404


# ── deep pipeline injection (the run is reframed around the cited table) ──────

def test_pipeline_injects_benchmark_and_merges_refs(tmp_path, monkeypatch):
    """With a benchmark selected, the iterative run persists .benchmark.md, merges the
    benchmark's cited BibTeX into the idea's ref.bib, and announces it."""
    monkeypatch.setattr(llm, "chat", fake_chat)
    monkeypatch.setattr(llm, "stream_chat_thinking", _make_stream(True))
    monkeypatch.setattr(literature, "search_recent_papers", fake_search)
    store = Store(tmp_path)
    idea = store.add_idea("Test idea")
    store.put_spec(idea.id, PINNED_SPEC)
    service.save_benchmark(store, "tsf", _BENCH_MD)                      # a user-created benchmark

    events = []
    async def go():
        async for ev in ip.stream_iterative_research_events(
                store, LLMSettings(), idea.id, max_hypotheses=1, benchmark="tsf"):
            events.append(ev)
    asyncio.run(go())

    ipath = store.idea_path(idea.id)
    assert (ipath / ".benchmark.md").is_file()                          # persisted for the job
    assert "foo2020bar" in (ipath / "ref.bib").read_text()              # cited baselines merged
    assert any("benchmark template in force" in e.get("text", "") for e in events if e.get("type") == "delta")
    # the experiment context (rebuilt from disk) now carries the benchmark note
    assert "FIXED protocol" in service.idea_benchmark_note(store, idea.id)
