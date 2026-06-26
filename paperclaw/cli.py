"""Command-line interface for PaperClaw.

Two modes, mirroring the web frontend feature-for-feature:
  - without --backend: runs locally (files under $PAPERCLAW_HOME, config from
    env vars / .env / settings.yaml)
  - with --backend [URL]: connects to a running backend (URL defaults to
    http://127.0.0.1:8230)
"""

import argparse
import sys

from paperclaw.client import DEFAULT_BACKEND, ClientError, make_client


def _fmt_ts(ts: float) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _print_question(q: dict) -> None:
    print(f"\n? {q['prompt']}")
    for i, opt in enumerate(q.get("options", []), 1):
        print(f"  {i}. {opt}")
    if q.get("allowFreeText", True):
        print("  (or type a free-text answer)")


def _print_reply(msg: dict) -> None:
    model = f"  [{msg['servedModel']}]" if msg.get("servedModel") else ""
    print(f"\nARX{model}:\n{msg['content']}")
    if msg.get("specUpdated"):
        print("  📋 spec updated")
    if msg.get("createdIdeaId"):
        print(f"  💡 idea created: {msg['createdIdeaId']}")
    if msg.get("createdDomainId"):
        print(f"  🌐 domain created: {msg['createdDomainId']}")
    if msg.get("question"):
        _print_question(msg["question"])


def _chat_ctx(args) -> dict:
    return {"idea_id": args.idea, "seed_id": args.seed, "domain_id": args.domain}


def cmd_chat(client, args) -> None:
    if args.interactive:
        print("Interactive chat — empty line or Ctrl-D to exit. Answer questions by number or text.")
        last_question = None
        while True:
            try:
                line = input("\nyou> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not line:
                break
            # numeric answer to the pending question dialog
            if last_question and line.isdigit():
                idx = int(line) - 1
                options = last_question.get("options", [])
                if 0 <= idx < len(options):
                    line = options[idx]
            _, reply = client.chat_send(line, **_chat_ctx(args))
            _print_reply(reply)
            last_question = reply.get("question")
    else:
        if not args.message:
            print("error: provide a MESSAGE or use --interactive", file=sys.stderr)
            sys.exit(2)
        _, reply = client.chat_send(" ".join(args.message), **_chat_ctx(args))
        _print_reply(reply)


def cmd_domain(client, args) -> None:
    if args.action == "list":
        for d in client.domains_list():
            mark = "✓" if d["isSelected"] else " "
            print(f"[{mark}] {d['id']}  {d['name']}")
    elif args.action == "create":
        d = client.domain_create(args.name)
        print(f"created {d['id']}  {d['name']}")
    elif args.action == "auto":
        error: list[str] = []
        result: list[dict] = []
        thinking_started: list[bool] = []

        def _on_domain_event(event: dict) -> None:
            t = event.get("type")
            if t == "status":
                print(f"  {event.get('message', '')}", flush=True)
            elif t == "search":
                papers = [*event.get("broad", []), *event.get("sota", [])]
                print(f"  🔍 OpenAlex returned {len(papers)} papers:", flush=True)
                for label in papers:
                    print(f"     · {label}", flush=True)
            elif t == "thinking":
                if not thinking_started:
                    print("  🧠 thinking…", flush=True)
                    thinking_started.append(True)
                print(event.get("text", ""), end="", flush=True)
            elif t == "codebase":
                if thinking_started:
                    print(flush=True); thinking_started.clear()
                print(f"  📦 {event.get('message', '')} ({event.get('url', '')})", flush=True)
            elif t == "done":
                if thinking_started:
                    print(flush=True)  # close the thinking line
                result.append(event.get("result", {}))
            elif t == "error":
                error.append(event.get("message", "unknown error"))

        client.domain_auto_stream(args.prompt, _on_domain_event)
        if error:
            raise ClientError(error[0])
        if result:
            d = result[0]
            print(f"created {d['id']}  {d['name']}")
    elif args.action == "select":
        d = client.domain_select(args.id, not args.off)
        print(f"{d['id']} selected={d['isSelected']}")
    elif args.action == "show":
        print(client.domain_spec(args.id))
    elif args.action == "suggest":
        for s in client.domain_suggestions(args.id):
            print(f"· {s}")
    elif args.action == "codebase":
        if args.clear:
            d = client.domain_clear_codebase(args.id)
            print(f"{d['id']} reference codebase cleared")
        elif args.url:
            d = client.domain_set_codebase(args.id, args.url)
            print(f"{d['id']} reference codebase: {d.get('codebaseUrl')} "
                  f"({d.get('codebaseFiles', 0)} files)")
        else:
            d = next((x for x in client.domains_list() if x["id"] == args.id), None)
            if not d:
                raise ClientError("Domain not found")
            url = d.get("codebaseUrl")
            print(f"{d['id']} reference codebase: {url or '(none)'} "
                  f"({d.get('codebaseFiles', 0)} files)")
    elif args.action == "delete":
        client.domain_delete(args.id)
        print("deleted")


def cmd_brainstorm(client, args) -> None:
    if args.action == "list":
        for s in client.seeds_list():
            badge = "📝" if s.get("draft") else "·"
            print(f"{badge} {s['id']}  {s['text']}")
    elif args.action == "add":
        s = client.seed_add(args.text)
        print(f"added {s['id']}")
    elif args.action == "generate":
        seeds: list[dict] = []
        error: list[str] = []

        def _on_seeds_event(event: dict) -> None:
            t = event.get("type")
            if t == "status":
                print(f"  {event.get('message', '')}", flush=True)
            elif t == "done":
                seeds.extend(event.get("results", []))
            elif t == "error":
                error.append(event.get("message", "unknown error"))

        client.seeds_generate_stream(
            _on_seeds_event, hint=args.hint,
            idea_types=args.type or None, emphasis=args.emphasis or None, count=args.count,
        )
        if error:
            raise ClientError(error[0])
        for s in seeds:
            print(f"📝 {s['id']}  {s['text']}")
    elif args.action == "show":
        seeds = {s["id"]: s for s in client.seeds_list()}
        seed = seeds.get(args.id)
        if not seed:
            raise ClientError("Seed not found")
        print(seed.get("draft") or seed["text"])
    elif args.action == "pin":
        _, reply = client.chat_send("/pin_idea", seed_id=args.id)
        _print_reply(reply)
    elif args.action == "delete":
        client.seed_delete(args.id)
        print("deleted")


def cmd_idea(client, args) -> None:
    if args.action == "list":
        for i in client.ideas_list():
            mark = "●" if i["isActive"] else " "
            print(f"{mark} {i['id']}  {i['title']}")
    elif args.action == "create":
        i = client.idea_create(args.title)
        print(f"created {i['id']}  {i['title']}")
    elif args.action == "show":
        print(client.idea_spec(args.id))
    elif args.action == "duplicate":
        i = client.idea_duplicate(args.id)
        print(f"duplicated → {i['id']}  {i['title']}")
    elif args.action == "delete":
        client.idea_delete(args.id)
        print("deleted")
    elif args.action == "domains":
        names = {d["id"]: d["name"] for d in client.domains_list()}
        current = client.idea_domains_get(args.id)
        if args.clear:
            current = []
        elif args.set is not None:
            current = [x.strip() for x in args.set.split(",") if x.strip()]
        else:
            for did in (args.add or []):
                if did not in current:
                    current.append(did)
            current = [d for d in current if d not in (args.remove or [])]
        if args.clear or args.set is not None or args.add or args.remove:
            current = client.idea_domains_set(args.id, current)
        if not current:
            print("(no domains connected)")
        for did in current:
            print(f"{did}  {names.get(did, '?')}")
    elif args.action == "color":
        idea = client.idea_set_color(args.id, args.color)
        print(f"flagged {idea['id']} → {idea.get('color') or '(cleared)'}")
    elif args.action == "resources":
        changed = any(getattr(args, k) is not None for k in ("mode", "ssh", "codebase"))
        if changed:
            r = client.idea_resources_set(
                args.id, experiment_mode=args.mode, ssh_target_id=args.ssh,
                use_reference_codebase=args.codebase)
        else:
            r = client.idea_resources_get(args.id)
        print(f"compute: {r['experimentMode']}"
              + (f" → {r['sshTargetId']}" if r.get("sshTargetId") else "")
              + f"  | reference codebase: {'on' if r['useReferenceCodebase'] else 'off'}")
        print(f"LLM: {r['llmProvider']} · {r['llmModel']} · "
              f"{'key configured' if r['llmKeyConfigured'] else 'key MISSING'}")
        if r.get("sshTargets"):
            print("SSH hosts: " + ", ".join(f"{t['id']} ({t.get('host')})" for t in r["sshTargets"]))


def cmd_research(client, args) -> None:
    error: list[str] = []

    def on_event(ev: dict) -> None:
        t = ev.get("type")
        if t == "round":
            print(f"\n── Hypothesis {ev['round']} ──", flush=True)
        elif t == "phase":
            print(f"\n  ▶ {ev.get('label', '')}", flush=True)
        elif t in ("delta", "thinking"):
            print(ev.get("text", ""), end="", flush=True)
        elif t == "round_done":
            print(f"  → enough for paper: {ev.get('enough')}", flush=True)
        elif t == "compile":
            print(f"  LaTeX compile {'ok' if ev.get('ok') else 'failed'} (attempt {ev.get('attempt')})", flush=True)
        elif t == "page_check":
            verdict = "compliant" if ev.get("compliant") else "OVER LIMIT"
            print(f"  page check: {ev.get('pages')} pages / limit {ev.get('limit')} → {verdict}", flush=True)
        elif t == "paper_ready":
            print(f"  📄 paper ready: {ev.get('download_url')}", flush=True)
        elif t in ("error", "needs_domain"):
            error.append(ev.get("message", "unknown error"))
        elif t == "done":
            print("✓ research complete", flush=True)

    client.research_stream(
        args.idea, on_event, restart=args.restart,
        max_hypotheses=args.max_hypotheses, page_limit=args.page_limit,
        benchmark=getattr(args, "benchmark", None),
    )
    if error:
        raise ClientError(error[0])


def _fmt_age(ts: float) -> str:
    import time as _t
    s = max(0, int(_t.time() - (ts or 0)))
    return f"{s}s ago" if s < 60 else f"{s // 60}m ago" if s < 3600 else f"{s // 3600}h ago"


def _auto_on_event(error: list[str]):
    """Shared SSE event printer for `run` / `resume` — streams the pipeline to stdout
    and collects any error message into *error*."""
    def on_event(ev: dict) -> None:
        t = ev.get("type")
        if t == "phase":
            print(f"\n▶ {ev.get('label', '')}", flush=True)
        elif t == "doctor":
            print(f"  doctor: {'READY ✓' if ev.get('ok') else 'NOT READY ✗'}", flush=True)
            for c in ev.get("checks", []):
                if c.get("status") != "ok":
                    print(f"    [{c.get('status')}] {c.get('label')}: {c.get('detail')}", flush=True)
        elif t == "domain_created":
            print(f"  \U0001f310 domain: {ev.get('name')} ({ev.get('domainId')})", flush=True)
        elif t == "idea_created":
            print(f"  \U0001f4a1 idea: {ev.get('title')} ({ev.get('ideaId')})", flush=True)
        elif t == "round":
            print(f"\n── Hypothesis {ev['round']} ({ev.get('hypothesisId')}) ──", flush=True)
        elif t in ("delta", "thinking"):
            print(ev.get("text", ""), end="", flush=True)
        elif t == "hypothesis_status":
            print(f"\n  [{ev.get('hypothesisId')} → {ev.get('status')}]", flush=True)
        elif t == "round_done":
            print(f"  → positives so far: {ev.get('positives')}", flush=True)
        elif t == "compile":
            print(f"  LaTeX compile {'ok' if ev.get('ok') else 'failed'} (attempt {ev.get('attempt')})", flush=True)
        elif t == "page_check":
            verdict = "compliant" if ev.get("compliant") else "OVER LIMIT"
            print(f"  page check: {ev.get('pages')} pages / limit {ev.get('limit')} → {verdict}", flush=True)
        elif t == "paper_ready":
            print(f"\n  \U0001f4c4 paper ready: {ev.get('download_url')}", flush=True)
        elif t in ("error", "needs_domain"):
            error.append(ev.get("message", "unknown error"))
        elif t == "done":
            print("\n✓ auto research complete", flush=True)
    return on_event


def cmd_run(client, args) -> None:
    """Run the autonomous pipeline (doctor → domain → idea → hypotheses → paper)
    from a topic, an existing --idea, or a --config YAML."""
    error: list[str] = []
    on_event = _auto_on_event(error)
    # per-run overrides (else the global RunConfig / defaults are used)
    over = dict(
        experiment_mode=getattr(args, "experiment_mode", None),
        ssh_target_id=getattr(args, "ssh_target", None),
        writing_style=getattr(args, "style", None),
        benchmark=getattr(args, "benchmark", None),
        use_reference_codebase=not getattr(args, "no_codebase", False),
        fill_page=getattr(args, "fill_page", False),
    )
    if getattr(args, "idea", None):
        positive = args.positive if args.positive is not None else 2
        max_h = args.max_hypotheses if args.max_hypotheses is not None else 6
        page = args.page_limit if args.page_limit is not None else 8
        depth = args.max_depth if args.max_depth is not None else 3
        print(f"⚡ auto research on idea {args.idea}  (positive {positive} · max {max_h} · pages {page} · depth {depth})"
              "   (Ctrl+C or `paperclaw stop` to stop)")
        client.auto_idea_stream(args.idea, on_event,
                                target_positive=positive, max_hypotheses=max_h, page_limit=page,
                                max_depth=depth, **over)
    else:
        # Merge a YAML --config (base) with explicit CLI flags (override).
        cfg: dict = {}
        if getattr(args, "config", None):
            import yaml
            try:
                with open(args.config, encoding="utf-8") as f:
                    cfg = yaml.safe_load(f) or {}
            except (OSError, yaml.YAMLError) as exc:
                raise ClientError(f"could not read --config {args.config}: {exc}")
        topic = args.topic or cfg.get("topic")
        if not topic:
            raise ClientError('no topic — pass one as the argument, or use --idea <id> / --config <file>')
        positive = args.positive if args.positive is not None else int(cfg.get("positive", 2))
        max_h = args.max_hypotheses if args.max_hypotheses is not None else int(cfg.get("max_hypotheses", 6))
        page = args.page_limit if args.page_limit is not None else int(cfg.get("page_limit", 8))
        depth = args.max_depth if args.max_depth is not None else int(cfg.get("max_depth", 3))
        print(f'⚡ auto research: "{topic}"  (positive {positive} · max {max_h} · pages {page} · depth {depth})'
              "   (Ctrl+C or `paperclaw stop` to stop)")
        client.auto_research_stream(topic, on_event,
                                    target_positive=positive, max_hypotheses=max_h, page_limit=page,
                                    max_depth=depth, **over)
    if error:
        raise ClientError(error[0])


def cmd_status(client, args) -> None:
    """Show auto-run status — all ideas (parallel runs), or one with --idea."""
    runs = client.auto_status()
    if getattr(args, "idea", None):
        runs = [r for r in runs if r.get("ideaId") == args.idea]
    if not runs:
        print('no auto run found (start one with: paperclaw run "<topic>")')
        return
    for st in runs:
        icon = {"running": "▶", "done": "✓", "error": "✗", "stopped": "⏸",
                "interrupted": "⚠"}.get(st.get("status"), "·")
        print(f"{icon} auto run — {st.get('status')}   ({_fmt_age(st.get('updatedAt'))})")
        print(f"  topic:  {st.get('topic')}")
        if st.get("domainName"):
            print(f"  domain: {st.get('domainName')}")
        if st.get("ideaTitle"):
            print(f"  idea:   {st.get('ideaTitle')} ({st.get('ideaId')})")
        print(f"  phase:  {st.get('phase')} — {st.get('label')}")
        if st.get("phase") in ("hypotheses", "paper", "done"):
            print(f"  loop:   round {st.get('round')}/{st.get('maxHypotheses')} · "
                  f"{st.get('positives')}/{st.get('targetPositive')} positive"
                  + (f" · current {st.get('currentHypothesisId')}" if st.get("currentHypothesisId") else ""))
        if st.get("paperReady"):
            print("  paper:  ✓ ready")
        if st.get("error"):
            print(f"  error:  {st.get('error')}")


def cmd_stop(client, args) -> None:
    """Stop a running auto pipeline from any terminal (cancel its experiment + signal it)."""
    res = client.auto_stop(getattr(args, "idea", None))
    print(("⏹ stopped" if res.get("ok") else "·") + f" — {res.get('detail')}")


def cmd_resume(client, args) -> None:
    """Continue a stopped/interrupted auto run (the pipeline is resumable)."""
    error: list[str] = []
    on_event = _auto_on_event(error)
    rid = getattr(args, "idea", None)
    print(f"↻ resuming the {'idea ' + rid if rid else 'last'} auto run…  (Ctrl+C to stop)")
    client.auto_resume_stream(on_event, rid)
    if error:
        raise ClientError(error[0])


def cmd_references(client, args) -> None:
    action = args.action or "list"
    if action == "add":
        doi, query = getattr(args, "doi", None), getattr(args, "query", None)
        if not (doi or query):
            raise ClientError("provide --doi or --query")
        client.references_add(args.idea, doi=doi, query=query)
        print("added.")
        view = client.references_list(args.idea)
    elif action == "validate":
        for r in client.references_validate(args.idea):
            print(f"  [{r['status']}] {r['key']} — {r['detail']}")
        return
    elif action == "generate":
        print("gathering references from OpenAlex…")
        view = client.references_generate(args.idea)
    else:
        view = client.references_list(args.idea)

    entries = view.get("entries", [])
    if not entries:
        print("  (no references yet)")
    for e in entries:
        authors = e.get("authors") or []
        auth = (authors[0] if authors else "?") + (" et al." if len(authors) > 1 else "")
        year = f", {e['year']}" if e.get("year") else ""
        print(f"  {e['key']}  {(e.get('title') or '')[:60]}  ({auth}{year})")


def _print_hyp_nodes(nodes, indent=0) -> None:
    for n in nodes:
        # the derived progress stage (planned/experiment/…) when set, else the status
        stage = n.get("stage") or n.get("status", "")
        tag = f"  ({stage})" if stage and stage != "untested" else ""
        print(f"{'  ' * indent}- [{n['id']}] {n['statement']}{tag}")
        _print_hyp_nodes(n.get("children") or [], indent + 1)


def cmd_hypothesis(client, args) -> None:
    action = args.action or "show"
    if action == "plan":
        print(f"generating testing plan for {args.id}…")
        d = client.hypothesis_plan(args.idea, args.id)
        print(f"\n=== PLAN ({d['hypothesisId']}) ===\n{d.get('plan') or '(none)'}")
        return
    if action == "experiment":
        print(f"running experiment for {args.id} (writes code, then runs it)…")
        error: list[str] = []

        def on_event(ev: dict) -> None:
            t = ev.get("type")
            if t == "phase":
                print(f"\n\n▶ {ev.get('label', '')}", flush=True)
            elif t in ("delta", "thinking"):
                print(ev.get("text", ""), end="", flush=True)
            elif t == "hypothesis_status":
                print(f"\n[status: {ev.get('status')}]", flush=True)
            elif t == "error":
                error.append(ev.get("message", "unknown error"))

        client.hypothesis_experiment_stream(args.idea, args.id, on_event)
        if error:
            raise ClientError(error[0])
        print("\n✓ experiment complete")
        return
    if action == "detail":
        d = client.hypothesis_detail(args.idea, args.id)
        print(f"hypothesis {d['hypothesisId']} — status: {d['status']}")
        for label, key in (("PLAN", "plan"), ("CODE (run.py)", "code"),
                           ("EXPERIMENT", "experiment"), ("LOG", "log"), ("REPORT", "report")):
            if d.get(key):
                print(f"\n=== {label} ===\n{d[key]}")
        if d.get("figures"):
            print(f"\nfigures: {', '.join(d['figures'])}")
        return
    if action == "delete":
        client.hypothesis_delete(args.idea, args.id)
        print(f"removed hypothesis {args.id}")
        return
    if action == "add-child":
        hmap = client.hypothesis_add_child(args.idea, args.id, args.statement)
        print(f"added sub-hypothesis under {args.id}")
        _print_hyp_nodes(hmap.get("nodes", []))
        return
    if action == "rerun":
        job = client.hypothesis_rerun(args.idea, args.id)
        print(f"reran {args.id}: cleared results + started job {job.get('jobId')} "
              f"(monitor: paperclaw hypothesis {args.idea} experiment {args.id}, or paperclaw experiments)")
        return
    if action == "files":
        base = f"hypotheses/{args.id}"
        if getattr(args, "cat", None):
            rel = args.cat if args.cat.startswith(base + "/") else f"{base}/{args.cat}"
            data = client.workspace_file(args.idea, rel)
            try:
                print(data.decode("utf-8"))
            except UnicodeDecodeError:
                print(f"[binary file, {len(data)} bytes — not printable as text]")
            return
        entries = [e for e in client.workspace_files(args.idea, base).get("entries", [])
                   if not e.get("isDir")]
        if not entries:
            print(f"no files under {base}/ yet — run the experiment first")
            return
        print(f"files under {base}/:")
        for e in entries:
            print(f"  {e['size']:>9}  {e['path'][len(base) + 1:]}")
        print(f"\nview one with: paperclaw hypothesis {args.idea} files {args.id} --cat <path>")
        return
    if action == "generate":
        print("generating hypothesis map…")
        hmap = client.hypothesis_map_generate(args.idea)
    else:
        hmap = client.hypothesis_map_show(args.idea)
    nodes = hmap.get("nodes", [])
    if not nodes:
        print("  (no hypothesis map yet — run: paperclaw hypothesis <idea> generate)")
    _print_hyp_nodes(nodes)


def cmd_history(client, args) -> None:
    if args.context:
        for m in client.messages(args.context):
            who = "you" if m["role"] == "user" else "PaperClaw"
            print(f"[{_fmt_ts(m['timestamp'])}] {who}: {m['content']}\n")
    else:
        icons = {"scratch": "💬", "domain": "🌐", "seed": "📝", "idea": "💡"}
        for c in client.contexts():
            print(f"{icons.get(c['kind'], '·')} {c['contextId']:<22} {c['title'][:44]:<44} "
                  f"{c['messageCount']:>3} msgs  {_fmt_ts(c['lastTimestamp'])}")


def cmd_settings(client, args) -> None:
    if args.action == "set":
        out = client.settings_set(provider=args.provider, base_url=args.base_url,
                                  model=args.model, api_key=args.api_key,
                                  image_base_url=args.image_base_url, image_model=args.image_model,
                                  image_api_key=args.image_api_key,
                                  openalex_api_key=args.openalex_api_key)
    else:
        out = client.settings_show()
    print(f"provider: {out['provider']}")
    print(f"base_url: {out.get('baseUrl') or '(default)'}")
    print(f"model:    {out['model']}")
    print(f"api_key:  {out['apiKeyMasked'] or '(not set)'}")
    print(f"openalex: {out.get('openalexKeyMasked') or '(not set)'}")


def _print_machine(m: dict) -> None:
    name = m["label"]
    if not m.get("reachable", True):
        print(f"  {name} [{m['scope']}]: ⚠ unreachable — {m.get('error', '')}")
        return
    print(f"  {name} [{m['scope']}]")
    if m.get("os"):
        print(f"    OS:  {m['os']}")
    cpu = m.get("cpuModel") or "CPU"
    print(f"    CPU: {cpu} ({m.get('cpuCores')} cores / {m.get('cpuThreads')} threads)")
    if m.get("memTotalGb"):
        print(f"    MEM: {m['memTotalGb']} GB")
    gpus = m.get("gpus", [])
    if gpus:
        for g in gpus:
            vram = f" {g['memoryTotalMb'] / 1024:.0f}GB" if g.get("memoryTotalMb") else ""
            print(f"    GPU: {g['name']}{vram}")
    else:
        print("    GPU: none detected")
    for d in m.get("disks", []):
        size = f"{d['sizeGb']}GB" if d.get("sizeGb") else "?"
        print(f"    DISK: {d['name']} {d.get('model') or ''} {size} {d['kind']}")


def cmd_hardware(client, args) -> None:
    if args.action == "detect":
        print("detecting compute resources (local + SSH remotes)…", flush=True)
        view = client.hardware_detect()
    elif args.action == "ssh-add":
        import uuid
        view = client.hardware_show()
        targets = view.get("sshTargets", [])
        targets.append({
            "id": uuid.uuid4().hex[:12], "host": args.host, "user": args.user,
            "port": args.port, "keyPath": args.key, "label": args.label,
        })
        view = client.hardware_ssh_set(targets)
        print(f"added remote {args.user}@{args.host}:{args.port}")
    elif args.action == "ssh-remove":
        view = client.hardware_show()
        targets = [t for t in view.get("sshTargets", []) if t["id"] != args.id]
        view = client.hardware_ssh_set(targets)
        print("removed")
    elif args.action == "run-config":
        patch = {}
        if args.mode is not None: patch["experimentMode"] = args.mode
        if args.python is not None: patch["pythonPath"] = args.python
        if args.ssh_target is not None: patch["sshTargetId"] = args.ssh_target
        if args.agent_command is not None: patch["agentCommand"] = args.agent_command
        if args.max_attempts is not None: patch["maxAttempts"] = args.max_attempts
        view = client.hardware_run_config(patch)
    else:  # show
        view = client.hardware_show()

    machines = view.get("machines", [])
    if machines:
        print("machines:")
        for m in machines:
            _print_machine(m)
    elif args.action not in ("ssh-add", "ssh-remove", "run-config"):
        print("no machines detected yet — run: paperclaw hardware detect")
    targets = view.get("sshTargets", [])
    if targets:
        print("ssh remotes:")
        for t in targets:
            print(f"  {t['id']}  {t['user']}@{t['host']}:{t['port']}  {t.get('label') or ''}")
    rc = view.get("runConfig")
    if rc:
        mode = rc["experimentMode"]
        bits = []
        if mode == "ssh":  # ssh needs only its connection — no interpreter / extra settings
            bits.append(f"ssh-target: {rc.get('sshTargetId') or '(none — pick one)'}")
        elif mode == "cli" and rc.get("agentCommand"):
            bits.append(f"agent: {rc['agentCommand']}")
        bits.append(f"attempts: {rc['maxAttempts']}")
        print(f"experiment mode: {mode} ({', '.join(bits)})")


def cmd_experiment_run(client, args) -> None:
    """(internal) The detached worker: run one hypothesis experiment to completion,
    writing events.jsonl + job.json. Always runs against the local PaperClaw home."""
    import asyncio

    from paperclaw import jobs
    from paperclaw.config import paperclaw_home, load_settings
    from paperclaw.server.store import Store

    home = paperclaw_home()
    store = Store(home)
    settings = load_settings(home)
    rc = asyncio.run(jobs.run_experiment_job_blocking(store, settings, args.idea, args.hid, args.job))
    sys.exit(rc)


def cmd_experiments(client, args) -> None:
    jobs = client.experiments_list()
    if not jobs:
        print("no experiment jobs yet")
        return
    icons = {"running": "▶", "done": "✓", "error": "✗", "cancelled": "■", "interrupted": "⚠"}
    for j in jobs:
        age = _fmt_ts(j.get("startedAt"))
        print(f" {icons.get(j['status'], '·')} {j['status']:<11} {j['hypothesisId']:<8} "
              f"{j['ideaTitle'][:36]:<36} {age}")


def cmd_styles(client, args) -> None:
    if args.action == "list":
        for s in client.writing_styles_list(args.domain):
            print(f"· {s['name']:<22} [{s['scope']}]  {s['title']}")
    elif args.action == "show":
        print(client.writing_style_get(args.name, args.domain)["content"])
    elif args.action == "add":
        content = open(args.file, encoding="utf-8").read() if args.file else sys.stdin.read()
        out = client.writing_style_save(args.name, content, args.domain)
        print(f"saved writing style: {out['name']}" + (f" (domain {args.domain})" if args.domain else " (global)"))


def cmd_benchmark(client, args) -> None:
    if args.action == "list":
        for b in client.benchmarks_list(args.domain):
            print(f"· {b['name']:<22} [{b['scope']}]  {b['title']}")
    elif args.action == "show":
        print(client.benchmark_get(args.name, args.domain)["content"])
    elif args.action == "add":
        content = open(args.file, encoding="utf-8").read() if args.file else sys.stdin.read()
        out = client.benchmark_save(args.name, content, args.domain)
        print(f"saved benchmark: {out['name']}" + (f" (domain {args.domain})" if args.domain else " (global)"))


def cmd_doctor(client, args) -> None:
    report = client.doctor()
    icons = {"ok": "✓", "warn": "!", "fail": "✗"}
    for c in report["checks"]:
        print(f" {icons.get(c['status'], '·')} {c['label']:<22} {c['detail']}")
        if c.get("hint") and c["status"] != "ok":
            print(f"     → {c['hint']}")
    print()
    print("environment ready ✓" if report["ok"]
          else "environment NOT ready ✗ — fix the ✗ checks above")
    if not report["ok"]:
        sys.exit(1)


COMMANDS = {"serve", "chat", "domain", "brainstorm", "idea", "research", "run", "status", "stop", "resume", "references", "hypothesis", "history", "settings", "hardware", "doctor", "styles", "benchmark", "experiments", "experiment-run"}


def _normalize_argv(argv: list[str]) -> list[str]:
    """Let `--backend` be used with or without a URL: if the next token is a
    subcommand (or missing), insert the default URL so argparse doesn't eat it."""
    if "--backend" in argv:
        i = argv.index("--backend")
        nxt = argv[i + 1] if i + 1 < len(argv) else None
        if nxt is None or nxt in COMMANDS or nxt.startswith("-"):
            argv = [*argv[: i + 1], DEFAULT_BACKEND, *argv[i + 1:]]
    return argv


def main() -> None:
    parser = argparse.ArgumentParser(prog="paperclaw", description=__doc__)
    parser.add_argument(
        "--backend", default=None, metavar="URL",
        help=f"connect to a running backend (bare --backend means {DEFAULT_BACKEND}); "
             "omit the flag entirely to run locally",
    )
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="Run the backend API server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8230)
    serve.add_argument("--reload", action="store_true", help="Auto-reload on code changes")

    chat = sub.add_parser("chat", help="Chat (scratch / --domain / --seed / --idea)")
    chat.add_argument("message", nargs="*", help="message to send")
    chat.add_argument("-i", "--interactive", action="store_true", help="REPL mode")
    chat.add_argument("--idea", help="idea id")
    chat.add_argument("--seed", help="brainstormed draft id")
    chat.add_argument("--domain", help="domain id")

    domain = sub.add_parser("domain", help="Manage domains")
    dsub = domain.add_subparsers(dest="action", required=True)
    dsub.add_parser("list")
    p = dsub.add_parser("create"); p.add_argument("name")
    p = dsub.add_parser("auto"); p.add_argument("prompt", help="short domain description")
    p = dsub.add_parser("select"); p.add_argument("id"); p.add_argument("--off", action="store_true")
    p = dsub.add_parser("show"); p.add_argument("id")
    p = dsub.add_parser("suggest"); p.add_argument("id")
    p = dsub.add_parser("codebase", help="set/show/clear the domain's reference codebase")
    p.add_argument("id")
    p.add_argument("--url", help="GitHub repo URL to download as the reference codebase")
    p.add_argument("--clear", action="store_true", help="remove the reference codebase")
    p = dsub.add_parser("delete"); p.add_argument("id")

    brainstorm = sub.add_parser("brainstorm", help="Seeds and idea drafts")
    bsub = brainstorm.add_subparsers(dest="action", required=True)
    bsub.add_parser("list")
    p = bsub.add_parser("add"); p.add_argument("text")
    p = bsub.add_parser("generate"); p.add_argument("--hint")
    p.add_argument("--type", action="append", metavar="KIND",
                   choices=["application", "algorithm", "analysis", "benchmark"],
                   help="idea type (repeatable); default: any")
    p.add_argument("--emphasis", action="append", metavar="ASPECT",
                   choices=["performance", "efficiency", "robustness", "interpretability"],
                   help="emphasis/criteria (repeatable); default: any")
    p.add_argument("--count", type=int, help="number of ideas to generate (1–12)")
    p = bsub.add_parser("show"); p.add_argument("id")
    p = bsub.add_parser("pin"); p.add_argument("id")
    p = bsub.add_parser("delete"); p.add_argument("id")

    idea = sub.add_parser("idea", help="Manage ideas")
    isub = idea.add_subparsers(dest="action", required=True)
    isub.add_parser("list")
    p = isub.add_parser("create"); p.add_argument("title")
    p = isub.add_parser("show"); p.add_argument("id")
    p = isub.add_parser("duplicate", help="fork an idea (copies IDEA.md + ref.bib)"); p.add_argument("id")
    p = isub.add_parser("delete"); p.add_argument("id")
    p = isub.add_parser("domains", help="show/connect the idea's domains (mounted at ./domains/<name>)")
    p.add_argument("id")
    p.add_argument("--add", action="append", metavar="DOMAIN_ID", help="connect a domain (repeatable)")
    p.add_argument("--remove", action="append", metavar="DOMAIN_ID", help="disconnect a domain (repeatable)")
    p.add_argument("--set", help="comma-separated domain ids to connect (replaces the set)")
    p.add_argument("--clear", action="store_true", help="disconnect all domains")
    p = isub.add_parser("color", help="flag an idea with a sidebar colour")
    p.add_argument("id")
    p.add_argument("color", nargs="?", default="", help="green | yellow | grey (omit to clear)")
    p = isub.add_parser("resources", help="show/allocate the idea's experiment resources (compute/GPU)")
    p.add_argument("id")
    p.add_argument("--mode", choices=["cli", "executed", "ssh", "simulated"],
                   help="experiment-execution mode for this idea")
    p.add_argument("--ssh", help="SSH target id (GPU host) when --mode ssh")
    p.add_argument("--codebase", type=lambda v: v.lower() in ("1", "true", "yes", "on"),
                   metavar="BOOL", help="reuse the pinned domain's reference codebase (true/false)")

    research = sub.add_parser("research", help="Run the iterative hypothesis-loop pipeline on an idea")
    research.add_argument("idea", help="idea id")
    research.add_argument("--restart", action="store_true", help="discard saved rounds and start fresh")
    research.add_argument("--max-hypotheses", dest="max_hypotheses", type=int, default=4)
    research.add_argument("--page-limit", dest="page_limit", type=int, default=9)
    research.add_argument("--benchmark", dest="benchmark", default=None,
                          help="benchmark-template name (fixed protocol + published, cited baselines)")

    run = sub.add_parser("run", help="Run the autonomous pipeline: topic → doctor → domain → idea → hypotheses → paper")
    run.add_argument("topic", nargs="?",
                     help='research topic, e.g. "diffusion models". Omit and pass --idea or --config instead.')
    run.add_argument("--idea", help="run an EXISTING idea by id (skip domain/idea creation)")
    run.add_argument("--config", help="YAML file of run settings "
                     "(topic / positive / max_hypotheses / page_limit / max_depth); CLI flags override it")
    run.add_argument("--positive", type=int, default=None,
                     help="write the paper once this many hypotheses are SUPPORTED (default 2)")
    run.add_argument("--max-hypotheses", dest="max_hypotheses", type=int, default=None,
                     help="stop after this many hypotheses if not enough positives (default 6)")
    run.add_argument("--page-limit", dest="page_limit", type=int, default=None)
    run.add_argument("--max-depth", dest="max_depth", type=int, default=None,
                     help="cap hypothesis-map depth; at the cap, grow siblings instead of deeper (default 3)")
    # per-run overrides (else the global RunConfig / defaults are used)
    run.add_argument("--experiment-mode", dest="experiment_mode",
                     choices=["simulated", "executed", "ssh", "cli"], default=None,
                     help="experiment execution for THIS run (overrides the global RunConfig)")
    run.add_argument("--ssh-target", dest="ssh_target", default=None,
                     help="SSH remote id to run experiments on (with --experiment-mode ssh)")
    run.add_argument("--style", dest="style", default=None,
                     help="writing-style guide name for the paper (e.g. technical-concise)")
    run.add_argument("--benchmark", dest="benchmark", default=None,
                     help="benchmark-template name — run only the new method on its fixed protocol "
                          "and compare to its published, cited baselines (a name from `benchmark list`)")
    run.add_argument("--no-codebase", dest="no_codebase", action="store_true",
                     help="do NOT reuse the pinned domain's reference codebase for experiments")
    run.add_argument("--fill-page", dest="fill_page", action="store_true",
                     help="make the paper FILL the page limit (main text ends at the last allowed page)")
    rstatus = sub.add_parser("status", help="show auto-run status (all ideas, or one with --idea)")
    rstatus.add_argument("--idea", help="show only this idea's run")
    rstop = sub.add_parser("stop", help="stop a running auto pipeline (from any terminal)")
    rstop.add_argument("--idea", help="which idea's run to stop (omit if only one is running)")
    rresume = sub.add_parser("resume", help="continue a stopped auto run")
    rresume.add_argument("--idea", help="which idea's run to resume (omit to resume the last)")

    refs = sub.add_parser("references", help="Manage an idea's ref.bib (list/add/validate)")
    refs.add_argument("idea", help="idea id")
    rsub = refs.add_subparsers(dest="action")
    radd = rsub.add_parser("add", help="add a reference by DOI or search query")
    radd.add_argument("--doi")
    radd.add_argument("--query")
    rsub.add_parser("validate", help="check each entry against Crossref/OpenAlex")
    rsub.add_parser("generate", help="auto-gather real references from OpenAlex")
    rsub.add_parser("list")

    hyp = sub.add_parser("hypothesis", help="Idea hypothesis map (show/generate)")
    hyp.add_argument("idea", help="idea id")
    hypsub = hyp.add_subparsers(dest="action")
    hypsub.add_parser("show")
    hypsub.add_parser("generate", help="LLM-generate the hypothesis map from IDEA.md")
    hdet = hypsub.add_parser("detail", help="show one hypothesis's plan/experiment/report")
    hdet.add_argument("id", help="hypothesis node id")
    hpl = hypsub.add_parser("plan", help="generate the testing plan for one hypothesis")
    hpl.add_argument("id", help="hypothesis node id")
    hexp = hypsub.add_parser("experiment", help="run one hypothesis's experiment (writes code, runs it)")
    hexp.add_argument("id", help="hypothesis node id")
    hfil = hypsub.add_parser("files", help="browse a hypothesis's workspace dir (code/figures/results)")
    hfil.add_argument("id", help="hypothesis node id")
    hfil.add_argument("--cat", help="print one file's contents (path relative to the hypothesis dir)")
    hdel = hypsub.add_parser("delete", help="remove a hypothesis node (and its subtree) from the map")
    hdel.add_argument("id", help="hypothesis node id")
    hadd = hypsub.add_parser("add-child", help="add a sub-hypothesis under a node")
    hadd.add_argument("id", help="parent hypothesis node id")
    hadd.add_argument("statement", help="the sub-hypothesis (a single falsifiable claim)")
    hrer = hypsub.add_parser("rerun", help="clear a hypothesis's results + start a fresh experiment job")
    hrer.add_argument("id", help="hypothesis node id")

    history = sub.add_parser("history", help="List conversations / show one")
    history.add_argument("context", nargs="?", help="context id (omit to list all)")

    settings = sub.add_parser("settings", help="Show or change LLM settings")
    ssub = settings.add_subparsers(dest="action", required=True)
    ssub.add_parser("show")
    p = ssub.add_parser("set")
    p.add_argument("--provider", choices=["anthropic", "openai"])
    p.add_argument("--base-url", dest="base_url")
    p.add_argument("--model")
    p.add_argument("--api-key", dest="api_key")
    p.add_argument("--image-base-url", dest="image_base_url", help="image-generation API base URL (paper figures)")
    p.add_argument("--image-model", dest="image_model", help="image-generation model, e.g. gpt-image-1")
    p.add_argument("--image-api-key", dest="image_api_key", help="image-generation API key")
    p.add_argument("--openalex-api-key", dest="openalex_api_key",
                   help="OpenAlex API key (literature search — dedicated budget vs the rate-limited anonymous pool)")

    hardware = sub.add_parser("hardware", help="Detect compute resources / manage SSH remotes")
    hwsub = hardware.add_subparsers(dest="action", required=True)
    hwsub.add_parser("show", help="show the saved hardware snapshot + SSH remotes")
    hwsub.add_parser("detect", help="probe local + remotes now and write HARDWARE.md")
    p = hwsub.add_parser("ssh-add", help="add an SSH remote (key-based)")
    p.add_argument("--host", required=True)
    p.add_argument("--user", required=True)
    p.add_argument("--port", type=int, default=22)
    p.add_argument("--key", help="path to the SSH private key")
    p.add_argument("--label", help="friendly name for the machine")
    p = hwsub.add_parser("ssh-remove", help="remove an SSH remote by id")
    p.add_argument("id")
    sub.add_parser("doctor", help="Check that the key environment is ready (LLM, LaTeX, image gen)")

    sub.add_parser("experiments", help="List experiment jobs (running / recent)")
    exprun = sub.add_parser("experiment-run", help="(internal) run a hypothesis experiment as a detached job")
    exprun.add_argument("idea"); exprun.add_argument("hid")
    exprun.add_argument("--job", required=True)

    styles = sub.add_parser("styles", help="Writing-style guides (used by /write_paper --style)")
    stsub = styles.add_subparsers(dest="action", required=True)
    p = stsub.add_parser("list", help="list global + domain style guides")
    p.add_argument("--domain", help="also include a domain's styles")
    p = stsub.add_parser("show", help="print a style guide's markdown")
    p.add_argument("name"); p.add_argument("--domain", help="resolve within a domain first")
    p = stsub.add_parser("add", help="create/overwrite a style guide (from --file or stdin)")
    p.add_argument("name"); p.add_argument("--file", help="markdown file (else read stdin)")
    p.add_argument("--domain", help="save as a domain-scoped style")

    bench = sub.add_parser("benchmark", help="Benchmark templates (fixed protocol + published, cited baselines)")
    bnsub = bench.add_subparsers(dest="action", required=True)
    p = bnsub.add_parser("list", help="list global + domain benchmark templates")
    p.add_argument("--domain", help="also include a domain's benchmarks")
    p = bnsub.add_parser("show", help="print a benchmark template's markdown")
    p.add_argument("name"); p.add_argument("--domain", help="resolve within a domain first")
    p = bnsub.add_parser("add", help="create/overwrite a benchmark template (from --file or stdin)")
    p.add_argument("name"); p.add_argument("--file", help="markdown/csv file (else read stdin)")
    p.add_argument("--domain", help="save as a domain-scoped benchmark")

    p = hwsub.add_parser("run-config", help="show/set experiment execution mode")
    p.add_argument("--mode", choices=["cli", "executed", "ssh", "simulated"],
                   help="cli (default, real) | executed (real, in-process agent) | ssh (agent runs "
                        "bash on a remote — needs only --ssh-target) | simulated (NOT real — narrated/fake, avoid)")
    p.add_argument("--python", help="python interpreter path for the legacy single-shot runners "
                                    "(ignored by executed/ssh — the agent runs python via bash)")
    p.add_argument("--ssh-target", dest="ssh_target", help="SSH remote id for ssh mode (the only setting ssh needs)")
    p.add_argument("--agent-command", dest="agent_command",
                   help="cli mode: headless agent command template, e.g. "
                        "'claude -p {prompt} --dangerously-skip-permissions' or 'opencode run {prompt}'")
    p.add_argument("--max-attempts", dest="max_attempts", type=int, help="generate→fix retries")

    args = parser.parse_args(_normalize_argv(sys.argv[1:]))

    if args.command == "serve":
        import uvicorn
        # Force-close lingering connections after a few seconds on shutdown/reload. PaperClaw
        # holds LONG-LIVED SSE streams (chat, experiment + auto-run logs); without this,
        # a `--reload` (or Ctrl+C) hangs forever at "Waiting for connections to close" because
        # those streams never end on their own — wedging the whole backend.
        kwargs = dict(host=args.host, port=args.port, reload=args.reload,
                      timeout_graceful_shutdown=5)
        if args.reload:
            # Watch only the SOURCE package — NOT the launch dir. Otherwise uvicorn's
            # reloader watches everything under cwd (incl. the PaperClaw home / ./saves),
            # and a running experiment writing run_experiment.py / results.json / *.log
            # there triggers an endless reload storm that makes the API unresponsive.
            import os
            import paperclaw
            kwargs["reload_dirs"] = [os.path.dirname(os.path.abspath(paperclaw.__file__))]
        uvicorn.run("paperclaw.server.app:create_app", factory=True, **kwargs)
        return
    if args.command is None:
        parser.print_help()
        return

    client = make_client(args.backend)
    handlers = {
        "chat": cmd_chat,
        "domain": cmd_domain,
        "brainstorm": cmd_brainstorm,
        "idea": cmd_idea,
        "research": cmd_research,
        "run": cmd_run,
        "status": cmd_status,
        "stop": cmd_stop,
        "resume": cmd_resume,
        "references": cmd_references,
        "hypothesis": cmd_hypothesis,
        "history": cmd_history,
        "settings": cmd_settings,
        "hardware": cmd_hardware,
        "doctor": cmd_doctor,
        "styles": cmd_styles,
        "benchmark": cmd_benchmark,
        "experiments": cmd_experiments,
        "experiment-run": cmd_experiment_run,
    }
    try:
        handlers[args.command](client, args)
    except ClientError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
