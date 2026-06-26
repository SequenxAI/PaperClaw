"""bash_output tool — poll a detached background job (launched by `bash` with
run_in_background=true). Returns the job's status (running / done+exit-code / stopped) and its
recent output, or a summary of ALL jobs when no id is given; can also stop a job.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from paperclaw.tools import background

SCHEMA: dict[str, Any] = {
    "name": "bash_output",
    "description": (
        "Check a background job started by `bash` (run_in_background=true): its status and the "
        "most recent output. Pass the job_id you got back; omit it to list ALL jobs. Set "
        "kill=true to stop the job. Use this to report run progress instead of blocking — if a "
        "job is still running, end your turn and check again next time."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "job_id": {"type": "string", "description": "Job id from `bash` run_in_background. "
                       "Omit to list all jobs."},
            "lines": {"type": "integer", "description": "How many trailing log lines to show "
                      "(default 40)."},
            "kill": {"type": "boolean", "description": "Stop the job (SIGTERM its process group)."},
        },
        "required": [],
    },
}


def execute(base_dir: Path, inputs: dict[str, Any]) -> str:
    job_id = (inputs.get("job_id") or "").strip() or None
    lines = inputs.get("lines")
    lines = int(lines) if isinstance(lines, (int, float, str)) and str(lines).isdigit() else 40
    return background.status(base_dir, job_id=job_id, lines=lines, kill=bool(inputs.get("kill")))
