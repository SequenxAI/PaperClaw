import { useEffect, useRef } from 'react'
import s from './styles.module.css'

export interface FeedbackEntry {
  id: string
  kind: 'step' | 'thinking' | 'text'
  text: string
}

interface Props {
  open: boolean
  title?: string
  entries: FeedbackEntry[]
  running: boolean
  onClose: () => void
}

/** A dedicated slide-in panel showing the LIVE agent feed (thinking + steps +
 *  streamed output) for a running action — domain creation, idea brainstorm, or an
 *  auto run. Independent of the idea context so it works during any of them. */
export default function FeedbackPanel({ open, title, entries, running, onClose }: Props) {
  const feedRef = useRef<HTMLDivElement>(null)
  // Stick to the bottom as new content streams in — but ONLY if the user is already near
  // the bottom. If they scrolled UP to read, don't yank them back down (and scroll the
  // feed's OWN container, never the page).
  useEffect(() => {
    if (!open) return
    const el = feedRef.current
    if (!el) return
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80
    if (nearBottom) el.scrollTop = el.scrollHeight
  }, [entries, open])

  if (!open) return null
  return (
    <div className={s.drawer}>
      <div className={s.head}>
        <span className={s.title}>
          {running ? <span className={s.spinner}>⚡</span> : '✓'} {title || 'Agent feedback'}
        </span>
        <button className={s.close} onClick={onClose} title="Hide">✕</button>
      </div>
      <div className={s.feed} ref={feedRef}>
        {entries.length === 0 && (
          <div className={s.empty}>{running ? 'Waiting for the agent…' : 'No activity.'}</div>
        )}
        {entries.map(e => (
          e.kind === 'step' ? (
            <div key={e.id} className={s.step}>{e.text}</div>
          ) : e.kind === 'thinking' ? (
            <details key={e.id} className={s.thinking} open>
              <summary>🧠 Thinking</summary>
              <pre className={s.pre}>{e.text}</pre>
            </details>
          ) : (
            <pre key={e.id} className={s.pre}>{e.text}</pre>
          )
        ))}
        {running && <span className={s.cursor} />}
      </div>
    </div>
  )
}
