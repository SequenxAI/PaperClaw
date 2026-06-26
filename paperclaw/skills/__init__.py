"""Slash-command skill registry for PaperClaw.

Skills are the commands surfaced by the `/` menu in the chat interface.
Each skill has a command string (e.g. `/create_domain`) and a description
shown to the user.

Skill *execution* (the actual handler logic) lives in service.py; this
package only holds the registry and command-name constants so both the
HTTP route and the service can import from a single source of truth.
"""

from paperclaw.skills.registry import (
    GENERATE_PLAN_COMMAND,
    GENERATE_REPORT_COMMAND,
    HYPOTHESIS_MAP_COMMAND,
    IDEA_GENERATION_COMMAND,
    PIN_IDEA_COMMAND,
    SETUP_BENCHMARK_COMMAND,
    SETUP_CODEBASE_COMMAND,
    SETUP_VENUE_COMMAND,
    SKILLS,
    VALIDATE_REFERENCES_COMMAND,
    WRITE_PAPER_COMMAND,
)

__all__ = ["SKILLS", "PIN_IDEA_COMMAND", "IDEA_GENERATION_COMMAND",
           "HYPOTHESIS_MAP_COMMAND", "GENERATE_PLAN_COMMAND", "GENERATE_REPORT_COMMAND",
           "WRITE_PAPER_COMMAND", "SETUP_VENUE_COMMAND", "SETUP_CODEBASE_COMMAND",
           "SETUP_BENCHMARK_COMMAND", "VALIDATE_REFERENCES_COMMAND"]
