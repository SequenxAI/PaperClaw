"""Iterative hypothesis-loop research pipeline.

A second, resumable pipeline (the classic 4-phase one in ``research_pipeline.py``
is preserved). It implements:

    for k = 1, 2, ...:
        propose hypothesis k       (HYPOTHESIS_PROPOSE_SYSTEM)
        experiment: test it        (simulated EXPERIMENT_SYSTEM | executed runner)
        reflect, advance           (REFLECT_SYSTEM → "Enough For Paper? yes/no")
        if enough (or k == max):  break
    write paper:
        select strongest subset + write LaTeX   (LATEX_PAPER_SYSTEM)
        compile with pdflatex (agentic fix loop) and read the page count
        check page-limit compliance              (from the compile log)

Per-round artifacts in the idea folder make it resumable: ``.hyp_{k}.md`` /
``.exp_{k}.md`` / ``.reflect_{k}.md`` (a round is complete once its reflection
exists), plus ``paper.tex`` / ``paper.pdf``. Executed experiments write under
``experiments/{k}/``.

Events yielded (SSE dicts):
  {"type": "round",       "round": k}
  {"type": "phase",       "phase": str, "label": str}
  {"type": "delta",       "text": str}
  {"type": "phase_done",  "phase": str, "content": str}
  {"type": "round_done",  "round": k, "enough": bool}
  {"type": "compile",     "ok": bool, "attempt": int}
  {"type": "page_check",  "pages": int|None, "limit": int, "compliant": bool}
  {"type": "paper_ready", "download_url": str}
  {"type": "done"} | {"type": "error", "message": str} | {"type": "needs_domain", "message": str}
"""

import asyncio
import os
import re
import shutil
import subprocess
import tempfile
from datetime import date
from pathlib import Path
from typing import AsyncIterator

from paperclaw import agents, experiments, llm
from paperclaw.config import LLMSettings
from paperclaw.prompts.pipeline import (
    CODEBASE_NOTE,
    EXPERIMENT_SYSTEM,
    HYPOTHESIS_PLAN_SYSTEM,
    HYPOTHESIS_REPORT_SYSTEM,
    LATEX_FIX_SYSTEM,
    LATEX_PAGE_FILL_NOTE,
    LATEX_PAPER_SYSTEM,
    LATEX_REFS_NOTE,
    LATEX_SHORTEN_SYSTEM,
    LATEX_VENUE_TEMPLATE_NOTE,
    PAPER_RIGOR_RULES,
)
from paperclaw.server.store import Store

_BLOCK_RE = re.compile(r"```([\w-]+)\s*\n(.*?)```", re.DOTALL)
_FENCE_STRIP_RE = re.compile(r"^```[\w-]*\s*\n?|```\s*$", re.MULTILINE)
_ENOUGH_RE = re.compile(r"##\s*Enough For Paper\s*\n+\s*([A-Za-z]+)", re.IGNORECASE)
_PAGES_RE = re.compile(r"Output written on [^\(]*\((\d+) pages?", re.IGNORECASE)
_TARGET_VENUE_RE = re.compile(r"##\s*Target Venue\s*\n+(.+)", re.IGNORECASE)

_COMPILE_TIMEOUT = 120
_MAX_COMPILE_FIXES = 3


def _extract_block(text: str, tag: str) -> str:
    for m in _BLOCK_RE.finditer(text):
        if m.group(1).lower() == tag.lower():
            return m.group(2).strip()
    stripped = _FENCE_STRIP_RE.sub("", text).strip()
    return stripped or text.strip()


def _parse_enough(reflection: str) -> bool:
    m = _ENOUGH_RE.search(reflection)
    return bool(m and m.group(1).strip().lower().startswith("y"))


_PRIMARY_VENUE_RE = re.compile(r"\*\*Primary target venue:\*\*\s*(.+)", re.IGNORECASE)


def _resolve_venue(store: Store, spec: str) -> str:
    """Idea's ## Target Venue → else the pinned domain's primary target venue."""
    m = _TARGET_VENUE_RE.search(spec)
    if m:
        line = m.group(1).strip().split("\n")[0].strip()
        if line and "tbd" not in line.lower():
            return line
    # Fall back: find the pinned domain (by name appearing in the spec) and read it.
    low = spec.lower()
    for domain in store.list_domains():
        if domain.name and domain.name.lower() in low:
            dm = _PRIMARY_VENUE_RE.search(store.get_domain_spec(domain.id) or "")
            if dm and "tbd" not in dm.group(1).lower():
                return dm.group(1).strip()
    return "a top-tier conference in this field"


def _venue_skeleton(idea_path: Path | None) -> str | None:
    """The uploaded venue template's skeleton ``.tex`` (whose preamble the paper keeps
    verbatim), or None if the idea has no venue template with a style/class file. Lets
    an auto run BASE the paper on an uploaded venue template, like the chat path does."""
    if idea_path is None:
        return None
    venue = idea_path / "venue"
    if not venue.is_dir():
        return None
    if not (any(venue.rglob("*.sty")) or any(venue.rglob("*.cls"))):
        return None  # no style/class → not a real venue template
    texs = sorted(venue.rglob("*.tex"))
    if not texs:
        return None
    pref = [t for t in texs if re.search(r"template|skeleton|submission|sample|main", t.name, re.I)]
    chosen = (pref or texs)[0]
    try:
        return chosen.read_text(encoding="utf-8", errors="ignore")[:8000]
    except OSError:
        return None


# ── Artifact helpers ──────────────────────────────────────────────────────────

def _load(idea_path: Path | None, name: str) -> str | None:
    if idea_path is None:
        return None
    p = idea_path / name
    return p.read_text(encoding="utf-8") if p.is_file() else None


def _save(idea_path: Path | None, name: str, content: str) -> None:
    if idea_path is not None:
        (idea_path / name).write_text(content, encoding="utf-8")


def _read(path: Path) -> str | None:
    return path.read_text(encoding="utf-8") if path.is_file() else None


def _clear_iterative(idea_path: Path | None) -> None:
    if idea_path is None:
        return
    # legacy flat artifacts (older runs)
    for pat in (".hyp_*.md", ".exp_*.md", ".reflect_*.md"):
        for p in idea_path.glob(pat):
            p.unlink()
    for name in ("paper.tex", "paper.pdf", "paper.aux", "paper.log", "paper.out"):
        (idea_path / name).unlink(missing_ok=True)
    for sub in ("experiments", "hypotheses", "figures"):  # per-hypothesis dirs (#6) + figures
        d = idea_path / sub
        if d.is_dir():
            shutil.rmtree(d)


def _list_figures(idea_path: Path | None) -> list[str]:
    if idea_path is None:
        return []
    figs = (list(idea_path.glob("figures/*.png"))            # conceptual
            + list(idea_path.glob("hypotheses/*/*.png"))     # per-hypothesis data figures
            + list(idea_path.glob("experiments/*/*.png")))   # legacy
    return sorted(str(p.relative_to(idea_path)) for p in figs)


def _format_node(node: dict) -> str:
    """Render a hypothesis-map node (root + sub-hypotheses) as plan-step input."""
    parts = [f"# {node.get('statement', '').strip()}"]
    if node.get("rationale"):
        parts.append(f"Rationale: {node['rationale']}")
    for child in node.get("children") or []:
        line = f"- {child.get('statement', '').strip()}"
        if child.get("test"):
            line += f"  (test: {child['test']})"
        parts.append(line)
    return "\n".join(parts)


_VERDICT_RE = re.compile(r"##\s*Verdict\s*\n+\s*([A-Za-z]+)", re.IGNORECASE)


def _verdict_to_status(reflection: str) -> str:
    m = _VERDICT_RE.search(reflection or "")
    word = (m.group(1).lower() if m else "")
    # "PARTIALLY SUPPORTED" — the first captured token is "partially"; treat a
    # partially-supported hypothesis as a positive (validated) node, not a failure.
    if word.startswith(("support", "partial")):
        return "supported"
    if word.startswith("refut"):
        return "refuted"
    return "inconclusive"


def _next_untested(data: dict) -> dict | None:
    """First node (DFS) whose status is still 'untested' — drives the loop and
    lets newly-expanded children get picked up. Processed/blocked nodes are skipped."""
    def walk(nodes: list) -> dict | None:
        for n in nodes:
            if n.get("status", "untested") == "untested":
                return n
            found = walk(n.get("children") or [])
            if found:
                return found
        return None
    return walk(data.get("nodes", []))


def _count_supported(data: dict) -> int:
    """Total SUPPORTED nodes in the map across ALL runs — so a RESUME counts
    hypotheses already proven in earlier sessions (drives both the auto-mode stop
    condition and the status display, not just this session's new positives)."""
    def walk(nodes: list) -> int:
        return sum((1 if n.get("status") == "supported" else 0) + walk(n.get("children") or [])
                   for n in nodes)
    return walk(data.get("nodes", []))


def _collect_paper_rounds(idea_path: "Path | None", store: Store, idea_id: str,
                          session_rounds: list[dict]) -> list[dict]:
    """All tested-hypothesis rounds for the paper, rebuilt from ON-DISK artifacts so a
    RESUME includes hypotheses proven in EARLIER sessions — not just this session's
    *session_rounds*. (Without this, a resume that stops on already-met positives writes
    the paper from an empty list and omits every prior result that exists on disk.)

    Walks the map in DFS order; a node counts as tested once it has a report.md or
    experiment.md. Recovers each round's enough/blocked flags from the in-session round
    when present. Falls back to *session_rounds* if there's no idea dir or nothing on
    disk."""
    if idea_path is None:
        return session_rounds
    by_id = {r.get("id"): r for r in session_rounds}
    out: list[dict] = []

    def walk(nodes: list) -> None:
        for n in nodes:
            hid = str(n.get("id"))
            hdir = idea_path / "hypotheses" / hid
            report = _read(hdir / "report.md")
            exp = _read(hdir / "experiment.md")
            if report or exp:  # tested at some point (this run or an earlier one)
                sess = by_id.get(n.get("id"), {})
                out.append({
                    "k": len(out) + 1, "id": n.get("id"), "hypothesis": _format_node(n),
                    "plan": _read(hdir / "plan.md") or sess.get("plan", ""),
                    "experiment": exp or sess.get("experiment", ""),
                    "report": report or sess.get("report", ""),
                    "enough": sess.get("enough", False),
                    "blocked": sess.get("blocked", False),
                })
            walk(n.get("children") or [])

    walk((store.get_hypothesis_map(idea_id) or {}).get("nodes", []))
    return out or session_rounds


def _resolve_ssh_target(store: Store, run_cfg):
    """The SSH target for ssh-mode runs: by id, else the first configured remote."""
    from paperclaw.server.models import SSHTarget
    targets = (store.get_hardware_state() or {}).get("sshTargets", [])
    if not targets:
        return None
    if run_cfg.ssh_target_id:
        for t in targets:
            if t.get("id") == run_cfg.ssh_target_id:
                return SSHTarget.model_validate(t)
    return SSHTarget.model_validate(targets[0])


def _resolve_domain_codebase(store: Store, idea_id: str | None, spec: str | None) -> Path | None:
    """The reference-codebase dir of a CONNECTED domain that actually HAS one, or None.
    Honors EXPLICIT connections (`.domains.json`) first; with none recorded, name-matches a
    domain by name in the spec (same precedence as `service.resolve_domain_ids_for_idea`).
    Returns the first connected domain that has a downloaded codebase — so a connected domain
    without a repo doesn't shadow one that has it."""
    ids = store.get_idea_domain_ids(idea_id) if idea_id else None
    if ids is None:  # never explicitly connected → fall back to name-matching the spec
        low = (spec or "").lower()
        ids = [d.id for d in store.list_domains() if d.name and d.name.lower() in low]
    for did in ids:
        cb = store.domain_codebase_path(did)  # None unless the dir exists AND is non-empty
        if cb is not None:
            return cb
    return None


def _link_codebase(out_dir: Path, codebase_path: Path) -> None:
    """Expose the domain codebase to the experiment as ./reference (symlink, or a
    copy if symlinks aren't supported)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    link = out_dir / "reference"
    if link.exists() or link.is_symlink():
        return
    try:
        link.symlink_to(Path(codebase_path).resolve(), target_is_directory=True)
    except OSError:
        try:
            shutil.copytree(codebase_path, link)
        except OSError:
            pass


async def _fallback_to_executed(settings, idea_ctx, plan, out_dir, run_cfg, note):
    """Run the in-process agentic coding agent, prefixed with a status note (used when
    `cli` mode is selected but its CLI binary isn't installed)."""
    yield {"type": "status", "text": note}
    async for ev in agents.run_agentic_experiment(settings, idea_ctx, plan, out_dir, run_cfg):
        yield ev


def _select_experiment_runner(settings, idea_ctx, plan, out_dir, run_cfg, target,
                              codebase_path=None):
    """Pick the experiment runner for a non-simulated run config:
    ``cli`` → external headless agent CLI; ``ssh`` (target resolved) → the agentic
    coding agent driving bash ON THE REMOTE; otherwise (``executed``) → that same
    agentic coding agent locally.

    **Fallback:** if ``cli`` is selected but its CLI binary (e.g. ``claude``) isn't on
    PATH, fall back to the in-process agentic runner (which drives our configured LLM)
    so the experiment still runs instead of failing.

    When *codebase_path* is set, the domain's reference codebase is linked into the
    experiment dir as ``./reference`` and the task context is told to reuse it."""
    if codebase_path is not None:
        _link_codebase(out_dir, codebase_path)
        idea_ctx = idea_ctx + CODEBASE_NOTE
    if run_cfg.experiment_mode == "cli":
        if agents.agent_command_available(run_cfg):
            return agents.run_cli_agent(settings, idea_ctx, plan, out_dir, run_cfg)
        binary = (run_cfg.agent_command or "the configured CLI").split()[0] if run_cfg.agent_command else "claude"
        return _fallback_to_executed(
            settings, idea_ctx, plan, out_dir, run_cfg,
            f"\n⚠️ `{binary}` CLI not found — falling back to the in-process coding agent "
            "(uses the configured LLM).\n")
    # ssh mode (target resolved) runs the SAME agentic loop with a remote executor; local
    # `executed` passes target=None. The SSH remote needs only its connection (no interpreter).
    return agents.run_agentic_experiment(settings, idea_ctx, plan, out_dir, run_cfg, target=target)


def _set_node_status(store: Store, idea_id: str, hid: str, status: str) -> None:
    data = store.get_hypothesis_map(idea_id)
    if not data:
        return

    def walk(nodes: list) -> bool:
        for n in nodes:
            if n.get("id") == hid:
                n["status"] = status
                return True
            if walk(n.get("children") or []):
                return True
        return False

    if walk(data.get("nodes", [])):
        store.put_hypothesis_map(idea_id, data)


def _format_prior(rounds: list[dict]) -> str:
    if not rounds:
        return "\n\n(No prior hypotheses yet — this is the first.)"
    parts = ["\n\n## Prior hypotheses tested"]
    for r in rounds:
        if r.get("blocked"):
            parts.append(f"\n### Round {r['k']} (BLOCKED — infeasible)\n{r['hypothesis']}")
            continue
        parts.append(
            f"\n### Round {r['k']}\n{r['hypothesis']}\n\nResults:\n{r['experiment']}\n\nReport:\n{r.get('report', '')}"
        )
    return "\n".join(parts)


_FEASIBILITY_RE = re.compile(r"##\s*Feasibility\s*\n+\s*([A-Za-z]+)", re.IGNORECASE)


def _is_infeasible(plan: str) -> bool:
    m = _FEASIBILITY_RE.search(plan or "")
    return bool(m and m.group(1).strip().lower().startswith("infeasible"))


# ── LaTeX compilation ─────────────────────────────────────────────────────────

def _count_pages(pdf_path: Path, log: str) -> int | None:
    """Page count, in order of reliability: pdflatex log → pypdf → PDF bytes."""
    m = _PAGES_RE.search(log)  # pdflatex prints "Output written on … (N pages"
    if m:
        return int(m.group(1))
    try:
        from pypdf import PdfReader  # optional dependency; reliable when present
        return len(PdfReader(str(pdf_path)).pages)
    except Exception:
        pass
    try:  # last resort: parse the PDF page tree directly (works for tectonic output)
        data = pdf_path.read_bytes()
        counts = [int(x) for x in re.findall(rb"/Count\s+(\d+)", data)]
        if counts:
            return max(counts)
        pages = len(re.findall(rb"/Type\s*/Page[^s]", data))
        return pages or None
    except Exception:
        return None


def _ocr_check(pdf: Path) -> dict | None:
    """Optional OCR sanity check of the rendered PDF (graceful no-op).

    Needs `pdftoppm` + `tesseract`; returns {"chars": N} for the first rendered
    page or None when the tools aren't installed / anything fails. Used to confirm
    the compiled paper actually renders readable text, beyond the page count.
    """
    if not pdf.is_file() or not (shutil.which("pdftoppm") and shutil.which("tesseract")):
        return None
    try:
        d = Path(tempfile.mkdtemp())
        subprocess.run(["pdftoppm", "-png", "-r", "100", "-f", "1", "-l", "1", str(pdf), str(d / "p")],
                       capture_output=True, timeout=60)
        imgs = list(d.glob("*.png"))
        if not imgs:
            return None
        r = subprocess.run(["tesseract", str(imgs[0]), "stdout"],
                           capture_output=True, text=True, timeout=60)
        return {"chars": len((r.stdout or "").strip())}
    except Exception:
        return None


_PAPER_FILE_RE = re.compile(r"paper(?:_v\d+)?\.(tex|pdf|md)$")
_TEX_ASSET_EXT = {".sty", ".cls", ".bst", ".bib", ".png", ".jpg", ".jpeg", ".pdf", ".eps"}

_LATEXMK_TIMEOUT = 300  # latexmk runs the engine + bibtex over several passes

# Pick the LaTeX engine the way Overleaf does: an explicit `% !TEX program = …`
# magic comment wins; else XeLaTeX/LuaLaTeX when the source needs fontspec /
# unicode-math (pdfLaTeX can't drive those); else pdfLaTeX.
_TEX_PROGRAM_RE = re.compile(
    r"%\s*!TEX\s+(?:TS-)?program\s*=\s*(pdflatex|xelatex|lualatex)", re.IGNORECASE)
_FONTSPEC_RE = re.compile(
    r"\\usepackage(?:\[[^\]]*\])?\{[^}]*\b(?:fontspec|unicode-math)\b[^}]*\}")
_LATEXMK_FLAG = {"pdflatex": "-pdf", "xelatex": "-pdfxe", "lualatex": "-pdflua"}


def _bindir_works(bindir: Path) -> bool:
    """True if *bindir* holds latexmk + a kpsewhich that resolves latex.ltx — i.e.
    a real TeX Live with a texmf tree, not a binaries-only stub."""
    lk, kp = bindir / "latexmk", bindir / "kpsewhich"
    if not (lk.exists() and kp.exists()):
        return False
    try:
        r = subprocess.run([str(kp), "latex.ltx"],
                           capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    return bool(r.stdout.strip())


def _texlive_bindir() -> str | None:
    """Path to a bin dir holding a WORKING TeX Live (latexmk + a kpsewhich that
    resolves latex.ltx), or None. Checks PATH first, then common real-install
    locations — so a real `~/texlive` is found even when a binaries-only stub
    (e.g. a conda ``texlive-core`` with no texmf tree) shadows it on PATH."""
    candidates: list[Path] = []
    onpath = shutil.which("latexmk")
    if onpath:
        candidates.append(Path(onpath).parent)
    roots = [Path.home() / "texlive", *sorted(Path.home().glob("texlive*"), reverse=True)]
    roots += sorted(Path("/usr/local/texlive").glob("*"), reverse=True)
    for root in roots:
        binroot = root / "bin"
        if binroot.is_dir():
            candidates.extend(sorted(binroot.glob("*"), reverse=True))
    seen: set[str] = set()
    for d in candidates:
        key = str(d)
        if key in seen:
            continue
        seen.add(key)
        if _bindir_works(d):
            return key
    return None


def _pick_latex_engine(tex_source: str, bindir: str | None = None) -> str:
    """pdflatex | xelatex | lualatex for *tex_source*, honouring a `% !TEX program`
    magic comment, then falling back to lualatex for fontspec/unicode-math, else
    pdflatex. Downgrades to pdflatex if the chosen engine isn't installed (looked
    up in *bindir* when given, else on PATH)."""
    m = _TEX_PROGRAM_RE.search(tex_source)
    engine = m.group(1).lower() if m else (
        "lualatex" if _FONTSPEC_RE.search(tex_source) else "pdflatex")
    present = (Path(bindir) / engine).exists() if bindir else bool(shutil.which(engine))
    return engine if present else "pdflatex"


_MISSING_TEX_FILE_RE = re.compile(r"File [`']([^'`]+\.(?:sty|cls|bst))' not found")
_TLMGR_PKG_RE = re.compile(r"^([A-Za-z0-9@._+-]+):\s*$", re.MULTILINE)
_TLMGR_SEARCH_TIMEOUT = 60
_TLMGR_INSTALL_TIMEOUT = 240


def _tlmgr_install_missing(log: str, tlmgr: str, env: dict, skip: set[str]) -> list[str]:
    """Parse a compile *log* for `File 'X.sty' not found`, resolve each to its
    TeX Live package via ``tlmgr search --file`` and install it. Returns the
    package names newly installed. This is what gives latexmk tectonic-like
    self-sufficiency: missing CTAN packages are fetched on demand."""
    files: list[str] = []
    for f in _MISSING_TEX_FILE_RE.findall(log):
        if f not in files:
            files.append(f)
    installed: list[str] = []
    for fname in files[:4]:  # bound the per-compile work
        try:
            s = subprocess.run([tlmgr, "search", "--global", "--file", "/" + fname],
                               env=env, capture_output=True, text=True,
                               timeout=_TLMGR_SEARCH_TIMEOUT)
        except (OSError, subprocess.SubprocessError):
            continue
        m = _TLMGR_PKG_RE.search(s.stdout or "")
        if not m:
            continue
        pkg = m.group(1)
        if pkg in skip or pkg.startswith("00texlive"):
            continue
        try:
            r = subprocess.run([tlmgr, "install", pkg], env=env, capture_output=True,
                               text=True, timeout=_TLMGR_INSTALL_TIMEOUT)
        except (OSError, subprocess.SubprocessError):
            continue
        out = (r.stdout or "") + (r.stderr or "")
        if r.returncode == 0 or "already present" in out:
            installed.append(pkg)
    return installed


def latex_status() -> dict:
    """Diagnostic snapshot of the LaTeX toolchain (for the `doctor` command).

    Returns the discovered TeX Live bin dir (or None), whether tectonic / pdflatex
    are present as fallbacks, and which engines resolve — looked up inside the real
    TeX Live bin dir when one was found, else on PATH."""
    tl = _texlive_bindir()
    engines: dict[str, str | None] = {}
    for e in ("pdflatex", "xelatex", "lualatex", "bibtex", "biber"):
        if tl:
            p = Path(tl) / e
            engines[e] = str(p) if p.exists() else None
        else:
            engines[e] = shutil.which(e)
    return {
        "texlive_bindir": tl,
        "tectonic": shutil.which("tectonic"),
        "pdflatex": shutil.which("pdflatex"),
        "engines": engines,
    }


def compile_tex(work_dir: Path, tex_name: str = "paper.tex") -> tuple[bool, int | None, str]:
    """Compile an EXISTING `.tex` file in *work_dir* into the matching `.pdf`;
    return (ok, page_count, log_tail).

    Compiles in a self-contained TEMP dir with the support assets copied in — the
    `.tex`, every file from a `venue/` template (`.sty`/`.cls`/`.bst`/`.bib` +
    figures), and the workspace's own figures — so `\\usepackage{aaai2026}`, the
    bibliography style, and `\\includegraphics` all resolve next to the source
    (no engine searches subdirs). The `venue/` tree is ALSO copied verbatim as a
    `venue/` subdir, so a path-prefixed `\\usepackage{venue/aaai2026}` resolves too.

    Engine order (Overleaf-matching first):
      1. ``latexmk`` on a real TeX Live — runs pdfLaTeX/XeLaTeX/LuaLaTeX (see
         :func:`_pick_latex_engine`) + bibtex/biber over as many passes as needed,
         exactly like Overleaf. This is the preferred path.
      2. ``tectonic`` — self-contained fallback when no full TeX Live is installed.
      3. raw ``pdflatex`` — last resort.
    A real-TeX-Live compile returns latexmk's own log (the real, fixable error);
    we only drop to tectonic when TeX Live is unusable, never to mask an error.
    """
    work_dir = Path(work_dir)
    tex_path = work_dir / tex_name
    if not tex_path.is_file():
        return False, None, f"{tex_name} not found in the workspace."
    # Compile by BASENAME in the temp root so a SUBDIR-qualified name (e.g. the agent
    # compiling a standalone figure `figures/foo.tex`) works instead of crashing on a
    # missing dest subdir; the output PDF lands next to the source (preserving subdir).
    run_name = tex_path.name
    out_pdf = tex_path.with_suffix(".pdf")
    out_pdf.unlink(missing_ok=True)  # don't let a stale PDF mask a failed compile

    # TeX Live is ALWAYS the default engine; tectonic is only a fallback when no
    # real TeX Live exists — so we don't even probe for it while TeX Live is present
    # (the `and` short-circuits when tl_bindir is truthy).
    tl_bindir = _texlive_bindir()  # a real TeX Live (may be off-PATH, e.g. ~/texlive)
    if not tl_bindir and not shutil.which("tectonic") and not shutil.which("pdflatex"):
        return False, None, "No LaTeX engine (latexmk/tectonic/pdflatex) is installed."

    cdir = Path(tempfile.mkdtemp(prefix="paperclaw_tex_"))
    try:
        shutil.copy(tex_path, cdir / run_name)  # basename → temp ROOT (never a missing subdir)
    except OSError as exc:  # never let staging crash the caller — return a clean error
        shutil.rmtree(cdir, ignore_errors=True)
        return False, None, f"could not stage {tex_name} for compile: {exc}"
    for src in (work_dir / "venue", work_dir):  # venue assets first, then root figures
        if src.is_dir():
            for f in src.iterdir():
                if (f.is_file() and f.suffix.lower() in _TEX_ASSET_EXT
                        and not _PAPER_FILE_RE.fullmatch(f.name)):  # skip other paper versions
                    dst = cdir / f.name
                    if not dst.exists():
                        try:
                            shutil.copy(f, dst)
                        except OSError:
                            pass
    # Also keep the venue/ tree verbatim so a path-prefixed
    # `\usepackage{venue/aaai2026}` / `\includegraphics{venue/figure1}` resolves.
    venue_dir = work_dir / "venue"
    if venue_dir.is_dir():
        try:
            shutil.copytree(venue_dir, cdir / "venue", dirs_exist_ok=True)
        except OSError:
            pass
    # Copy the whole figures/ tree (TikZ/pgf `.tex` figure SOURCES + their `.pdf`/`.png`
    # outputs), preserving structure, so `\input{figures/foo.tex}` and
    # `\includegraphics{figures/foo.png}` both resolve (the per-figure copy below only
    # catches PNGs).
    fig_dir = work_dir / "figures"
    if fig_dir.is_dir():
        try:
            shutil.copytree(fig_dir, cdir / "figures", dirs_exist_ok=True)
        except OSError:
            pass
    # Per-hypothesis / conceptual result figures live in SUBDIRS (figures/,
    # hypotheses/<id>/, experiments/<k>/). Copy each one in preserving its relative
    # path, so the agent can \includegraphics{hypotheses/H1/loss.png} the REAL
    # experiment plots (the top-level copy above only catches root-level figures).
    for rel in _list_figures(work_dir):
        src_f, dst_f = work_dir / rel, cdir / rel
        if src_f.is_file() and not dst_f.exists():
            try:
                dst_f.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(src_f, dst_f)
            except OSError:
                pass
    cdir_pdf = cdir / f"{tex_path.stem}.pdf"

    def _finish(log: str) -> tuple[bool, int | None, str]:
        ok = cdir_pdf.is_file()
        pages = _count_pages(cdir_pdf, log) if ok else None
        if ok:
            shutil.move(str(cdir_pdf), str(out_pdf))
        shutil.rmtree(cdir, ignore_errors=True)
        return ok, pages, log[-4000:]

    if tl_bindir:
        # The Overleaf toolchain: latexmk drives pdfLaTeX/XeLaTeX/LuaLaTeX + bibtex
        # over as many passes as the document needs. Its log is the real, fixable
        # error — never fall through to tectonic from here. Run the discovered TeX
        # Live with its bin dir prepended to PATH (so latexmk's child engines/bibtex
        # come from the same real install, not a stub that shadows it).
        env = os.environ.copy()
        env["PATH"] = tl_bindir + os.pathsep + env.get("PATH", "")
        engine = _pick_latex_engine(
            tex_path.read_text(encoding="utf-8", errors="ignore"), tl_bindir)
        latexmk = str(Path(tl_bindir) / "latexmk")
        tlmgr = str(Path(tl_bindir) / "tlmgr")
        tlmgr = tlmgr if Path(tlmgr).exists() else None
        cmd = [latexmk, _LATEXMK_FLAG[engine],  # -pdf / -pdfxe / -pdflua selects the engine
               "-interaction=nonstopmode", "-halt-on-error", "-file-line-error", run_name]
        log, installed_any = "", []
        for _ in range(5):  # compile; on a missing CTAN package, install it and retry
            try:
                p = subprocess.run(cmd, cwd=str(cdir), env=env,
                                   capture_output=True, text=True, timeout=_LATEXMK_TIMEOUT)
            except subprocess.TimeoutExpired:
                shutil.rmtree(cdir, ignore_errors=True)
                return False, None, f"LaTeX compile timed out after {_LATEXMK_TIMEOUT}s."
            log = (p.stdout or "") + "\n" + (p.stderr or "")
            if cdir_pdf.is_file() or not tlmgr:
                break
            new = _tlmgr_install_missing(log, tlmgr, env, skip=set(installed_any))
            if not new:
                break  # nothing left to auto-install → it's a real source error
            installed_any += new
            # latexmk records the failed run in its database and would otherwise say
            # "Nothing to do"; clear its aux/db so it re-runs now the package exists.
            for ext in (".fdb_latexmk", ".fls", ".log", ".aux"):
                (cdir / f"{tex_path.stem}{ext}").unlink(missing_ok=True)
        head = f"[latexmk -{engine} | {tl_bindir}]"
        if installed_any:
            head += f" [auto-installed: {', '.join(installed_any)}]"
        return _finish(head + "\n" + log)

    # ── Fallbacks (no real TeX Live) ──────────────────────────────────────────
    tectonic = shutil.which("tectonic")
    if tectonic:
        # Self-contained fallback when no full TeX Live is installed. Its failure is
        # also a real source error, so return ITS log — do NOT fall to pdflatex,
        # whose stub env ("missing pdflatex.fmt") would mask the true cause.
        try:
            p = subprocess.run([tectonic, run_name], cwd=str(cdir),
                               capture_output=True, text=True, timeout=_COMPILE_TIMEOUT)
        except subprocess.TimeoutExpired:
            shutil.rmtree(cdir, ignore_errors=True)
            return False, None, f"LaTeX compile timed out after {_COMPILE_TIMEOUT}s."
        return _finish((p.stdout or "") + "\n" + (p.stderr or ""))

    pdflatex = shutil.which("pdflatex")  # last resort (guard above guarantees it exists here)
    log = ""
    for _ in range(2):  # twice so references/labels resolve
        try:
            p = subprocess.run([pdflatex, "-interaction=nonstopmode", "-halt-on-error", run_name],
                               cwd=str(cdir), capture_output=True, text=True, timeout=_COMPILE_TIMEOUT)
        except subprocess.TimeoutExpired:
            shutil.rmtree(cdir, ignore_errors=True)
            return False, None, f"LaTeX compile timed out after {_COMPILE_TIMEOUT}s."
        log = (p.stdout or "") + "\n" + (p.stderr or "")
    return _finish(log)


def _compile_latex(work_dir: Path, tex: str) -> tuple[bool, int | None, str]:
    """Write paper.tex (from a source string) and compile it — used by the classic
    iterative pipeline. Thin wrapper over :func:`compile_tex`."""
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "paper.tex").write_text(tex, encoding="utf-8")
    return compile_tex(work_dir, "paper.tex")


# ── Main loop ─────────────────────────────────────────────────────────────────

async def stream_iterative_research_events(
    store: Store,
    settings: LLMSettings,
    idea_id: str,
    restart: bool = False,
    max_hypotheses: int = 4,
    page_limit: int = 9,
    target_positive: int | None = None,
    max_depth: int | None = None,
    *,
    experiment_mode: str | None = None,
    ssh_target_id: str | None = None,
    writing_style: str | None = None,
    benchmark: str | None = None,
    use_reference_codebase: bool = True,
    fill_page: bool = False,
) -> AsyncIterator[dict]:
    """When *target_positive* is set (auto mode), the hypothesis loop stops once that
    many hypotheses are SUPPORTED, or after *max_hypotheses* tested — then writes the
    paper from the supported ones. When None, it stops on the report's "Enough For
    Paper" reflection (classic behaviour).

    *max_depth* caps how DEEP the hypothesis map may grow: a node already at the cap
    expands SIDEWAYS (new siblings under its parent / new roots) instead of deeper, so
    the loop tries a parent/sibling direction rather than refining endlessly. None =
    unlimited depth.

    PER-RUN overrides (from the web UI's Auto settings / `auto run` flags): *experiment_mode*
    + *ssh_target_id* override the global RunConfig for THIS run; *writing_style* (a style
    name) injects a prose-style guide into the paper stage; *use_reference_codebase* False
    skips linking the domain's reference codebase into experiments."""
    spec = store.get_spec(idea_id)
    if spec is None:
        yield {"type": "error", "message": "Idea not found"}
        return
    # A domain is OPTIONAL. If one is linked, ground the run in its DOMAIN.md (Crucial
    # Papers with real authors/year, datasets, venues) so the paper cites the field
    # correctly; if NOT, just proceed with the domain context left BLANK (don't block).
    from paperclaw import service as _svc
    domain_id = _svc.resolve_domain_id_for_idea(store, idea_id)
    domain_md = store.get_domain_spec(domain_id) if domain_id else None
    if domain_md:
        yield {"type": "delta", "text": f"\n[grounding in pinned domain → domains/{domain_id}/DOMAIN.md]\n"}
    else:
        yield {"type": "delta", "text": "\n[no domain linked — proceeding without domain grounding]\n"}

    idea_path = store.idea_path(idea_id)
    run_cfg = store.effective_run_config(idea_id)  # the idea's allocated resources over global
    if experiment_mode:  # explicit per-run override (Auto settings / `auto run` flags) wins
        run_cfg = run_cfg.model_copy(update={
            "experiment_mode": experiment_mode,
            "ssh_target_id": ssh_target_id or run_cfg.ssh_target_id,
        })
    if restart:
        _clear_iterative(idea_path)

    base_ctx = f"IDEA.md:\n{spec}"
    if domain_md:  # pinned domain's literature/field context for every generation step
        base_ctx += ("\n\nDOMAIN.md (the idea's pinned domain — cite its Crucial Papers "
                     "by their real AUTHORS and YEAR, and use its datasets/benchmarks and "
                     f"venues; source: domains/{domain_id}/DOMAIN.md):\n{domain_md}")
    # Benchmark template (per-run name, or the domain's sole benchmark): reframes the WHOLE
    # loop — every step reads base_ctx, so the note propagates to plan/experiment/report/paper.
    bench_md = _svc.resolve_benchmark(store, domain_id, benchmark)
    bench_file = idea_path / ".benchmark.md"
    if bench_md:
        from paperclaw import benchmarks as _bench
        from paperclaw.prompts.benchmarks import BENCHMARK_NOTE
        base_ctx += BENCHMARK_NOTE.replace("{benchmark}", bench_md)
        # persist so the DETACHED experiment job (which rebuilds context from disk via
        # service.stream_hypothesis_experiment) reframes the experiment too, not just the
        # in-process plan/report/paper steps that read base_ctx.
        bench_file.write_text(bench_md, encoding="utf-8")
        added = _bench.merge_into_refbib(idea_path / "ref.bib", bench_md)
        yield {"type": "delta", "text":
               f"\n[benchmark template in force — run only the new method on its fixed protocol; "
               f"merged {added} cited reference(s) into ref.bib]\n"}
    else:
        bench_file.unlink(missing_ok=True)  # no benchmark this run — don't leave a stale one
    max_hypotheses = max(1, min(10, max_hypotheses))

    # Auto mode: ensure a hypothesis map exists (generate it up front if missing).
    if restart or store.get_hypothesis_map(idea_id) is None:
        yield {"type": "phase", "phase": "hypothesis_map", "label": "Building hypothesis map…"}
        try:
            from paperclaw import service  # local import avoids any load-order cycle
            hmap = await service.generate_hypothesis_map(store, settings, idea_id)
            yield {"type": "hypothesis_map", "count": len(hmap.nodes)}
        except Exception as exc:  # not fatal — the loop can still run
            yield {"type": "delta", "text": f"\n[hypothesis map generation skipped: {exc}]\n"}

    # Auto mode: seed ref.bib from real literature if it's empty.
    if not (store.get_ref_bib(idea_id) or "").strip():
        yield {"type": "phase", "phase": "references", "label": "Gathering references…"}
        try:
            from paperclaw import service
            view = await service.generate_references(store, settings, idea_id)
            yield {"type": "references", "count": len(view.entries)}
        except Exception as exc:
            yield {"type": "delta", "text": f"\n[reference gathering skipped: {exc}]\n"}

    # Each hypothesis gets its own directory under hypotheses/<id>/ holding
    # plan.md → experiment.md (+ results/logs) → report.md (#6); its map-node
    # status is updated from the report verdict. The loop is status-driven: it
    # processes any 'untested' node (DFS) so sub-hypotheses ADDED by a report's
    # expansion get tested too, up to the max_hypotheses budget.
    if not (store.get_hypothesis_map(idea_id) or {}).get("nodes"):
        yield {"type": "error", "message": "No hypotheses to test — the hypothesis map is empty."}
        return

    hw_summary = (store.get_hardware_md() or
                  "No hardware detected (assume a modest single-GPU / CPU machine).")[:2000]
    rounds: list[dict] = []
    tested = 0  # hypotheses tested in THIS session (caps new work at max_hypotheses)
    # SUPPORTED total across ALL runs — so a RESUME recognises hypotheses already
    # proven earlier (drives the stop condition + the status display).
    positives = _count_supported(store.get_hypothesis_map(idea_id) or {})
    if positives:  # surface prior-run positives in the auto-run status immediately
        yield {"type": "positives", "positives": positives}

    while tested < max_hypotheses:
        # Already enough supported hypotheses for the paper? Stop BEFORE testing more
        # (the resume case the user hit: 2 positives already exist, target is 2).
        if target_positive is not None and positives >= target_positive:
            break
        node = _next_untested(store.get_hypothesis_map(idea_id) or {})
        if node is None:
            break
        tested += 1
        k = tested
        hid = node["id"]
        hdir = (idea_path or Path(tempfile.mkdtemp())) / "hypotheses" / hid
        hdir.mkdir(parents=True, exist_ok=True)
        node_text = _format_node(node)
        prior = _format_prior(rounds)
        yield {"type": "round", "round": k, "hypothesisId": hid}

        # ── plan (+ resource estimation + feasibility) ───────────────────────
        # (restart already cleared the dir; root plans were re-made by map-gen)
        plan = _read(hdir / "plan.md")
        if plan is None:
            yield {"type": "phase", "phase": f"hyp{k}_plan", "label": f"Planning hypothesis {k}…"}
            raw = ""
            try:
                async for ev in llm.stream_chat_thinking(
                    settings, HYPOTHESIS_PLAN_SYSTEM,
                    [{"role": "user", "content":
                      f"{base_ctx}{prior}\n\nTarget hypothesis:\n{node_text}\n\nAvailable hardware:\n{hw_summary}"}],
                    max_tokens=1400,
                ):
                    if ev["type"] == "thinking":
                        yield {"type": "thinking", "text": ev["text"]}
                    else:
                        raw += ev["text"]
                        yield {"type": "delta", "text": ev["text"]}
            except (llm.LLMNotConfigured, llm.LLMError) as exc:
                yield {"type": "error", "message": str(exc)}
                return
            plan = _extract_block(raw, "plan")
            (hdir / "plan.md").write_text(plan, encoding="utf-8")
        else:
            yield {"type": "phase", "phase": f"hyp{k}_plan", "label": f"Loaded plan {k}"}
        yield {"type": "phase_done", "phase": f"hyp{k}_plan", "content": plan}

        # ── feasibility gate: reject if the plan can't run on this hardware ──
        if _is_infeasible(plan):
            _set_node_status(store, idea_id, hid, "blocked")
            yield {"type": "hypothesis_status", "hypothesisId": hid, "status": "blocked"}
            rounds.append({"k": k, "id": hid, "hypothesis": node_text, "plan": plan,
                           "experiment": "", "report": "", "enough": False, "blocked": True})
            yield {"type": "round_done", "round": k, "enough": False, "blocked": True}
            continue

        # ── experiment ───────────────────────────────────────────────────────
        exp = _read(hdir / "experiment.md")
        if exp is None:
            yield {"type": "phase", "phase": f"hyp{k}_experiment", "label": f"Testing hypothesis {k}…"}
            if run_cfg.experiment_mode in ("executed", "ssh", "cli"):
                target = _resolve_ssh_target(store, run_cfg) if run_cfg.experiment_mode == "ssh" else None
                if run_cfg.experiment_mode == "ssh" and target is None:
                    exp = ("_SSH experiment mode is selected but no SSH remote is configured "
                           "(Settings → Hardware). Skipping execution._")
                else:
                    # Run the experiment as a DETACHED, monitored job (appears in the
                    # global monitor + survives the backend dying); tail its log into
                    # the pipeline stream. plan.md is already written, so the job skips
                    # planning and just executes.
                    from paperclaw import jobs
                    try:
                        # pin THIS run's effective config (per-run experiment mode / SSH
                        # target / codebase toggle) so the detached child uses it, not the
                        # global on-disk config.
                        jobs.start_experiment_job(
                            store, idea_id, hid,
                            run_config=run_cfg, use_reference_codebase=use_reference_codebase)
                        async for ev in jobs.tail_experiment_events(store, idea_id, hid):
                            t = ev.get("type")
                            if t == "thinking":
                                yield {"type": "thinking", "text": ev["text"]}
                            elif t in ("delta", "status"):
                                yield {"type": "delta", "text": ev.get("text", "")}
                    except Exception as exc:  # never let a runner bug kill the pipeline
                        yield {"type": "delta", "text": f"\n[execution error: {exc}]\n"}
                    exp = _read(hdir / "experiment.md") or "_No experiment results produced._"
            else:
                raw = ""
                try:
                    async for ev in llm.stream_chat_thinking(
                        settings, EXPERIMENT_SYSTEM,
                        [{"role": "user", "content": f"{base_ctx}\n\nPlan under test:\n{plan}"}],
                        max_tokens=1600,
                    ):
                        if ev["type"] == "thinking":
                            yield {"type": "thinking", "text": ev["text"]}
                        else:
                            raw += ev["text"]
                            yield {"type": "delta", "text": ev["text"]}
                except (llm.LLMNotConfigured, llm.LLMError) as exc:
                    yield {"type": "error", "message": str(exc)}
                    return
                exp = _extract_block(raw, "experiment-results")
            (hdir / "experiment.md").write_text(exp, encoding="utf-8")
        else:
            yield {"type": "phase", "phase": f"hyp{k}_experiment", "label": f"Loaded experiment {k}"}
        yield {"type": "phase_done", "phase": f"hyp{k}_experiment", "content": exp}

        # ── report ───────────────────────────────────────────────────────────
        rep = _read(hdir / "report.md")
        if rep is None:
            yield {"type": "phase", "phase": f"hyp{k}_report", "label": f"Writing report {k}…"}
            raw = ""
            ctx = f"{base_ctx}{prior}\n\nHypothesis {k}:\n{node_text}\n\nPlan:\n{plan}\n\nResults:\n{exp}"
            try:
                async for ev in llm.stream_chat_thinking(
                    settings, HYPOTHESIS_REPORT_SYSTEM, [{"role": "user", "content": ctx}], max_tokens=1400,
                ):
                    if ev["type"] == "thinking":
                        yield {"type": "thinking", "text": ev["text"]}
                    else:
                        raw += ev["text"]
                        yield {"type": "delta", "text": ev["text"]}
            except (llm.LLMNotConfigured, llm.LLMError) as exc:
                yield {"type": "error", "message": str(exc)}
                return
            rep = _extract_block(raw, "report")
            (hdir / "report.md").write_text(rep, encoding="utf-8")
        else:
            yield {"type": "phase", "phase": f"hyp{k}_report", "label": f"Loaded report {k}"}
        yield {"type": "phase_done", "phase": f"hyp{k}_report", "content": rep}

        status = _verdict_to_status(rep)
        _set_node_status(store, idea_id, hid, status)
        yield {"type": "hypothesis_status", "hypothesisId": hid, "status": status}
        if status == "supported":
            positives += 1

        enough = _parse_enough(rep)
        rounds.append({"k": k, "id": hid, "hypothesis": node_text, "plan": plan,
                       "experiment": exp, "report": rep, "enough": enough, "blocked": False})
        yield {"type": "round_done", "round": k, "enough": enough, "positives": positives}

        # grow the tree: add sub-hypotheses recommended by the report (best-effort)
        try:
            from paperclaw import service
            added = await service.expand_hypothesis(store, settings, idea_id, hid, rep, max_depth=max_depth)
            if added:
                yield {"type": "hypothesis_expanded", "hypothesisId": hid, "added": added}
        except Exception:
            pass
        # Stop: auto mode counts SUPPORTED hypotheses; classic mode uses the
        # report's "Enough For Paper" reflection. (max_hypotheses caps both.)
        if target_positive is not None:
            if positives >= target_positive:
                break
        elif enough:
            break

    # ── conceptual figures for the paper (best-effort, resumable) ─────────────
    if idea_path is not None and not list(idea_path.glob("figures/*.png")):
        yield {"type": "phase", "phase": "figures", "label": "Drawing conceptual figures…"}
        try:
            async for ev in experiments.generate_figures(settings, base_ctx, idea_path / "figures", run_cfg):
                if ev["type"] == "thinking":
                    yield {"type": "thinking", "text": ev["text"]}
                elif ev["type"] in ("delta", "status"):
                    yield {"type": "delta", "text": ev["text"]}
        except Exception as exc:
            yield {"type": "delta", "text": f"\n[figure generation skipped: {exc}]\n"}

    # ── paper: select subset + write LaTeX + compile + page check ─────────────
    yield {"type": "phase", "phase": "paper", "label": "Selecting results and writing the LaTeX paper…"}
    figures = _list_figures(idea_path)
    venue = _resolve_venue(store, spec)
    paper_system = (
        LATEX_PAPER_SYSTEM
        .replace("{venue}", venue)
        .replace("{page_limit}", str(page_limit))
        .replace("{today}", date.today().strftime("%B %d, %Y"))
        .replace("{rigor_rules}", PAPER_RIGOR_RULES)
    )
    if fill_page:  # the main text must FILL the budget (Conclusion at the page-limit end)
        paper_system += (LATEX_PAGE_FILL_NOTE
                         .replace("{page_limit_minus_1}", str(page_limit - 1))
                         .replace("{page_limit}", str(page_limit)))
    # Rebuild from disk so a RESUME (which may have tested nothing this session) still
    # writes the paper from ALL tested hypotheses, including prior-session results.
    paper_rounds = _collect_paper_rounds(idea_path, store, idea_id, rounds)
    all_rounds = "\n\n".join(
        f"### Hypothesis {r['k']}\n{r['hypothesis']}\n\nPlan:\n{r['plan']}\n\n"
        f"Results:\n{r['experiment']}\n\nReport:\n{r.get('report', '')}"
        for r in paper_rounds if not r.get("blocked")
    )
    ref_bib = _load(idea_path, "ref.bib") or ""
    paper_ctx = (
        f"{base_ctx}\n\n## Tested hypotheses\n{all_rounds}\n\n"
        f"Available figure files (use ONLY these with \\includegraphics, or none): {figures}"
    )
    if ref_bib.strip():
        paper_ctx += LATEX_REFS_NOTE + ref_bib
    # Prose-style guide (separate from venue formatting): a chosen style, else the
    # house DEFAULT_STYLE — the voice always comes from a writing style, not the prompt.
    from paperclaw import service
    style_md = service.resolve_writing_style(
        store, service.resolve_domain_id_for_idea(store, idea_id), writing_style)
    if style_md:
        paper_ctx += ("\n\n## Writing style guide (follow this prose style)\n" + style_md)
    skeleton = _venue_skeleton(idea_path)
    if skeleton:  # an uploaded venue template → base the paper on it (preamble verbatim)
        paper_ctx += LATEX_VENUE_TEMPLATE_NOTE + "```\n" + skeleton + "\n```"
    raw = ""
    try:
        async for ev in llm.stream_chat_thinking(
            settings, paper_system, [{"role": "user", "content": paper_ctx}], max_tokens=16000,
        ):
            if ev["type"] == "thinking":
                yield {"type": "thinking", "text": ev["text"]}
            else:
                raw += ev["text"]
                yield {"type": "delta", "text": ev["text"]}
    except (llm.LLMNotConfigured, llm.LLMError) as exc:
        yield {"type": "error", "message": str(exc)}
        return
    tex = _extract_block(raw, "latex")

    work_dir = idea_path or Path(tempfile.mkdtemp())
    yield {"type": "phase", "phase": "compile", "label": "Compiling LaTeX…"}
    ok, pages, log = await asyncio.to_thread(_compile_latex, work_dir, tex)
    yield {"type": "compile", "ok": ok, "attempt": 1}

    attempt = 1
    while not ok and attempt < _MAX_COMPILE_FIXES:
        attempt += 1
        yield {"type": "delta", "text": f"\n[compile failed; fixing LaTeX (attempt {attempt})…]\n"}
        raw = ""
        try:
            async for ev in llm.stream_chat_thinking(
                settings, LATEX_FIX_SYSTEM,
                [{"role": "user", "content": f"LaTeX source:\n{tex}\n\nCompiler log:\n{log}"}],
                max_tokens=16000,
            ):
                if ev["type"] == "thinking":
                    yield {"type": "thinking", "text": ev["text"]}
                else:
                    raw += ev["text"]
                    yield {"type": "delta", "text": ev["text"]}
        except (llm.LLMNotConfigured, llm.LLMError) as exc:
            yield {"type": "error", "message": str(exc)}
            return
        tex = _extract_block(raw, "latex")
        ok, pages, log = await asyncio.to_thread(_compile_latex, work_dir, tex)
        yield {"type": "compile", "ok": ok, "attempt": attempt}

    # ── enforce the page limit: if over, shorten and recompile (format compliance)
    shrink = 0
    while ok and pages is not None and pages > page_limit and shrink < 2:
        shrink += 1
        yield {"type": "phase", "phase": "compile",
               "label": f"Paper is {pages}pp > {page_limit}pp limit — shortening…"}
        raw = ""
        try:
            async for ev in llm.stream_chat_thinking(
                settings,
                LATEX_SHORTEN_SYSTEM.replace("{pages}", str(pages)).replace("{page_limit}", str(page_limit)),
                [{"role": "user", "content": tex}], max_tokens=16000,
            ):
                if ev["type"] == "thinking":
                    yield {"type": "thinking", "text": ev["text"]}
                else:
                    raw += ev["text"]
                    yield {"type": "delta", "text": ev["text"]}
        except (llm.LLMNotConfigured, llm.LLMError):
            break
        tex = _extract_block(raw, "latex")
        ok, pages, log = await asyncio.to_thread(_compile_latex, work_dir, tex)
        yield {"type": "compile", "ok": ok, "attempt": _MAX_COMPILE_FIXES + shrink}

    # ── optional OCR format check (graceful: needs pdftoppm + tesseract) ───────
    ocr = await asyncio.to_thread(_ocr_check, work_dir / "paper.pdf") if ok else None
    compliant = bool(ok and pages is not None and pages <= page_limit)
    yield {"type": "page_check", "pages": pages, "limit": page_limit,
           "compliant": compliant, "ocrChecked": bool(ocr)}

    if ok:
        yield {"type": "paper_ready", "download_url": f"/api/ideas/{idea_id}/paper"}
    yield {"type": "done"}
