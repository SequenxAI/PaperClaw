"""Tests for the domain reference-codebase fetcher (httpx mocked — no network)."""

import io
import tarfile

import httpx
import pytest

from paperclaw import codebase


def _make_tarball(files: dict[str, bytes], top: str = "owner-repo-abc123") -> bytes:
    """A GitHub-style .tar.gz: one wrapping top-level dir, files inside it."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel, data in files.items():
            info = tarfile.TarInfo(name=f"{top}/{rel}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class _Resp:
    def __init__(self, content: bytes):
        self.content = content
    def raise_for_status(self):
        pass


def test_resolve_tarball_url():
    owner, repo, ref, url = codebase.resolve_tarball_url("https://github.com/psf/requests")
    assert (owner, repo, ref) == ("psf", "requests", "")
    assert url == "https://api.github.com/repos/psf/requests/tarball/"
    _, _, ref2, url2 = codebase.resolve_tarball_url("https://github.com/psf/requests/tree/main")
    assert ref2 == "main" and url2.endswith("/tarball/main")
    # .git suffix tolerated
    assert codebase.resolve_tarball_url("https://github.com/a/b.git")[1] == "b"


def test_resolve_tarball_url_rejects_non_github():
    with pytest.raises(codebase.CodebaseError):
        codebase.resolve_tarball_url("https://gitlab.com/a/b")
    with pytest.raises(codebase.CodebaseError):
        codebase.resolve_tarball_url("not a url")


def test_download_codebase_extracts(tmp_path, monkeypatch):
    tar = _make_tarball({"model.py": b"import torch\n", "data/loader.py": b"x=1\n",
                         ".git/config": b"secret"})
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _Resp(tar))
    dest = tmp_path / "codebase"
    info = codebase.download_codebase("https://github.com/o/r", dest)
    assert info["fileCount"] == 2  # .git/ skipped
    assert (dest / "model.py").read_text() == "import torch\n"
    assert (dest / "data" / "loader.py").is_file()
    assert not (dest / ".git").exists()  # the wrapping dir is stripped, .git skipped


def test_download_codebase_empty_is_error(tmp_path, monkeypatch):
    tar = _make_tarball({".git/config": b"x"})  # nothing but skipped files
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _Resp(tar))
    with pytest.raises(codebase.CodebaseError):
        codebase.download_codebase("https://github.com/o/r", tmp_path / "cb")


def test_download_codebase_clears_old(tmp_path, monkeypatch):
    dest = tmp_path / "cb"
    dest.mkdir()
    (dest / "stale.py").write_text("old")
    tar = _make_tarball({"new.py": b"new"})
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _Resp(tar))
    codebase.download_codebase("https://github.com/o/r", dest)
    assert not (dest / "stale.py").exists() and (dest / "new.py").is_file()


def test_runner_links_codebase_and_notes_it(tmp_path, monkeypatch):
    """_select_experiment_runner links the codebase as ./reference + augments the
    task context so the experiment agent reuses it."""
    from paperclaw import agents, iterative_pipeline as ip
    from paperclaw.server.models import RunConfig

    cb = tmp_path / "codebase"
    cb.mkdir()
    (cb / "model.py").write_text("net")
    out = tmp_path / "hyp"
    out.mkdir()

    captured = {}

    def fake_agentic(settings, idea_ctx, plan, out_dir, run_cfg, target=None):
        captured["ctx"] = idea_ctx
        captured["target"] = target
        yield {"type": "done"}

    monkeypatch.setattr(agents, "run_agentic_experiment", fake_agentic)

    list(ip._select_experiment_runner(None, "IDEA.md: x", "plan", out,
                                      RunConfig(experimentMode="executed"), None, cb))
    assert (out / "reference").exists()                 # linked in
    assert "./reference" in captured["ctx"]             # told to reuse it
    assert captured["target"] is None                   # local executed → no remote
