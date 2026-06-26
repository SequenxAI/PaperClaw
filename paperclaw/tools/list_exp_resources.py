"""list_exp_resources tool — report the experiment resources available so the agent can decide
WHERE/HOW to run before launching: the detected compute (local + SSH remotes — CPU/GPU/MEM/disk),
the current experiment-execution mode + coding-agent CLI, and the LLM provider/model.

SECURITY: API keys are reported as "configured" / "MISSING" — never the secret value.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SCHEMA: dict[str, Any] = {
    "name": "list_exp_resources",
    "description": (
        "List the experiment resources available BEFORE running one: the detected compute "
        "(local + any SSH remote hosts — CPU/GPU/MEM/disk), the current experiment-execution "
        "mode and coding-agent CLI, and the configured LLM provider/model. Call this FIRST when "
        "you are about to run an experiment, to decide where (local vs an SSH GPU host) and how "
        "to run it. (API keys are shown only as configured/missing, never their value.)"
    ),
    "input_schema": {"type": "object", "properties": {}, "required": []},
}


def _find_home(base: Path) -> Path:
    """The PaperClaw home that holds hardware.json / settings.*. An idea/domain workspace is
    ``<home>/{ideas,domains}/<id>``; otherwise walk up to a home marker."""
    if base.parent.name in ("ideas", "domains"):
        return base.parent.parent
    for d in [base, *base.parents]:
        if (d / "hardware.json").exists() or any(
                (d / f"settings.{ext}").exists() for ext in ("yaml", "yml", "json")):
            return d
    return base


def execute(base_dir, inputs: dict[str, Any]) -> str:
    from paperclaw import config
    home = _find_home(Path(base_dir))
    out: list[str] = []

    # 1. Compute — the rendered HARDWARE.md (local + SSH remotes), if detected.
    hw_md = home / "HARDWARE.md"
    if hw_md.is_file():
        out.append("## Compute resources (local + SSH remotes)\n"
                   + hw_md.read_text(encoding="utf-8", errors="replace")[:6000])
    else:
        out.append("## Compute resources\n(No hardware snapshot yet — run `paperclaw hardware "
                   "detect`, or Settings → Hardware → Detect now.)")

    # 2. Experiment-execution config + configured SSH remotes.
    hw: dict = {}
    try:
        hw = json.loads((home / "hardware.json").read_text())
    except (OSError, ValueError):
        pass
    rc = hw.get("runConfig") or {}
    mode = rc.get("experimentMode") or "cli"
    targets = hw.get("sshTargets") or []
    lines = [f"- mode: {mode}"]
    if mode == "cli" and rc.get("agentCommand"):
        lines.append(f"- coding-agent CLI: `{rc['agentCommand']}`")
    if rc.get("sshTargetId"):
        lines.append(f"- SSH target in use: {rc['sshTargetId']}")
    lines.append("- SSH remotes: " + (", ".join(
        f"{t.get('id') or '?'} ({t.get('host', '?')})" for t in targets) if targets else "none configured"))
    out.append("## Experiment execution\n" + "\n".join(lines))

    # 3. LLM / coding agent (provider/model; key presence only — NEVER the value).
    try:
        s = config.load_settings(home)
        out.append("## LLM / coding agent\n"
                   f"- provider: {s.provider}\n"
                   f"- model: {s.model}\n"
                   f"- base_url: {s.base_url or '(provider default)'}\n"
                   f"- API key: {'configured' if s.api_key else 'MISSING'}")
    except Exception as exc:  # never let a settings read break the tool
        out.append(f"## LLM\n(could not read settings: {exc})")

    return "\n\n".join(out)
