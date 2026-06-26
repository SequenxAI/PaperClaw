"""Built-in benchmark templates + the prompts that inject them.

A *benchmark template* pins a FIXED experimental protocol (datasets, metric(s),
settings) plus a PUBLISHED leaderboard whose rows are CITED — literature numbers, not
measured by us. When one is selected for an idea it reframes the whole run: the agent
runs ONLY the new method on this exact protocol, and the paper's main results table
reuses these cited baseline rows beside the measured row(s).

Each template is markdown with three sections — ``## Protocol``, ``## Published results``
(a table whose method rows carry a cite key), ``## References`` (BibTeX for those keys).
The first ``# heading`` is the display title. Built-ins are seeded into
``<home>/benchmarks/`` on first use (disk I/O lives in ``paperclaw/benchmarks.py``).

ANTI-FABRICATION: built-ins ship the protocol + canonical baselines + their REAL
citations, but leave the metric cells as ``<from {cite}>`` — we NEVER seed invented
numbers. Fill them by pasting the published table or via ``/setup_benchmark <paper>``.
"""

# Appended to the pipeline's base_ctx when a benchmark is active (mirrors CODEBASE_NOTE).
# Injected via .replace("{benchmark}", md) — NOT str.format — so the literal \cite{key}
# braces below survive untouched.
BENCHMARK_NOTE = """

## Benchmark template (FIXED protocol + PUBLISHED, CITED baselines) — BINDING
A benchmark template is in force for this idea. Treat it as binding:
- The PROTOCOL (datasets, metric(s), horizons/lookback/splits) is FIXED — use it EXACTLY.
- The leaderboard rows are PUBLISHED baseline numbers from the literature (each with a cite
  key); they are GIVEN. Do NOT re-run, re-train, or re-tune the baselines, and do NOT change
  the protocol.
- Implement and run ONLY the new/proposed method on this exact protocol and produce its numbers
  on the SAME datasets/metric(s), so they line up row-for-row with the table.
- A result is SUPPORTED when the new method MATCHES or BEATS the cited baseline(s) on the PRIMARY
  metric by a consistent, realistic margin (never "best on everything").
- The paper's MAIN RESULTS table = these cited baseline rows (verbatim, each with its \\cite{key})
  + the measured new-method row(s); clearly attribute literature numbers vs measured-here. Keep the
  table's layout as in the benchmark/source paper, and BOLD the best value in each metric column
  (lowest for error metrics, highest for score metrics) — including the new method's cell when it wins.

{benchmark}
"""

# Directive for the /setup_benchmark chat skill (extract a leaderboard from a named paper).
# Modeled on SETUP_VENUE_DIRECTIVE; runs in a DOMAIN chat (sandboxed to the domain dir).
SETUP_BENCHMARK_DIRECTIVE = """

You are setting up a BENCHMARK TEMPLATE for this domain{paper}. Do this and nothing else:

★ HARD RULE — every number in the table MUST come from a paper you ACTUALLY READ this turn. Do NOT
fill any value from an abstract's prose, a search snippet, or your own prior knowledge/memory. If
you didn't read a cell's value in the paper's results table, leave that cell BLANK. Never invent,
round, or approximate. Keep it BOUNDED: read AT MOST ~3 papers; if a value isn't found quickly,
leave it blank and FINISH — do NOT loop or keep retrying.

1. Find the source(s): if a paper / arXiv id / URL was named, use it; otherwise `openalex_search` /
   `web_search` (+ the domain's Crucial Papers) for the standard benchmark and a paper that
   tabulates the leaderboard (a survey/SOTA paper usually lists several methods at once — prefer one).
2. READ the actual text of each source (this is the ONLY way to get real numbers):
   - PREFER the HTML full text — `fetch_url` the paper's HTML (e.g. `https://ar5iv.org/abs/<arxiv-id>`,
     or a project / Papers-with-Code page). HTML pages include the RESULTS TABLES as text.
   - Only if no HTML is available: `download_file("<pdf-url>", "paper.pdf")` to bring the PDF INTO the
     workspace, THEN `read_pdf("paper.pdf", page=N)` to page through it. (`read_pdf` reads a LOCAL
     workspace file — it CANNOT open a URL, so you must download first.)
   Read the protocol too (datasets, metric(s), horizons/lookback/splits).
3. Transcribe ONLY what you read: the protocol + the leaderboard (method × dataset/metric → value),
   copying numbers EXACTLY. Blank what you couldn't find.
   - COPY THE FULL DETAIL: if the paper reports detailed results (e.g. per-horizon 96/192/336/720,
     per-setting, or per-subset breakdowns — not just an average), transcribe EVERY detailed cell,
     not the averaged summary alone. Prefer the most granular table the paper gives; keep the
     averages too if shown. Detailed numbers are what make the comparison faithful.
   - FOLLOW THE ORIGINAL PAPER'S TABLE FORMAT: keep the same structure — same rows vs columns
     (which axis is the method, which is the dataset/horizon/metric), the same method ORDER, and
     the same dataset/horizon grouping. Do NOT transpose, re-order, merge, or reorganize it.
   - BOLD the BEST result in each metric column with markdown (`**0.123**`) — the lowest value for
     error metrics (MSE/MAE/RMSE…), the highest for score metrics (accuracy/F1/AUC…); note the
     metric's direction (↓ lower-is-better / ↑ higher) in the header. Bold only a value you read.
4. For every method row, add its source paper's real citation with the `cite` tool (verified BibTeX
   + key) and put that key in the table's `cite` column.
5. `write_file` `benchmarks/<short-name>.md` with: `# <Title>`, `## Protocol`, `## Published results`
   (a markdown table; last column `cite` = the cite key), and `## References` (the BibTeX entries).
6. Confirm in ONE sentence what benchmark you saved, how many methods/datasets it covers, and which
   paper(s) you read.
"""


# name → markdown, seeded into <home>/benchmarks/ on first use. INTENTIONALLY EMPTY:
# the benchmark library starts empty and a run defaults to NO benchmark — users create
# one by pasting/uploading a table or via `/setup_benchmark <paper>` (no built-in default).
BUILTIN_BENCHMARKS: dict[str, str] = {}
