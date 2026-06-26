import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import LeftSidebar from './components/LeftSidebar'
import MainPanel from './components/MainPanel'
import ResourcesPanel from './components/ResourcesPanel'
import SettingsModal from './components/SettingsModal'
import AutoRunLauncher from './components/AutoRunLauncher'
import AutoRunSettingsModal, { type AutoRunSettings } from './components/AutoRunSettingsModal'
import FeedbackPanel, { type FeedbackEntry } from './components/FeedbackPanel'
import { api, SCRATCH_ID } from './api'

const API_BASE = window.location.protocol === 'file:' ? 'http://127.0.0.1:8230' : ''
import type { AutoRunStatus, BrainstormOptions, Domain, ExperimentJob, Idea, Message, MessagePart, Resource, Seed, TodoItem } from './types'
import styles from './App.module.css'

let _id = 0
const localId = () => `local-${Date.now()}-${++_id}`

/** A chat send target — one of an idea / draft seed / domain (or scratch). */
type ChatCtx = { ideaId?: string; seedId?: string; domainId?: string }

/** The conversation context id a send belongs to (mirror of the logic the
 *  stream/pending slots are keyed by). */
const ctxKeyOf = (ctx: ChatCtx): string =>
  ctx.domainId ? `domain-${ctx.domainId}`
    : ctx.seedId ? `seed-${ctx.seedId}`
      : ctx.ideaId ?? SCRATCH_ID

/** A message the user typed while a reply was still streaming — held until the
 *  current reply finishes, then auto-sent (Claude Code style). */
interface QueuedMsg { id: string; text: string; ctx: ChatCtx }

type Theme = 'dark' | 'light'

const RIGHT_MIN = 220
const RIGHT_MAX = 1100

export default function App() {
  const [domains, setDomains] = useState<Domain[]>([])
  const [seeds, setSeeds] = useState<Seed[]>([])
  const [ideas, setIdeas] = useState<Idea[]>([])
  const [mapVersion, setMapVersion] = useState(0)  // bumped when an agent reply regenerates the map
  const [messages, setMessages] = useState<Message[]>([])
  // In-flight chat streams, keyed by conversation context id. Kept OUT of
  // `messages` so switching conversations (which reloads `messages`) never
  // discards a stream that is still running in another conversation — it stays
  // here and is merged back in by `displayMessages` when that context is viewed.
  const [pending, setPending] = useState<Record<string, Message[]>>({})
  // Messages the user sent while a reply was still streaming, keyed by context
  // id. They render as a "queued" strip and are auto-sent (one at a time, in
  // order) when that conversation's in-flight reply finishes — so sends are
  // sequential like Claude Code, not concurrent.
  const [queued, setQueued] = useState<Record<string, QueuedMsg[]>>({})
  const queuedRef = useRef<Record<string, QueuedMsg[]>>({})
  // How many chat streams are live per context (normally 0 or 1). Drives the
  // enqueue decision and the drain-on-finish, without reading async-stale state.
  const activeStreams = useRef<Record<string, number>>({})
  // Abort controller for the live chat stream per context — lets the Stop button
  // cancel an in-flight reply (the latest stream in that conversation).
  const chatAbort = useRef<Record<string, AbortController>>({})
  const [resources, setResources] = useState<Resource[]>([])
  const [ideaSpec, setIdeaSpec] = useState<string | null>(null)
  const [domainSpec, setDomainSpec] = useState<string | null>(null)
  const [generating, setGenerating] = useState(false)
  const [generateStatus, setGenerateStatus] = useState<string | null>(null)
  const [autoCreatingDomain, setAutoCreatingDomain] = useState(false)
  const [autoCreateStatus, setAutoCreateStatus] = useState<string | null>(null)
  const [streamingDomainSpec, setStreamingDomainSpec] = useState<string | null>(null)
  const [domainSearch, setDomainSearch] = useState<{ query: string; broad: string[]; sota: string[] } | null>(null)
  const [domainThinking, setDomainThinking] = useState('')
  const [brainstormOpts, setBrainstormOpts] = useState<BrainstormOptions>(() => {
    try {
      const raw = localStorage.getItem('paperclaw-brainstorm-opts')
      if (raw) return { ideaTypes: [], emphasis: [], count: 5, ...JSON.parse(raw) }
    } catch { /* ignore */ }
    return { ideaTypes: [], emphasis: [], count: 5 }
  })
  const [paperText, setPaperText] = useState<string | null>(null)
  const [paperHasPdf, setPaperHasPdf] = useState(false)
  const [paperFile, setPaperFile] = useState<string | null>(null)  // shown version filename
  const [paperVersions, setPaperVersions] = useState<number[]>([])  // available versions
  const [paperVersion, setPaperVersion] = useState<number | null>(null)  // selected (null = latest)
  // Bumped on every paper (re)fetch — appended to the PDF iframe URL so a RECOMPILE
  // of the same version busts the browser cache (else the iframe shows the stale PDF,
  // e.g. without freshly-inserted figures).
  const [paperToken, setPaperToken] = useState(0)
  // Right-panel activity (generate map / references / plan …) surfaced as status
  // lines in the conversation history, keyed by context id (the idea id).
  const [activities, setActivities] = useState<Record<string, { key: string; text: string; status: 'running' | 'done' | 'error'; timestamp: number }[]>>({})
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [autoLauncherOpen, setAutoLauncherOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [toast, setToast] = useState<string | null>(null)
  const [activeSeedId, setActiveSeedId] = useState<string | null>(null)
  const [viewDomainId, setViewDomainId] = useState<string | null>(null)
  const [theme, setTheme] = useState<Theme>(
    () => (localStorage.getItem('paperclaw-theme') as Theme) || 'light'
  )
  const [rightWidth, setRightWidth] = useState(
    () => Number(localStorage.getItem('paperclaw-right-width')) || 296
  )
  const rightWidthRef = useRef(rightWidth)
  rightWidthRef.current = rightWidth

  const activeIdea = activeSeedId || viewDomainId ? undefined : ideas.find(i => i.isActive)
  const activeSeed = activeSeedId ? seeds.find(s => s.id === activeSeedId) ?? null : null
  const activeDomain = viewDomainId ? domains.find(d => d.id === viewDomainId) ?? null : null

  // Track the active idea id in a ref so async stream callbacks can tell
  // whether the user is still viewing the idea a background run belongs to.
  const activeIdeaIdRef = useRef<string | undefined>(undefined)
  activeIdeaIdRef.current = activeIdea?.id
  // Always-current ideas list for async callbacks (experiment poll / jump).
  const ideasRef = useRef<Idea[]>([])
  ideasRef.current = ideas

  // Refetch ideas from the server, preserving the local `isActive` selection, and
  // return the fresh list. Used to surface ideas created OUTSIDE this tab — e.g. by
  // `paperclaw auto …` in a terminal — so they appear in the sidebar.
  const refreshIdeas = useCallback(async (): Promise<Idea[]> => {
    try {
      const fresh = await api.getIdeas()
      setIdeas(prev => fresh.map(f => ({ ...f, isActive: prev.find(p => p.id === f.id)?.isActive ?? false })))
      return fresh
    } catch { return ideasRef.current }
  }, [])
  const chatId = viewDomainId
    ? `domain-${viewDomainId}`
    : activeSeedId
      ? `seed-${activeSeedId}`
      : activeIdea?.id ?? SCRATCH_ID

  // Async stream callbacks capture a stale `chatId`; this ref always holds the
  // conversation currently on screen so a finishing stream knows whether to
  // fold its result into the visible `messages` or leave it for a later reload.
  const chatIdRef = useRef(chatId)
  chatIdRef.current = chatId

  const fail = (e: unknown) => setError(String((e as Error).message ?? e))

  // ── Initial load ────────────────────────────────────────
  useEffect(() => {
    api.getDomains().then(setDomains).catch(fail)
    api.getSeeds().then(setSeeds).catch(fail)
    api.getIdeas().then(setIdeas).catch(fail)
  }, [])

  // ── Experiment-job monitor (poll all jobs across ideas) ──
  const [experiments, setExperiments] = useState<ExperimentJob[]>([])
  // Overall `paperclaw auto` pipeline status (polled), so the web UI can watch a
  // CLI-launched auto run's phase (doctor→domain→idea→hypotheses→paper).
  const [autoRun, setAutoRun] = useState<AutoRunStatus | null>(null)
  // Idea ids with a LIVE auto run (any idea, not just the active one) — drives the
  // sidebar's per-idea running indicator. Ideas can auto-run in parallel.
  const [runningAutoIdeas, setRunningAutoIdeas] = useState<Set<string>>(new Set())
  const [focusHyp, setFocusHyp] = useState<{ ideaId: string; hid: string; nonce: number } | null>(null)
  useEffect(() => {
    const poll = () => {
      api.getExperiments().then(jobs => {
        setExperiments(jobs)
        // A job for an idea this tab doesn't know about (e.g. started by `paperclaw auto` in a
        // terminal) → refresh the lists so the new idea/domain show up and are navigable.
        if (jobs.some(j => !ideasRef.current.some(i => i.id === j.ideaId))) {
          refreshIdeas()
          api.getDomains().then(setDomains).catch(() => {})
        }
      }).catch(() => {})
      // Which ideas are auto-running right now (global) — for the sidebar indicator.
      api.listAutoRuns().then(runs => setRunningAutoIdeas(
        new Set(runs.filter(r => r.status === 'running' && r.ideaId).map(r => r.ideaId as string))
      )).catch(() => {})
      // Auto-run status is PER IDEA — poll only the idea currently on screen, so its
      // panel shows its own banner (ideas can auto-run in parallel).
      const aid = activeIdeaIdRef.current
      if (aid) api.getIdeaAutoRun(aid).then(r => { if (activeIdeaIdRef.current === aid) setAutoRun(r) }).catch(() => {})
      else setAutoRun(null)
    }
    poll()
    const t = setInterval(poll, 4000)
    return () => clearInterval(t)
  }, [refreshIdeas])
  const jumpToExperiment = async (job: ExperimentJob) => {
    let idea = ideasRef.current.find(i => i.id === job.ideaId)
    if (!idea) idea = (await refreshIdeas()).find(i => i.id === job.ideaId)  // externally-created idea
    if (idea) setActiveIdea(idea.id)
    setFocusHyp({ ideaId: job.ideaId, hid: job.hypothesisId, nonce: Date.now() })
  }
  const stopAutoRun = async () => {
    const aid = activeIdeaIdRef.current
    if (!aid) return
    try { await api.stopIdeaAutoRun(aid) } catch { /* ignore */ }
    api.getIdeaAutoRun(aid).then(setAutoRun).catch(() => {})  // reflect 'stopped' immediately
  }
  const startAutoRun = async (settings: { topic: string; positive: number; maxHypotheses: number; pageLimit: number; maxDepth: number }) => {
    setAutoLauncherOpen(false)
    try {
      const res = await api.startAutoRun(settings)
      // Topic runs create a new idea mid-stream — the experiment poll surfaces it in
      // the sidebar; open it to watch its banner.
      setToast(res.ok ? `⚡ Auto run started — “${settings.topic}”` : `⚠️ ${res.detail}`)
    } catch (e) { fail(e) }
  }
  // Per-idea ⚡ Auto run: the button opens a settings modal; confirming launches the
  // same DETACHED, banner-tracked pipeline on the EXISTING idea with those per-run
  // settings (experiment execution / writing style / limits / codebase reuse).
  const [autoSettingsFor, setAutoSettingsFor] = useState<Idea | null>(null)
  const launchAutoRunForIdea = async (ideaId: string, settings: AutoRunSettings) => {
    setAutoSettingsFor(null)
    try {
      const res = await api.startAutoRun({ ideaId, ...settings })
      setToast(res.ok ? '⚡ Auto run started' : `⚠️ ${res.detail}`)
    } catch (e) { fail(e) }
    api.getIdeaAutoRun(ideaId).then(r => { if (activeIdeaIdRef.current === ideaId) setAutoRun(r) }).catch(() => {})
  }

  // ── Agent feedback panel (live stream for domain creation / brainstorm / auto run) ──
  const [feedback, setFeedback] = useState<{ title: string; entries: FeedbackEntry[]; running: boolean } | null>(null)
  const [feedbackOpen, setFeedbackOpen] = useState(false)
  const fbSession = useRef(0)  // bumped per action, so a stale async tail stops pushing
  // Streamed feedback is THROTTLED: tokens buffer in a ref and flush to state ~10×/s, so a
  // long stream (e.g. a multi-draft brainstorm = thousands of deltas) doesn't re-render the
  // whole app per token and freeze the UI (you couldn't even click Pin during a brainstorm).
  const fbBuf = useRef<[FeedbackEntry['kind'], string][]>([])
  const fbTimer = useRef<number | null>(null)
  const fbFlush = useCallback(() => {
    fbTimer.current = null
    const buf = fbBuf.current
    if (!buf.length) return
    fbBuf.current = []
    setFeedback(f => {
      if (!f) return f
      const entries = f.entries.slice()
      for (const [kind, text] of buf) {
        const last = entries[entries.length - 1]
        if (last && last.kind === kind && kind !== 'step')  // grow thinking/text blocks
          entries[entries.length - 1] = { ...last, text: last.text + text }
        else
          entries.push({ id: localId(), kind, text })
      }
      return { ...f, entries }
    })
  }, [])
  const fbStart = useCallback((title: string): number => {
    fbSession.current += 1
    fbBuf.current = []
    if (fbTimer.current != null) { clearTimeout(fbTimer.current); fbTimer.current = null }
    setFeedback({ title, entries: [], running: true })
    setFeedbackOpen(true)
    return fbSession.current
  }, [])
  const fbPush = useCallback((kind: FeedbackEntry['kind'], text: string) => {
    if (!text) return
    fbBuf.current.push([kind, text])
    if (fbTimer.current == null) fbTimer.current = window.setTimeout(fbFlush, 90)
  }, [fbFlush])
  const fbDone = useCallback(() => {
    fbFlush()  // flush any buffered tail before marking done
    setFeedback(f => (f ? { ...f, running: false } : f))
  }, [fbFlush])

  // Tail the active idea's detached auto run into the feedback panel while it runs.
  const autoActive = !!activeIdea && autoRun?.status === 'running' && autoRun.ideaId === activeIdea.id
  useEffect(() => {
    if (!autoActive || !activeIdea) return
    const mySession = fbStart(`Auto run · ${activeIdea.title}`)
    let stop = false
    let offset = 0
    const tick = async () => {
      if (stop || fbSession.current !== mySession) return
      try {
        const r = await api.getAutoRunLog(activeIdea.id, offset)
        if (fbSession.current !== mySession) return
        if (r.text) fbPush('text', r.text)
        offset = r.next
        if (!r.running) { fbDone(); return }
      } catch { /* ignore */ }
      if (!stop) setTimeout(tick, 1500)
    }
    tick()
    return () => { stop = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoActive, activeIdea?.id])

  // Switching ideas: drop the previous idea's banner immediately, then fetch the
  // newly-opened idea's run (the 4s poll keeps it fresh after that).
  useEffect(() => {
    setAutoRun(null)
    if (activeIdea) api.getIdeaAutoRun(activeIdea.id).then(setAutoRun).catch(() => {})
  }, [activeIdea?.id])

  // ── Conversation context: messages + idea spec/resources ─
  useEffect(() => {
    // Clear immediately so the PREVIOUS conversation's history doesn't linger under the new
    // one, and guard the async result: if the user switches again before this resolves, a
    // late response for the old chatId must NOT overwrite the new conversation (which made
    // histories appear "mixed"). `pending`/streaming is keyed separately, so it's unaffected.
    const forChat = chatId
    setMessages([])
    api.getMessages(chatId).then(m => { if (chatIdRef.current === forChat) setMessages(m) }).catch(fail)
    if (activeIdea) {
      api.getSpec(activeIdea.id).then(r => setIdeaSpec(r.content)).catch(fail)
      api.getResources(activeIdea.id).then(setResources).catch(fail)
    } else {
      setIdeaSpec(null)
      setResources([])
    }
  }, [chatId])

  // ── Domain spec view + tailored suggestion chips ────────
  const [domainSuggestions, setDomainSuggestions] = useState<string[] | null>(null)
  useEffect(() => {
    if (viewDomainId) {
      api.getDomainSpec(viewDomainId).then(r => setDomainSpec(r.content)).catch(fail)
      setDomainSuggestions(null)
      api.getDomainSuggestions(viewDomainId).then(setDomainSuggestions).catch(() => {})
    } else {
      setDomainSpec(null)
      setDomainSuggestions(null)
    }
  }, [viewDomainId])

  // ── Theme ───────────────────────────────────────────────
  useEffect(() => {
    document.documentElement.dataset.theme = theme
    localStorage.setItem('paperclaw-theme', theme)
  }, [theme])

  useEffect(() => {
    localStorage.setItem('paperclaw-brainstorm-opts', JSON.stringify(brainstormOpts))
  }, [brainstormOpts])

  const toggleTheme = () => setTheme(t => (t === 'dark' ? 'light' : 'dark'))

  // ── Toast ───────────────────────────────────────────────
  useEffect(() => {
    if (!toast) return
    const t = setTimeout(() => setToast(null), 5000)
    return () => clearTimeout(t)
  }, [toast])

  // ── Column resize (pointer capture) ─────────────────────
  const onDividerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    e.preventDefault()
    e.currentTarget.setPointerCapture(e.pointerId)
    document.body.style.userSelect = 'none'
  }
  const onDividerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!e.currentTarget.hasPointerCapture(e.pointerId)) return
    const w = Math.min(RIGHT_MAX, Math.max(RIGHT_MIN, window.innerWidth - e.clientX))
    setRightWidth(w)
  }
  const onDividerUp = (e: React.PointerEvent<HTMLDivElement>) => {
    if (e.currentTarget.hasPointerCapture(e.pointerId)) {
      e.currentTarget.releasePointerCapture(e.pointerId)
    }
    document.body.style.userSelect = ''
    localStorage.setItem('paperclaw-right-width', String(rightWidthRef.current))
  }

  // ── Domains ─────────────────────────────────────────────
  const refreshDomains = () => api.getDomains().then(setDomains).catch(fail)

  const autoDomain = async (prompt: string) => {
    setAutoCreatingDomain(true)
    setAutoCreateStatus(null)
    setStreamingDomainSpec(null)
    setDomainSearch(null)
    setDomainThinking('')
    fbStart(`Creating domain · ${prompt}`)
    try {
      for await (const ev of api.streamAutoCreateDomain(prompt)) {
        if (ev.type === 'status') {
          setAutoCreateStatus(ev.message as string)
          fbPush('step', `▶ ${ev.message as string}`)
        } else if (ev.type === 'search') {
          setDomainSearch({
            query: ev.query as string,
            broad: (ev.broad as string[]) ?? [],
            sota: (ev.sota as string[]) ?? [],
          })
          fbPush('step', `🔎 searched: ${ev.query as string} (${((ev.broad as string[]) ?? []).length + ((ev.sota as string[]) ?? []).length} papers)`)
        } else if (ev.type === 'thinking') {
          setDomainThinking(prev => prev + (ev.text as string))
          fbPush('thinking', ev.text as string)
        } else if (ev.type === 'codebase') {
          setAutoCreateStatus(`${ev.message as string}`)
          fbPush('step', `📦 ${ev.message as string}`)
        } else if (ev.type === 'delta') {
          setStreamingDomainSpec(prev => (prev ?? '') + (ev.text as string))
          fbPush('text', ev.text as string)
        } else if (ev.type === 'done') {
          const d = ev.result as Domain
          setDomains(prev => [...prev, d])
          setViewDomainId(d.id)
          setToast(`🌐 Domain created: ${d.name}`)
          fbPush('step', `✓ Domain created: ${d.name}`)
        } else if (ev.type === 'error') {
          fbPush('step', `⚠️ ${ev.message as string}`)
          fail(new Error(ev.message as string))
        }
      }
    } catch (e) {
      fail(e)
    } finally {
      fbDone()
      setAutoCreatingDomain(false)
      setAutoCreateStatus(null)
      setStreamingDomainSpec(null)
      setDomainSearch(null)
      setDomainThinking('')
    }
  }

  const startDomainWizard = () => {
    // wizard runs in the scratch conversation
    setActiveSeedId(null)
    setViewDomainId(null)
    setIdeas(prev => prev.map(i => ({ ...i, isActive: false })))
    sendMessageTo('/create_domain', {})
  }

  const viewDomain = (id: string) => {
    // selecting a domain opens its conversation — deselect idea/seed (bug fix)
    setActiveSeedId(null)
    setIdeas(prev => prev.map(i => ({ ...i, isActive: false })))
    setViewDomainId(v => (v === id ? null : id))
  }

  const openContext = (ctx: { contextId: string; kind: string }) => {
    if (ctx.kind === 'domain') {
      const id = ctx.contextId.replace(/^domain-/, '')
      setActiveSeedId(null)
      setIdeas(prev => prev.map(i => ({ ...i, isActive: false })))
      setViewDomainId(id)
    } else if (ctx.kind === 'seed') {
      const id = ctx.contextId.replace(/^seed-/, '')
      setViewDomainId(null)
      setIdeas(prev => prev.map(i => ({ ...i, isActive: false })))
      setActiveSeedId(id)
    } else if (ctx.kind === 'idea') {
      setActiveIdea(ctx.contextId)
    } else {
      // scratch
      setActiveSeedId(null)
      setViewDomainId(null)
      setIdeas(prev => prev.map(i => ({ ...i, isActive: false })))
    }
  }

  const selectDomain = (id: string, selected: boolean) => {
    setDomains(prev => prev.map(d => (d.id === id ? { ...d, isSelected: selected } : d)))
    api.selectDomain(id, selected).catch(fail)
  }

  const removeDomain = (id: string) => {
    setDomains(prev => prev.filter(d => d.id !== id))
    if (viewDomainId === id) setViewDomainId(null)
    api.deleteDomain(id).catch(fail)
  }

  const setDomainCodebase = (id: string, url: string) => {
    const p = url ? api.setDomainCodebase(id, url) : api.clearDomainCodebase(id)
    setToast(url ? '📦 Downloading reference codebase…' : '📦 Clearing reference codebase…')
    p.then(d => {
      setDomains(prev => prev.map(x => (x.id === id ? d : x)))
      setToast(url ? `📦 Codebase ready: ${d.codebaseFiles} files` : '📦 Reference codebase cleared')
    }).catch(fail)
  }

  // ── Brainstorm ──────────────────────────────────────────
  const addSeed = (text: string) => {
    if (!text.trim()) return
    api.addSeed(text.trim()).then(seed => setSeeds(prev => [seed, ...prev])).catch(fail)
  }

  const removeSeed = (id: string) => {
    setSeeds(prev => prev.filter(s => s.id !== id))
    if (activeSeedId === id) setActiveSeedId(null)
    api.deleteSeed(id).catch(fail)
  }

  const generateSeeds = async () => {
    setGenerating(true)
    setGenerateStatus(null)
    fbStart('Brainstorming idea drafts')
    try {
      for await (const ev of api.streamGenerateSeeds(undefined, brainstormOpts)) {
        if (ev.type === 'status') { setGenerateStatus(ev.message as string); fbPush('step', `▶ ${ev.message as string}`) }
        else if (ev.type === 'thinking') fbPush('thinking', ev.text as string)
        else if (ev.type === 'delta') fbPush('text', ev.text as string)
        else if (ev.type === 'done') {
          const created = ev.results as Seed[]
          setSeeds(prev => [...created, ...prev])
          setToast(`✨ ${created.length} idea draft${created.length > 1 ? 's' : ''} brainstormed`)
          fbPush('step', `✓ ${created.length} idea draft${created.length > 1 ? 's' : ''} brainstormed`)
        } else if (ev.type === 'error') {
          fbPush('step', `⚠️ ${ev.message as string}`)
          fail(new Error(ev.message as string))
        }
      }
    } catch (e) {
      fail(e)
    } finally {
      fbDone()
      setGenerating(false)
      setGenerateStatus(null)
    }
  }

  const openSeed = (seed: Seed) => {
    setViewDomainId(null)
    setActiveSeedId(seed.id)
  }

  const promoteSeed = (seed: Seed) => {
    if (seed.draft) {
      // pin via backend so the draft + conversation carry over
      api.sendChat('/pin_idea', { seedId: seed.id })
        .then(([, reply]) => afterReply(reply, seed.id))
        .catch(fail)
    } else {
      api.addIdea(seed.text).then(idea => {
        activateIdeaLocally(idea)
        setSeeds(prev => prev.filter(s => s.id !== seed.id))
        api.deleteSeed(seed.id).catch(fail)
      }).catch(fail)
    }
  }

  // ── Ideas ───────────────────────────────────────────────
  const activateIdeaLocally = (idea: Idea) => {
    setActiveSeedId(null)
    setViewDomainId(null)
    setIdeas(prev => [...prev.filter(i => i.id !== idea.id).map(i => ({ ...i, isActive: false })),
      { ...idea, isActive: true }].sort((a, b) => a.createdAt - b.createdAt))
    api.activateIdea(idea.id).catch(fail)
  }

  const addIdea = (title: string) => {
    if (!title.trim()) return
    api.addIdea(title.trim()).then(activateIdeaLocally).catch(fail)
  }

  const setActiveIdea = (id: string) => {
    setActiveSeedId(null)
    setViewDomainId(null)
    setIdeas(prev => prev.map(i => ({ ...i, isActive: i.id === id })))
    api.activateIdea(id).catch(fail)
  }

  const removeIdea = (id: string) => {
    setIdeas(prev => prev.filter(i => i.id !== id))
    api.deleteIdea(id).catch(fail)
  }

  const setIdeaColor = (id: string, color: string | null) => {
    setIdeas(prev => prev.map(i => (i.id === id ? { ...i, color } : i)))  // optimistic
    api.setIdeaColor(id, color).catch(fail)
  }

  const revealIdea = (id: string) => {
    api.revealIdea(id).then(loc => {
      navigator.clipboard?.writeText(loc.path).catch(() => {})
      setToast(loc.opened ? `Opened: ${loc.path}` : `Folder path copied: ${loc.path}`)
    }).catch(fail)
  }

  const duplicateIdea = (id: string) => {
    api.duplicateIdea(id).then(copy => {
      // add the fork and make it active (mirrors setActiveIdea)
      setActiveSeedId(null)
      setViewDomainId(null)
      setIdeas(prev => [...prev.map(i => ({ ...i, isActive: false })), { ...copy, isActive: true }])
      api.activateIdea(copy.id).catch(fail)
      setToast(`Duplicated → ${copy.title}`)
    }).catch(fail)
  }

  // View a specific paper version (null = latest) in the Paper tab.
  const selectPaperVersion = (v: number | null) => {
    if (!activeIdea) return
    setPaperVersion(v)
    api.getPaper(activeIdea.id, v ?? undefined)
      .then(p => { setPaperText(p.content); setPaperHasPdf(p.hasPdf); setPaperFile(p.paperFile ?? null); setPaperVersions(p.versions ?? []); setPaperToken(t => t + 1) })
      .catch(fail)
  }

  // ── Chat ────────────────────────────────────────────────
  const afterReply = (reply: Message, pinnedSeedId?: string) => {
    if (reply.createdDomainId) {
      refreshDomains()
      // wizard history moved into the domain chat — follow it there
      setActiveSeedId(null)
      setIdeas(prev => prev.map(i => ({ ...i, isActive: false })))
      setViewDomainId(reply.createdDomainId)
      setToast('🌐 Domain created')
    }
    if (reply.createdIdeaId) {
      api.getIdeas().then(fresh => {
        if (pinnedSeedId) {
          // pinned: seed is gone, jump to the new idea (conversation moved too)
          setSeeds(prev => prev.filter(s => s.id !== pinnedSeedId))
          setActiveSeedId(null)
          const idea = fresh.find(i => i.id === reply.createdIdeaId)
          if (idea) activateIdeaLocally(idea)
          setToast('💡 Idea pinned')
        } else {
          setIdeas(prev =>
            fresh.map(f => ({ ...f, isActive: prev.find(p => p.id === f.id)?.isActive ?? false }))
          )
        }
      }).catch(fail)
    }
    if (reply.specUpdated) {
      if (viewDomainId) {
        api.getDomainSpec(viewDomainId).then(r => setDomainSpec(r.content)).catch(fail)
      } else if (activeSeedId) {
        api.getSeeds().then(setSeeds).catch(fail)  // draft changed
      } else if (activeIdea) {
        api.getSpec(activeIdea.id).then(r => setIdeaSpec(r.content)).catch(fail)
      }
    }
    if (reply.mapUpdated) {
      setMapVersion(v => v + 1)   // tell the Hypotheses tab to refetch the map
      setToast('🌳 Hypothesis map updated')
    }
    if (reply.codebaseUpdated) {
      refreshDomains()            // a domain's reference codebase changed
      setToast('📦 Reference codebase updated')
    }
    if (reply.paperUpdated && activeIdea) {
      setPaperVersion(null)  // a new version → jump to latest
      api.getPaper(activeIdea.id)
        .then(p => { setPaperText(p.content); setPaperHasPdf(p.hasPdf); setPaperFile(p.paperFile ?? null); setPaperVersions(p.versions ?? []); setPaperToken(t => t + 1) })
        .catch(fail)
      setToast('📄 Paper updated')
    }
  }

  // Update one context's queue (ref is the source of truth; state mirrors it for
  // render). Stable — only touches refs + setState — so async callbacks can call it.
  const mutateQueue = useCallback((ctxId: string, fn: (l: QueuedMsg[]) => QueuedMsg[]) => {
    const list = fn(queuedRef.current[ctxId] ?? [])
    const next = { ...queuedRef.current }
    if (list.length) next[ctxId] = list; else delete next[ctxId]
    queuedRef.current = next
    setQueued(next)
  }, [])

  // Lets a finishing stream re-invoke the sender for the next queued message
  // without sendMessageTo having to reference itself (TDZ-safe).
  const sendMessageToRef = useRef<(content: string, ctx: ChatCtx) => void>(() => {})

  const sendMessageTo = useCallback(async (content: string, ctx: ChatCtx) => {
    if (!content.trim()) return
    const text = content.trim()
    // The conversation this send belongs to — may differ from what's on screen
    // by the time the stream finishes (the user can switch away mid-stream).
    const ctxId = ctxKeyOf(ctx)
    activeStreams.current[ctxId] = (activeStreams.current[ctxId] ?? 0) + 1
    // Per-stream abort handle so the Stop button can cancel this reply.
    const abort = new AbortController()
    chatAbort.current[ctxId] = abort
    let stopped = false   // user pressed Stop → finalize partial, don't error/drain
    const optimisticUser: Message = {
      id: localId(), role: 'user', content: text, timestamp: Date.now() / 1000, status: 'sent'
    }
    const placeholder: Message = {
      id: localId(), role: 'assistant', content: '', timestamp: Date.now() / 1000,
      status: 'streaming', statusMessage: 'Thinking…'
    }
    // Update only this context's pending slot, leaving other conversations alone.
    const patchPending = (fn: (list: Message[]) => Message[]) =>
      setPending(prev => ({ ...prev, [ctxId]: fn(prev[ctxId] ?? []) }))
    // Remove ONLY this stream's two messages — never the whole context entry, so a
    // second concurrent send in the same conversation isn't wiped out (which made a
    // half-streamed reply + its input suddenly vanish; retrying worked).
    const clearPending = () =>
      setPending(prev => {
        const rest = (prev[ctxId] ?? []).filter(m => m.id !== optimisticUser.id && m.id !== placeholder.id)
        if (rest.length) return { ...prev, [ctxId]: rest }
        const n = { ...prev }; delete n[ctxId]; return n
      })

    // APPEND (don't replace) so a still-streaming earlier message in this conversation
    // keeps rendering alongside this one.
    patchPending(list => [...list, optimisticUser, placeholder])

    // Build a CHRONOLOGICAL timeline (narration + tool calls interleaved in arrival
    // order) plus the accumulated thinking, so the reply renders like Claude.
    let accThinking = ''
    let accContent = ''
    let parts: MessagePart[] = []
    let todos: TodoItem[] = []
    let finalized = false   // set once a terminal (done/error/abort) event resolves the stream
    const appendText = (t: string) => {
      const last = parts[parts.length - 1]
      parts = last && last.kind === 'text'
        ? [...parts.slice(0, -1), { kind: 'text', text: last.text + t }]
        : [...parts, { kind: 'text', text: t }]
    }

    try {
      for await (const ev of api.streamChat(text, ctx, abort.signal)) {
        if (ev.type === 'delta') {
          appendText(ev.text as string)
          accContent += (ev.text as string)
          patchPending(list => list.map(m =>
            m.id === placeholder.id
              ? { ...m, content: m.content + (ev.text as string), parts, statusMessage: null }
              : m
          ))
        } else if (ev.type === 'thinking') {
          accThinking += (ev.text as string)
          patchPending(list => list.map(m =>
            m.id === placeholder.id ? { ...m, thinking: accThinking, statusMessage: 'Thinking…' } : m))
        } else if (ev.type === 'tool') {
          parts = [...parts, {
            kind: 'tool', name: ev.name as string,
            arg: (ev.arg as string) || undefined, detail: (ev.detail as string) || undefined,
          }]
          patchPending(list => list.map(m =>
            m.id === placeholder.id ? { ...m, parts, statusMessage: `${ev.name as string}…` } : m))
        } else if (ev.type === 'todos') {
          todos = ev.todos as TodoItem[]   // each write_todos call replaces the plan
          patchPending(list => list.map(m =>
            m.id === placeholder.id ? { ...m, todos } : m))
        } else if (ev.type === 'done') {
          const [userMsg, replyRaw] = (ev.messages as Message[])
          const reply: Message = {
            ...replyRaw,
            thinking: accThinking || replyRaw.thinking || null,
            parts: parts.length ? parts : replyRaw.parts,
            todos: todos.length ? todos : replyRaw.todos,
          }
          clearPending()
          // Fold the finished exchange into the visible list only if the user is
          // still in this conversation; otherwise a later reload will pick up the
          // server-persisted messages when they return to it.
          if (chatIdRef.current === ctxId) {
            setMessages(prev => [
              ...prev.filter(m => m.id !== optimisticUser.id && m.id !== placeholder.id),
              userMsg, reply,
            ])
          }
          afterReply(reply, ctx.seedId && reply.createdIdeaId ? ctx.seedId : undefined)
          finalized = true
        } else if (ev.type === 'error') {
          failStream(`⚠️ ${ev.message as string}`)
          finalized = true
        }
      }
    } catch (e) {
      // An AbortError is the user pressing Stop — finalize the partial reply
      // instead of showing it as an error.
      if (stopped || abort.signal.aborted || (e as Error)?.name === 'AbortError') stopStream()
      else failStream(`⚠️ ${String((e as Error).message ?? e)}`)
      finalized = true
    } finally {
      // The stream ENDED without a terminal done/error event — the connection dropped or
      // the server restarted (e.g. a dev --reload). Don't leave the placeholder spinning
      // forever: finalize it, keeping whatever streamed, so the user can re-send.
      if (!finalized)
        failStream('⚠️ The connection closed before the reply finished — the server may have '
          + 'restarted. Your message is saved; re-send to retry.')
      activeStreams.current[ctxId] = Math.max(0, (activeStreams.current[ctxId] ?? 1) - 1)
      if (chatAbort.current[ctxId] === abort) delete chatAbort.current[ctxId]
      // This conversation's reply finished — auto-send the next queued message
      // (sequential, Claude Code style). Only once no other stream is live here.
      // Stop ends only the CURRENT reply; a QUEUED message still gets sent (the user
      // typed it to run next), so drain the next one whether or not we were stopped.
      if (activeStreams.current[ctxId] === 0) {
        const q = queuedRef.current[ctxId]
        if (q && q.length) {
          const [next, ...rest] = q
          mutateQueue(ctxId, () => rest)
          sendMessageToRef.current(next.text, next.ctx)
        }
      }
    }

    // Resolve a stopped stream: keep whatever streamed so far as a normal (non-error)
    // assistant message, so the user sees the partial reply they interrupted.
    function stopStream() {
      stopped = true
      clearPending()
      if (chatIdRef.current === ctxId) {
        const partial: Message = {
          ...placeholder,
          content: accContent,
          parts: parts.length ? parts : undefined,
          thinking: accThinking || null,
          todos: todos.length ? todos : undefined,
          status: 'sent',
          statusMessage: null,
        }
        setMessages(prev => [
          ...prev.filter(m => m.id !== optimisticUser.id && m.id !== placeholder.id),
          optimisticUser, partial,
        ])
      }
    }

    // Resolve a failed stream: clear pending and, if still viewing this conversation,
    // show the optimistic user message + the error inline — KEEPING whatever already
    // streamed (thinking / tool chain / partial text / todos) so a mid-stream error
    // doesn't wipe the work and force a restart; the error is appended below it.
    function failStream(errText: string) {
      clearPending()
      if (chatIdRef.current === ctxId) {
        const errored: Message = {
          ...placeholder,
          content: accContent ? `${accContent}\n\n${errText}` : errText,
          parts: parts.length ? parts : undefined,
          thinking: accThinking || null,
          todos: todos.length ? todos : undefined,
          status: 'error' as const,
          statusMessage: null,
        }
        setMessages(prev => [
          ...prev.filter(m => m.id !== optimisticUser.id && m.id !== placeholder.id),
          optimisticUser, errored,
        ])
      }
    }
  }, [activeSeedId, activeIdea?.id, viewDomainId, mutateQueue])
  sendMessageToRef.current = sendMessageTo

  const sendMessage = useCallback((content: string) => {
    const text = content.trim()
    if (!text) return
    const ctx: ChatCtx = viewDomainId
      ? { domainId: viewDomainId }
      : activeSeedId
        ? { seedId: activeSeedId }
        : { ideaId: activeIdea?.id }
    const ctxId = ctxKeyOf(ctx)
    // A reply is still streaming in this conversation → queue the message and
    // auto-send it when that reply finishes, instead of starting a 2nd stream.
    if ((activeStreams.current[ctxId] ?? 0) > 0) {
      mutateQueue(ctxId, l => [...l, { id: localId(), text, ctx }])
      return
    }
    sendMessageTo(text, ctx)
  }, [sendMessageTo, activeSeedId, activeIdea?.id, viewDomainId, mutateQueue])

  // Stop the live reply in the current conversation (the Stop button). Aborts the
  // current stream; any QUEUED follow-up is kept and sent next (the drain in the
  // stream's finally handles it) — Stop ends the current reply, not the queue. Remove
  // a queued item explicitly with its ✕ if you don't want it sent.
  const stopChat = useCallback(() => {
    chatAbort.current[chatIdRef.current]?.abort()
  }, [])

  // Load any previously-generated paper when an idea opens (clears otherwise).
  useEffect(() => {
    if (activeIdea) {
      api.getPaper(activeIdea.id)
        .then(p => { setPaperText(p.content); setPaperHasPdf(p.hasPdf); setPaperFile(p.paperFile ?? null); setPaperVersions(p.versions ?? []); setPaperToken(t => t + 1) })
        .catch(() => { setPaperText(null); setPaperHasPdf(false); setPaperFile(null); setPaperVersions([]); setPaperVersion(null) })
    } else {
      setPaperText(null)
      setPaperHasPdf(false)
    }
  }, [activeIdea?.id])

  // Upsert an activity status line (running → done/error) into the active idea's
  // conversation, so right-panel generate actions show in the chat history.
  const onActivity = useCallback((key: string, text: string, status: 'running' | 'done' | 'error') => {
    const ctx = activeIdeaIdRef.current
    if (!ctx) return
    setActivities(prev => {
      const list = prev[ctx] ?? []
      const i = list.findIndex(a => a.key === key)
      const entry = i >= 0 ? { ...list[i], text, status } : { key, text, status, timestamp: Date.now() / 1000 }
      const next = i >= 0 ? list.map((a, j) => (j === i ? entry : a)) : [...list, entry]
      return { ...prev, [ctx]: next }
    })
  }, [])

  // Conversation context ids with a live chat stream (incl. ones running in the
  // background after the user switched away) — drives the sidebar loading icon.
  const streamingContexts = useMemo<Set<string>>(
    () => new Set(Object.entries(pending).filter(([, v]) => v.length > 0).map(([k]) => k)),
    [pending],
  )

  const displayMessages = useMemo<Message[]>(() => {
    // Persisted messages + any in-flight stream for THIS conversation.
    const pend = pending[chatId]
    let msgs = pend && pend.length ? [...messages, ...pend] : messages
    // Right-panel activity status lines for this conversation (sorted in by time).
    const acts = activities[chatId]
    if (acts && acts.length) {
      const actMsgs: Message[] = acts.map(a => ({
        id: `__act_${a.key}`,
        role: 'assistant',
        content: a.text,
        timestamp: a.timestamp,
        status: a.status === 'running' ? 'streaming' : a.status === 'error' ? 'error' : 'sent',
      }))
      msgs = [...msgs, ...actMsgs].sort((x, y) => x.timestamp - y.timestamp)
    }
    return msgs
  }, [messages, pending, chatId, activities])

  // ── Right panel content ─────────────────────────────────
  // paperText is passed separately to ResourcesPanel (its own "Paper" tab).
  // The spec slot only carries streaming content and the normal spec.
  const cleanStreamingSpec = streamingDomainSpec
    ? streamingDomainSpec.replace(/^```\s*new-domain\s*\n?/, '').replace(/\n?```\s*$/, '')
    : null

  const rightSpec =
    autoCreatingDomain ? (cleanStreamingSpec ?? '')
    : viewDomainId ? domainSpec
    : activeSeed ? (activeSeed.draft ?? null)
    : ideaSpec

  const rightSpecLabel =
    (viewDomainId || autoCreatingDomain) ? 'Domain'
    : activeSeed ? 'Draft' : 'Spec'

  const rightHasSpec =
    autoCreatingDomain ||
    (viewDomainId ? !!domainSpec : activeSeed ? !!activeSeed.draft : !!activeIdea)

  const specStreaming = autoCreatingDomain

  const domainProcess = autoCreatingDomain
    ? { query: domainSearch?.query ?? '', broad: domainSearch?.broad ?? [], sota: domainSearch?.sota ?? [], thinking: domainThinking }
    : null

  // The active idea already has results (a prior auto run that isn't running, or a
  // compiled paper) → the ⚡ button RESUMES (the launch is restart=False by default)
  // instead of implying a fresh start.
  const ideaHasResults = !!activeIdea && (
    (!!autoRun && autoRun.ideaId === activeIdea.id && autoRun.status !== 'running')
    || paperHasPdf || !!paperText
  )

  return (
    <div className={styles.layout}>
      <LeftSidebar
        domains={domains}
        seeds={seeds}
        ideas={ideas}
        streamingContexts={streamingContexts}
        runningAutoIdeas={runningAutoIdeas}
        generating={generating}
        generateStatus={generateStatus}
        autoCreatingDomain={autoCreatingDomain}
        autoCreateStatus={autoCreateStatus}
        activeSeedId={activeSeedId}
        viewDomainId={viewDomainId}
        onAutoDomain={autoDomain}
        onStartDomainWizard={startDomainWizard}
        onSelectDomain={selectDomain}
        onViewDomain={viewDomain}
        onRemoveDomain={removeDomain}
        onSetDomainCodebase={setDomainCodebase}
        onAddSeed={addSeed}
        onRemoveSeed={removeSeed}
        onGenerateSeeds={generateSeeds}
        brainstormOpts={brainstormOpts}
        onBrainstormOptsChange={setBrainstormOpts}
        onOpenSeed={openSeed}
        onPromoteSeed={promoteSeed}
        onAddIdea={addIdea}
        onSetActiveIdea={setActiveIdea}
        onRemoveIdea={removeIdea}
        onRevealIdea={revealIdea}
        onDuplicateIdea={duplicateIdea}
        onSetIdeaColor={setIdeaColor}
        onOpenSettings={() => setSettingsOpen(true)}
        onOpenAutoLauncher={() => setAutoLauncherOpen(true)}
        theme={theme}
        onToggleTheme={toggleTheme}
      />
      <MainPanel
        messages={displayMessages}
        activeIdea={activeIdea}
        activeSeed={activeSeed}
        activeDomain={activeDomain}
        suggestions={viewDomainId ? domainSuggestions ?? [] : undefined}
        error={error}
        onDismissError={() => setError(null)}
        onSend={sendMessage}
        onStopChat={stopChat}
        onPickContext={openContext}
        queued={queued[chatId] ?? []}
        onCancelQueued={(id) => mutateQueue(chatId, l => l.filter(m => m.id !== id))}
        isChatStreaming={(pending[chatId]?.length ?? 0) > 0}
        onAutoRunIdea={(id) => setAutoSettingsFor(ideas.find(i => i.id === id) ?? null)}
        autoResumable={ideaHasResults}
        experiments={experiments}
        onJumpExperiment={jumpToExperiment}
        autoRun={autoRun}
        onStopAutoRun={stopAutoRun}
      />
      <div
        className={styles.divider}
        onPointerDown={onDividerDown}
        onPointerMove={onDividerMove}
        onPointerUp={onDividerUp}
        title="Drag to resize"
      >
        <span className={styles.dividerGrip} />
      </div>
      <ResourcesPanel
        resources={resources}
        ideaId={activeIdea?.id}
        domainId={viewDomainId ?? undefined}
        domains={domains}
        onActivity={onActivity}
        onAgentCommand={sendMessage}
        mapVersion={mapVersion}
        focusHyp={focusHyp && focusHyp.ideaId === activeIdea?.id ? focusHyp : null}
        spec={rightSpec}
        specLabel={rightSpecLabel}
        hasSpec={rightHasSpec}
        specStreaming={specStreaming}
        domainProcess={domainProcess}
        paperText={paperText}
        paperFile={paperFile}
        paperDownloadUrl={activeIdea && (paperText || paperHasPdf) ? `${API_BASE}/api/ideas/${activeIdea.id}/paper?download=1&t=${paperToken}${paperVersion ? `&version=${paperVersion}` : ''}` : undefined}
        paperPdfUrl={activeIdea && paperHasPdf ? `${API_BASE}/api/ideas/${activeIdea.id}/paper?t=${paperToken}${paperVersion ? `&version=${paperVersion}` : ''}` : undefined}
        paperVersions={paperVersions}
        paperVersion={paperVersion}
        onSelectPaperVersion={selectPaperVersion}
        width={rightWidth}
        onSaveSpec={() => {}}
      />
      {settingsOpen && <SettingsModal onClose={() => setSettingsOpen(false)} />}
      <AutoRunLauncher
        open={autoLauncherOpen}
        onClose={() => setAutoLauncherOpen(false)}
        onStart={startAutoRun}
        running={autoRun?.status === 'running'}
      />
      <AutoRunSettingsModal
        open={!!autoSettingsFor}
        ideaId={autoSettingsFor?.id}
        ideaTitle={autoSettingsFor?.title}
        resume={ideaHasResults}
        onClose={() => setAutoSettingsFor(null)}
        onStart={settings => { if (autoSettingsFor) launchAutoRunForIdea(autoSettingsFor.id, settings) }}
      />
      {feedback && !feedbackOpen && (
        <button className={styles.feedbackFab} onClick={() => setFeedbackOpen(true)}
                title="Show the live agent feedback">
          {feedback.running ? '⚡' : '✓'} Agent feedback
        </button>
      )}
      <FeedbackPanel
        open={feedbackOpen}
        title={feedback?.title}
        entries={feedback?.entries ?? []}
        running={feedback?.running ?? false}
        onClose={() => setFeedbackOpen(false)}
      />
      {toast && <div className={styles.toast}>{toast}</div>}
    </div>
  )
}
