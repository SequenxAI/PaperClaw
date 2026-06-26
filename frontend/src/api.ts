import type {
  BrainstormOptions, ChatContext, Domain, DomainSpec, DoctorReport, HardwareView, Idea, IdeaDomains, IdeaResources, IdeaResourcesUpdate, IdeaLocation, IdeaSpec,
  HypothesisDetail, HypothesisMap, Message, PaperContent, ReferencesView, ReferenceValidation,
  Resource, RunConfig, Seed, SSHTarget, SettingsUpdate, SettingsView, Skill, WorkspaceListing,
  WritingStyle, Benchmark, ExperimentJob, AutoRunStatus
} from './types'

// Packaged desktop app loads from file:// — relative /api paths have no origin
// there, so target the local backend directly. Web builds keep relative paths
// (dev proxy / same-origin production serving).
const API_BASE = window.location.protocol === 'file:' ? 'http://127.0.0.1:8230' : ''

// ── SSE helpers ───────────────────────────────────────────────────────────────

export type SseEvent = Record<string, unknown>

/** Read server-sent events from a fetch Response body as an async generator. */
async function* readSSE(response: Response): AsyncGenerator<SseEvent> {
  const reader = response.body!.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() ?? ''
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue
        try { yield JSON.parse(line.slice(6)) as SseEvent } catch { /* skip malformed */ }
      }
    }
  } finally {
    reader.releaseLock()
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(API_BASE + path, {
    headers: { 'Content-Type': 'application/json' },
    ...init
  })
  if (!res.ok) {
    let detail = res.statusText
    try {
      const body = await res.json()
      if (body.detail) {
        // FastAPI validation errors come back as an array of {loc,msg,type}
        // objects — stringify them readably instead of "[object Object]".
        detail = typeof body.detail === 'string'
          ? body.detail
          : Array.isArray(body.detail)
            ? body.detail.map((d: { msg?: string }) => d?.msg ?? JSON.stringify(d)).join('; ')
            : JSON.stringify(body.detail)
      }
    } catch { /* keep statusText */ }
    throw new Error(detail)
  }
  if (res.status === 204) return undefined as T
  return res.json()
}

export const api = {
  // Brainstorm seeds
  getSeeds: () => request<Seed[]>('/api/brainstorm'),
  addSeed: (text: string) =>
    request<Seed>('/api/brainstorm', { method: 'POST', body: JSON.stringify({ text }) }),
  deleteSeed: (id: string) =>
    request<void>(`/api/brainstorm/${id}`, { method: 'DELETE' }),
  generateSeeds: (hint?: string, opts?: Partial<BrainstormOptions>) =>
    request<Seed[]>('/api/brainstorm/generate', {
      method: 'POST',
      body: JSON.stringify({ hint, ...opts }),
    }),

  // Domains
  getDomains: () => request<Domain[]>('/api/domains'),
  addDomain: (name: string) =>
    request<Domain>('/api/domains', { method: 'POST', body: JSON.stringify({ name }) }),
  autoCreateDomain: (prompt: string) =>
    request<Domain>('/api/domains/auto', { method: 'POST', body: JSON.stringify({ prompt }) }),
  selectDomain: (id: string, selected: boolean) =>
    request<Domain>(`/api/domains/${id}/select`, { method: 'PUT', body: JSON.stringify({ selected }) }),
  deleteDomain: (id: string) =>
    request<void>(`/api/domains/${id}`, { method: 'DELETE' }),
  getDomainSpec: (id: string) => request<DomainSpec>(`/api/domains/${id}/spec`),
  getDomainSuggestions: (id: string) => request<string[]>(`/api/domains/${id}/suggestions`),
  setDomainCodebase: (id: string, url: string) =>
    request<Domain>(`/api/domains/${id}/codebase`, { method: 'POST', body: JSON.stringify({ url }) }),
  clearDomainCodebase: (id: string) =>
    request<Domain>(`/api/domains/${id}/codebase`, { method: 'DELETE' }),

  // Ideas
  getIdeas: () => request<Idea[]>('/api/ideas'),
  addIdea: (title: string) =>
    request<Idea>('/api/ideas', { method: 'POST', body: JSON.stringify({ title }) }),
  activateIdea: (id: string) =>
    request<Idea>(`/api/ideas/${id}/activate`, { method: 'PUT' }),
  setIdeaColor: (id: string, color: string | null) =>
    request<Idea>(`/api/ideas/${id}/color`, { method: 'PUT', body: JSON.stringify({ color }) }),
  duplicateIdea: (id: string) =>
    request<Idea>(`/api/ideas/${id}/duplicate`, { method: 'POST' }),
  deleteIdea: (id: string) =>
    request<void>(`/api/ideas/${id}`, { method: 'DELETE' }),
  revealIdea: (id: string) =>
    request<IdeaLocation>(`/api/ideas/${id}/reveal`, { method: 'POST' }),

  // Skills (chat slash-commands)
  getSkills: () => request<Skill[]>('/api/skills'),

  // Spec (IDEA.md)
  getSpec: (ideaId: string) => request<IdeaSpec>(`/api/ideas/${ideaId}/spec`),
  putSpec: (ideaId: string, content: string) =>
    request<IdeaSpec>(`/api/ideas/${ideaId}/spec`, { method: 'PUT', body: JSON.stringify({ content }) }),

  // Connect an idea to domains (an idea may connect to several; mounts each under ./domains/<name>)
  getIdeaDomains: (ideaId: string) => request<IdeaDomains>(`/api/ideas/${ideaId}/domains`),
  setIdeaDomains: (ideaId: string, domainIds: string[]) =>
    request<IdeaDomains>(`/api/ideas/${ideaId}/domains`, { method: 'PUT', body: JSON.stringify({ domainIds }) }),

  // Per-idea experiment resource allocation (compute/GPU host; used by auto + manual runs)
  getIdeaResources: (ideaId: string) => request<IdeaResources>(`/api/ideas/${ideaId}/resources`),
  setIdeaResources: (ideaId: string, patch: IdeaResourcesUpdate) =>
    request<IdeaResources>(`/api/ideas/${ideaId}/resources`, { method: 'PUT', body: JSON.stringify(patch) }),

  // Generated paper (Markdown for rendering; null content if none yet)
  getPaper: (ideaId: string, version?: number) =>
    request<PaperContent>(`/api/ideas/${ideaId}/paper-content${version ? `?version=${version}` : ''}`),

  // Chat (contextId: idea id | "seed-<id>" | "domain-<id>" | "_scratch")
  getContexts: () => request<ChatContext[]>('/api/chat/contexts'),
  getMessages: (contextId: string) => request<Message[]>(`/api/chat/${contextId}/messages`),
  sendChat: (content: string, ctx?: { ideaId?: string; seedId?: string; domainId?: string }) =>
    request<Message[]>('/api/chat/send', {
      method: 'POST',
      body: JSON.stringify({
        ideaId: ctx?.ideaId, seedId: ctx?.seedId, domainId: ctx?.domainId, content
      })
    }),

  // Resources
  getResources: (ideaId: string) => request<Resource[]>(`/api/resources/${ideaId}`),

  // References (ref.bib)
  getReferences: (ideaId: string) => request<ReferencesView>(`/api/ideas/${ideaId}/references`),
  addReference: (ideaId: string, body: { doi?: string; query?: string }) =>
    request<ReferencesView>(`/api/ideas/${ideaId}/references/add`, { method: 'POST', body: JSON.stringify(body) }),
  validateReferences: (ideaId: string) =>
    request<ReferenceValidation[]>(`/api/ideas/${ideaId}/references/validate`, { method: 'POST' }),
  generateReferences: (ideaId: string) =>
    request<ReferencesView>(`/api/ideas/${ideaId}/references/generate`, { method: 'POST' }),

  // Hypothesis map
  getHypothesisMap: (ideaId: string) => request<HypothesisMap>(`/api/ideas/${ideaId}/hypothesis-map`),
  generateHypothesisMap: (ideaId: string) =>
    request<HypothesisMap>(`/api/ideas/${ideaId}/hypothesis-map/generate`, { method: 'POST' }),
  getHypothesisDetail: (ideaId: string, hid: string) =>
    request<HypothesisDetail>(`/api/ideas/${ideaId}/hypotheses/${hid}`),
  deleteHypothesisNode: (ideaId: string, hid: string) =>
    request<HypothesisMap>(`/api/ideas/${ideaId}/hypotheses/${hid}`, { method: 'DELETE' }),
  addChildHypothesis: (ideaId: string, hid: string, statement: string) =>
    request<HypothesisMap>(`/api/ideas/${ideaId}/hypotheses/${hid}/children`,
      { method: 'POST', body: JSON.stringify({ statement }) }),
  rerunExperiment: (ideaId: string, hid: string) =>
    request<ExperimentJob>(`/api/ideas/${ideaId}/hypotheses/${hid}/experiment/rerun`, { method: 'POST' }),
  generateHypothesisPlan: (ideaId: string, hid: string) =>
    request<HypothesisDetail>(`/api/ideas/${ideaId}/hypotheses/${hid}/plan`, { method: 'POST' }),
  runHypothesisExperiment: (ideaId: string, hid: string) =>
    request<HypothesisDetail>(`/api/ideas/${ideaId}/hypotheses/${hid}/experiment`, { method: 'POST' }),

  // Workspace files (browse the agent's code / figures / results / logs)
  getWorkspaceFiles: (ideaId: string, path = '') =>
    request<WorkspaceListing>(
      `/api/ideas/${ideaId}/files${path ? `?path=${encodeURIComponent(path)}` : ''}`),
  /** Fetchable URL for a single raw workspace file (img src / download / preview). */
  rawFileUrl: (ideaId: string, path: string) =>
    API_BASE + `/api/ideas/${ideaId}/raw?path=${encodeURIComponent(path)}`,

  // Settings
  getSettings: () => request<SettingsView>('/api/settings'),
  putSettings: (update: SettingsUpdate) =>
    request<SettingsView>('/api/settings', { method: 'PUT', body: JSON.stringify(update) }),

  // Doctor / environment readiness
  getDoctor: () => request<DoctorReport>('/api/doctor'),

  // Writing styles (prose-style guides for /write_paper)
  getWritingStyles: (domainId?: string) =>
    request<WritingStyle[]>(`/api/writing-styles${domainId ? `?domainId=${domainId}` : ''}`),
  saveWritingStyle: (name: string, content: string, domainId?: string) =>
    request<{ name: string }>('/api/writing-styles', {
      method: 'POST', body: JSON.stringify({ name, content, domainId }) }),
  // Benchmark templates (fixed protocol + published, cited baselines)
  getBenchmarks: (domainId?: string) =>
    request<Benchmark[]>(`/api/benchmarks${domainId ? `?domainId=${domainId}` : ''}`),
  getBenchmark: (name: string, domainId?: string) =>
    request<{ name: string; content: string }>(`/api/benchmarks/${encodeURIComponent(name)}${domainId ? `?domainId=${domainId}` : ''}`),
  // Benchmark templates available to an idea (idea scope + domain + global) — Spec tab picker
  getIdeaBenchmarks: (ideaId: string) =>
    request<Benchmark[]>(`/api/ideas/${ideaId}/benchmarks`),
  // A benchmark for the idea's Benchmark view — a specific name, else the one in force
  getIdeaBenchmark: (ideaId: string, name?: string) =>
    request<{ name: string | null; content: string | null }>(
      `/api/ideas/${ideaId}/benchmark${name ? `?name=${encodeURIComponent(name)}` : ''}`),
  saveBenchmark: (name: string, content: string, domainId?: string) =>
    request<{ name: string }>('/api/benchmarks', {
      method: 'POST', body: JSON.stringify({ name, content, domainId }) }),
  // Upload a LaTeX venue template (.zip / .sty / .cls / .tex) into an idea's venue/ dir.
  uploadVenueTemplate: (ideaId: string, filename: string, contentBase64: string) =>
    request<{ files: string[] }>(`/api/ideas/${ideaId}/venue/upload`, {
      method: 'POST', body: JSON.stringify({ filename, contentBase64 }) }),

  // Hardware / environment
  getHardware: () => request<HardwareView>('/api/hardware'),
  detectHardware: () => request<HardwareView>('/api/hardware/detect', { method: 'POST' }),
  putSshTargets: (sshTargets: SSHTarget[]) =>
    request<HardwareView>('/api/hardware/ssh', { method: 'PUT', body: JSON.stringify({ sshTargets }) }),
  putRunConfig: (cfg: RunConfig) =>
    request<HardwareView>('/api/hardware/run-config', { method: 'PUT', body: JSON.stringify(cfg) }),

  // ── Streaming endpoints (SSE via fetch) ──────────────────────────────────

  /** Stream a chat reply — yields SSE events: delta | done | error.
   *  Pass an AbortSignal and abort it to STOP generation (the Stop button). */
  streamChat: async function* (
    content: string,
    ctx: { ideaId?: string; seedId?: string; domainId?: string } = {},
    signal?: AbortSignal,
  ): AsyncGenerator<SseEvent> {
    const resp = await fetch(API_BASE + '/api/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content, ideaId: ctx.ideaId, seedId: ctx.seedId, domainId: ctx.domainId }),
      signal,
    })
    if (!resp.ok) { yield { type: 'error', message: resp.statusText }; return }
    yield* readSSE(resp)
  },

  /** Stream domain auto-creation — yields SSE events: status | done | error */
  streamAutoCreateDomain: async function* (prompt: string): AsyncGenerator<SseEvent> {
    const resp = await fetch(API_BASE + '/api/domains/auto-stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt }),
    })
    if (!resp.ok) { yield { type: 'error', message: resp.statusText }; return }
    yield* readSSE(resp)
  },

  /** Stream brainstorm seed generation — yields SSE events: status | done | error */
  streamGenerateSeeds: async function* (hint?: string, opts?: Partial<BrainstormOptions>): AsyncGenerator<SseEvent> {
    const resp = await fetch(API_BASE + '/api/brainstorm/generate-stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ hint, ...opts }),
    })
    if (!resp.ok) { yield { type: 'error', message: resp.statusText }; return }
    yield* readSSE(resp)
  },

  // Experiment jobs (detached, monitored) — start a process, then attach to its log.
  getExperiments: () => request<ExperimentJob[]>('/api/experiments'),
  // Auto-mode (`paperclaw auto`) pipeline status — PER IDEA (ideas run in parallel).
  getIdeaAutoRun: (ideaId: string) => request<AutoRunStatus | null>(`/api/ideas/${ideaId}/auto-run`),
  // Every idea's auto-run snapshot (global view — used to flag running ideas in the sidebar).
  listAutoRuns: () => request<AutoRunStatus[]>('/api/auto-runs'),
  // Tail a detached auto run's live log from byte `from` (the agent-feedback feed).
  getAutoRunLog: (ideaId: string, from = 0) =>
    request<{ text: string; next: number; running: boolean }>(`/api/ideas/${ideaId}/auto-run/log?from=${from}`),
  stopIdeaAutoRun: (ideaId: string) =>
    request<{ ok: boolean; detail: string }>(`/api/ideas/${ideaId}/auto-run/stop`, { method: 'POST' }),
  /** Launch a detached, banner-tracked auto run — from a `topic` (new domain+idea)
   *  or an `ideaId` (the per-idea ⚡ Auto run on an existing idea). The optional
   *  per-run overrides (experiment execution / writing style / reference codebase)
   *  apply to THIS run only. */
  startAutoRun: (body: {
    topic?: string; ideaId?: string; positive: number; maxHypotheses: number; pageLimit: number
    maxDepth?: number
    experimentMode?: string; sshTargetId?: string; writingStyle?: string; benchmark?: string
    useReferenceCodebase?: boolean; fillPage?: boolean
  }) =>
    request<{ ok: boolean; detail: string }>('/api/auto-run/start', { method: 'POST', body: JSON.stringify(body) }),
  startExperiment: (ideaId: string, hid: string) =>
    request<ExperimentJob>(`/api/ideas/${ideaId}/hypotheses/${hid}/experiment/start`, { method: 'POST' }),
  getExperimentJob: (ideaId: string, hid: string) =>
    request<ExperimentJob>(`/api/ideas/${ideaId}/hypotheses/${hid}/experiment/job`),
  cancelExperiment: (ideaId: string, hid: string) =>
    request<ExperimentJob>(`/api/ideas/${ideaId}/hypotheses/${hid}/experiment/cancel`, { method: 'POST' }),
  /** Attach to a job's live log (replays from `from`, then streams). GET = re-attachable.
   *  Pass a signal and abort it on unmount so connections don't accumulate. */
  streamExperimentLog: async function* (ideaId: string, hid: string, from = 0, signal?: AbortSignal): AsyncGenerator<SseEvent> {
    const resp = await fetch(API_BASE + `/api/ideas/${ideaId}/hypotheses/${hid}/experiment/stream?from=${from}`, { signal })
    if (!resp.ok) { yield { type: 'error', message: resp.statusText }; return }
    yield* readSSE(resp)
  },
}

export const SCRATCH_ID = '_scratch'
