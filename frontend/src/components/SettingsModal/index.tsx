import { useEffect, useState } from 'react'
import { api } from '../../api'
import type { DoctorReport, HardwareInfo, HardwareView, RunConfig, SettingsView, SSHTarget } from '../../types'
import s from './styles.module.css'

interface Props {
  onClose: () => void
}

type Section = 'llm' | 'academic' | 'hardware' | 'experiments' | 'doctor'

const NAV: { id: Section; label: string }[] = [
  { id: 'llm', label: '🔌 LLM' },
  { id: 'academic', label: '📚 Academic search' },
  { id: 'hardware', label: '🖥 Hardware' },
  { id: 'experiments', label: '🧪 Experiments' },
  { id: 'doctor', label: '🩺 Doctor' },
]

const DOCTOR_ICON: Record<string, string> = { ok: '✓', warn: '!', fail: '✗' }

const PROVIDER_DEFAULTS: Record<string, { baseUrl: string; model: string }> = {
  anthropic: { baseUrl: 'https://api.anthropic.com', model: 'claude-opus-4-8' },
  openai: { baseUrl: 'https://api.openai.com/v1', model: '' }
}

function newId(): string {
  return (crypto?.randomUUID?.() ?? `t${Date.now()}${Math.random().toString(16).slice(2)}`).slice(0, 12)
}

function machineSummary(m: HardwareInfo): string {
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

export default function SettingsModal({ onClose }: Props) {
  const [section, setSection] = useState<Section>('llm')

  const [loaded, setLoaded] = useState<SettingsView | null>(null)
  const [provider, setProvider] = useState('anthropic')
  const [baseUrl, setBaseUrl] = useState('')
  const [model, setModel] = useState('')
  const [apiKey, setApiKey] = useState('')
  // Image generation (paper figures)
  const [imageBaseUrl, setImageBaseUrl] = useState('')
  const [imageModel, setImageModel] = useState('')
  const [imageApiKey, setImageApiKey] = useState('')
  // OpenAlex literature search (domain survey / SOTA / references)
  const [openalexApiKey, setOpenalexApiKey] = useState('')
  const [saving, setSaving] = useState(false)
  const [status, setStatus] = useState<{ ok: boolean; text: string } | null>(null)

  // Hardware / environment
  const [hw, setHw] = useState<HardwareView | null>(null)
  const [ssh, setSsh] = useState<SSHTarget[]>([])
  const [detecting, setDetecting] = useState(false)
  const [hwStatus, setHwStatus] = useState<string>('')
  const [run, setRun] = useState<RunConfig | null>(null)
  const [runStatus, setRunStatus] = useState<string>('')

  // Doctor / environment readiness
  const [doctor, setDoctor] = useState<DoctorReport | null>(null)
  const [doctoring, setDoctoring] = useState(false)

  const runDoctor = () => {
    setDoctoring(true)
    api.getDoctor().then(setDoctor).catch(() => {}).finally(() => setDoctoring(false))
  }

  useEffect(() => {
    api.getSettings().then(sv => {
      setLoaded(sv)
      setProvider(sv.provider)
      setBaseUrl(sv.baseUrl ?? '')
      setModel(sv.model)
      setImageBaseUrl(sv.imageBaseUrl ?? '')
      setImageModel(sv.imageModel ?? '')
    }).catch(e => setStatus({ ok: false, text: String(e.message ?? e) }))
    api.getHardware().then(v => { setHw(v); setSsh(v.sshTargets); setRun(v.runConfig) }).catch(() => {})
    runDoctor()
  }, [])

  const onProviderChange = (p: string) => {
    setProvider(p)
    const d = PROVIDER_DEFAULTS[p]
    if (d && !baseUrl) setBaseUrl(d.baseUrl)
    if (d && d.model && (!model || model === PROVIDER_DEFAULTS.anthropic.model)) setModel(d.model)
  }

  const save = async () => {
    setSaving(true)
    setStatus(null)
    try {
      const sv = await api.putSettings({
        provider,
        baseUrl,
        model,
        ...(apiKey.trim() ? { apiKey: apiKey.trim() } : {}),
        imageBaseUrl,
        imageModel,
        ...(imageApiKey.trim() ? { imageApiKey: imageApiKey.trim() } : {}),
        ...(openalexApiKey.trim() ? { openalexApiKey: openalexApiKey.trim() } : {}),
      })
      setLoaded(sv)
      setApiKey('')
      setImageApiKey('')
      setOpenalexApiKey('')
      setStatus({ ok: true, text: 'Saved' })
    } catch (e) {
      setStatus({ ok: false, text: String((e as Error).message ?? e) })
    } finally {
      setSaving(false)
    }
  }

  const updateSsh = (id: string, patch: Partial<SSHTarget>) =>
    setSsh(rows => rows.map(r => (r.id === id ? { ...r, ...patch } : r)))
  const addSsh = () =>
    setSsh(rows => [...rows, { id: newId(), host: '', user: '', port: 22, keyPath: '', label: '' }])
  const removeSsh = (id: string) => setSsh(rows => rows.filter(r => r.id !== id))

  const saveRemotes = async () => {
    setHwStatus('Saving remotes…')
    try {
      const v = await api.putSshTargets(ssh.filter(r => r.host.trim() && r.user.trim()))
      setHw(v); setSsh(v.sshTargets); setHwStatus('Remotes saved')
    } catch (e) { setHwStatus(String((e as Error).message ?? e)) }
  }

  const detect = async () => {
    setDetecting(true)
    setHwStatus('Detecting — probing local' + (ssh.length ? ' + remotes over SSH…' : '…'))
    try {
      await api.putSshTargets(ssh.filter(r => r.host.trim() && r.user.trim()))
      const v = await api.detectHardware()
      setHw(v); setSsh(v.sshTargets); setHwStatus('Detected')
    } catch (e) { setHwStatus(String((e as Error).message ?? e)) }
    finally { setDetecting(false) }
  }

  const saveRun = async () => {
    if (!run) return
    setRunStatus('Saving…')
    try {
      const v = await api.putRunConfig(run)
      setRun(v.runConfig); setRunStatus('Saved')
    } catch (e) { setRunStatus(String((e as Error).message ?? e)) }
  }

  return (
    <div className={s.overlay} onClick={onClose}>
      <div className={s.modal} onClick={e => e.stopPropagation()}>
        <div className={s.header}>
          <span className={s.title}>⚙️ Settings</span>
          <button className={s.closeBtn} onClick={onClose} title="Close">×</button>
        </div>

        <div className={s.layout}>
          <nav className={s.nav}>
            {NAV.map(n => (
              <button
                key={n.id}
                className={`${s.navItem} ${section === n.id ? s.navItemActive : ''}`}
                onClick={() => setSection(n.id)}
              >
                {n.label}
              </button>
            ))}
          </nav>

          <div className={s.content}>
            {section === 'llm' && (
              <>
                <div className={s.field}>
                  <label className={s.label}>Provider</label>
                  <select className={s.select} value={provider} onChange={e => onProviderChange(e.target.value)}>
                    <option value="anthropic">Anthropic</option>
                    <option value="openai">OpenAI-compatible</option>
                  </select>
                </div>

                <div className={s.field}>
                  <label className={s.label}>Base URL</label>
                  <input
                    className={s.input}
                    value={baseUrl}
                    onChange={e => setBaseUrl(e.target.value)}
                    placeholder={PROVIDER_DEFAULTS[provider]?.baseUrl ?? ''}
                  />
                  <span className={s.hint}>Leave as default unless using a proxy or self-hosted endpoint.</span>
                </div>

                <div className={s.field}>
                  <label className={s.label}>Model</label>
                  <input
                    className={s.input}
                    value={model}
                    onChange={e => setModel(e.target.value)}
                    placeholder="e.g. claude-opus-4-8"
                  />
                </div>

                <div className={s.field}>
                  <label className={s.label}>API Key</label>
                  <input
                    className={s.input}
                    type="password"
                    value={apiKey}
                    onChange={e => setApiKey(e.target.value)}
                    placeholder={loaded?.hasKey ? `Saved: ${loaded.apiKeyMasked} — type to replace` : 'sk-…'}
                  />
                  <span className={s.hint}>Stored server-side only; never sent back to the browser.</span>
                </div>

                <div className={s.sectionDivider}>
                  <span className={s.sectionTitle}>🖼️ Image generation (paper figures)</span>
                </div>
                <span className={s.hint} style={{ marginBottom: 6 }}>
                  Optional OpenAI-style images API used to generate the paper's introduction &amp; method
                  figures. Leave the key empty to disable (figures fall back to matplotlib diagrams).
                </span>
                <div className={s.field}>
                  <label className={s.label}>Image API base URL</label>
                  <input
                    className={s.input}
                    value={imageBaseUrl}
                    onChange={e => setImageBaseUrl(e.target.value)}
                    placeholder="https://api.openai.com/v1 (default)"
                  />
                </div>
                <div className={s.field}>
                  <label className={s.label}>Image model</label>
                  <input
                    className={s.input}
                    value={imageModel}
                    onChange={e => setImageModel(e.target.value)}
                    placeholder="e.g. gpt-image-1, dall-e-3"
                  />
                </div>
                <div className={s.field}>
                  <label className={s.label}>Image API key</label>
                  <input
                    className={s.input}
                    type="password"
                    value={imageApiKey}
                    onChange={e => setImageApiKey(e.target.value)}
                    placeholder={loaded?.hasImageKey ? `Saved: ${loaded.imageKeyMasked} — type to replace` : 'sk-…'}
                  />
                </div>

                <div className={s.sshActions}>
                  <button className={`${s.btn} ${s.btnSmall} ${s.btnPrimary}`} disabled={saving} onClick={save}>
                    {saving ? 'Saving…' : 'Save'}
                  </button>
                  {status && (
                    <span className={`${s.hint} ${status.ok ? s.statusOk : s.statusErr}`}>{status.text}</span>
                  )}
                </div>
              </>
            )}

            {section === 'academic' && (
              <>
                <div className={s.sectionDivider}>
                  <span className={s.sectionTitle}>📚 OpenAlex (literature search)</span>
                </div>
                <span className={s.hint} style={{ marginBottom: 6 }}>
                  Powers the domain survey, SOTA papers, and references. OpenAlex now budget-limits
                  anonymous (per-IP) requests — without a key you may see “Found 0 papers (rate-limited)”.
                  Add a free API key (openalex.org) for a dedicated budget.
                </span>
                <div className={s.field}>
                  <label className={s.label}>OpenAlex API key</label>
                  <input
                    className={s.input}
                    type="password"
                    value={openalexApiKey}
                    onChange={e => setOpenalexApiKey(e.target.value)}
                    placeholder={loaded?.hasOpenalexKey ? `Saved: ${loaded.openalexKeyMasked} — type to replace` : 'get one at openalex.org'}
                  />
                  <span className={s.hint}>Stored server-side only; never sent back to the browser.</span>
                </div>

                <div className={s.sshActions}>
                  <button className={`${s.btn} ${s.btnSmall} ${s.btnPrimary}`} disabled={saving} onClick={save}>
                    {saving ? 'Saving…' : 'Save'}
                  </button>
                  {status && (
                    <span className={`${s.hint} ${status.ok ? s.statusOk : s.statusErr}`}>{status.text}</span>
                  )}
                </div>
              </>
            )}

            {section === 'hardware' && (
              <>
                <div className={s.sectionDivider}>
                  <span className={s.sectionTitle}>Hardware &amp; Environment</span>
                  <button className={`${s.btn} ${s.btnSmall} ${s.btnPrimary}`} disabled={detecting} onClick={detect}>
                    {detecting ? 'Detecting…' : 'Detect now'}
                  </button>
                </div>
                <span className={s.hint}>
                  “Local” is the machine running the backend (your computer in CLI local mode).
                  Add SSH remotes to detect GPU boxes you’ll run experiments on.
                </span>

                {hw && hw.machines.length > 0 && (
                  <div className={s.hwSummary}>
                    {hw.machines.map(m => (
                      <div key={m.label} className={s.hwMachine}>
                        <span className={s.hwMachineName}>{m.scope === 'local' ? '💻' : '🌐'} {m.label}</span>
                        <span className={`${s.hwMeta} ${!m.reachable ? s.hwMetaErr : ''}`}>{machineSummary(m)}</span>
                      </div>
                    ))}
                  </div>
                )}

                {ssh.map(r => (
                  <div key={r.id} className={s.sshRow}>
                    <div className={s.sshInputs}>
                      <input className={s.miniInput} value={r.user} placeholder="user"
                        onChange={e => updateSsh(r.id, { user: e.target.value })} />
                      <span className={s.sshAt}>@</span>
                      <input className={s.miniInput} value={r.host} placeholder="host / ip"
                        onChange={e => updateSsh(r.id, { host: e.target.value })} />
                      <input className={`${s.miniInput} ${s.sshPort}`} value={r.port} type="number" placeholder="22"
                        onChange={e => updateSsh(r.id, { port: parseInt(e.target.value || '22', 10) })} />
                      <button className={s.iconBtn} title="Remove" onClick={() => removeSsh(r.id)}>×</button>
                    </div>
                    <input className={s.miniInput} value={r.keyPath ?? ''} placeholder="SSH key path (e.g. ~/.ssh/id_ed25519)"
                      onChange={e => updateSsh(r.id, { keyPath: e.target.value })} />
                  </div>
                ))}

                <div className={s.sshActions}>
                  <button className={`${s.btn} ${s.btnSmall} ${s.btnGhost}`} onClick={addSsh}>+ Add SSH remote</button>
                  {ssh.length > 0 && (
                    <button className={`${s.btn} ${s.btnSmall} ${s.btnGhost}`} onClick={saveRemotes}>Save remotes</button>
                  )}
                  {hwStatus && <span className={s.hint}>{hwStatus}</span>}
                </div>
              </>
            )}

            {section === 'experiments' && (
              run ? (
                <>
                  <div className={s.field}>
                    <label className={s.label}>Mode</label>
                    <select
                      className={s.select}
                      value={run.experimentMode}
                      onChange={e => setRun({ ...run, experimentMode: e.target.value as RunConfig['experimentMode'] })}
                    >
                      <option value="cli">CLI agent — delegate to a headless coding agent (default)</option>
                      <option value="executed">Executed — run real generated code locally</option>
                      <option value="ssh">SSH — run on a remote GPU box (beta)</option>
                      <option value="simulated">Simulated — LLM-narrated, NOT real results</option>
                    </select>
                    <span className={s.hint}>
                      {run.experimentMode === 'executed'
                        ? '⚠ Runs model-written Python as a subprocess on the backend host (no timeout — long runs allowed). Use on a trusted, self-hosted box.'
                        : run.experimentMode === 'ssh'
                        ? '🧪 BETA — runs the agentic coding agent over SSH: it sets up the env and runs everything with bash on the selected remote (code is authored locally; results/figures are pulled back). Needs only the SSH remote — no interpreter or other settings.'
                        : run.experimentMode === 'cli'
                        ? '✓ Default. Shells out to an external headless coding-agent CLI (claude / opencode / openhands) in the experiment dir and streams its output. The CLI uses its own auth/model.'
                        : '⚠ NOT real data — the model NARRATES plausible (fabricated) numbers; nothing runs. Avoid for real research — use only for a quick demo.'}
                    </span>
                  </div>

                  {run.experimentMode === 'cli' && (
                    <div className={s.field}>
                      <label className={s.label}>Agent command</label>
                      <input
                        className={s.input}
                        value={run.agentCommand ?? ''}
                        placeholder="claude -p {prompt} --dangerously-skip-permissions"
                        onChange={e => setRun({ ...run, agentCommand: e.target.value })}
                      />
                      <span className={s.hint}>
                        Headless command run in the experiment dir. Placeholders: <code>{'{prompt}'}</code> (shell-quoted
                        task), <code>{'{task_file}'}</code> (task.md), <code>{'{dir}'}</code>. Examples:{' '}
                        <code>opencode run {'{prompt}'}</code> · <code>claude -p {'{prompt}'} --dangerously-skip-permissions</code>.
                        It must write <code>results.json</code> in the working dir. For <code>claude -p</code> the runner
                        adds <code>--output-format stream-json --verbose</code> automatically so its steps stream live.
                      </span>
                    </div>
                  )}

                  {run.experimentMode === 'ssh' && (
                    <div className={s.field}>
                      <label className={s.label}>SSH remote</label>
                      <select
                        className={s.select}
                        value={run.sshTargetId ?? ''}
                        onChange={e => setRun({ ...run, sshTargetId: e.target.value || null })}
                      >
                        <option value="">First configured remote</option>
                        {(hw?.sshTargets ?? []).map(t => (
                          <option key={t.id} value={t.id}>{t.label || `${t.user}@${t.host}`}</option>
                        ))}
                      </select>
                      {!(hw?.sshTargets ?? []).length && (
                        <span className={s.hint}>No SSH remotes yet — add one in the Hardware section above.</span>
                      )}
                    </div>
                  )}

                  {run.experimentMode === 'executed' && (
                    <div className={s.field}>
                      <label className={s.label}>Python interpreter</label>
                      <input
                        className={s.input}
                        value={run.pythonPath ?? ''}
                        placeholder="(backend default interpreter)"
                        onChange={e => setRun({ ...run, pythonPath: e.target.value })}
                      />
                      <span className={s.hint}>Path to a python/conda env with your research libraries (torch, numpy…).</span>
                    </div>
                  )}

                  <div className={s.sshActions}>
                    <button className={`${s.btn} ${s.btnSmall} ${s.btnPrimary}`} onClick={saveRun}>Save execution settings</button>
                    {runStatus && <span className={s.hint}>{runStatus}</span>}
                  </div>
                </>
              ) : (
                <span className={s.hint}>Loading…</span>
              )
            )}

            {section === 'doctor' && (
              <>
                <div className={s.sectionDivider}>
                  <span className={s.sectionTitle}>🩺 Environment readiness</span>
                </div>
                <span className={s.hint} style={{ marginBottom: 8 }}>
                  Checks the backend host: LLM config, chat agent, the LaTeX toolchain used
                  to compile papers, and image generation.
                </span>
                {doctor ? (
                  <>
                    <div className={s.doctorBanner} data-ok={doctor.ok}>
                      {doctor.ok ? '✓ Environment ready' : '✗ Environment not ready — fix the failing checks'}
                    </div>
                    <div className={s.doctorList}>
                      {doctor.checks.map(c => (
                        <div key={c.key} className={s.doctorRow} data-status={c.status}>
                          <span className={s.doctorIcon}>{DOCTOR_ICON[c.status] ?? '·'}</span>
                          <div className={s.doctorBody}>
                            <span className={s.doctorLabel}>{c.label}</span>
                            <span className={s.doctorDetail}>{c.detail}</span>
                            {c.hint && c.status !== 'ok' && (
                              <span className={s.doctorHint}>→ {c.hint}</span>
                            )}
                          </div>
                        </div>
                      ))}
                    </div>
                  </>
                ) : (
                  <span className={s.hint}>{doctoring ? 'Checking…' : 'No report yet.'}</span>
                )}
                <div className={s.sshActions}>
                  <button className={`${s.btn} ${s.btnSmall} ${s.btnGhost}`} onClick={runDoctor} disabled={doctoring}>
                    {doctoring ? 'Checking…' : '↻ Re-check'}
                  </button>
                </div>
              </>
            )}
          </div>
        </div>

        <div className={s.footer}>
          <button className={`${s.btn} ${s.btnGhost}`} onClick={onClose}>Close</button>
        </div>
      </div>
    </div>
  )
}
