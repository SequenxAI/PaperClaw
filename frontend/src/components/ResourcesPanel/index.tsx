import { useEffect, useMemo, useRef, useState } from 'react'
import 'katex/dist/katex.min.css'
import ResourceItem from './ResourceItem'
import DomainConnector from './DomainConnector'
import ResourcesEditor from './ResourcesEditor'
import { api } from '../../api'
import { renderMarkdown, renderMarkdownWithFigures, renderMarkdownWithMath, renderMarkdownWithMathAndFigures } from '../../lib/markdown'
import type {
  Domain, HardwareInfo, HardwareView, HypothesisDetail, HypothesisMap, HypothesisNode, Reference,
  ReferencesView, ReferenceValidation, Resource, ResourceTab, RightTab, WorkspaceEntry, WritingStyle, Benchmark,
} from '../../types'
import s from './styles.module.css'

interface DomainProcess {
  query: string
  broad: string[]
  sota: string[]
  thinking: string
}

type ActivityFn = (key: string, text: string, status: 'running' | 'done' | 'error') => void

interface Props {
  resources: Resource[]
  ideaId?: string
  domainId?: string
  domains?: Domain[]   // all domains, for the idea→domain connector in the Spec tab
  onActivity?: ActivityFn
  onAgentCommand?: (text: string) => void  // run a skill in the chat (e.g. /generate_hypothesis_map)
  mapVersion?: number                      // bump → refetch the hypothesis map
  focusHyp?: { ideaId: string; hid: string; nonce: number } | null  // jump to a hypothesis's experiment
  spec: string | null
  specLabel: string   // 'Spec' | 'Draft' | 'Domain' | 'Research'
  hasSpec: boolean
  specStreaming?: boolean
  domainProcess?: DomainProcess | null
  paperText?: string | null
  paperFile?: string | null
  paperVersions?: number[]
  paperVersion?: number | null
  onSelectPaperVersion?: (v: number | null) => void
  paperDownloadUrl?: string
  paperPdfUrl?: string
  width?: number
  onSaveSpec: (content: string) => void
}

const SHOW_SPEC_EDITOR = false

const RESOURCE_TABS: { id: ResourceTab; label: string }[] = [
  { id: 'all', label: 'All' },
  { id: 'papers', label: 'Papers' },
  { id: 'links', label: 'Links' },
  { id: 'datasets', label: 'Datasets' },
  { id: 'code', label: 'Code' }
]

function hwSummary(m: HardwareInfo): string {
  if (!m.reachable) return `⚠ unreachable${m.error ? ` — ${m.error}` : ''}`
  const bits: string[] = []
  if (m.cpuThreads) bits.push(`${m.cpuThreads} threads`)
  if (m.memTotalGb) bits.push(`${m.memTotalGb} GB RAM`)
  if (m.gpus.length) {
    const vram = m.gpus[0].memoryTotalMb ? ` ${Math.round(m.gpus[0].memoryTotalMb / 1024)}GB` : ''
    bits.push(`${m.gpus.length}× ${m.gpus[0].name}${vram}`)
  } else bits.push('no GPU')
  return bits.join(' · ')
}

function HardwareCard({ hw }: { hw: HardwareView | null }) {
  if (!hw || !hw.machines.length) {
    return (
      <div className={s.hwCard}>
        <div className={s.hwCardHead}>🖥 Hardware</div>
        <p className={s.hwCardEmpty}>
          No environment detected yet. Open ⚙️ Settings → Hardware and click “Detect now”.
        </p>
      </div>
    )
  }
  return (
    <div className={s.hwCard}>
      <div className={s.hwCardHead}>🖥 Hardware &amp; Environment</div>
      {hw.machines.map(m => (
        <div key={m.label} className={s.hwCardRow}>
          <span className={s.hwCardName}>{m.scope === 'local' ? '💻' : '🌐'} {m.label}</span>
          <span className={`${s.hwCardMeta} ${!m.reachable ? s.hwCardErr : ''}`}>{hwSummary(m)}</span>
        </div>
      ))}
    </div>
  )
}

const STATUS_BADGE: Record<string, { icon: string; cls: string }> = {
  VERIFIED:  { icon: '✓ verified',   cls: 'refOk' },
  MISMATCH:  { icon: '⚠ mismatch',   cls: 'refWarn' },
  NOT_FOUND: { icon: '✗ not found',  cls: 'refErr' },
  UNKNOWN:   { icon: '? unchecked',  cls: 'refMuted' },
}

function ReferencesCard({ ideaId, onActivity, onAgentCommand }: {
  ideaId: string; onActivity?: ActivityFn; onAgentCommand?: (t: string) => void
}) {
  const [view, setView] = useState<ReferencesView | null>(null)
  const [checks, setChecks] = useState<Record<string, ReferenceValidation>>({})
  const [busy, setBusy] = useState<'' | 'load' | 'validate' | 'add' | 'generate'>('load')
  const [query, setQuery] = useState('')

  useEffect(() => {
    setBusy('load'); setChecks({})
    api.getReferences(ideaId).then(setView).catch(() => setView(null)).finally(() => setBusy(''))
  }, [ideaId])

  const generate = async () => {
    setBusy('generate')
    onActivity?.('refs-gen', '📚 Gathering references from OpenAlex…', 'running')
    try {
      const v = await api.generateReferences(ideaId)
      setView(v)
      onActivity?.('refs-gen', `📚 References ready — ${v.entries.length} entr${v.entries.length === 1 ? 'y' : 'ies'}`, 'done')
    } catch { onActivity?.('refs-gen', '⚠️ Reference gathering failed', 'error') }
    finally { setBusy('') }
  }

  const validate = async () => {
    setBusy('validate')
    onActivity?.('refs-val', '🔎 Validating references…', 'running')
    try {
      const results = await api.validateReferences(ideaId)
      setChecks(Object.fromEntries(results.map(r => [r.key, r])))
      const bad = results.filter(r => r.status !== 'VERIFIED').length
      onActivity?.('refs-val', `🔎 Validated ${results.length} reference${results.length === 1 ? '' : 's'}${bad ? ` — ${bad} need attention` : ' — all verified'}`, 'done')
    } catch { onActivity?.('refs-val', '⚠️ Reference validation failed', 'error') }
    finally { setBusy('') }
  }

  const add = async () => {
    const q = query.trim()
    if (!q) return
    setBusy('add')
    try {
      const isDoi = /^10\.\d{4,}\//.test(q)
      setView(await api.addReference(ideaId, isDoi ? { doi: q } : { query: q }))
      setQuery('')
    } catch { /* ignore */ } finally { setBusy('') }
  }

  const entries: Reference[] = view?.entries ?? []
  return (
    <div className={s.hwCard}>
      <div className={s.refHead}>
        <span className={s.hwCardHead}>📚 References (ref.bib)</span>
        <span className={s.refActions}>
          <button className={s.refBtn} disabled={busy === 'generate'} onClick={generate}>
            {busy === 'generate' ? 'Generating…' : 'Generate'}
          </button>
          {entries.length > 0 && (
            <button className={s.refBtn} disabled={busy === 'validate'}
              onClick={() => onAgentCommand ? onAgentCommand('/validate_references') : validate()}
              title="Validate every entry against Crossref/OpenAlex — streams each result in the chat">
              {busy === 'validate' ? 'Validating…' : 'Validate'}
            </button>
          )}
        </span>
      </div>

      {entries.length === 0 ? (
        <p className={s.hwCardEmpty}>
          No references yet. Click <strong>Generate</strong> to gather real papers from OpenAlex,
          add one by DOI/title below, or let the assistant cite as it works.
        </p>
      ) : (
        <div className={s.refList}>
          {entries.map(e => {
            const v = checks[e.key]
            const badge = v ? STATUS_BADGE[v.status] : null
            return (
              <div key={e.key} className={s.refItem} title={v?.detail || ''}>
                <div className={s.refTitle}>{e.title || e.key}</div>
                <div className={s.refMeta}>
                  {(e.authors[0] || '?')}{e.authors.length > 1 ? ' et al.' : ''}{e.year ? ` · ${e.year}` : ''}{e.venue ? ` · ${e.venue}` : ''}
                  {badge && <span className={`${s.refBadge} ${s[badge.cls]}`}>{badge.icon}</span>}
                </div>
              </div>
            )
          })}
        </div>
      )}

      <div className={s.refAddRow}>
        <input
          className={s.refInput}
          value={query}
          placeholder="Add by DOI (10.…) or title"
          onChange={e => setQuery(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') add() }}
        />
        <button className={s.refBtn} disabled={busy === 'add' || !query.trim()} onClick={add}>
          {busy === 'add' ? 'Adding…' : 'Add'}
        </button>
      </div>
    </div>
  )
}

const HYP_STATUS_CLS: Record<string, string> = {
  supported: 'gOk', refuted: 'gErr', inconclusive: 'gWarn',
  blocked: 'gBlk', untested: 'gUnt',
  planned: 'gPlan', experiment: 'gRun',  // derived progress stages
}

// Left-to-right tree: depth → x (columns), leaf order → y (rows). Roots stack
// vertically (using the panel's vertical space); children grow rightward as the
// tree is expanded after verdicts.
const NODE_W = 210, NODE_H = 70, COL = 210 + 56, ROW_V = 70 + 18

/** Graph (tree) view of the hypothesis map — SVG edges + positioned node cards.
 *  Left-click a node to pin it; right-click for a context menu (View / Delete). */
function HypothesisGraph({ roots, onPin, onDelete, onAddChild, onRerun }: {
  roots: HypothesisNode[]; onPin: (id: string) => void; onDelete: (id: string) => void
  onAddChild: (id: string) => void; onRerun: (id: string) => void
}) {
  const [menu, setMenu] = useState<{ id: string; x: number; y: number; status: string } | null>(null)
  useEffect(() => {
    if (!menu) return
    const close = () => setMenu(null)
    window.addEventListener('click', close)
    window.addEventListener('contextmenu', close)
    return () => { window.removeEventListener('click', close); window.removeEventListener('contextmenu', close) }
  }, [menu])

  const pos = new Map<string, { x: number; y: number; node: HypothesisNode }>()
  let leaf = 0
  let maxDepth = 0
  const place = (n: HypothesisNode, depth: number): number => {
    maxDepth = Math.max(maxDepth, depth)
    const kids = n.children ?? []
    let y: number
    if (!kids.length) { y = leaf * ROW_V; leaf++ }
    else { const ys = kids.map(k => place(k, depth + 1)); y = (ys[0] + ys[ys.length - 1]) / 2 }
    pos.set(n.id, { x: depth * COL, y, node: n })
    return y
  }
  roots.forEach(r => place(r, 0))

  const edges: { x1: number; y1: number; x2: number; y2: number }[] = []
  const walk = (n: HypothesisNode) => {
    const p = pos.get(n.id)!
    ;(n.children ?? []).forEach(c => {
      const cp = pos.get(c.id)!
      edges.push({ x1: p.x + NODE_W, y1: p.y + NODE_H / 2, x2: cp.x, y2: cp.y + NODE_H / 2 })
      walk(c)
    })
  }
  roots.forEach(walk)

  const width = (maxDepth + 1) * COL
  const height = Math.max(leaf, 1) * ROW_V

  return (
    <div className={s.graphScroll}>
      <svg width={width} height={height} className={s.graph}>
        {edges.map((e, i) => (
          <path key={i} className={s.graphEdge} fill="none"
            d={`M${e.x1},${e.y1} C${(e.x1 + e.x2) / 2},${e.y1} ${(e.x1 + e.x2) / 2},${e.y2} ${e.x2},${e.y2}`} />
        ))}
        {[...pos.values()].map(p => (
          <foreignObject key={p.node.id} x={p.x} y={p.y} width={NODE_W} height={NODE_H}>
            <div
              className={`${s.gNode} ${s[HYP_STATUS_CLS[p.node.stage || p.node.status] ?? 'hypMuted']}`}
              title="left-click to pin · right-click for options"
              onClick={() => onPin(p.node.id)}
              onContextMenu={e => {
                e.preventDefault(); e.stopPropagation()
                setMenu({ id: p.node.id, x: e.clientX, y: e.clientY, status: p.node.status })
              }}
            >
              <span className={s.gNodeId}>{p.node.id}<span className={s.gNodeStatus}> · {p.node.stage || p.node.status}</span></span>
              <span className={s.gNodeText}>{p.node.statement}</span>
            </div>
          </foreignObject>
        ))}
      </svg>
      {menu && (
        <div className={s.ctxMenu} style={{ left: menu.x, top: menu.y }} onClick={e => e.stopPropagation()}>
          <button className={s.ctxItem} onClick={() => { onPin(menu.id); setMenu(null) }}>
            👁 View hypothesis
          </button>
          <button className={s.ctxItem} onClick={() => { onAddChild(menu.id); setMenu(null) }}>
            ➕ Add sub-hypothesis
          </button>
          {menu.status !== 'untested' && menu.status !== 'blocked' && (
            <button className={s.ctxItem} onClick={() => { onRerun(menu.id); setMenu(null) }}>
              🔁 Rerun experiment{menu.status === 'inconclusive' ? ' (inconclusive)' : ''}
            </button>
          )}
          <button className={`${s.ctxItem} ${s.ctxItemDanger}`} onClick={() => { onDelete(menu.id); setMenu(null) }}>
            🗑 Delete hypothesis
          </button>
        </div>
      )}
    </div>
  )
}

// Validation steps: plan → experiment → report; a step is locked until the
// previous one is complete (#4).
const HYP_STEPS = [
  { id: 'plan', label: '📋 Plan' },
  { id: 'experiment', label: '🧪 Experiment' },
  { id: 'report', label: '📊 Report' },
  { id: 'files', label: '📁 Files' },  // always available — browse the workspace dir
] as const
type HypStep = typeof HYP_STEPS[number]['id']

const IMG_EXT = /\.(png|jpe?g|gif|svg|webp|bmp)$/i
const fmtSize = (n: number) =>
  n < 1024 ? `${n} B` : n < 1024 * 1024 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1024 / 1024).toFixed(1)} MB`
const fileIcon = (p: string) =>
  IMG_EXT.test(p) ? '🖼️' : /\.(py|ipynb|js|ts|sh|tex)$/i.test(p) ? '📜'
    : /\.json$/i.test(p) ? '🔢' : /\.(md|txt|log|csv|tsf|bib)$/i.test(p) ? '📄' : '📦'

// Browse an idea's workspace directory (the agent's code / figures / results /
// logs) — list on the left, preview (image inline, text in a <pre>) on the right.
function WorkspaceBrowser({ ideaId, basePath }: { ideaId: string; basePath: string }) {
  const [entries, setEntries] = useState<WorkspaceEntry[] | null>(null)
  const [sel, setSel] = useState('')
  const [text, setText] = useState<string | null>(null)
  const [err, setErr] = useState('')

  const load = () => {
    setEntries(null); setErr('')
    api.getWorkspaceFiles(ideaId, basePath)
      .then(v => setEntries(v.entries.filter(e => !e.isDir)))
      .catch(() => setErr('Could not load files.'))
  }
  useEffect(() => { load() }, [ideaId, basePath])  // eslint-disable-line react-hooks/exhaustive-deps

  const open = async (path: string) => {
    setSel(path); setText(null)
    if (IMG_EXT.test(path)) return  // shown via <img>
    try {
      const r = await fetch(api.rawFileUrl(ideaId, path))
      setText(r.ok ? await r.text() : '(could not read file)')
    } catch { setText('(could not read file)') }
  }

  if (err) return <p className={s.hypMeta}>{err}</p>
  if (!entries) return <p className={s.hypMeta}>Loading files…</p>
  if (!entries.length)
    return <p className={s.hypMeta}>No files yet — run the experiment to populate <code>{basePath}/</code>.</p>

  return (
    <div className={s.fileBrowser}>
      <div className={s.fileList}>
        <div className={s.fileListHead}>
          <span title={basePath}>{basePath}/</span>
          <button className={s.refBtnTiny} onClick={load} title="Refresh">↻</button>
        </div>
        {entries.map(e => (
          <button
            key={e.path}
            className={`${s.fileItem} ${sel === e.path ? s.fileItemActive : ''}`}
            onClick={() => open(e.path)}
            title={e.path}
          >
            <span className={s.fileName}>{fileIcon(e.path)} {e.path.slice(basePath.length + 1)}</span>
            <span className={s.fileSize}>{fmtSize(e.size)}</span>
          </button>
        ))}
      </div>
      <div className={s.filePreview}>
        {!sel ? (
          <p className={s.hypMeta}>Select a file to preview.</p>
        ) : IMG_EXT.test(sel) ? (
          <img className={s.filePreviewImg} src={api.rawFileUrl(ideaId, sel)} alt={sel} />
        ) : text == null ? (
          <p className={s.hypMeta}>Loading…</p>
        ) : (
          <pre className={s.researchStream}>{text}</pre>
        )}
        {sel && (
          <a className={s.fileDownload} href={api.rawFileUrl(ideaId, sel)} target="_blank" rel="noreferrer">
            ⬇ open / download {sel.split('/').pop()}
          </a>
        )}
      </div>
    </div>
  )
}

function HypothesisDetailView({ ideaId, hid, onActivity, onAgentCommand, mapVersion }: {
  ideaId: string; hid: string; onActivity?: ActivityFn
  onAgentCommand?: (t: string) => void; mapVersion?: number
}) {
  const [detail, setDetail] = useState<HypothesisDetail | null>(null)
  const [step, setStep] = useState<HypStep>('plan')
  const [busy, setBusy] = useState<'load' | 'plan' | 'experiment' | ''>('load')
  const [streaming, setStreaming] = useState(false)  // attached to a live experiment job
  const [live, setLive] = useState('')        // streamed code/text/status feedback
  const [thinking, setThinking] = useState('')  // streamed model reasoning
  const streamAbort = useRef<AbortController | null>(null)  // abort the SSE on unmount

  useEffect(() => {
    setBusy('load'); setStep('plan'); setLive(''); setThinking('')
    api.getHypothesisDetail(ideaId, hid).then(setDetail).catch(() => setDetail(null)).finally(() => setBusy(''))
    // re-attach to a still-running experiment so the live log shows on (re)open / reload
    api.getExperimentJob(ideaId, hid).then(j => {
      if (j.status === 'running') { setStep('experiment'); attachLog() }
    }).catch(() => {})
    // close the SSE stream when the view changes/unmounts — otherwise connections pile
    // up (browsers cap ~6 per host) and everything else hangs.
    return () => { streamAbort.current?.abort(); streamAbort.current = null }
  }, [ideaId, hid])

  // Refetch when the chat agent wrote a plan/report (App bumps mapVersion) so the
  // Plan/Report steps pick up the new artifact without a manual reload. The same
  // signal clears `sent` (the op finished → re-enable the button).
  const [sent, setSent] = useState<'plan' | 'report' | ''>('')
  useEffect(() => {
    if (!mapVersion) return
    setSent('')
    api.getHypothesisDetail(ideaId, hid).then(setDetail).catch(() => {})
  }, [mapVersion, ideaId, hid])
  // fallback: re-enable the button even if no refresh arrives (e.g. the run failed)
  useEffect(() => {
    if (!sent) return
    const t = setTimeout(() => setSent(''), 90000)
    return () => clearTimeout(t)
  }, [sent])

  // Plan/report generation runs as a skill in the conversation (the deepagents agent
  // writes hypotheses/<id>/plan.md|report.md — its work streams in the chat, so we
  // do NOT also add a duplicate activity line). The button disables until the
  // refresh arrives, so a second click can't fire a duplicate run.
  const generatePlan = () => { if (sent) return; onAgentCommand?.(`/generate_plan ${hid}`); setSent('plan') }
  const generateReport = () => { if (sent) return; onAgentCommand?.(`/generate_report ${hid}`); setSent('report') }

  // Attach to the detached job's live log (replays the whole log, then streams). Used
  // both right after starting AND on (re)open of a still-running job — survives reloads.
  // `streaming` (not `busy`) gates the live panel, so a slower detail-load finishing
  // can't clobber it and make the tab look fresh.
  const attachLog = async () => {
    streamAbort.current?.abort()                 // replace any prior stream
    const ctrl = new AbortController()
    streamAbort.current = ctrl
    setStreaming(true); setLive(''); setThinking(''); setStep('experiment')
    try {
      for await (const ev of api.streamExperimentLog(ideaId, hid, 0, ctrl.signal)) {
        if (ev.type === 'thinking') setThinking(t => t + (ev.text as string))
        else if (ev.type === 'delta') setLive(t => t + (ev.text as string))
        else if (ev.type === 'phase') setLive(t => t + `\n\n[${ev.label as string}]\n`)
        else if (ev.type === 'done') break
      }
      if (!ctrl.signal.aborted) setDetail(await api.getHypothesisDetail(ideaId, hid))
    } catch { /* stream dropped/aborted — the job keeps running; re-open to re-attach */ }
    finally { if (streamAbort.current === ctrl) { streamAbort.current = null; setStreaming(false) } }
  }

  const runExperiment = async () => {
    setStep('experiment'); setLive(''); setThinking(''); setStreaming(true)
    try {
      await api.startExperiment(ideaId, hid)  // detached process; runs even if we navigate away
      await attachLog()
    } catch {
      setStreaming(false)
      onActivity?.(`exp-${hid}`, `⚠️ Couldn't start the experiment for ${hid}`, 'error')
    }
  }

  const cancelExperiment = async () => {
    try { await api.cancelExperiment(ideaId, hid) } catch { /* ignore */ }
  }

  if (busy === 'load' || !detail) {
    return <div className={s.hypMeta} style={{ padding: '12px' }}>Loading {hid}…</div>
  }

  const done: Record<HypStep, boolean> = {
    plan: !!detail.plan, experiment: !!detail.experiment, report: !!detail.report, files: false,
  }
  const enabled: Record<HypStep, boolean> = {
    plan: true, experiment: done.plan, report: done.experiment, files: true,
  }
  const active = enabled[step] ? step : 'plan'
  const body = active === 'plan' ? detail.plan : active === 'experiment' ? detail.experiment : detail.report
  // figures live in the hypothesis dir; resolve relative <img> srcs through /raw.
  const figUrl = (name: string) => api.rawFileUrl(ideaId, `hypotheses/${hid}/${name}`)

  return (
    <div className={s.hypDetail}>
      <div className={s.hypStatusLine}>
        <span className={`${s.hypBadge} ${s[HYP_STATUS_CLS[detail.status] ?? 'gUnt']}`}>{detail.status}</span>
      </div>

      <div className={s.stepTabs}>
        {HYP_STEPS.map(st => (
          <button
            key={st.id}
            disabled={!enabled[st.id]}
            className={`${s.stepTab} ${active === st.id ? s.stepTabActive : ''} ${!enabled[st.id] ? s.stepTabLocked : ''}`}
            onClick={() => enabled[st.id] && setStep(st.id)}
            title={!enabled[st.id] ? 'Complete the previous step first' : ''}
          >
            {done[st.id] ? '✓ ' : !enabled[st.id] ? '🔒 ' : ''}{st.label}
          </button>
        ))}
      </div>

      <div className={s.hypBody}>
        {active === 'experiment' ? (
          <>
            <div className={s.mapActions}>
              <button className={s.refBtn} disabled={streaming} onClick={runExperiment}>
                {streaming ? 'Running…' : detail.experiment ? 'Re-run experiment' : 'Run experiment'}
              </button>
              {streaming && (
                <button className={s.refBtn} onClick={cancelExperiment} title="Stop the detached experiment process">
                  ■ Cancel
                </button>
              )}
              <span className={s.hypMeta}>Runs as a detached job (no timeout) — keeps going if you navigate away or the backend restarts.</span>
            </div>
            {streaming ? (
              <div className={s.hypDetailSection}>
                {thinking && (
                  <details className={s.hypLog} open>
                    <summary>🧠 Thinking</summary>
                    <pre className={s.researchStream}>{thinking}</pre>
                  </details>
                )}
                <div className={s.hypDetailTitle}>⚡ Live — writing code & running</div>
                <pre className={s.researchStream}>{live}<span className={s.specCursor} /></pre>
              </div>
            ) : (detail.code || detail.experiment || detail.log) ? (
            <>
              {detail.code && (
                <div className={s.hypDetailSection}>
                  <div className={s.hypDetailTitle}>✍️ Phase 1 — generated code (run.py)</div>
                  <pre className={s.researchStream}>{detail.code}</pre>
                </div>
              )}
              <div className={s.hypDetailSection}>
                <div className={s.hypDetailTitle}>▶ Phase 2 — run {detail.experiment ? 'results' : 'output'}</div>
                {detail.experiment
                  ? <div className={s.researchContent} dangerouslySetInnerHTML={{ __html: renderMarkdownWithFigures(detail.experiment, figUrl) }} />
                  : <p className={s.hypMeta}>No results yet — click “Run experiment”.</p>}
                {detail.log && (
                  <details className={s.hypLog} open={!detail.experiment}>
                    <summary>📜 Execution log</summary>
                    <pre className={s.researchStream}>{detail.log}</pre>
                  </details>
                )}
              </div>
            </>
            ) : (
              <p className={s.hypMeta}>Not run yet — click “Run experiment” above (or ▶ Auto for the whole pipeline).</p>
            )}
          </>
        ) : active === 'files' ? (
          <WorkspaceBrowser ideaId={ideaId} basePath={`hypotheses/${hid}`} />
        ) : body ? (
          <div className={s.researchContent} dangerouslySetInnerHTML={{ __html: renderMarkdownWithFigures(body, figUrl) }} />
        ) : active === 'plan' ? (
          <div className={s.empty}>
            <p className={s.emptyText}>No testing plan yet.</p>
            <button className={s.refBtn} disabled={sent === 'plan'} onClick={generatePlan}>
              {sent === 'plan' ? 'Generating… (see chat)' : 'Generate testing plan'}
            </button>
          </div>
        ) : (
          <div className={s.empty}>
            <p className={s.emptyText}>No report yet.</p>
            <button className={s.refBtn} disabled={!detail.experiment || sent === 'report'} onClick={generateReport}
              title={detail.experiment ? 'Agent writes the report + proposes follow-ups, in the chat'
                : 'Run the experiment first'}>
              {sent === 'report' ? 'Generating… (see chat)' : 'Generate report'}
            </button>
            {!detail.experiment && <p className={s.hypMeta}>Run the experiment first.</p>}
          </div>
        )}
      </div>
    </div>
  )
}

function HypothesisMapView({ ideaId, onActivity, onAgentCommand, mapVersion, focusHid }: {
  ideaId: string; onActivity?: ActivityFn; onAgentCommand?: (t: string) => void; mapVersion?: number
  focusHid?: { hid: string; nonce: number } | null
}) {
  const [map, setMap] = useState<HypothesisMap | null>(null)
  const [busy, setBusy] = useState<'load' | 'gen' | ''>('load')
  const [pins, setPins] = useState<string[]>([])     // pinned hypothesis ids → sub-tabs
  const [sub, setSub] = useState<string>('map')      // 'map' | a pinned id
  const [paperSel, setPaperSel] = useState<Set<string>>(new Set())  // selected for the paper
  const [styles, setStyles] = useState<WritingStyle[]>([])
  const [styleName, setStyleName] = useState('')  // chosen writing style ('' = default)
  const [paperSent, setPaperSent] = useState(false)  // /write_paper in flight (double-click guard)

  useEffect(() => {
    setBusy('load'); setPins([]); setSub('map')
    api.getHypothesisMap(ideaId).then(setMap).catch(() => setMap(null)).finally(() => setBusy(''))
    api.getWritingStyles().then(setStyles).catch(() => {})
  }, [ideaId])

  // Refetch after the chat agent regenerates the map (App bumps mapVersion).
  useEffect(() => {
    if (!mapVersion) return
    setBusy('')  // the generation finished → re-enable the Generate button
    api.getHypothesisMap(ideaId).then(setMap).catch(() => {})
  }, [mapVersion, ideaId])
  // fallback: re-enable Generate even if no refresh arrives (e.g. the run failed)
  useEffect(() => {
    if (busy !== 'gen') return
    const t = setTimeout(() => setBusy(''), 90000)
    return () => clearTimeout(t)
  }, [busy])

  // Generation runs as a skill in the conversation panel (deepagents reads IDEA.md
  // + writes .hypothesis_map.json — its work streams in the chat, so we do NOT also
  // add a duplicate activity line). Disable until the refresh arrives so a second
  // click can't fire a duplicate run.
  const generate = () => {
    if (busy === 'gen') return
    onAgentCommand?.('/generate_hypothesis_map')
    setBusy('gen')
  }
  const pin = (id: string) => { setPins(p => (p.includes(id) ? p : [...p, id])); setSub(id) }
  const unpin = (id: string) => { setPins(p => p.filter(x => x !== id)); setSub(s => (s === id ? 'map' : s)) }

  // Top monitor → pin + open the requested hypothesis (its Experiment step auto-attaches).
  useEffect(() => {
    if (focusHid?.hid) pin(focusHid.hid)
  }, [focusHid?.nonce])
  const removeNode = (id: string) => {
    if (!window.confirm(`Delete hypothesis ${id} and all its sub-hypotheses?`)) return
    // drop pins for the node and any of its descendants (ids like H1.2.1)
    setPins(p => p.filter(x => x !== id && !x.startsWith(id + '.')))
    setSub(cur => (cur === id || cur.startsWith(id + '.') ? 'map' : cur))
    api.deleteHypothesisNode(ideaId, id).then(setMap).catch(() => {})
  }
  const addChild = (id: string) => {
    const statement = window.prompt(`New sub-hypothesis under ${id} — a single falsifiable claim:`)?.trim()
    if (!statement) return
    api.addChildHypothesis(ideaId, id, statement).then(setMap).catch(() => {})
  }
  const rerun = (id: string) => {
    if (!window.confirm(`Rerun the experiment for ${id}? This clears its current results and starts a fresh detached run.`)) return
    api.rerunExperiment(ideaId, id)
      .then(() => { pin(id); api.getHypothesisMap(ideaId).then(setMap).catch(() => {}) })
      .catch(() => {})
  }

  const nodes = map?.nodes ?? []
  const flatten = (ns: HypothesisNode[]): HypothesisNode[] => ns.flatMap(n => [n, ...flatten(n.children ?? [])])
  const verified = flatten(nodes).filter(n => n.status === 'supported')  // "verified" hypotheses
  const selCount = verified.filter(n => paperSel.has(n.id)).length
  const toggleSel = (id: string) => setPaperSel(s => {
    const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n
  })
  const writePaper = () => {
    const ids = verified.filter(n => paperSel.has(n.id)).map(n => n.id)
    if (!ids.length || paperSent) return
    const styleArg = styleName ? ` --style ${styleName}` : ''
    onAgentCommand?.('/write_paper ' + ids.join(' ') + styleArg)
    setPaperSent(true)                          // streams in the chat — no duplicate activity line
    setTimeout(() => setPaperSent(false), 8000)  // guard against an accidental double-click
  }

  return (
    <div className={s.specWrap}>
      {/* sub-tabs: [Map] [H1] [H2] … (right-click a pin to remove it) */}
      <div className={s.subTabs}>
        <button className={`${s.subTab} ${sub === 'map' ? s.subTabActive : ''}`} onClick={() => setSub('map')}>
          🌳 Map
        </button>
        {pins.map(id => (
          <button
            key={id}
            className={`${s.subTab} ${sub === id ? s.subTabActive : ''}`}
            onClick={() => setSub(id)}
            onContextMenu={e => { e.preventDefault(); unpin(id) }}
            title="right-click to remove this pin"
          >
            {id}
          </button>
        ))}
      </div>

      {sub === 'map' ? (
        <div className={s.mapPage}>
          <div className={s.mapActions}>
            <button className={`${s.refBtn} ${s.mapGenBtn}`} disabled={busy === 'gen'} onClick={generate}>
              {busy === 'gen' ? 'Generating…' : nodes.length ? 'Regenerate hypothesis map' : 'Generate hypothesis map'}
            </button>
            {nodes.length > 0 && <span className={s.hypMeta}>Left-click a node to pin it · right-click for options.</span>}
          </div>
          {busy === 'load' ? (
            <div className={s.hypMeta} style={{ padding: '12px' }}>Loading…</div>
          ) : nodes.length === 0 ? (
            <div className={s.empty}>
              <span className={s.emptyIcon}>🌳</span>
              <p className={s.emptyText}>
                No hypothesis map yet. Click “Generate hypothesis map” above — or ask the assistant
                in chat to add/refine hypotheses.
              </p>
            </div>
          ) : (
            <HypothesisGraph roots={nodes} onPin={pin} onDelete={removeNode} onAddChild={addChild} onRerun={rerun} />
          )}

          {verified.length > 0 && (
            <div className={s.paperPanel}>
              <div className={s.paperPanelHead}>📄 Write paper from verified hypotheses</div>
              {verified.map(n => (
                <label key={n.id} className={s.paperPick}>
                  <input type="checkbox" checked={paperSel.has(n.id)} onChange={() => toggleSel(n.id)} />
                  <span className={s.paperPickId}>{n.id}</span>
                  <span className={s.paperPickText} title={n.statement}>{n.statement}</span>
                </label>
              ))}
              <label className={s.paperPick} title="Prose-style guide the paper will follow (separate from venue formatting)">
                <span className={s.paperPickId}>✍️ Style</span>
                <select className={s.select} style={{ width: 'auto' }} value={styleName} onChange={e => setStyleName(e.target.value)}>
                  <option value="">Default</option>
                  {styles.map(st => (
                    <option key={st.name} value={st.name}>{st.title}{st.scope === 'domain' ? ' (domain)' : ''}</option>
                  ))}
                </select>
              </label>
              <div className={s.mapActions}>
                <button className={`${s.refBtn} ${s.mapGenBtn}`} disabled={!selCount || paperSent} onClick={writePaper}>
                  {paperSent ? 'Writing… (see chat)' : `Generate paper from selected (${selCount})`}
                </button>
                <button
                  className={s.refBtn}
                  onClick={() => onAgentCommand?.('/setup_venue')}
                  title="Agent downloads the target venue's LaTeX template + writes venue/STYLE.md"
                >
                  ⚙️ Set up venue template
                </button>
              </div>
              <span className={s.hypMeta}>The agent reads each report and writes paper.md (following venue/STYLE.md) — live in the chat.</span>
            </div>
          )}
        </div>
      ) : (
        <HypothesisDetailView key={sub} ideaId={ideaId} hid={sub} onActivity={onActivity}
          onAgentCommand={onAgentCommand} mapVersion={mapVersion} />
      )}
    </div>
  )
}

function DomainProcessView({ proc }: { proc: DomainProcess }) {
  const total = proc.broad.length + proc.sota.length
  return (
    <div className={s.procWrap}>
      {total > 0 && (
        <details className={s.procBlock} open>
          <summary className={s.procSummary}>🔍 Searched OpenAlex — {total} papers</summary>
          <div className={s.procBody}>
            {proc.broad.map((label, i) => (
              <div key={`b${i}`} className={s.procPaper}>{label}</div>
            ))}
            {proc.sota.map((label, i) => (
              <div key={`s${i}`} className={s.procPaper}>
                <span className={s.procSota}>SOTA</span> {label}
              </div>
            ))}
          </div>
        </details>
      )}
      {proc.thinking && (
        <details className={s.procBlock} open>
          <summary className={s.procSummary}>🧠 Thinking</summary>
          <div className={s.procThinking}>{proc.thinking}</div>
        </details>
      )}
    </div>
  )
}

/** The version number shown by a paper filename (paper.md/paper.pdf = 1, paper_v3.* = 3). */
function paperFileVersion(name?: string | null): number | undefined {
  if (!name) return undefined
  const m = /paper_v(\d+)\./.exec(name)
  return m ? Number(m[1]) : 1
}

export default function ResourcesPanel({ resources, ideaId, domainId, domains, onActivity, onAgentCommand, mapVersion, focusHyp, spec, specLabel, hasSpec, specStreaming, domainProcess, paperText, paperFile, paperVersions, paperVersion, onSelectPaperVersion, paperDownloadUrl, paperPdfUrl, width, onSaveSpec }: Props) {
  const [rightTab, setRightTab] = useState<RightTab>('spec')
  const [tab, setTab] = useState<ResourceTab>('all')
  const [hardware, setHardware] = useState<HardwareView | null>(null)
  // Spec tab sub-view: the IDEA.md / DOMAIN.md spec, or the benchmark library. Both an idea
  // (its own + domain + global) and a domain (its + global) show a list + per-template view.
  const [specSub, setSpecSub] = useState<'spec' | 'benchmark' | 'resources'>('spec')
  const [benchMd, setBenchMd] = useState<string | null>(null)
  const [benchList, setBenchList] = useState<Benchmark[]>([])
  const [benchSel, setBenchSel] = useState('')
  const [benchLoaded, setBenchLoaded] = useState(false)
  const benchEnabled = !!(ideaId || domainId)

  useEffect(() => {  // context changed → reset the sub-view + benchmark cache
    setSpecSub('spec'); setBenchMd(null); setBenchList([]); setBenchSel(''); setBenchLoaded(false)
  }, [ideaId, domainId])

  useEffect(() => {  // open the Benchmark sub-tab → load the available list (idea or domain scope)
    if (rightTab !== 'spec' || specSub !== 'benchmark' || benchLoaded) return
    setBenchLoaded(true)
    const p = ideaId ? api.getIdeaBenchmarks(ideaId)
            : domainId ? api.getBenchmarks(domainId) : null
    p?.then(list => { setBenchList(list); if (list.length) setBenchSel(list[0].name) })
      .catch(() => setBenchList([]))
  }, [rightTab, specSub, ideaId, domainId, benchLoaded])

  useEffect(() => {  // fetch the selected template's markdown (idea: idea>domain>global; domain: domain>global)
    if (specSub !== 'benchmark' || !benchSel) return
    const p = ideaId ? api.getIdeaBenchmark(ideaId, benchSel)
            : domainId ? api.getBenchmark(benchSel, domainId) : null
    p?.then(b => setBenchMd(b.content)).catch(() => setBenchMd(null))
  }, [specSub, ideaId, domainId, benchSel])

  const benchHtml = useMemo(() => (benchMd ? renderMarkdown(benchMd) : ''), [benchMd])

  // Refresh the global hardware snapshot whenever the Resources tab opens, so a
  // detection just run from Settings shows up here (no App-level wiring needed).
  useEffect(() => {
    if (rightTab === 'resources') api.getHardware().then(setHardware).catch(() => {})
  }, [rightTab])

  // Jump-to-experiment from the top monitor → open the Hypotheses tab.
  useEffect(() => {
    if (focusHyp) setRightTab('hypothesis')
  }, [focusHyp?.nonce])

  // Auto-switch to Paper tab when a paper (markdown or compiled PDF) becomes available
  useEffect(() => {
    if (paperText || paperPdfUrl) setRightTab('paper')
  }, [!!paperText, !!paperPdfUrl])

  // Fall back to spec if paper cleared (idea switched)
  useEffect(() => {
    if (!paperText && !paperPdfUrl && rightTab === 'paper') setRightTab('spec')
  }, [paperText, paperPdfUrl])

  const specHtml = useMemo(() => {
    if (!spec || specStreaming) return ''
    return renderMarkdown(spec)
  }, [spec, specStreaming])

  const paperHtml = useMemo(() => {
    if (!paperText) return ''
    // resolve figure images (fig_intro.png / fig_method.png) from the idea workspace
    return ideaId
      ? renderMarkdownWithMathAndFigures(paperText, name => api.rawFileUrl(ideaId, name))
      : renderMarkdownWithMath(paperText)
  }, [paperText, ideaId])

  const filtered = tab === 'all'
    ? resources
    : resources.filter(r => r.type === tab.slice(0, -1) as Resource['type'])

  void onSaveSpec
  void SHOW_SPEC_EDITOR

  return (
    <aside className={s.panel} style={width ? { width, minWidth: width } : undefined}>
      <div className={s.header}>
        <div className={s.rightTabs}>
          <button
            className={`${s.rightTab} ${rightTab === 'spec' ? s.rightTabActive : ''}`}
            onClick={() => setRightTab('spec')}
          >
            {specLabel}
          </button>
          {ideaId && (
            <button
              className={`${s.rightTab} ${rightTab === 'hypothesis' ? s.rightTabActive : ''}`}
              onClick={() => setRightTab('hypothesis')}
            >
              🌳 Hypotheses
            </button>
          )}
          {ideaId && (
            <button
              className={`${s.rightTab} ${rightTab === 'references' ? s.rightTabActive : ''}`}
              onClick={() => setRightTab('references')}
            >
              📚 References
            </button>
          )}
          {(paperText || paperPdfUrl) && (
            <button
              className={`${s.rightTab} ${rightTab === 'paper' ? s.rightTabActive : ''}`}
              onClick={() => setRightTab('paper')}
            >
              📄 Paper
            </button>
          )}
          <button
            className={`${s.rightTab} ${rightTab === 'resources' ? s.rightTabActive : ''}`}
            onClick={() => setRightTab('resources')}
          >
            Resources{resources.length > 0 ? ` ${resources.length}` : ''}
          </button>
        </div>
      </div>

      {rightTab === 'hypothesis' && ideaId ? (
        <HypothesisMapView key={ideaId} ideaId={ideaId} onActivity={onActivity} onAgentCommand={onAgentCommand} mapVersion={mapVersion}
          focusHid={focusHyp && focusHyp.ideaId === ideaId ? focusHyp : null} />
      ) : rightTab === 'references' && ideaId ? (
        <div className={s.specWrap}><div className={s.tabScroll}><ReferencesCard key={ideaId} ideaId={ideaId} onActivity={onActivity} onAgentCommand={onAgentCommand} /></div></div>
      ) : rightTab === 'paper' ? (
        <div className={s.specWrap}>
          {(paperDownloadUrl || paperFile) && (
            <div className={s.specToolbar}>
              {paperVersions && paperVersions.length > 1 && (
                <select
                  className={s.select}
                  style={{ width: 'auto' }}
                  value={paperVersion ?? paperFileVersion(paperFile) ?? Math.max(...paperVersions)}
                  onChange={e => onSelectPaperVersion?.(Number(e.target.value))}
                  title="View a paper version"
                >
                  {[...paperVersions].reverse().map(v => (
                    <option key={v} value={v}>
                      {v === Math.max(...paperVersions) ? `v${v} (latest)` : `v${v}`}
                    </option>
                  ))}
                </select>
              )}
              {paperFile && <span className={s.hypMeta} title="Older versions kept as paper.*, paper_v2.*, …">{paperFile}</span>}
              {paperDownloadUrl && (
                <a className={s.specBtn} href={paperDownloadUrl} target="_blank" rel="noreferrer" download>
                  ⬇ Download {paperPdfUrl ? 'PDF' : 'Markdown'}
                </a>
              )}
            </div>
          )}
          {paperText ? (
            <div
              className={`${s.specMarkdown} ${s.paperView}`}
              dangerouslySetInnerHTML={{ __html: paperHtml || '<p>Rendering paper…</p>' }}
            />
          ) : paperPdfUrl ? (
            <iframe className={s.paperFrame} src={paperPdfUrl} title="Compiled paper (PDF)" />
          ) : (
            <div className={`${s.specMarkdown} ${s.paperView}`} dangerouslySetInnerHTML={{ __html: '<p>Rendering paper…</p>' }} />
          )}
        </div>
      ) : rightTab === 'spec' ? (
        !hasSpec ? (
          <div className={s.empty}>
            <span className={s.emptyIcon}>📋</span>
            <p className={s.emptyText}>
              Select a Domain, a brainstormed draft, or an Idea to see its spec here.
            </p>
          </div>
        ) : specStreaming ? (
          <div className={s.specWrap}>
            {domainProcess && <DomainProcessView proc={domainProcess} />}
            {(spec || !domainProcess) && (
              <pre className={s.specStream}>{spec}<span className={s.specCursor} /></pre>
            )}
          </div>
        ) : (
          <div className={s.specWrap}>
            {benchEnabled && (
              <div className={s.tabs}>
                <button className={`${s.tab} ${specSub === 'spec' ? s.tabActive : ''}`}
                        onClick={() => setSpecSub('spec')}>📋 {specLabel}</button>
                <button className={`${s.tab} ${specSub === 'benchmark' ? s.tabActive : ''}`}
                        onClick={() => setSpecSub('benchmark')}>📊 Benchmark</button>
                {ideaId && (
                  <button className={`${s.tab} ${specSub === 'resources' ? s.tabActive : ''}`}
                          onClick={() => setSpecSub('resources')}>📦 Resources</button>
                )}
              </div>
            )}
            {specSub === 'resources' && ideaId ? (
              <ResourcesEditor key={ideaId} ideaId={ideaId} />
            ) : specSub === 'benchmark' && benchEnabled ? (
              benchList.length === 0 ? (
                <div className={s.empty}>
                  <span className={s.emptyIcon}>📊</span>
                  <p className={s.emptyText}>
                    No benchmark templates yet. Run <code>/setup_benchmark &lt;paper&gt;</code> in the
                    {ideaId ? ' chat' : ' domain chat'}, or add one with <code>paperclaw benchmark add</code>.
                  </p>
                </div>
              ) : (
                <>
                  {/* picker over the available templates (idea: own + domain + global; domain: + global) */}
                  {benchList.length > 1 && (
                    <select className={s.select ?? ''} value={benchSel}
                            onChange={e => setBenchSel(e.target.value)}>
                      {benchList.map(b => (
                        <option key={`${b.scope}/${b.name}`} value={b.name}>
                          {b.title || b.name}{b.scope !== 'global' ? ` (${b.scope})` : ''}
                        </option>
                      ))}
                    </select>
                  )}
                  <div className={s.specMarkdown}
                       dangerouslySetInnerHTML={{ __html: benchHtml || '<p>Loading…</p>' }} />
                </>
              )
            ) : (
              <>
                {ideaId && <DomainConnector key={ideaId} ideaId={ideaId} domains={domains ?? []} />}
                <div
                  className={s.specMarkdown}
                  dangerouslySetInnerHTML={{ __html: specHtml || '<p>Loading…</p>' }}
                />
              </>
            )}
          </div>
        )
      ) : (
        <>
          <HardwareCard hw={hardware} />

          <div className={s.tabs}>
            {RESOURCE_TABS.map(t => (
              <button
                key={t.id}
                className={`${s.tab} ${tab === t.id ? s.tabActive : ''}`}
                onClick={() => setTab(t.id)}
              >
                {t.label}
              </button>
            ))}
          </div>

          {filtered.length === 0 ? (
            <div className={s.empty}>
              <span className={s.emptyIcon}>📚</span>
              <p className={s.emptyText}>
                Resources will appear here as you research ideas and explore papers.
              </p>
            </div>
          ) : (
            <div className={s.list}>
              {filtered.map(r => <ResourceItem key={r.id} resource={r} />)}
            </div>
          )}
        </>
      )}
    </aside>
  )
}
