"""Skill (slash-command) definitions — names, descriptions, and command constants."""

from paperclaw.server.models import Skill

PIN_IDEA_COMMAND = "/pin_idea"
IDEA_GENERATION_COMMAND = "/idea_generation"
HYPOTHESIS_MAP_COMMAND = "/generate_hypothesis_map"
GENERATE_PLAN_COMMAND = "/generate_plan"
GENERATE_REPORT_COMMAND = "/generate_report"
WRITE_PAPER_COMMAND = "/write_paper"
SETUP_VENUE_COMMAND = "/setup_venue"
SETUP_CODEBASE_COMMAND = "/setup_codebase"
SETUP_BENCHMARK_COMMAND = "/setup_benchmark"
VALIDATE_REFERENCES_COMMAND = "/validate_references"

SKILLS: list[Skill] = [
    Skill(
        command="/create_domain",
        description="Guided domain creation — step-by-step questions (papers, datasets, libraries)",
    ),
    Skill(
        command=PIN_IDEA_COMMAND,
        description="Pin the current brainstormed draft into the Ideas panel (creates IDEA.md)",
    ),
    Skill(
        command=IDEA_GENERATION_COMMAND,
        description="Crystallize the current conversation into a new Idea (creates IDEA.md)",
    ),
    Skill(
        command=HYPOTHESIS_MAP_COMMAND, requires_idea=True,
        description="Agent generates the hypothesis map for the current idea (writes .hypothesis_map.json)",
    ),
    Skill(
        command=GENERATE_PLAN_COMMAND, requires_idea=True,
        description="Agent writes a hypothesis's testing plan: /generate_plan <id> (writes hypotheses/<id>/plan.md)",
    ),
    Skill(
        command=GENERATE_REPORT_COMMAND, requires_idea=True,
        description="Agent writes a hypothesis's report + proposes follow-ups: /generate_report <id>",
    ),
    Skill(
        command=WRITE_PAPER_COMMAND, requires_idea=True,
        description="Agent writes a paper from selected verified hypotheses; --style <name> picks a writing style",
    ),
    Skill(
        command=VALIDATE_REFERENCES_COMMAND, requires_idea=True,
        description="Validate every ref.bib entry against Crossref/OpenAlex, streaming each result",
    ),
    Skill(
        command=SETUP_VENUE_COMMAND, requires_idea=True,
        description="Agent downloads the target venue's LaTeX template + writes venue/STYLE.md",
    ),
    Skill(
        command=SETUP_CODEBASE_COMMAND,
        description="Set a domain's reference codebase: /setup_codebase <github-url> (downloads it for experiments)",
    ),
    Skill(
        command=SETUP_BENCHMARK_COMMAND,
        description="Build a domain's benchmark template from a paper: /setup_benchmark <arxiv|url|title> (run in a domain chat)",
    ),
]
