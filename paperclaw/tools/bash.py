"""bash tool — run a shell command in the research workspace.

Lets the chat agent execute code (run/re-run scripts, inspect files, install a
package) in the idea/domain workspace. SECURITY: runs arbitrary shell as a
subprocess — for a trusted, self-hosted deployment (same trust model as the
experiment coding agent). Capped at 10 min; long training runs should go through
the detached hypothesis Experiment runner instead.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

_TIMEOUT = 600          # seconds — don't hang the chat on a runaway command
_OUTPUT_TAIL = 8_000    # chars of combined output returned

SCHEMA: dict[str, Any] = {
    "name": "bash",
    "description": (
        "Run a shell command in the CURRENT workspace folder (inherits the server's env, "
        "so python/conda are on PATH) and get its combined stdout+stderr and exit code. Use "
        "it to run or re-run scripts (e.g. `python run.py`), inspect/grep files, or install a "
        "package. A foreground command is capped at 10 minutes AND cannot be interrupted by "
        "Stop. For a LONG job (training, a full rerun) set run_in_background=true: it launches "
        "DETACHED, returns a job id immediately, keeps running after this turn / a restart, and "
        "you poll it with the `bash_output` tool. Don't block waiting on a long job."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The shell command to run."},
            "run_in_background": {
                "type": "boolean",
                "description": "Launch detached and return a job id immediately (for long jobs); "
                               "poll it with bash_output. Default false (run in foreground).",
            },
        },
        "required": ["command"],
    },
}


def execute(base_dir: Path, inputs: dict[str, Any]) -> str:
    command = (inputs.get("command") or "").strip()
    if not command:
        return "Error: 'command' is required."
    if inputs.get("run_in_background"):
        from paperclaw.tools import background
        return background.launch(base_dir, command)
    try:
        p = subprocess.run(["bash", "-c", command], cwd=str(base_dir),
                           capture_output=True, text=True, timeout=_TIMEOUT)
    except subprocess.TimeoutExpired:
        return (f"[timed out after {_TIMEOUT}s — for a long experiment use the detached "
                "Experiment runner instead of bash]")
    except Exception as exc:  # bash missing, cwd gone, …
        return f"[failed to run: {exc}]"
    out = (p.stdout or "") + (("\n[stderr]\n" + p.stderr) if p.stderr else "")
    out = out[-_OUTPUT_TAIL:].strip()
    return f"(exit {p.returncode})\n{out}" if out else f"(exit {p.returncode}, no output)"
