import { useEffect, useState } from 'react'
import { api } from '../../api'
import type { Domain } from '../../types'
import s from './styles.module.css'

/**
 * Connect an idea to one or MORE domains. The connected domains are mounted read-only
 * under the idea's ./domains/<name>/ so the chat agent can read their DOMAIN.md, codebase,
 * benchmarks and prior-run artifacts. Brainstorm-created ideas are auto-connected (their
 * IDEA.md names the domain); this lets you add/remove connections by hand.
 */
export default function DomainConnector({ ideaId, domains }: { ideaId: string; domains: Domain[] }) {
  const [connected, setConnected] = useState<string[]>([])
  const [open, setOpen] = useState(false)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    let alive = true
    setOpen(false)
    api.getIdeaDomains(ideaId).then(r => { if (alive) setConnected(r.domainIds) }).catch(() => {})
    return () => { alive = false }
  }, [ideaId])

  async function toggle(id: string) {
    const next = connected.includes(id) ? connected.filter(x => x !== id) : [...connected, id]
    setConnected(next)  // optimistic
    setSaving(true)
    try { setConnected((await api.setIdeaDomains(ideaId, next)).domainIds) }
    catch { /* leave optimistic state; a refetch will reconcile */ }
    finally { setSaving(false) }
  }

  const connectedDomains = domains.filter(d => connected.includes(d.id))
  return (
    <div className={s.domainConnector}>
      <div className={s.domainRow}>
        <span className={s.domainLabel}>🔗 Domains</span>
        {connectedDomains.length === 0
          ? <span className={s.domainNone}>none connected</span>
          : connectedDomains.map(d => <span key={d.id} className={s.domainChip}>{d.name}</span>)}
        <button className={s.domainEdit} onClick={() => setOpen(o => !o)}>{open ? 'Done' : 'Edit'}</button>
      </div>
      {open && (
        <div className={s.domainPicker}>
          {domains.length === 0
            ? <span className={s.domainNone}>No domains yet — create one first.</span>
            : domains.map(d => (
                <label key={d.id} className={s.domainOption}>
                  <input type="checkbox" checked={connected.includes(d.id)} disabled={saving}
                         onChange={() => toggle(d.id)} />
                  {d.name}
                </label>
              ))}
        </div>
      )}
    </div>
  )
}
