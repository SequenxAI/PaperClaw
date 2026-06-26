"""Disk-backed store under the PaperClaw home directory.

Layout:
    <home>/seeds.json
    <home>/domains/<id>/domain.json   — Domain metadata
    <home>/domains/<id>/DOMAIN.md     — the domain spec
    <home>/ideas/<id>/idea.json       — Idea metadata
    <home>/ideas/<id>/IDEA.md         — the spec
    <home>/ideas/<id>/messages.json   — conversation history
    <home>/seed_chats/<id>.json       — conversation on a brainstormed draft
    <home>/scratch_messages.json      — conversation with no idea selected
"""

import json
import re
import shutil
import time
import uuid
from pathlib import Path

from paperclaw.domains import new_domain_spec
from paperclaw.ideas import new_spec
from paperclaw.server.models import Domain, Idea, Message, Resource, RunConfig, Seed

SCRATCH_ID = "_scratch"
SEED_PREFIX = "seed-"      # message-context key prefix for seed conversations
DOMAIN_PREFIX = "domain-"  # message-context key prefix for domain conversations


def default_run_config() -> RunConfig:
    """The experiment-execution config to use when none is saved yet. Defaults to the
    **CLI agent** (`claude`), which runs REAL experiments — never `simulated` (which
    only narrates plausible/fake numbers). If the `claude` CLI is missing the doctor
    flags it; switch to `executed` (in-process agent + API key) or install claude."""
    return RunConfig(experimentMode="cli",
                     agentCommand="claude -p {prompt} --dangerously-skip-permissions")


def _uid() -> str:
    return uuid.uuid4().hex[:12]


class Store:
    def __init__(self, home: Path) -> None:
        self.home = home
        self.ideas_dir = home / "ideas"
        self.ideas_dir.mkdir(parents=True, exist_ok=True)
        self.domains_dir = home / "domains"
        self.domains_dir.mkdir(parents=True, exist_ok=True)
        self.resources: dict[str, list[Resource]] = {}  # not yet persisted

    # ── Domains ────────────────────────────────────────────
    def _domain_dir(self, domain_id: str) -> Path:
        return self.domains_dir / domain_id

    def list_domains(self) -> list[Domain]:
        domains = [
            Domain.model_validate_json(meta.read_text())
            for meta in sorted(self.domains_dir.glob("*/domain.json"))
        ]
        domains.sort(key=lambda d: d.created_at)
        return domains

    def _save_domain(self, domain: Domain) -> None:
        d = self._domain_dir(domain.id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "domain.json").write_text(domain.model_dump_json(by_alias=True, indent=2))

    def add_domain(self, name: str, spec: str | None = None) -> Domain:
        domain = Domain(id=_uid(), name=name, createdAt=time.time())
        self._save_domain(domain)
        (self._domain_dir(domain.id) / "DOMAIN.md").write_text(spec or new_domain_spec(name))
        return domain

    def set_domain_selected(self, domain_id: str, selected: bool) -> Domain | None:
        for domain in self.list_domains():
            if domain.id == domain_id:
                domain.is_selected = selected
                self._save_domain(domain)
                return domain
        return None

    def remove_domain(self, domain_id: str) -> bool:
        d = self._domain_dir(domain_id)
        if not d.is_dir():
            return False
        shutil.rmtree(d)  # rmtree (not unlink-each) so any subdirs are removed too
        (self.home / "domain_chats" / f"{domain_id}.json").unlink(missing_ok=True)
        return True

    def domain_path(self, domain_id: str) -> Path | None:
        """Return the domain folder Path, or None if the domain doesn't exist."""
        d = self._domain_dir(domain_id)
        return d if d.is_dir() else None

    def get_domain_spec(self, domain_id: str) -> str | None:
        path = self._domain_dir(domain_id) / "DOMAIN.md"
        return path.read_text() if path.is_file() else None

    def put_domain_spec(self, domain_id: str, content: str) -> bool:
        d = self._domain_dir(domain_id)
        if not d.is_dir():
            return False
        (d / "DOMAIN.md").write_text(content)
        # suggestions are derived from the spec — invalidate the cache
        (d / "suggestions.json").unlink(missing_ok=True)
        return True

    def get_domain_suggestions(self, domain_id: str) -> list[str] | None:
        path = self._domain_dir(domain_id) / "suggestions.json"
        if path.is_file():
            try:
                return json.loads(path.read_text())
            except json.JSONDecodeError:
                return None
        return None

    def put_domain_suggestions(self, domain_id: str, suggestions: list[str]) -> None:
        d = self._domain_dir(domain_id)
        if d.is_dir():
            (d / "suggestions.json").write_text(json.dumps(suggestions, indent=2))

    # ── Domain reference codebase ──────────────────────────────
    def domain_codebase_dir(self, domain_id: str) -> Path:
        """The dir holding the domain's downloaded reference codebase."""
        return self._domain_dir(domain_id) / "codebase"

    def domain_codebase_path(self, domain_id: str) -> Path | None:
        """The codebase dir if it exists and is non-empty, else None."""
        d = self.domain_codebase_dir(domain_id)
        if d.is_dir() and any(d.iterdir()):
            return d
        return None

    def set_domain_codebase_meta(self, domain_id: str, url: str | None,
                                 file_count: int) -> Domain | None:
        """Record the codebase URL + file count on the Domain (persisted in domain.json)."""
        for domain in self.list_domains():
            if domain.id == domain_id:
                domain.codebase_url = url
                domain.codebase_files = file_count
                self._save_domain(domain)
                return domain
        return None

    def clear_domain_codebase(self, domain_id: str) -> Domain | None:
        """Remove the downloaded codebase and clear its metadata."""
        shutil.rmtree(self.domain_codebase_dir(domain_id), ignore_errors=True)
        return self.set_domain_codebase_meta(domain_id, None, 0)

    def selected_domain_specs(self) -> list[tuple[Domain, str]]:
        out = []
        for domain in self.list_domains():
            if domain.is_selected:
                spec = self.get_domain_spec(domain.id)
                if spec:
                    out.append((domain, spec))
        return out

    # ── Seeds (brainstorm) ──────────────────────────────────
    @property
    def _seeds_path(self) -> Path:
        return self.home / "seeds.json"

    def _load_seeds(self) -> list[Seed]:
        if self._seeds_path.is_file():
            return [Seed.model_validate(s) for s in json.loads(self._seeds_path.read_text())]
        return []

    def _save_seeds(self, seeds: list[Seed]) -> None:
        self._seeds_path.write_text(
            json.dumps([s.model_dump(by_alias=True) for s in seeds], indent=2)
        )

    def list_seeds(self) -> list[Seed]:
        return self._load_seeds()

    def add_seed(self, text: str, draft: str | None = None) -> Seed:
        seeds = self._load_seeds()
        seed = Seed(id=_uid(), text=text, createdAt=time.time(), draft=draft)
        seeds.insert(0, seed)
        self._save_seeds(seeds)
        return seed

    def get_seed(self, seed_id: str) -> Seed | None:
        return next((s for s in self._load_seeds() if s.id == seed_id), None)

    def put_seed_draft(self, seed_id: str, draft: str, title: str | None = None) -> bool:
        seeds = self._load_seeds()
        for seed in seeds:
            if seed.id == seed_id:
                seed.draft = draft
                if title:
                    seed.text = title
                self._save_seeds(seeds)
                return True
        return False

    def remove_seed(self, seed_id: str) -> bool:
        seeds = self._load_seeds()
        remaining = [s for s in seeds if s.id != seed_id]
        if len(remaining) == len(seeds):
            return False
        self._save_seeds(remaining)
        chat = self.home / "seed_chats" / f"{seed_id}.json"
        chat.unlink(missing_ok=True)
        return True

    def pin_seed(self, seed_id: str) -> Idea | None:
        """Promote a seed into a real Idea; its draft becomes IDEA.md and its
        conversation moves to the new idea."""
        seed = self.get_seed(seed_id)
        if seed is None:
            return None
        idea = self.add_idea(seed.text)
        if seed.draft:
            self.put_spec(idea.id, seed.draft)
        chat = self.home / "seed_chats" / f"{seed_id}.json"
        if chat.is_file():
            (self._idea_dir(idea.id) / "messages.json").write_text(chat.read_text())
        self.remove_seed(seed_id)
        return idea

    # ── Ideas ──────────────────────────────────────────────
    def _idea_dir(self, idea_id: str) -> Path:
        return self.ideas_dir / idea_id

    def idea_path(self, idea_id: str) -> Path | None:
        """Absolute directory of an idea, or None if it doesn't exist."""
        d = self._idea_dir(idea_id)
        return d if d.is_dir() else None

    # ── Paper versions (paper.{tex,pdf,md} = v1, then paper_v2.*, paper_v3.*, …) ──
    _PAPER_RE = re.compile(r"paper(?:_v(\d+))?\.(md|tex|pdf)$")

    def _paper_files(self, idea_id: str) -> list[tuple[int, str, Path]]:
        """(version, ext, path) for every paper artifact in the idea workspace."""
        d = self._idea_dir(idea_id)
        out: list[tuple[int, str, Path]] = []
        if d.is_dir():
            for p in d.iterdir():
                if p.is_file():
                    m = self._PAPER_RE.fullmatch(p.name)
                    if m:
                        out.append((int(m.group(1)) if m.group(1) else 1, m.group(2), p))
        return out

    def max_paper_version(self, idea_id: str) -> int:
        return max((v for v, _, _ in self._paper_files(idea_id)), default=0)

    def next_paper_name(self, idea_id: str) -> str:
        """Filename for the NEXT paper version's LaTeX source (preserves earlier ones)."""
        v = self.max_paper_version(idea_id)
        return "paper.tex" if v == 0 else f"paper_v{v + 1}.tex"

    def paper_version_list(self, idea_id: str) -> list[int]:
        """Sorted distinct paper versions present (e.g. [1, 2, 3])."""
        return sorted({v for v, _, _ in self._paper_files(idea_id)})

    def paper_artifacts(self, idea_id: str, version: int | None = None) -> tuple[Path | None, Path | None, Path | None]:
        """(pdf, md, tex) for *version*. Default (version=None) is the latest VIEWABLE
        version — one that has a pdf or md — so a half-finished tex-only version (e.g.
        a failed compile) never hides the paper. The Paper tab shows pdf > md > tex."""
        fs = self._paper_files(idea_id)
        if not fs:
            return (None, None, None)
        if version is None:
            viewable = [v for v, e, _ in fs if e in ("pdf", "md")]
            version = max(viewable) if viewable else max(v for v, _, _ in fs)
        pdf = next((p for ver, e, p in fs if ver == version and e == "pdf"), None)
        md = next((p for ver, e, p in fs if ver == version and e == "md"), None)
        tex = next((p for ver, e, p in fs if ver == version and e == "tex"), None)
        return (pdf, md, tex)

    # ── Workspace files (browse code / figures / artifacts) ──
    _WORKSPACE_SKIP = {"__pycache__", ".git", ".ipynb_checkpoints", "node_modules"}

    def idea_file(self, idea_id: str, rel: str) -> Path | None:
        """Resolve a FILE inside an idea workspace; None if missing or it would
        escape the workspace (path-traversal guard)."""
        base = self._idea_dir(idea_id)
        if not base.is_dir():
            return None
        target = (base / rel).resolve()
        try:
            target.relative_to(base.resolve())
        except ValueError:
            return None
        return target if target.is_file() else None

    def list_idea_files(self, idea_id: str, rel: str = "") -> list[dict] | None:
        """Recursively list files+dirs under an idea workspace, each as
        ``{path, size, isDir}`` with ``path`` relative to the idea root. Returns
        None if the idea (or the requested subdir) doesn't exist. Skips caches."""
        base = self._idea_dir(idea_id)
        if not base.is_dir():
            return None
        start = (base / rel).resolve()
        try:
            start.relative_to(base.resolve())
        except ValueError:
            return None
        if not start.is_dir():
            return None
        entries: list[dict] = []
        for p in start.rglob("*"):
            relpath = p.relative_to(base)
            if any(part in self._WORKSPACE_SKIP for part in relpath.parts):
                continue
            is_dir = p.is_dir()
            try:
                size = 0 if is_dir else p.stat().st_size
            except OSError:
                size = 0
            entries.append({"path": relpath.as_posix(), "size": size, "isDir": is_dir})
            if len(entries) >= 4000:  # safety cap for pathological trees
                break
        entries.sort(key=lambda e: e["path"])
        return entries

    def list_ideas(self) -> list[Idea]:
        ideas = []
        for meta in sorted(self.ideas_dir.glob("*/idea.json")):
            ideas.append(Idea.model_validate_json(meta.read_text()))
        ideas.sort(key=lambda i: i.created_at)
        return ideas

    def _save_idea(self, idea: Idea) -> None:
        d = self._idea_dir(idea.id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "idea.json").write_text(idea.model_dump_json(by_alias=True, indent=2))

    def add_idea(self, title: str, description: str | None = None) -> Idea:
        idea = Idea(id=_uid(), title=title, description=description, createdAt=time.time())
        self._save_idea(idea)
        (self._idea_dir(idea.id) / "IDEA.md").write_text(new_spec(title))
        return idea

    def duplicate_idea(self, idea_id: str) -> Idea | None:
        """Create a new idea that copies the source's DEFINITION — its IDEA.md spec
        and ref.bib — under a fresh id/title "(copy)". Starts otherwise clean (no
        conversation, hypothesis map, experiments, or paper): a duplicate is a fork
        of the idea, not of a particular run. Returns None if the source is gone."""
        src = self._idea_dir(idea_id)
        if not src.is_dir():
            return None
        try:
            original = Idea.model_validate_json((src / "idea.json").read_text())
        except (OSError, ValueError):
            return None
        new = Idea(id=_uid(), title=f"{original.title} (copy)",
                   description=original.description, createdAt=time.time())
        self._save_idea(new)  # creates the dir + idea.json
        dst = self._idea_dir(new.id)
        spec = src / "IDEA.md"
        (dst / "IDEA.md").write_text(spec.read_text() if spec.is_file() else new_spec(new.title))
        bib = src / "ref.bib"
        if bib.is_file():
            (dst / "ref.bib").write_text(bib.read_text())
        return new

    def set_active_idea(self, idea_id: str) -> Idea | None:
        found = None
        for idea in self.list_ideas():
            was = idea.is_active
            idea.is_active = idea.id == idea_id
            if idea.is_active:
                found = idea
            if idea.is_active != was:
                self._save_idea(idea)
        return found

    _IDEA_COLORS = (None, "green", "yellow", "grey")

    def set_idea_color(self, idea_id: str, color: str | None) -> Idea | None:
        """Flag an idea with a sidebar colour (green | yellow | grey | None to clear)."""
        if color == "":
            color = None
        if color not in self._IDEA_COLORS:
            return None
        d = self._idea_dir(idea_id)
        if not (d / "idea.json").is_file():
            return None
        idea = Idea.model_validate_json((d / "idea.json").read_text())
        idea.color = color
        self._save_idea(idea)
        return idea

    def remove_idea(self, idea_id: str) -> bool:
        d = self._idea_dir(idea_id)
        if not d.is_dir():
            return False
        shutil.rmtree(d)  # rmtree so the experiments/ subdir (executed runs) is removed too
        self.resources.pop(idea_id, None)
        return True

    # ── Spec (IDEA.md) ─────────────────────────────────────
    def get_spec(self, idea_id: str) -> str | None:
        path = self._idea_dir(idea_id) / "IDEA.md"
        return path.read_text() if path.is_file() else None

    def put_spec(self, idea_id: str, content: str) -> bool:
        d = self._idea_dir(idea_id)
        if not d.is_dir():
            return False
        (d / "IDEA.md").write_text(content)
        return True

    # ── Domain pins (.domains.json = the idea's connected domains) ───────────
    def get_idea_domain_ids(self, idea_id: str) -> list[str] | None:
        """The idea's EXPLICITLY-connected domain ids (order preserved), or None if the
        idea was never explicitly connected (caller then falls back to name-matching)."""
        path = self._idea_dir(idea_id) / ".domains.json"
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        return [str(x) for x in data] if isinstance(data, list) else None

    def set_idea_domain_ids(self, idea_id: str, ids: list[str]) -> bool:
        """Persist the idea's connected domain ids (dedup, order-preserving). Empty list
        writes `[]` (an explicit 'no domains'), distinct from never-connected (no file)."""
        d = self._idea_dir(idea_id)
        if not d.is_dir():
            return False
        seen: list[str] = []
        for x in ids:
            if x and x not in seen:
                seen.append(x)
        (d / ".domains.json").write_text(json.dumps(seen))
        return True

    # ── References (ref.bib) ───────────────────────────────
    def get_ref_bib(self, idea_id: str) -> str:
        path = self._idea_dir(idea_id) / "ref.bib"
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def put_ref_bib(self, idea_id: str, content: str) -> bool:
        d = self._idea_dir(idea_id)
        if not d.is_dir():
            return False
        (d / "ref.bib").write_text(content, encoding="utf-8")
        return True

    # ── Hypothesis map ─────────────────────────────────────
    def get_hypothesis_map(self, idea_id: str) -> dict | None:
        path = self._idea_dir(idea_id) / ".hypothesis_map.json"
        if path.is_file():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return None
        return None

    def put_hypothesis_map(self, idea_id: str, data: dict) -> bool:
        d = self._idea_dir(idea_id)
        if not d.is_dir():
            return False
        (d / ".hypothesis_map.json").write_text(json.dumps(data, indent=2))
        return True

    # ── Messages ───────────────────────────────────────────
    def _messages_path(self, idea_id: str) -> Path:
        if idea_id == SCRATCH_ID:
            return self.home / "scratch_messages.json"
        if idea_id.startswith(SEED_PREFIX):
            return self.home / "seed_chats" / f"{idea_id[len(SEED_PREFIX):]}.json"
        if idea_id.startswith(DOMAIN_PREFIX):
            return self.home / "domain_chats" / f"{idea_id[len(DOMAIN_PREFIX):]}.json"
        return self._idea_dir(idea_id) / "messages.json"

    def move_scratch_to_domain(self, domain_id: str) -> None:
        """Carry the wizard conversation from scratch into the new domain's chat."""
        scratch = self.home / "scratch_messages.json"
        if not scratch.is_file():
            return
        target = self.home / "domain_chats" / f"{domain_id}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(scratch.read_text())
        scratch.unlink()

    def list_contexts(self) -> list[dict]:
        """All conversations: scratch, domain chats, seed chats, idea chats."""
        domains = {d.id: d.name for d in self.list_domains()}
        seeds = {s.id: s.text for s in self._load_seeds()}
        out: list[dict] = []

        def entry(context_id: str, kind: str, title: str, path: Path):
            if not path.is_file():
                return
            try:
                msgs = json.loads(path.read_text())
            except json.JSONDecodeError:
                return
            if not msgs:
                return
            out.append({
                "contextId": context_id,
                "kind": kind,
                "title": title,
                "messageCount": len(msgs),
                "lastTimestamp": msgs[-1].get("timestamp", 0),
            })

        entry(SCRATCH_ID, "scratch", "Scratch conversation", self.home / "scratch_messages.json")
        for did, name in domains.items():
            entry(f"{DOMAIN_PREFIX}{did}", "domain", name, self.home / "domain_chats" / f"{did}.json")
        for sid, text in seeds.items():
            entry(f"{SEED_PREFIX}{sid}", "seed", text, self.home / "seed_chats" / f"{sid}.json")
        for idea in self.list_ideas():
            entry(idea.id, "idea", idea.title, self._idea_dir(idea.id) / "messages.json")

        out.sort(key=lambda c: c["lastTimestamp"], reverse=True)
        return out

    def list_messages(self, idea_id: str) -> list[Message]:
        path = self._messages_path(idea_id)
        if path.is_file():
            return [Message.model_validate(m) for m in json.loads(path.read_text())]
        return []

    def add_message(
        self,
        idea_id: str,
        role: str,
        content: str,
        spec_updated: bool = False,
        map_updated: bool = False,
        paper_updated: bool = False,
        codebase_updated: bool = False,
        served_model: str | None = None,
        created_idea_id: str | None = None,
        created_domain_id: str | None = None,
        question: dict | None = None,
        thinking: str | None = None,
        parts: list[dict] | None = None,
        todos: list[dict] | None = None,
    ) -> Message:
        msgs = self.list_messages(idea_id)
        msg = Message(
            id=_uid(),
            role=role,
            content=content,
            timestamp=time.time(),
            status="sent",
            specUpdated=spec_updated,
            mapUpdated=map_updated,
            paperUpdated=paper_updated,
            codebaseUpdated=codebase_updated,
            servedModel=served_model,
            createdIdeaId=created_idea_id,
            createdDomainId=created_domain_id,
            question=question,
            thinking=thinking or None,
            parts=parts or None,
            todos=todos or None,
        )
        msgs.append(msg)
        path = self._messages_path(idea_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([m.model_dump(by_alias=True) for m in msgs], indent=2))
        return msg

    # ── Resources ──────────────────────────────────────────
    def list_resources(self, idea_id: str) -> list[Resource]:
        return self.resources.get(idea_id, [])

    # ── Hardware / environment ─────────────────────────────
    # Global (account-level), not per-idea: a snapshot of detected machines plus
    # the SSH targets to probe. Stored as hardware.json; HARDWARE.md is the
    # human/LLM-readable render shown in the right-column Resources panel.
    @property
    def _hardware_path(self) -> Path:
        return self.home / "hardware.json"

    @property
    def hardware_md_path(self) -> Path:
        return self.home / "HARDWARE.md"

    def get_hardware_state(self) -> dict:
        """Persisted hardware snapshot + SSH config ({} if none yet)."""
        if self._hardware_path.is_file():
            try:
                return json.loads(self._hardware_path.read_text())
            except json.JSONDecodeError:
                return {}
        return {}

    def save_hardware_state(self, state: dict) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        self._hardware_path.write_text(json.dumps(state, indent=2))

    def get_hardware_md(self) -> str | None:
        p = self.hardware_md_path
        return p.read_text() if p.is_file() else None

    def put_hardware_md(self, content: str) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        self.hardware_md_path.write_text(content)

    def get_run_config(self) -> RunConfig:
        """Experiment-execution config; stored in hardware.json. When NOTHING is saved
        yet, auto-default by detected tooling (see :func:`default_run_config`)."""
        saved = self.get_hardware_state().get("runConfig")
        return RunConfig.model_validate(saved) if saved else default_run_config()

    def save_run_config(self, cfg: RunConfig) -> None:
        state = self.get_hardware_state()
        state["runConfig"] = cfg.model_dump(by_alias=True)
        self.save_hardware_state(state)

    # ── Per-idea resource allocation (.resources.json) ─────────────────────────
    # An idea can pin WHERE its experiments run (compute mode + SSH GPU host); these
    # override the global run config for every experiment of that idea (auto OR manual),
    # so different ideas can run on different GPUs in parallel.
    def get_idea_resources(self, idea_id: str) -> dict | None:
        """The idea's saved resource allocation ({experimentMode?, sshTargetId?,
        useReferenceCodebase?}), or None if it was never set."""
        path = self._idea_dir(idea_id) / ".resources.json"
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def set_idea_resources(self, idea_id: str, resources: dict) -> bool:
        d = self._idea_dir(idea_id)
        if not d.is_dir():
            return False
        (d / ".resources.json").write_text(json.dumps(resources))
        return True

    def effective_run_config(self, idea_id: str) -> RunConfig:
        """The global run config with the idea's allocation applied on top — the EFFECTIVE
        config every experiment of this idea should use (auto + manual). Falls back to the
        global config for any field the idea didn't pin."""
        cfg = self.get_run_config()
        alloc = self.get_idea_resources(idea_id) or {}
        mode = alloc.get("experimentMode")
        if mode in ("simulated", "executed", "ssh", "cli"):
            cfg.experiment_mode = mode
        if "sshTargetId" in alloc:
            cfg.ssh_target_id = alloc.get("sshTargetId") or None
        return cfg

    # ── Auto-mode run status ────────────────────────────────────────────────
    # `paperclaw auto` streams its phase progress to the launching terminal;
    # ── Auto-run status (PER IDEA) ─────────────────────────────────────────────
    # Each idea owns its `.auto_run.json` snapshot so ideas can auto-run in PARALLEL
    # and each idea's panel shows only its own banner. The CLI (`auto status`) lists
    # them all; the web UI polls one idea's snapshot at a time.
    def _auto_run_path(self, idea_id: str) -> Path | None:
        d = self._idea_dir(idea_id)
        return d / ".auto_run.json" if d.is_dir() else None

    def get_idea_auto_run(self, idea_id: str) -> dict | None:
        p = self._auto_run_path(idea_id)
        if p and p.is_file():
            try:
                return json.loads(p.read_text())
            except json.JSONDecodeError:
                return None
        return None

    def put_idea_auto_run(self, idea_id: str, state: dict) -> None:
        d = self._idea_dir(idea_id)
        if not d.is_dir():
            return  # idea not created yet (topic run pre-`idea_created`) — skip
        (d / ".auto_run.json").write_text(json.dumps(state, indent=2))

    def list_auto_runs(self) -> list[dict]:
        """Every idea's auto-run snapshot (newest first), for `auto status` / a global view."""
        out: list[dict] = []
        if self.ideas_dir.is_dir():
            for f in self.ideas_dir.glob("*/.auto_run.json"):
                try:
                    out.append(json.loads(f.read_text()))
                except json.JSONDecodeError:
                    pass
        out.sort(key=lambda s: s.get("startedAt", 0), reverse=True)
        return out
