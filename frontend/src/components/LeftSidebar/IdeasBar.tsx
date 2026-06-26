import { useEffect, useState, KeyboardEvent } from 'react'
import type { Idea } from '../../types'
import s from './styles.module.css'

interface Props {
  ideas: Idea[]
  streamingContexts: Set<string>
  runningAutoIdeas: Set<string>
  isOpen: boolean
  onToggle: () => void
  onAdd: (title: string) => void
  onSetActive: (id: string) => void
  onRemove: (id: string) => void
  onReveal: (id: string) => void
  onDuplicate: (id: string) => void
  onSetColor: (id: string, color: string | null) => void
}

const FLAG_COLORS: Record<string, string> = { green: '#22c55e', yellow: '#eab308', grey: '#9ca3af' }

interface CtxMenu {
  ideaId: string
  x: number
  y: number
}

export default function IdeasBar({
  ideas, streamingContexts, runningAutoIdeas, isOpen, onToggle, onAdd, onSetActive, onRemove, onReveal, onDuplicate, onSetColor
}: Props) {
  const [ctxMenu, setCtxMenu] = useState<CtxMenu | null>(null)

  useEffect(() => {
    if (!ctxMenu) return
    const close = () => setCtxMenu(null)
    window.addEventListener('click', close)
    window.addEventListener('contextmenu', close)
    return () => {
      window.removeEventListener('click', close)
      window.removeEventListener('contextmenu', close)
    }
  }, [ctxMenu])

  const [draft, setDraft] = useState('')
  const [adding, setAdding] = useState(false)

  const commit = () => {
    if (!draft.trim()) { setAdding(false); return }
    onAdd(draft)
    setDraft('')
    setAdding(false)
  }

  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') { e.preventDefault(); commit() }
    if (e.key === 'Escape') { setDraft(''); setAdding(false) }
  }

  return (
    <>
      <div className={s.divider} />
      <div
        className={`${s.sectionHeader} ${isOpen ? s.active : ''}`}
        onClick={onToggle}
        role="button"
        aria-expanded={isOpen}
      >
        <span className={s.sectionIcon}>💡</span>
        <span className={s.sectionTitle}>Ideas</span>
        {ideas.length > 0 && (
          <span style={{ fontSize: 'var(--fs-xs)', color: 'var(--text-muted)', marginRight: 4 }}>
            {ideas.length}
          </span>
        )}
        <div className={s.sectionActions} onClick={e => e.stopPropagation()}>
          <button
            className={s.iconBtn}
            title="New idea"
            onClick={() => { setAdding(true); if (!isOpen) onToggle() }}
          >
            +
          </button>
        </div>
        <span className={`${s.chevron} ${isOpen ? s.chevronOpen : ''}`}>›</span>
      </div>

      {isOpen && (
        <div className={s.sectionBody}>
          {adding && (
            <div className={s.topicInput}>
              <input
                autoFocus
                value={draft}
                onChange={e => setDraft(e.target.value)}
                onKeyDown={onKey}
                onBlur={commit}
                placeholder="Idea title…"
                maxLength={120}
              />
            </div>
          )}

          {ideas.length === 0 && !adding ? (
            <p className={s.emptyHint}>No ideas yet — promote a seed or press +</p>
          ) : (
            <div className={s.topicList}>
              {ideas.map(idea => (
                <div
                  key={idea.id}
                  className={`${s.topicItem} ${idea.isActive ? s.topicActive : ''}`}
                  onClick={() => onSetActive(idea.id)}
                  onContextMenu={e => {
                    e.preventDefault()
                    e.stopPropagation()
                    setCtxMenu({ ideaId: idea.id, x: e.clientX, y: e.clientY })
                  }}
                  role="button"
                >
                  {runningAutoIdeas.has(idea.id)
                    ? <span className={s.topicBulletRunning} title="Auto run in progress…" />
                    : <span className={s.topicBullet}
                            style={idea.color ? { background: FLAG_COLORS[idea.color] } : undefined}
                            title={idea.color ? `flagged ${idea.color}` : undefined} />}
                  <span className={s.topicName} title={idea.title}>{idea.title}</span>
                  {streamingContexts.has(idea.id) && (
                    <span className={s.itemSpinner} title="Generating…" />
                  )}
                  <button
                    className={s.topicDelete}
                    onClick={e => { e.stopPropagation(); onRemove(idea.id) }}
                    title="Remove (deletes IDEA.md)"
                  >
                    ×
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {ctxMenu && (
        <div
          className={s.ctxMenu}
          style={{ left: ctxMenu.x, top: ctxMenu.y }}
          onClick={e => e.stopPropagation()}
        >
          <button
            className={s.ctxItem}
            onClick={() => { onDuplicate(ctxMenu.ideaId); setCtxMenu(null) }}
          >
            📄 Duplicate idea
          </button>
          <button
            className={s.ctxItem}
            onClick={() => { onReveal(ctxMenu.ideaId); setCtxMenu(null) }}
          >
            📂 Open in file explorer
          </button>
          <div className={s.ctxFlags}>
            <span className={s.ctxFlagLabel}>Flag:</span>
            {(['green', 'yellow', 'grey'] as const).map(c => (
              <button key={c} className={s.ctxFlagDot} style={{ background: FLAG_COLORS[c] }}
                      title={c} onClick={() => { onSetColor(ctxMenu.ideaId, c); setCtxMenu(null) }} />
            ))}
            <button className={s.ctxFlagClear} title="clear flag"
                    onClick={() => { onSetColor(ctxMenu.ideaId, null); setCtxMenu(null) }}>✕</button>
          </div>
          <button
            className={`${s.ctxItem} ${s.ctxItemDanger}`}
            onClick={() => { onRemove(ctxMenu.ideaId); setCtxMenu(null) }}
          >
            🗑 Delete idea
          </button>
        </div>
      )}
    </>
  )
}
