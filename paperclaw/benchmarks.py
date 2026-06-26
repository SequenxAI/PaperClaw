"""Benchmark-template library — a FIXED protocol + a PUBLISHED, cited leaderboard,
chosen for an idea so the run is reframed around "run the new method on this exact
protocol and match/beat these cited numbers" (see ``prompts/benchmarks.py``).

Two scopes, resolved domain-first (the writing-style layout):
  - global   `<home>/benchmarks/<name>.md`        (field-agnostic, shared)
  - domain   `domains/<id>/benchmarks/<name>.md`  (overrides a global of the same name)

The global library is seeded with the built-in templates on first use.
"""

import re
from pathlib import Path

from paperclaw import references
from paperclaw.prompts.benchmarks import BUILTIN_BENCHMARKS  # built-in templates

_NAME_RE = re.compile(r"[^a-z0-9._-]+")
_HEADING_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
_REFS_RE = re.compile(r"(?im)^##\s+References\s*$(.*)\Z", re.S)

DIRNAME = "benchmarks"

__all__ = [
    "BUILTIN_BENCHMARKS", "DIRNAME", "ensure_seeded",
    "list_benchmarks", "get_benchmark", "save_benchmark",
    "references_block", "merge_into_refbib",
]


def _safe_name(name: str) -> str | None:
    n = _NAME_RE.sub("-", (name or "").strip().lower()).strip("-")
    return n or None


def _title(md: str, fallback: str) -> str:
    m = _HEADING_RE.search(md or "")
    return m.group(1).strip() if m else fallback


def ensure_seeded(home: Path) -> None:
    """Write the built-in templates into `<home>/benchmarks/` if missing."""
    d = Path(home) / DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    for name, md in BUILTIN_BENCHMARKS.items():
        f = d / f"{name}.md"
        if not f.exists():
            f.write_text(md, encoding="utf-8")


def _scan(d: Path, scope: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if d.is_dir():
        for f in sorted(d.glob("*.md")):
            out[f.stem] = {"name": f.stem, "scope": scope,
                           "title": _title(f.read_text(encoding="utf-8", errors="ignore"), f.stem)}
    return out


_SCOPE_ORDER = {"idea": 0, "domain": 1, "global": 2}


def list_benchmarks(home: Path, domain_dir: Path | None = None,
                    idea_dir: Path | None = None) -> list[dict]:
    """All available benchmarks. Scopes, narrowest-wins on a name collision and listed
    first: idea (``idea_dir``) > domain (``domain_dir``) > global (``home``)."""
    ensure_seeded(home)
    items = _scan(Path(home) / DIRNAME, "global")
    if domain_dir is not None:
        items.update(_scan(Path(domain_dir) / DIRNAME, "domain"))   # domain overrides global
    if idea_dir is not None:
        items.update(_scan(Path(idea_dir) / DIRNAME, "idea"))       # idea overrides both
    return sorted(items.values(), key=lambda s: (_SCOPE_ORDER.get(s["scope"], 9), s["name"]))


def get_benchmark(home: Path, domain_dir: Path | None, name: str,
                  idea_dir: Path | None = None) -> str | None:
    """Resolve a benchmark's markdown by name — idea dir, then domain dir, then global."""
    safe = _safe_name(name)
    if not safe:
        return None
    candidates = []
    if idea_dir is not None:
        candidates.append(Path(idea_dir) / DIRNAME / f"{safe}.md")
    if domain_dir is not None:
        candidates.append(Path(domain_dir) / DIRNAME / f"{safe}.md")
    ensure_seeded(home)
    candidates.append(Path(home) / DIRNAME / f"{safe}.md")
    for c in candidates:
        if c.is_file():
            return c.read_text(encoding="utf-8", errors="ignore")
    return None


def save_benchmark(home: Path, domain_dir: Path | None, name: str, content: str) -> str | None:
    """Create/overwrite a benchmark (domain-scoped if domain_dir given). Returns the
    saved name, or None if the name is invalid."""
    safe = _safe_name(name)
    if not safe:
        return None
    base = Path(domain_dir) if domain_dir is not None else Path(home)
    d = base / DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{safe}.md").write_text(content, encoding="utf-8")
    return safe


def references_block(md: str) -> str:
    """The raw BibTeX under a benchmark's ``## References`` heading (to the end), or ''."""
    m = _REFS_RE.search(md or "")
    return m.group(1).strip() if m else ""


def _split_bibtex(text: str) -> list[str]:
    """Split a BibTeX blob into raw ``@type{...}`` entries (brace-balanced)."""
    entries: list[str] = []
    i, n = 0, len(text)
    while True:
        at = text.find("@", i)
        if at < 0:
            break
        brace = text.find("{", at)
        if brace < 0:
            break
        depth, j = 0, brace
        while j < n:
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
                if depth == 0:
                    j += 1
                    break
            j += 1
        entries.append(text[at:j].strip())
        i = j
    return entries


def merge_into_refbib(ref_bib: Path, md: str) -> int:
    """Append the benchmark's cited BibTeX entries to *ref_bib*, skipping keys already
    present. Returns the number of entries added. So the paper can ``\\cite{key}`` the
    published baseline rows it shows."""
    block = references_block(md)
    if not block:
        return 0
    existing = ref_bib.read_text(encoding="utf-8") if ref_bib.is_file() else ""
    have = references.keys_in(existing)
    additions: list[str] = []
    for entry in _split_bibtex(block):
        parsed = references.parse_bibtex(entry)
        key = parsed[0]["key"] if parsed else None
        if key and key not in have:
            additions.append(entry)
            have.add(key)
    if not additions:
        return 0
    ref_bib.parent.mkdir(parents=True, exist_ok=True)
    body = "\n\n".join(additions) + "\n"
    merged = (existing.rstrip() + "\n\n" + body) if existing.strip() else body
    ref_bib.write_text(merged, encoding="utf-8")
    return len(additions)
