# PaperClaw — Environment setup guide

How to point PaperClaw at your model, keys, compute, and LaTeX toolchain — then verify it all
with one command. Every setting can be applied **three ways** (they share one backend):

- **Web UI** — **⚙️ Settings** (gear, bottom-left).
- **`settings.yaml`** — drop one in the project dir (copy [`settings.example.yaml`](../settings.example.yaml)); read by backend **and** CLI on start. See the [README](../README.md).
- **CLI / env vars** — `paperclaw settings set …`, `paperclaw hardware …`, or `PAPERCLAW_*` env vars / `.env`.

> **Precedence (highest first):** env vars → `.env` (cwd) → `.env` (`$PAPERCLAW_HOME`) →
> `$PAPERCLAW_HOME/settings.yaml` (written by the Settings UI / `settings set`) →
> `./settings.yaml` (project-dir DEFAULT). So a change saved in the Settings UI persists and
> overrides the project-dir default. Secrets stay server-side (`saves/settings.yaml`, mode `600`) and are never sent to the browser.

### At a glance

| Setting | What it's for | Where |
|---|---|---|
| [LLM provider/model/key](#1-llm-provider-model--api-key) | the brain — chat, planning, paper writing | `LLM:` in `settings.yaml` · `settings set` · `PAPERCLAW_*` |
| [Image generation](#2-image-generation-optional) | paper figures (intro/method diagrams) | `image_generation:` · `settings set --image-*` |
| [Academic search](#3-academic-search--openalex-optional) | literature survey, SOTA, references | `academic_search.open_alex` · `OPENALEX_API_KEY` |
| [Experiment execution](#4-experiment-execution) | how experiments actually run | `paperclaw hardware run-config` |
| [Hardware & SSH](#5-hardware--ssh-remotes) | detect CPU/GPU/mem; add remotes | `paperclaw hardware {detect,ssh-add}` |
| [LaTeX toolchain](#6-latex-toolchain) | compile the paper to PDF | `tectonic` or TeX Live (auto-detected) |
| [Benchmark templates](#8-benchmark-templates-optional) | beat published SOTA on a fixed protocol | `paperclaw benchmark` · `/setup_benchmark` |
| [Doctor](#7-verify-with-the-doctor) | one-shot readiness check | `paperclaw doctor` |

---

## 1. LLM provider, model & API key

The core setting. PaperClaw talks to **Anthropic** (official SDK) or **any OpenAI-compatible**
endpoint (set `base_url` for a proxy / self-hosted / gateway). Default model `claude-opus-4-8`.

```yaml
# settings.yaml
LLM:
  provider: anthropic        # anthropic | openai
  base_url: null             # e.g. https://api.openai.com/v1, http://localhost:11434/v1 (Ollama)
  api_key: ""
  model: claude-opus-4-8
```

```bash
# …or persist via CLI
paperclaw settings set --provider anthropic --model claude-opus-4-8 --api-key sk-…
```

| Env var | Field |
|---|---|
| `PAPERCLAW_PROVIDER` | `anthropic` \| `openai` |
| `PAPERCLAW_BASE_URL` | proxy / self-hosted endpoint |
| `PAPERCLAW_MODEL` | model id |
| `PAPERCLAW_API_KEY` | API key (`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` are provider-matched fallbacks) |

> Without a key the app still runs and replies with a configuration hint — nothing crashes.

---

## 2. Image generation (optional)

An OpenAI-style **images** endpoint for paper figures (intro/method diagrams). Leave the key
empty to disable — PaperClaw then falls back to matplotlib / TikZ figures.

```yaml
image_generation:
  base_url: null             # null = OpenAI images endpoint; set for a proxy
  api_key: ""
  model: null                # e.g. gpt-image-1, dall-e-3
```

```bash
paperclaw settings set --image-api-key sk-… --image-model gpt-image-1
```

Env vars: `PAPERCLAW_IMAGE_BASE_URL`, `PAPERCLAW_IMAGE_API_KEY`, `PAPERCLAW_IMAGE_MODEL`.

---

## 3. Academic search — OpenAlex (optional)

PaperClaw surveys [OpenAlex](https://openalex.org) live for the domain survey, SOTA papers, and
references. OpenAlex budget-limits **anonymous** (per-IP) requests, so without a key a survey can
return *"Found 0 papers"*. A free key gets a dedicated budget.

```yaml
academic_search:
  open_alex:
    api_key: ""
```

```bash
paperclaw settings set --openalex-api-key oa-…     # or env: OPENALEX_API_KEY
```

---

## 4. Experiment execution

How the hypothesis experiments actually run. Set the mode once; every run uses it.

```bash
paperclaw hardware run-config --mode cli           # show current with: paperclaw hardware run-config
```

| Mode | Meaning |
|---|---|
| **`cli`** *(default, real)* | delegates each experiment to a headless coding-agent CLI (e.g. the `claude` CLI). Streams its output live; reads `results.json` when done. **Recommended.** |
| **`executed`** *(real)* | drives the configured LLM in-process through a write→run→inspect→fix coding loop. Used automatically if `cli` is selected but its binary isn't on PATH. |
| **`ssh`** *(🧪 beta)* | the **same agentic bash loop, run on a configured SSH remote** — the LLM authors code locally and runs everything with `bash` on the remote box (it sets up the env in a login shell), then `results.json` + figures are pulled back. Needs **only** the SSH remote — no interpreter or other settings. |
| **`simulated`** *(NOT real)* | the LLM narrates **fabricated** results — nothing runs. For a quick demo only; avoid for real research. |

```bash
# cli mode uses an external agent's own auth/model (inherited env), not settings.yaml:
paperclaw hardware run-config --mode cli \
  --agent-command 'claude -p {prompt} --dangerously-skip-permissions'
# ssh mode: just point at a remote you added (see §5) — that's the only setting it needs:
paperclaw hardware run-config --mode ssh --ssh-target gpu-box
```

> The `claude` CLI ([Claude Code](https://claude.com/claude-code)) is the recommended coding
> agent for `cli` mode — the doctor detects it and tells you which mode is active.

---

## 5. Hardware & SSH remotes

PaperClaw probes CPU / GPU / memory / disk on the **local** host and any **SSH remotes**, writes
`HARDWARE.md`, and plans experiments against the compute you actually have.

```bash
paperclaw hardware detect            # probe local + remotes now → HARDWARE.md
paperclaw hardware show              # the saved snapshot + remotes
paperclaw hardware ssh-add --host gpu.box --user me --key ~/.ssh/id_ed25519 --label "A100 box"
paperclaw hardware ssh-remove <id>
```

In the web UI this lives under **⚙️ Settings → Hardware** ("Detect now", add remotes); the
read-only snapshot shows in the right-column **Resources** tab.

---

## 6. LaTeX toolchain

The paper stage compiles LaTeX → PDF. PaperClaw uses, in order:

1. **`latexmk`** on a real **TeX Live** (runs pdf/xe/lua-LaTeX + bibtex/biber; auto-installs
   missing packages via `tlmgr`),
2. **`tectonic`** (self-contained) as the fallback when no full TeX Live is installed,
3. raw `pdflatex` as a last resort.

You don't configure this — it's auto-detected. Install **either** `tectonic` (simplest, one
binary) **or** a TeX Live distribution; the doctor reports which engines it found.

---

## 7. Verify with the doctor

One command (no LLM calls) checks the whole environment is ready — PaperClaw home (writable),
LLM config, chat agent, the coding agent (`claude` CLI), the LaTeX toolchain, and image
generation. Each check is `ok | warn | fail`; only a `fail` blocks readiness.

```bash
paperclaw doctor        # exit 0 when ready, 1 otherwise
```

In the web UI: **⚙️ Settings → 🩺 Doctor**.

---

## 8. Benchmark templates (optional)

In fields like time-series forecasting, work is judged by **beating published SOTA on a standard
benchmark** — and those competitor numbers are *already published*, so you cite them rather than
re-run them. A **benchmark template** pins a fixed protocol + a published, cited leaderboard and
reframes the whole run: the agent runs **only the new method** on that exact protocol and the
paper's main results table reuses the **cited baseline rows + your measured row(s)**.

A template is markdown with three sections — `## Protocol` (datasets / metric(s) / horizons /
splits), `## Published results` (a table whose method rows carry a cite key), and `## References`
(BibTeX for those keys). Templates are **per-domain** (reusable across the field's ideas), with a
global fallback. The library starts **empty** — there's no default benchmark; you create one.

```bash
paperclaw benchmark list                          # your templates (global + domain) — empty at first
paperclaw benchmark add ltsf --domain <id> --file ltsf.md   # paste your published table
paperclaw benchmark show ltsf                      # view it
paperclaw run --idea <id> --benchmark ltsf         # reframe the run around it
```

Two ways to fill one in (numbers must be **real** — never invented):
- **Paste/upload** a results table (`paperclaw benchmark add`, or the 📊 picker's ⬆ in the Auto-run
  settings) — you provide the values + citations.
- **Extract from a paper** — in a **domain chat**, `/setup_benchmark <arxiv-id | url | title>`: the
  agent reads the paper's table, writes `benchmarks/<name>.md`, and `cite`s the source.

When a benchmark is active the run merges its cited BibTeX into the idea's `ref.bib` so the paper
can `\cite` the baseline rows. Pick one per run in **⚡ Auto run → Benchmark**.

---

Configuration is shared across web and CLI, so set it once and use either. For the full command
set, see the [CLI mode](../README.md#-2-cli-mode) section of the README.
