"""CLI client backends.

LocalClient  — operates on the local PaperClaw home directly (Store + service);
               configuration from env vars / .env / settings.yaml.
RemoteClient — talks to a running PaperClaw backend over HTTP.

Both return plain dicts (camelCase keys — the wire format) so the CLI treats
them identically.
"""

import asyncio
import json
from typing import Any, Callable

import httpx

from paperclaw import service
from paperclaw.config import paperclaw_home, load_settings, save_settings
from paperclaw.server.store import Store

DEFAULT_BACKEND = "http://127.0.0.1:8230"


class ClientError(Exception):
    pass


def _dump(obj: Any) -> Any:
    if isinstance(obj, list):
        return [_dump(o) for o in obj]
    if hasattr(obj, "model_dump"):
        return obj.model_dump(by_alias=True)
    return obj


class LocalClient:
    """Runs everything in-process against the local PaperClaw home."""

    def __init__(self) -> None:
        self.home = paperclaw_home()
        self.home.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.home)
        self.settings = load_settings(self.home)

    def _run(self, coro):
        try:
            return asyncio.run(coro)
        except service.NotFound as exc:
            raise ClientError(str(exc))
        except Exception as exc:  # llm.LLMError / LLMNotConfigured etc.
            raise ClientError(str(exc))

    # Domains
    def domains_list(self): return _dump(self.store.list_domains())
    def domain_create(self, name): return _dump(self.store.add_domain(name))
    def domain_auto(self, prompt):
        return _dump(self._run(service.auto_create_domain(self.store, self.settings, prompt)))

    def domain_auto_stream(self, prompt: str, on_event: Callable[[dict], None]) -> None:
        async def _run():
            async for event in service.stream_auto_create_domain_events(self.store, self.settings, prompt):
                on_event(event)
        try:
            asyncio.run(_run())
        except service.NotFound as exc:
            raise ClientError(str(exc))
        except Exception as exc:
            raise ClientError(str(exc))
    def domain_select(self, domain_id, selected):
        d = self.store.set_domain_selected(domain_id, selected)
        if d is None: raise ClientError("Domain not found")
        return _dump(d)
    def domain_delete(self, domain_id):
        if not self.store.remove_domain(domain_id): raise ClientError("Domain not found")
    def domain_spec(self, domain_id):
        spec = self.store.get_domain_spec(domain_id)
        if spec is None: raise ClientError("Domain not found")
        return spec
    def domain_suggestions(self, domain_id):
        return self._run(service.domain_suggestions(self.store, self.settings, domain_id))
    def domain_set_codebase(self, domain_id, url):
        try:
            return _dump(service.set_domain_codebase(self.store, domain_id, url))
        except (service.NotFound, service.codebase.CodebaseError) as exc:
            raise ClientError(str(exc))
    def domain_clear_codebase(self, domain_id):
        try:
            return _dump(service.clear_domain_codebase(self.store, domain_id))
        except service.NotFound as exc:
            raise ClientError(str(exc))

    # Writing styles
    def writing_styles_list(self, domain_id=None):
        return service.list_writing_styles(self.store, domain_id)
    def writing_style_get(self, name, domain_id=None):
        md = service.get_writing_style(self.store, domain_id, name)
        if md is None: raise ClientError("Writing style not found")
        return {"name": name, "content": md}
    def writing_style_save(self, name, content, domain_id=None):
        saved = service.save_writing_style(self.store, name, content, domain_id)
        if saved is None: raise ClientError("Invalid style name")
        return {"name": saved}

    def benchmarks_list(self, domain_id=None):
        return service.list_benchmarks(self.store, domain_id)
    def benchmark_get(self, name, domain_id=None):
        md = service.get_benchmark(self.store, domain_id, name)
        if md is None: raise ClientError("Benchmark not found")
        return {"name": name, "content": md}
    def benchmark_save(self, name, content, domain_id=None):
        saved = service.save_benchmark(self.store, name, content, domain_id)
        if saved is None: raise ClientError("Invalid benchmark name")
        return {"name": saved}

    # Seeds
    def seeds_list(self): return _dump(self.store.list_seeds())
    def seed_add(self, text): return _dump(self.store.add_seed(text))
    def seed_delete(self, seed_id):
        if not self.store.remove_seed(seed_id): raise ClientError("Seed not found")
    def seeds_generate(self, hint=None, idea_types=None, emphasis=None, count=None):
        return _dump(self._run(service.generate_seeds(
            self.store, self.settings, hint=hint,
            idea_types=idea_types, emphasis=emphasis, count=count,
        )))

    def seeds_generate_stream(self, on_event: Callable[[dict], None], hint=None,
                              idea_types=None, emphasis=None, count=None) -> None:
        async def _run():
            async for event in service.stream_generate_seeds_events(
                self.store, self.settings, hint=hint,
                idea_types=idea_types, emphasis=emphasis, count=count,
            ):
                on_event(event)
        try:
            asyncio.run(_run())
        except service.NotFound as exc:
            raise ClientError(str(exc))
        except Exception as exc:
            raise ClientError(str(exc))

    # Ideas
    def ideas_list(self): return _dump(self.store.list_ideas())
    def idea_create(self, title): return _dump(self.store.add_idea(title))
    def idea_duplicate(self, idea_id):
        d = self.store.duplicate_idea(idea_id)
        if d is None: raise ClientError("Idea not found")
        return _dump(d)
    def idea_delete(self, idea_id):
        if not self.store.remove_idea(idea_id): raise ClientError("Idea not found")
    def idea_spec(self, idea_id):
        spec = self.store.get_spec(idea_id)
        if spec is None: raise ClientError("Idea not found")
        return spec
    def idea_domains_get(self, idea_id):
        return service.get_idea_domains(self.store, idea_id)["domainIds"]
    def idea_domains_set(self, idea_id, domain_ids):
        return service.set_idea_domains(self.store, idea_id, domain_ids)["domainIds"]
    def idea_set_color(self, idea_id, color):
        idea = self.store.set_idea_color(idea_id, color or None)
        if idea is None:
            raise ClientError("Idea not found, or invalid color")
        return _dump(idea)
    def idea_resources_get(self, idea_id):
        try:
            return service.get_idea_resources_view(self.store, self.settings, idea_id)
        except service.NotFound as exc:
            raise ClientError(str(exc))
    def idea_resources_set(self, idea_id, *, experiment_mode=None, ssh_target_id=None,
                           use_reference_codebase=None):
        try:
            service.set_idea_resources(self.store, idea_id, experiment_mode=experiment_mode,
                                       ssh_target_id=ssh_target_id,
                                       use_reference_codebase=use_reference_codebase)
            return service.get_idea_resources_view(self.store, self.settings, idea_id)
        except service.NotFound as exc:
            raise ClientError(str(exc))
    def references_list(self, idea_id):
        return _dump(service.get_references(self.store, idea_id))
    def references_add(self, idea_id, doi=None, query=None):
        return _dump(self._run(service.add_reference(self.store, idea_id, doi=doi, query=query)))
    def references_validate(self, idea_id):
        return _dump(self._run(service.validate_references(self.store, idea_id)))
    def references_generate(self, idea_id):
        return _dump(self._run(service.generate_references(self.store, self.settings, idea_id)))
    def hypothesis_map_show(self, idea_id):
        return _dump(service.get_hypothesis_map(self.store, idea_id))
    def hypothesis_map_generate(self, idea_id):
        return _dump(self._run(service.generate_hypothesis_map(self.store, self.settings, idea_id)))
    def hypothesis_detail(self, idea_id, hid):
        return _dump(service.get_hypothesis_detail(self.store, idea_id, hid))
    def hypothesis_delete(self, idea_id, hid):
        try:
            return _dump(service.delete_hypothesis_node(self.store, idea_id, hid))
        except service.NotFound as exc:
            raise ClientError(str(exc))
    def hypothesis_add_child(self, idea_id, parent_hid, statement):
        try:
            return _dump(service.add_child_hypothesis(self.store, idea_id, parent_hid, statement))
        except service.NotFound as exc:
            raise ClientError(str(exc))
    def hypothesis_rerun(self, idea_id, hid):
        try:
            return service.rerun_hypothesis_experiment(self.store, idea_id, hid)
        except service.NotFound as exc:
            raise ClientError(str(exc))
    def hypothesis_plan(self, idea_id, hid):
        return _dump(self._run(service.generate_hypothesis_plan(self.store, self.settings, idea_id, hid)))
    def hypothesis_experiment(self, idea_id, hid):
        return _dump(self._run(service.run_hypothesis_experiment(self.store, self.settings, idea_id, hid)))
    def workspace_files(self, idea_id, path=""):
        entries = self.store.list_idea_files(idea_id, path)
        if entries is None:
            raise ClientError("Idea or path not found")
        return {"ideaId": idea_id, "root": path, "entries": entries}
    def workspace_file(self, idea_id, path):
        fp = self.store.idea_file(idea_id, path)
        if fp is None:
            raise ClientError("File not found")
        return fp.read_bytes()
    def hypothesis_experiment_stream(self, idea_id, hid, on_event):
        async def _run():
            async for ev in service.stream_hypothesis_experiment(self.store, self.settings, idea_id, hid):
                on_event(ev)
        try:
            asyncio.run(_run())
        except service.NotFound as exc:
            raise ClientError(str(exc))
        except Exception as exc:
            raise ClientError(str(exc))
    def research_stream(self, idea_id, on_event, restart=False, max_hypotheses=4, page_limit=9,
                        benchmark=None):
        from paperclaw import iterative_pipeline
        async def _run():
            async for ev in iterative_pipeline.stream_iterative_research_events(
                self.store, self.settings, idea_id, restart=restart,
                max_hypotheses=max_hypotheses, page_limit=page_limit, benchmark=benchmark,
            ):
                on_event(ev)
        try:
            asyncio.run(_run())
        except service.NotFound as exc:
            raise ClientError(str(exc))
        except Exception as exc:
            raise ClientError(str(exc))

    def auto_research_stream(self, topic, on_event, target_positive=2, max_hypotheses=6, page_limit=8,
                             **over):
        async def _run():
            async for ev in service.stream_auto_research(
                self.store, self.settings, self.home, topic,
                target_positive=target_positive, max_hypotheses=max_hypotheses, page_limit=page_limit,
                **over,
            ):
                on_event(ev)
        try:
            asyncio.run(_run())
        except service.NotFound as exc:
            raise ClientError(str(exc))
        except Exception as exc:
            raise ClientError(str(exc))

    def auto_resume_stream(self, on_event, idea_id=None):
        async def _run():
            async for ev in service.stream_auto_resume(self.store, self.settings, self.home, idea_id):
                on_event(ev)
        try:
            asyncio.run(_run())
        except service.NotFound as exc:
            raise ClientError(str(exc))
        except Exception as exc:
            raise ClientError(str(exc))

    def auto_idea_stream(self, idea_id, on_event, target_positive=2, max_hypotheses=6, page_limit=8,
                         **over):
        async def _run():
            async for ev in service.stream_auto_idea(
                self.store, self.settings, self.home, idea_id,
                target_positive=target_positive, max_hypotheses=max_hypotheses, page_limit=page_limit,
                **over,
            ):
                on_event(ev)
        try:
            asyncio.run(_run())
        except service.NotFound as exc:
            raise ClientError(str(exc))
        except Exception as exc:
            raise ClientError(str(exc))

    def auto_stop(self, idea_id=None):
        return service.stop_auto_run(self.store, idea_id)

    def auto_status(self):
        return service.list_auto_runs_view(self.store)

    # Chat
    def chat_send(self, content, idea_id=None, seed_id=None, domain_id=None):
        return _dump(self._run(service.send_chat(
            self.store, self.settings, content,
            idea_id=idea_id, seed_id=seed_id, domain_id=domain_id,
        )))
    def contexts(self): return self.store.list_contexts()
    def messages(self, context_id): return _dump(self.store.list_messages(context_id))

    # Settings
    def settings_show(self):
        s = self.settings
        masked = f"{s.api_key[:4]}…{s.api_key[-4:]}" if len(s.api_key) > 8 else "•" * len(s.api_key)
        oa = s.openalex_api_key
        oa_masked = f"{oa[:4]}…{oa[-4:]}" if len(oa) > 8 else "•" * len(oa)
        return {"provider": s.provider, "baseUrl": s.base_url, "model": s.model,
                "apiKeyMasked": masked, "hasKey": bool(s.api_key),
                "openalexKeyMasked": oa_masked, "hasOpenalexKey": bool(oa)}
    def settings_set(self, provider=None, base_url=None, model=None, api_key=None,
                     image_base_url=None, image_model=None, image_api_key=None,
                     openalex_api_key=None):
        from paperclaw import literature
        s = self.settings
        if provider: s.provider = provider
        if base_url is not None: s.base_url = base_url or None
        if model: s.model = model
        if api_key: s.api_key = api_key
        if image_base_url is not None: s.image_base_url = image_base_url or None
        if image_model is not None: s.image_model = image_model or None
        if image_api_key: s.image_api_key = image_api_key
        if openalex_api_key: s.openalex_api_key = openalex_api_key
        save_settings(self.home, s)
        literature.configure(s.openalex_api_key)
        return self.settings_show()

    # Hardware / environment
    def hardware_show(self):
        return _dump(service.get_hardware_view(self.store))
    def hardware_detect(self):
        return _dump(self._run(service.detect_hardware(self.store, self.settings)))
    def hardware_ssh_set(self, targets):
        from paperclaw.server.models import SSHTarget
        objs = [SSHTarget.model_validate(t) for t in targets]
        return _dump(service.save_ssh_targets(self.store, objs))
    def hardware_run_config(self, patch):
        from paperclaw.server.models import RunConfig
        cfg = self.store.get_run_config().model_dump(by_alias=True)
        cfg.update({k: v for k, v in patch.items() if v is not None})
        return _dump(service.save_run_config(self.store, RunConfig.model_validate(cfg)))

    # Doctor / environment readiness
    def doctor(self):
        return _dump(service.environment_report(self.settings, self.home))

    # Experiment jobs (detached, monitored)
    def experiments_list(self):
        from paperclaw import jobs
        return jobs.list_experiment_jobs(self.store)


class RemoteClient:
    """Talks to a running backend (`--backend URL`)."""

    def __init__(self, backend: str) -> None:
        self.http = httpx.Client(base_url=backend.rstrip("/"), timeout=300.0)

    def _req(self, method: str, path: str, **kwargs) -> Any:
        try:
            resp = self.http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ClientError(f"Cannot reach backend: {exc}")
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except Exception:
                detail = resp.text
            raise ClientError(f"{resp.status_code}: {detail}")
        return resp.json() if resp.status_code != 204 else None

    # Domains
    def domains_list(self): return self._req("GET", "/api/domains")
    def domain_create(self, name): return self._req("POST", "/api/domains", json={"name": name})
    def domain_auto(self, prompt): return self._req("POST", "/api/domains/auto", json={"prompt": prompt})

    def domain_auto_stream(self, prompt: str, on_event: Callable[[dict], None]) -> None:
        try:
            with self.http.stream("POST", "/api/domains/auto-stream", json={"prompt": prompt}) as resp:
                if resp.status_code >= 400:
                    raise ClientError(f"{resp.status_code}: {resp.read().decode()}")
                for line in resp.iter_lines():
                    if line.startswith("data: "):
                        try:
                            on_event(json.loads(line[6:]))
                        except json.JSONDecodeError:
                            pass
        except httpx.HTTPError as exc:
            raise ClientError(f"Cannot reach backend: {exc}")
    def domain_select(self, domain_id, selected):
        return self._req("PUT", f"/api/domains/{domain_id}/select", json={"selected": selected})
    def domain_delete(self, domain_id): self._req("DELETE", f"/api/domains/{domain_id}")
    def domain_spec(self, domain_id):
        return self._req("GET", f"/api/domains/{domain_id}/spec")["content"]
    def domain_suggestions(self, domain_id):
        return self._req("GET", f"/api/domains/{domain_id}/suggestions")
    def domain_set_codebase(self, domain_id, url):
        return self._req("POST", f"/api/domains/{domain_id}/codebase", json={"url": url})
    def domain_clear_codebase(self, domain_id):
        return self._req("DELETE", f"/api/domains/{domain_id}/codebase")

    # Writing styles
    def writing_styles_list(self, domain_id=None):
        q = f"?domainId={domain_id}" if domain_id else ""
        return self._req("GET", f"/api/writing-styles{q}")
    def writing_style_get(self, name, domain_id=None):
        q = f"?domainId={domain_id}" if domain_id else ""
        return self._req("GET", f"/api/writing-styles/{name}{q}")
    def writing_style_save(self, name, content, domain_id=None):
        return self._req("POST", "/api/writing-styles",
                         json={"name": name, "content": content, "domainId": domain_id})

    # Benchmark templates
    def benchmarks_list(self, domain_id=None):
        q = f"?domainId={domain_id}" if domain_id else ""
        return self._req("GET", f"/api/benchmarks{q}")
    def benchmark_get(self, name, domain_id=None):
        q = f"?domainId={domain_id}" if domain_id else ""
        return self._req("GET", f"/api/benchmarks/{name}{q}")
    def benchmark_save(self, name, content, domain_id=None):
        return self._req("POST", "/api/benchmarks",
                         json={"name": name, "content": content, "domainId": domain_id})

    # Seeds
    def seeds_list(self): return self._req("GET", "/api/brainstorm")
    def seed_add(self, text): return self._req("POST", "/api/brainstorm", json={"text": text})
    def seed_delete(self, seed_id): self._req("DELETE", f"/api/brainstorm/{seed_id}")
    def seeds_generate(self, hint=None, idea_types=None, emphasis=None, count=None):
        return self._req("POST", "/api/brainstorm/generate", json={
            "hint": hint, "ideaTypes": idea_types, "emphasis": emphasis, "count": count,
        })

    def seeds_generate_stream(self, on_event: Callable[[dict], None], hint=None,
                              idea_types=None, emphasis=None, count=None) -> None:
        body = {"hint": hint, "ideaTypes": idea_types, "emphasis": emphasis, "count": count}
        try:
            with self.http.stream("POST", "/api/brainstorm/generate-stream", json=body) as resp:
                if resp.status_code >= 400:
                    raise ClientError(f"{resp.status_code}: {resp.read().decode()}")
                for line in resp.iter_lines():
                    if line.startswith("data: "):
                        try:
                            on_event(json.loads(line[6:]))
                        except json.JSONDecodeError:
                            pass
        except httpx.HTTPError as exc:
            raise ClientError(f"Cannot reach backend: {exc}")

    # Ideas
    def ideas_list(self): return self._req("GET", "/api/ideas")
    def idea_create(self, title): return self._req("POST", "/api/ideas", json={"title": title})
    def idea_duplicate(self, idea_id): return self._req("POST", f"/api/ideas/{idea_id}/duplicate")
    def idea_delete(self, idea_id): self._req("DELETE", f"/api/ideas/{idea_id}")
    def idea_spec(self, idea_id): return self._req("GET", f"/api/ideas/{idea_id}/spec")["content"]
    def idea_domains_get(self, idea_id):
        return self._req("GET", f"/api/ideas/{idea_id}/domains")["domainIds"]
    def idea_domains_set(self, idea_id, domain_ids):
        return self._req("PUT", f"/api/ideas/{idea_id}/domains", {"domainIds": domain_ids})["domainIds"]
    def idea_set_color(self, idea_id, color):
        return self._req("PUT", f"/api/ideas/{idea_id}/color", {"color": color or None})
    def idea_resources_get(self, idea_id):
        return self._req("GET", f"/api/ideas/{idea_id}/resources")
    def idea_resources_set(self, idea_id, *, experiment_mode=None, ssh_target_id=None,
                           use_reference_codebase=None):
        body = {}
        if experiment_mode is not None: body["experimentMode"] = experiment_mode
        if ssh_target_id is not None: body["sshTargetId"] = ssh_target_id
        if use_reference_codebase is not None: body["useReferenceCodebase"] = use_reference_codebase
        return self._req("PUT", f"/api/ideas/{idea_id}/resources", body)
    def references_list(self, idea_id):
        return self._req("GET", f"/api/ideas/{idea_id}/references")
    def references_add(self, idea_id, doi=None, query=None):
        return self._req("POST", f"/api/ideas/{idea_id}/references/add", json={"doi": doi, "query": query})
    def references_validate(self, idea_id):
        return self._req("POST", f"/api/ideas/{idea_id}/references/validate")
    def references_generate(self, idea_id):
        return self._req("POST", f"/api/ideas/{idea_id}/references/generate")
    def hypothesis_map_show(self, idea_id):
        return self._req("GET", f"/api/ideas/{idea_id}/hypothesis-map")
    def hypothesis_map_generate(self, idea_id):
        return self._req("POST", f"/api/ideas/{idea_id}/hypothesis-map/generate")
    def hypothesis_detail(self, idea_id, hid):
        return self._req("GET", f"/api/ideas/{idea_id}/hypotheses/{hid}")
    def hypothesis_delete(self, idea_id, hid):
        return self._req("DELETE", f"/api/ideas/{idea_id}/hypotheses/{hid}")
    def hypothesis_add_child(self, idea_id, parent_hid, statement):
        return self._req("POST", f"/api/ideas/{idea_id}/hypotheses/{parent_hid}/children",
                         {"statement": statement})
    def hypothesis_rerun(self, idea_id, hid):
        return self._req("POST", f"/api/ideas/{idea_id}/hypotheses/{hid}/experiment/rerun")
    def hypothesis_plan(self, idea_id, hid):
        return self._req("POST", f"/api/ideas/{idea_id}/hypotheses/{hid}/plan")
    def hypothesis_experiment(self, idea_id, hid):
        return self._req("POST", f"/api/ideas/{idea_id}/hypotheses/{hid}/experiment")
    def workspace_files(self, idea_id, path=""):
        return self._req("GET", f"/api/ideas/{idea_id}/files",
                         params={"path": path} if path else None)
    def workspace_file(self, idea_id, path):
        try:
            resp = self.http.request("GET", f"/api/ideas/{idea_id}/raw", params={"path": path})
        except httpx.HTTPError as exc:
            raise ClientError(f"Cannot reach backend: {exc}")
        if resp.status_code >= 400:
            raise ClientError(f"{resp.status_code}: file not found")
        return resp.content
    def hypothesis_experiment_stream(self, idea_id, hid, on_event):
        try:
            with self.http.stream("POST", f"/api/ideas/{idea_id}/hypotheses/{hid}/experiment/stream") as resp:
                if resp.status_code >= 400:
                    raise ClientError(f"{resp.status_code}: {resp.read().decode()}")
                for line in resp.iter_lines():
                    if line.startswith("data: "):
                        try:
                            on_event(json.loads(line[6:]))
                        except json.JSONDecodeError:
                            pass
        except httpx.HTTPError as exc:
            raise ClientError(f"Cannot reach backend: {exc}")
    def research_stream(self, idea_id, on_event, restart=False, max_hypotheses=4, page_limit=9):
        body = {"restart": restart, "maxHypotheses": max_hypotheses, "pageLimit": page_limit}
        try:
            with self.http.stream("POST", f"/api/ideas/{idea_id}/iterative-research-stream", json=body) as resp:
                if resp.status_code >= 400:
                    raise ClientError(f"{resp.status_code}: {resp.read().decode()}")
                for line in resp.iter_lines():
                    if line.startswith("data: "):
                        try:
                            on_event(json.loads(line[6:]))
                        except json.JSONDecodeError:
                            pass
        except httpx.HTTPError as exc:
            raise ClientError(f"Cannot reach backend: {exc}")

    def auto_research_stream(self, topic, on_event, target_positive=2, max_hypotheses=6, page_limit=8,
                             **over):
        # Auto mode is a long-running local orchestration (doctor → domain → idea →
        # hypotheses → paper); no HTTP route yet, so run it against the local home.
        raise ClientError("`auto` runs locally — drop --backend (it builds files under $PAPERCLAW_HOME).")

    def auto_resume_stream(self, on_event, idea_id=None):
        raise ClientError("`auto resume` runs locally — drop --backend (it works on files under $PAPERCLAW_HOME).")

    def auto_idea_stream(self, idea_id, on_event, target_positive=2, max_hypotheses=6, page_limit=8,
                         **over):
        raise ClientError("`auto run --idea` runs locally — drop --backend (it works on files under $PAPERCLAW_HOME).")

    def auto_stop(self, idea_id=None):
        if not idea_id:  # pick the single running run, like the local path
            running = [r for r in self.auto_status() if r.get("status") == "running" and r.get("ideaId")]
            if len(running) == 1:
                idea_id = running[0]["ideaId"]
            elif not running:
                return {"ok": False, "detail": "no running auto run found"}
            else:
                return {"ok": False, "detail": "multiple auto runs — specify which with --idea <id>"}
        try:
            resp = self.http.post(f"/api/ideas/{idea_id}/auto-run/stop")
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            raise ClientError(f"Cannot reach backend: {exc}")

    def auto_status(self):
        try:
            resp = self.http.get("/api/auto-runs")
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            raise ClientError(f"Cannot reach backend: {exc}")

    # Chat
    def chat_send(self, content, idea_id=None, seed_id=None, domain_id=None):
        return self._req("POST", "/api/chat/send", json={
            "content": content, "ideaId": idea_id, "seedId": seed_id, "domainId": domain_id,
        })
    def contexts(self): return self._req("GET", "/api/chat/contexts")
    def messages(self, context_id): return self._req("GET", f"/api/chat/{context_id}/messages")

    # Settings
    def settings_show(self): return self._req("GET", "/api/settings")
    def settings_set(self, provider=None, base_url=None, model=None, api_key=None,
                     image_base_url=None, image_model=None, image_api_key=None,
                     openalex_api_key=None):
        body = {}
        if provider: body["provider"] = provider
        if base_url is not None: body["baseUrl"] = base_url
        if model: body["model"] = model
        if api_key: body["apiKey"] = api_key
        if image_base_url is not None: body["imageBaseUrl"] = image_base_url
        if image_model is not None: body["imageModel"] = image_model
        if image_api_key: body["imageApiKey"] = image_api_key
        if openalex_api_key: body["openalexApiKey"] = openalex_api_key
        return self._req("PUT", "/api/settings", json=body)

    # Hardware / environment
    def hardware_show(self): return self._req("GET", "/api/hardware")
    def hardware_detect(self): return self._req("POST", "/api/hardware/detect")
    def hardware_ssh_set(self, targets):
        return self._req("PUT", "/api/hardware/ssh", json={"sshTargets": targets})
    def hardware_run_config(self, patch):
        cfg = dict(self._req("GET", "/api/hardware").get("runConfig", {}))
        cfg.update({k: v for k, v in patch.items() if v is not None})
        return self._req("PUT", "/api/hardware/run-config", json=cfg)

    # Doctor / environment readiness — checks the BACKEND host's environment
    def doctor(self): return self._req("GET", "/api/doctor")

    # Experiment jobs (detached, monitored)
    def experiments_list(self): return self._req("GET", "/api/experiments")


def make_client(backend: str | None):
    return RemoteClient(backend) if backend else LocalClient()
