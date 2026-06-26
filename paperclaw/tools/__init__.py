"""Research workspace tools available to the LLM agent.

Each module exposes:
  SCHEMA   — Anthropic tool_use definition (passed in tools=[...])
  execute  — callable(base_dir: Path, inputs: dict) -> str

``ALL_TOOLS`` is the list of schemas; ``EXECUTORS`` maps name → execute fn.
"""

from pathlib import Path
from typing import Any, Callable

from paperclaw.tools import (
    apply_patch,
    bash,
    bash_output,
    cite,
    fetch_url,
    hypothesis,
    list_exp_resources,
    list_files,
    openalex_search,
    read_file,
    read_image,
    read_pdf,
    web_search,
    write_file,
)

ALL_TOOLS: list[dict[str, Any]] = [
    apply_patch.SCHEMA,
    bash.SCHEMA,
    bash_output.SCHEMA,
    read_file.SCHEMA,
    read_image.SCHEMA,
    read_pdf.SCHEMA,
    write_file.SCHEMA,
    list_files.SCHEMA,
    list_exp_resources.SCHEMA,
    openalex_search.SCHEMA,
    web_search.SCHEMA,
    fetch_url.SCHEMA,
    cite.SCHEMA,
    hypothesis.ADD_SCHEMA,
    hypothesis.UPDATE_SCHEMA,
    hypothesis.REMOVE_SCHEMA,
]

# An executor returns a tool-result payload: usually a string, but read_image
# returns a list of Anthropic content blocks (text + image) so the vision model
# can see the figure. The tool loops handle both.
EXECUTORS: dict[str, Callable[[Path, dict[str, Any]], "str | list[dict[str, Any]]"]] = {
    "apply_patch":        apply_patch.execute,
    "bash":               bash.execute,
    "bash_output":        bash_output.execute,
    "read_file":          read_file.execute,
    "read_image":         read_image.execute,
    "read_pdf":           read_pdf.execute,
    "write_file":         write_file.execute,
    "list_files":         list_files.execute,
    "list_exp_resources": list_exp_resources.execute,
    "openalex_search":    openalex_search.execute,
    "web_search":         web_search.execute,
    "fetch_url":          fetch_url.execute,
    "cite":               cite.execute,
    "hypothesis_add":     hypothesis.add,
    "hypothesis_update":  hypothesis.update,
    "hypothesis_remove":  hypothesis.remove,
}

# Tools that modify a workspace file (so callers can detect spec changes / refreshes).
WRITE_TOOLS: frozenset[str] = frozenset({"apply_patch", "write_file"})
