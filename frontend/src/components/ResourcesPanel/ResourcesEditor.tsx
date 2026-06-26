import { useEffect, useState } from 'react'
import { api } from '../../api'
import type { IdeaResources } from '../../types'
import s from './styles.module.css'

const MODES: { id: string; label: string }[] = [
  { id: 'cli', label: 'CLI agent (local)' },
  { id: 'executed', label: 'In-process agent (local)' },
  { id: 'ssh', label: 'SSH remote (GPU host)' },
  { id: 'simulated', label: 'Simulated (no real run)' },
]

/**
 * Allocate an idea's experiment resources — the compute (mode + SSH GPU host) every experiment
 * of this idea uses, whether launched by Auto run or manually from the Hypotheses tab (NOT the
 * chat). The active LLM is shown read-only.
 */
export default function ResourcesEditor({ ideaId }: { ideaId: string }) {
  const [res, setRes] = useState<IdeaResources | null>(null)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    let alive = true
    api.getIdeaResources(ideaId).then(r => { if (alive) setRes(r) }).catch(() => {})
    return () => { alive = false }
  }, [ideaId])

  async function patch(p: Partial<{ experimentMode: string; sshTargetId: string | null; useReferenceCodebase: boolean }>) {
    setSaving(true)
    try { setRes(await api.setIdeaResources(ideaId, p)) }
    catch { /* ignore */ }
    finally { setSaving(false) }
  }

  if (!res) return <div className={s.empty}><p className={s.emptyText}>Loading resources…</p></div>

  return (
    <div className={s.resEditor}>
      <p className={s.resHint}>
        Resources for this idea's experiments — used by <strong>Auto run</strong> and a manual
        run from the 🌳 Hypotheses tab.
      </p>

      <label className={s.resField}>
        <span className={s.resLabel}>🖥️ Compute / execution</span>
        <select className={s.select} value={res.experimentMode ?? 'cli'} disabled={saving}
                onChange={e => patch({ experimentMode: e.target.value })}>
          {MODES.map(m => <option key={m.id} value={m.id}>{m.label}</option>)}
        </select>
      </label>

      {res.experimentMode === 'ssh' && (
        <label className={s.resField}>
          <span className={s.resLabel}>🌐 SSH GPU host</span>
          {res.sshTargets.length === 0 ? (
            <span className={s.domainNone}>No SSH remotes — add one in Settings → Hardware.</span>
          ) : (
            <select className={s.select} value={res.sshTargetId ?? ''} disabled={saving}
                    onChange={e => patch({ sshTargetId: e.target.value || null })}>
              <option value="">(choose a host)</option>
              {res.sshTargets.map(t => (
                <option key={t.id} value={t.id}>{t.label || `${t.user}@${t.host}`}</option>
              ))}
            </select>
          )}
        </label>
      )}

      <label className={s.resCheck}>
        <input type="checkbox" checked={res.useReferenceCodebase} disabled={saving}
               onChange={e => patch({ useReferenceCodebase: e.target.checked })} />
        Reuse the pinned domain's reference codebase
      </label>

      <div className={s.resLlm}>
        <span className={s.resLabel}>🔑 LLM (from Settings — read-only)</span>
        <div className={s.resLlmRow}>
          {res.llmProvider} · {res.llmModel || '(default)'}
          {res.llmBaseUrl ? ` · ${res.llmBaseUrl}` : ''}
          {' · '}{res.llmKeyConfigured ? 'API key configured' : 'API key MISSING'}
        </div>
      </div>
    </div>
  )
}
