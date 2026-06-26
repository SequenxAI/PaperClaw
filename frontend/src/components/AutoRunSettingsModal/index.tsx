import { useEffect, useState } from 'react'
import { api } from '../../api'
import type { SSHTarget, WritingStyle, Benchmark } from '../../types'
import s from './styles.module.css'

export interface AutoRunSettings {
  positive: number
  maxHypotheses: number
  pageLimit: number
  maxDepth: number
  experimentMode: string
  sshTargetId?: string
  writingStyle?: string
  benchmark?: string
  useReferenceCodebase: boolean
  fillPage: boolean
}

interface Props {
  open: boolean
  ideaId?: string    // target for venue-template uploads
  ideaTitle?: string
  domainId?: string  // scopes the writing-style list (domain-first)
  resume?: boolean   // the idea already has results → frame this as a Resume
  onClose: () => void
  onStart: (settings: AutoRunSettings) => void
}

/** Read a File as base64 (no data: prefix) — for binary venue-template zips. */
function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const r = new FileReader()
    r.onload = () => resolve(String(r.result).split(',')[1] ?? '')
    r.onerror = () => reject(r.error)
    r.readAsDataURL(file)
  })
}

function NumField({ label, value, min, max, onChange }:
  { label: string; value: number; min: number; max: number; onChange: (n: number) => void }) {
  return (
    <label className={s.field}>
      <span>{label}</span>
      <input type="number" min={min} max={max} value={value}
             onChange={e => onChange(Math.max(min, Math.min(max, Number(e.target.value) || min)))} />
    </label>
  )
}

/** Per-idea ⚡ Auto run settings: environment + coding agent, writing style, run
 *  limits, and reference-codebase reuse — all applied to THIS run only. */
export default function AutoRunSettingsModal({ open, ideaId, ideaTitle, domainId, resume, onClose, onStart }: Props) {
  const [positive, setPositive] = useState(2)
  const [maxHypotheses, setMaxHypotheses] = useState(6)
  const [pageLimit, setPageLimit] = useState(8)
  const [maxDepth, setMaxDepth] = useState(3)
  // Experiment execution is encoded as one <select> value: cli | executed | simulated | ssh:<id>
  const [exec, setExec] = useState('cli')
  const [useCodebase, setUseCodebase] = useState(true)
  const [fillPage, setFillPage] = useState(false)
  const [style, setStyle] = useState('')  // '' = default prose
  const [bench, setBench] = useState('')   // '' = no benchmark
  const [sshTargets, setSshTargets] = useState<SSHTarget[]>([])
  const [styles, setStyles] = useState<WritingStyle[]>([])
  const [benchmarks, setBenchmarks] = useState<Benchmark[]>([])
  const [venueStatus, setVenueStatus] = useState<string | null>(null)
  const [styleStatus, setStyleStatus] = useState<string | null>(null)
  const [benchStatus, setBenchStatus] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    setVenueStatus(null); setStyleStatus(null); setBenchStatus(null)
    api.getHardware().then(h => {
      setSshTargets(h.sshTargets ?? [])
      const rc = h.runConfig
      if (rc) setExec(rc.experimentMode === 'ssh' && rc.sshTargetId ? `ssh:${rc.sshTargetId}` : rc.experimentMode)
    }).catch(() => {})
    api.getWritingStyles(domainId).then(setStyles).catch(() => {})
    api.getBenchmarks(domainId).then(setBenchmarks).catch(() => {})
  }, [open, domainId])

  // Upload a custom writing-style .md, then select it.
  const onStyleFile = async (file?: File) => {
    if (!file) return
    try {
      const content = await file.text()
      const name = file.name.replace(/\.md$/i, '')
      const r = await api.saveWritingStyle(name, content, domainId)
      const fresh = await api.getWritingStyles(domainId)
      setStyles(fresh); setStyle(r.name); setStyleStatus(`✓ added “${r.name}”`)
    } catch (e) { setStyleStatus(`⚠️ ${String((e as Error).message ?? e)}`) }
  }

  // Upload a benchmark template (.md / .csv published-results table), then select it.
  const onBenchFile = async (file?: File) => {
    if (!file) return
    try {
      const content = await file.text()
      const name = file.name.replace(/\.(md|csv|txt)$/i, '')
      const r = await api.saveBenchmark(name, content, domainId)
      const fresh = await api.getBenchmarks(domainId)
      setBenchmarks(fresh); setBench(r.name); setBenchStatus(`✓ added “${r.name}”`)
    } catch (e) { setBenchStatus(`⚠️ ${String((e as Error).message ?? e)}`) }
  }

  // Upload a LaTeX venue template (zip or single style/class/tex) into the idea's venue/.
  const onVenueFile = async (file?: File) => {
    if (!file || !ideaId) return
    setVenueStatus('uploading…')
    try {
      const b64 = await fileToBase64(file)
      const r = await api.uploadVenueTemplate(ideaId, file.name, b64)
      setVenueStatus(`✓ ${file.name} — ${r.files.length} file${r.files.length === 1 ? '' : 's'} in venue/`)
    } catch (e) { setVenueStatus(`⚠️ ${String((e as Error).message ?? e)}`) }
  }

  if (!open) return null

  const start = () => {
    const [mode, sshId] = exec.startsWith('ssh:') ? ['ssh', exec.slice(4)] : [exec, undefined]
    onStart({
      positive, maxHypotheses, pageLimit, maxDepth,
      experimentMode: mode, sshTargetId: sshId,
      writingStyle: style || undefined, benchmark: bench || undefined,
      useReferenceCodebase: useCodebase, fillPage,
    })
  }

  return (
    <div className={s.overlay} onClick={onClose}>
      <div className={s.modal} onClick={e => e.stopPropagation()}>
        <h2 className={s.title}>{resume ? '▶ Resume auto run' : '⚡ Auto run'}</h2>
        <p className={s.sub}>
          {ideaTitle ? <><em>{ideaTitle}</em> — </> : null}
          {resume
            ? 'continues from existing results — tests untested hypotheses, then writes the paper. Settings apply to this run only.'
            : 'hypotheses → experiments → paper. These settings apply to this run only.'}
        </p>

        <label className={s.label}>Experiment execution (environment + coding agent)</label>
        <select className={s.select} value={exec} onChange={e => setExec(e.target.value)}>
          <option value="cli">CLI coding agent (claude) — local</option>
          <option value="executed">In-process coding agent — local</option>
          {sshTargets.map(t => (
            <option key={t.id} value={`ssh:${t.id}`}>SSH remote: {t.label || `${t.user}@${t.host}`}</option>
          ))}
          <option value="simulated">Simulated — no real run (fake data)</option>
        </select>

        <label className={s.label}>Writing style</label>
        <div className={s.uploadRow}>
          <select className={s.select} value={style} onChange={e => setStyle(e.target.value)}>
            <option value="">Default</option>
            {styles.map(w => (
              <option key={`${w.scope}/${w.name}`} value={w.name}>
                {w.title || w.name}{w.scope === 'domain' ? ' (domain)' : ''}
              </option>
            ))}
          </select>
          <label className={s.uploadBtn} title="Upload a custom .md style guide">
            ⬆ .md
            <input type="file" accept=".md,text/markdown" hidden
                   onChange={e => onStyleFile(e.target.files?.[0])} />
          </label>
        </div>
        {styleStatus && <div className={s.uploadStatus}>{styleStatus}</div>}

        <label className={s.label}>Benchmark (optional — published baselines to beat)</label>
        <div className={s.uploadRow}>
          <select className={s.select} value={bench} onChange={e => setBench(e.target.value)}>
            <option value="">None</option>
            {benchmarks.map(b => (
              <option key={`${b.scope}/${b.name}`} value={b.name}>
                {b.title || b.name}{b.scope === 'domain' ? ' (domain)' : ''}
              </option>
            ))}
          </select>
          <label className={s.uploadBtn} title="Upload a benchmark table (.md / .csv)">
            ⬆ table
            <input type="file" accept=".md,.csv,.txt,text/markdown,text/csv" hidden
                   onChange={e => onBenchFile(e.target.files?.[0])} />
          </label>
        </div>
        {benchStatus && <div className={s.uploadStatus}>{benchStatus}</div>}

        <label className={s.label}>LaTeX template (optional)</label>
        <div className={s.uploadRow}>
          <span className={s.uploadHint}>
            Upload a venue template (.zip / .sty / .cls / .tex) — the paper is based on it.
          </span>
          <label className={`${s.uploadBtn} ${!ideaId ? s.uploadDisabled : ''}`}
                 title={ideaId ? 'Upload a LaTeX venue template' : 'Available once the idea exists'}>
            ⬆ template
            <input type="file" accept=".zip,.sty,.cls,.tex" hidden disabled={!ideaId}
                   onChange={e => onVenueFile(e.target.files?.[0])} />
          </label>
        </div>
        {venueStatus && <div className={s.uploadStatus}>{venueStatus}</div>}

        <label className={s.check}>
          <input type="checkbox" checked={useCodebase} onChange={e => setUseCodebase(e.target.checked)} />
          <span>Reuse the pinned domain's reference codebase for experiments</span>
        </label>

        <label className={s.check}>
          <input type="checkbox" checked={fillPage} onChange={e => setFillPage(e.target.checked)} />
          <span>Fill the page limit — main text ends at the last allowed page</span>
        </label>

        <div className={s.row}>
          <NumField label="Paper on N positive" value={positive} min={1} max={10} onChange={setPositive} />
          <NumField label="Max hypotheses" value={maxHypotheses} min={1} max={20} onChange={setMaxHypotheses} />
          <NumField label="Max tree depth" value={maxDepth} min={1} max={6} onChange={setMaxDepth} />
          <NumField label="Page limit" value={pageLimit} min={1} max={20} onChange={setPageLimit} />
        </div>

        <div className={s.actions}>
          <button className={s.cancel} onClick={onClose}>Cancel</button>
          <button className={s.start} onClick={start}>{resume ? 'Resume run' : 'Start run'}</button>
        </div>
      </div>
    </div>
  )
}
