"""Application home directory and persisted settings."""

import os
from pathlib import Path

import yaml
from pydantic import BaseModel

DEFAULT_MODEL = "claude-opus-4-8"


def paperclaw_home() -> Path:
    """Workspace root: $PAPERCLAW_HOME, else ``./saves`` (relative to the working dir).

    Always returned ABSOLUTE — so detached children we spawn (experiment jobs,
    `auto` runs) that inherit PAPERCLAW_HOME and run with a different cwd resolve to the
    SAME directory instead of doubling a relative path (e.g. ``saves/saves``)."""
    return Path(os.environ.get("PAPERCLAW_HOME", "saves")).resolve()


def claude_cli_available() -> bool:
    """True if the `claude` (Claude Code) CLI is on PATH — the recommended headless
    coding agent for experiments (`cli` experiment mode)."""
    import shutil
    return shutil.which("claude") is not None


class LLMSettings(BaseModel):
    provider: str = "anthropic"  # "anthropic" | "openai" (OpenAI-compatible)
    base_url: str | None = None
    api_key: str = ""
    model: str = DEFAULT_MODEL
    # Chat file editor: "deepagents" (default — used when the optional package is
    # installed; otherwise falls back to "builtin" automatically) | "builtin".
    # NOT a settings file field anymore — it always defaults to "deepagents"; power
    # users can still override it with the PAPERCLAW_CHAT_AGENT env var.
    chat_agent: str = "deepagents"
    # Image generation API for paper figures (intro/method diagrams). OpenAI-style
    # images endpoint by default; base_url None = OpenAI. Empty key = disabled.
    image_base_url: str | None = None
    image_api_key: str = ""
    image_model: str | None = None  # e.g. "gpt-image-1", "dall-e-3"
    # OpenAlex literature search (domain survey, SOTA, references). OpenAlex now
    # budget-limits anonymous (per-IP) requests; an api_key gets a dedicated budget.
    # Empty = anonymous (may hit HTTP 429).
    openalex_api_key: str = ""


# Config files, in read-precedence order. We write YAML (comments-friendly) but
# still READ a legacy settings.json (JSON is valid YAML, so the parser handles both).
SETTINGS_FILENAMES = ("settings.yaml", "settings.yml", "settings.json")


def settings_path(home: Path) -> Path:
    """Canonical path settings are WRITTEN to (YAML)."""
    return home / "settings.yaml"


def _resolve_settings_file(directory: Path) -> Path | None:
    """First existing settings file in ``directory`` (yaml > yml > json), else None."""
    for name in SETTINGS_FILENAMES:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def _settings_from_config(raw: dict) -> LLMSettings:
    """Parse the on-disk settings.yaml. The file groups keys into sub-dicts:

        LLM:              {provider, base_url, api_key, model}
        image_generation: {base_url, api_key, model}
        academic_search:  {open_alex: {api_key}}

    Backward-compatible with the old FLAT layout (top-level provider/api_key/…).
    `chat_agent` is intentionally NOT read from the file (always defaults to
    "deepagents"; override via PAPERCLAW_CHAT_AGENT).
    """
    llm = raw.get("LLM") or raw.get("llm") or {}
    img = raw.get("image_generation") or raw.get("image") or {}
    acad = raw.get("academic_search") or {}
    open_alex = (acad.get("open_alex") or acad.get("openalex") or {}) if isinstance(acad, dict) else {}
    data = {
        "provider": llm.get("provider", raw.get("provider", "anthropic")),
        "base_url": llm.get("base_url", raw.get("base_url")),
        "api_key": llm.get("api_key", raw.get("api_key", "")),
        "model": llm.get("model", raw.get("model", DEFAULT_MODEL)),
        "image_base_url": img.get("base_url", raw.get("image_base_url")),
        "image_api_key": img.get("api_key", raw.get("image_api_key", "")),
        "image_model": img.get("model", raw.get("image_model")),
        "openalex_api_key": open_alex.get("api_key", raw.get("openalex_api_key", "")),
    }
    return LLMSettings.model_validate(data)


def _settings_to_config(settings: LLMSettings) -> dict:
    """Serialize to the nested on-disk layout (no `chat_agent` — see LLMSettings)."""
    return {
        "LLM": {
            "provider": settings.provider,
            "base_url": settings.base_url,
            "api_key": settings.api_key,
            "model": settings.model,
        },
        "image_generation": {
            "base_url": settings.image_base_url,
            "api_key": settings.image_api_key,
            "model": settings.image_model,
        },
        "academic_search": {
            "open_alex": {"api_key": settings.openalex_api_key},
        },
    }


def _parse_env_file(path: Path) -> dict[str, str]:
    """Minimal .env parser: KEY=VALUE lines, # comments, optional quotes."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip("'\"")
    return out


def _deep_merge(base: dict, over: dict) -> dict:
    """Recursively merge ``over`` onto ``base`` (``over`` wins); nested dicts merge per-key."""
    out = dict(base)
    for k, v in over.items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_settings(home: Path) -> LLMSettings:
    """Settings precedence (highest first): env vars > .env (cwd, then PaperClaw home)
    > ``$PAPERCLAW_HOME/settings.yaml`` (the UI / ``settings set`` writes here)
    > ``./settings.yaml`` (project working-directory DEFAULT).

    The project-directory ``settings.yaml`` is a no-command DEFAULT (copy
    ``settings.example.yaml``) for first run / CLI from a repo; the persisted home file
    OVERRIDES it, so a change made in the Settings UI actually takes effect (and each backend
    instance uses its OWN ``$PAPERCLAW_HOME`` file rather than the shared cwd one). YAML keeps
    the file commentable; a legacy ``settings.json`` is still read (JSON is valid YAML).
    Recognized env keys: PAPERCLAW_PROVIDER, PAPERCLAW_BASE_URL, PAPERCLAW_MODEL,
    PAPERCLAW_API_KEY — plus ANTHROPIC_API_KEY / OPENAI_API_KEY as provider-matched fallbacks.
    """
    settings = LLMSettings()
    # Merge cwd ./settings.* (DEFAULT, lower) under $PAPERCLAW_HOME/settings.* (UI-written,
    # higher) so the persisted home config wins. If home == cwd they resolve to one file.
    merged: dict = {}
    for path in (_resolve_settings_file(Path.cwd()), _resolve_settings_file(home)):
        if path is not None:
            try:
                merged = _deep_merge(merged, yaml.safe_load(path.read_text()) or {})
            except Exception:
                pass
    if merged:
        settings = _settings_from_config(merged)

    env: dict[str, str] = {}
    env.update(_parse_env_file(home / ".env"))
    env.update(_parse_env_file(Path.cwd() / ".env"))
    env.update({k: v for k, v in os.environ.items() if v})

    if env.get("PAPERCLAW_PROVIDER") in ("anthropic", "openai"):
        settings.provider = env["PAPERCLAW_PROVIDER"]
    if env.get("PAPERCLAW_BASE_URL"):
        settings.base_url = env["PAPERCLAW_BASE_URL"]
    if env.get("PAPERCLAW_MODEL"):
        settings.model = env["PAPERCLAW_MODEL"]
    if env.get("PAPERCLAW_CHAT_AGENT"):
        settings.chat_agent = env["PAPERCLAW_CHAT_AGENT"]
    if env.get("PAPERCLAW_IMAGE_BASE_URL"):
        settings.image_base_url = env["PAPERCLAW_IMAGE_BASE_URL"]
    if env.get("PAPERCLAW_IMAGE_API_KEY"):
        settings.image_api_key = env["PAPERCLAW_IMAGE_API_KEY"]
    if env.get("PAPERCLAW_IMAGE_MODEL"):
        settings.image_model = env["PAPERCLAW_IMAGE_MODEL"]
    if env.get("PAPERCLAW_API_KEY"):
        settings.api_key = env["PAPERCLAW_API_KEY"]
    elif not settings.api_key:
        fallback = "ANTHROPIC_API_KEY" if settings.provider == "anthropic" else "OPENAI_API_KEY"
        if env.get(fallback):
            settings.api_key = env[fallback]
    # OpenAlex key: env (or .env) overrides the saved setting, like the LLM key.
    if env.get("OPENALEX_API_KEY"):
        settings.openalex_api_key = env["OPENALEX_API_KEY"]

    # Push the resolved OpenAlex key into the literature client so every entry
    # point (server / CLI / detached children all call load_settings) uses it.
    from paperclaw import literature
    literature.configure(settings.openalex_api_key)
    return settings


def save_settings(home: Path, settings: LLMSettings) -> None:
    home.mkdir(parents=True, exist_ok=True)
    path = settings_path(home)  # settings.yaml
    path.write_text(yaml.safe_dump(_settings_to_config(settings),
                                   sort_keys=False, default_flow_style=False, allow_unicode=True))
    os.chmod(path, 0o600)  # contains the API key
