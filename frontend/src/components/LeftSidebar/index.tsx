import { useState } from 'react'
import DomainsBar from './DomainsBar'
import BrainstormBar from './BrainstormBar'
import IdeasBar from './IdeasBar'
import type { BrainstormOptions, Domain, Idea, Seed } from '../../types'
import s from './styles.module.css'

interface Props {
  domains: Domain[]
  seeds: Seed[]
  ideas: Idea[]
  streamingContexts: Set<string>  // context ids with a live chat stream
  runningAutoIdeas: Set<string>   // idea ids with a live auto run
  generating: boolean
  generateStatus: string | null
  autoCreatingDomain: boolean
  autoCreateStatus: string | null
  activeSeedId: string | null
  viewDomainId: string | null
  onAutoDomain: (prompt: string) => void
  onStartDomainWizard: () => void
  onSelectDomain: (id: string, selected: boolean) => void
  onViewDomain: (id: string) => void
  onRemoveDomain: (id: string) => void
  onSetDomainCodebase: (id: string, url: string) => void
  onAddSeed: (text: string) => void
  onRemoveSeed: (id: string) => void
  onGenerateSeeds: () => void
  brainstormOpts: BrainstormOptions
  onBrainstormOptsChange: (opts: BrainstormOptions) => void
  onOpenSeed: (seed: Seed) => void
  onPromoteSeed: (seed: Seed) => void
  onAddIdea: (title: string) => void
  onSetActiveIdea: (id: string) => void
  onRemoveIdea: (id: string) => void
  onRevealIdea: (id: string) => void
  onDuplicateIdea: (id: string) => void
  onSetIdeaColor: (id: string, color: string | null) => void
  onOpenSettings: () => void
  onOpenAutoLauncher?: () => void
  theme: 'dark' | 'light'
  onToggleTheme: () => void
}

export default function LeftSidebar({
  domains, seeds, ideas, streamingContexts, runningAutoIdeas, generating, generateStatus, autoCreatingDomain, autoCreateStatus,
  activeSeedId, viewDomainId,
  onAutoDomain, onStartDomainWizard, onSelectDomain, onViewDomain, onRemoveDomain, onSetDomainCodebase,
  onAddSeed, onRemoveSeed, onGenerateSeeds, brainstormOpts, onBrainstormOptsChange, onOpenSeed, onPromoteSeed,
  onAddIdea, onSetActiveIdea, onRemoveIdea, onRevealIdea, onDuplicateIdea, onSetIdeaColor,
  onOpenSettings, onOpenAutoLauncher, theme, onToggleTheme
}: Props) {
  const [domainsOpen, setDomainsOpen] = useState(true)
  const [brainstormOpen, setBrainstormOpen] = useState(true)
  const [ideasOpen, setIdeasOpen] = useState(true)

  return (
    <aside className={s.sidebar}>
      <div className={s.header}>
        <img className={s.logo} src="/logo.png" alt="" />
        <span className={s.appName}>PaperClaw</span>
      </div>

      {onOpenAutoLauncher && (
        <button className={s.autoRunBtn} onClick={onOpenAutoLauncher}
                title="Run the whole pipeline from a topic: domain → idea → hypotheses → paper">
          ⚡ Auto run
        </button>
      )}

      <div className={s.panels}>
        <DomainsBar
          domains={domains}
          streamingContexts={streamingContexts}
          isOpen={domainsOpen}
          autoCreating={autoCreatingDomain}
          autoCreateStatus={autoCreateStatus}
          viewDomainId={viewDomainId}
          onToggle={() => setDomainsOpen(v => !v)}
          onAuto={onAutoDomain}
          onStartWizard={onStartDomainWizard}
          onSelect={onSelectDomain}
          onView={onViewDomain}
          onRemove={onRemoveDomain}
          onSetCodebase={onSetDomainCodebase}
        />
        <BrainstormBar
          seeds={seeds}
          streamingContexts={streamingContexts}
          isOpen={brainstormOpen}
          generating={generating}
          generateStatus={generateStatus}
          activeSeedId={activeSeedId}
          onToggle={() => setBrainstormOpen(v => !v)}
          onAdd={onAddSeed}
          onRemove={onRemoveSeed}
          onGenerate={onGenerateSeeds}
          opts={brainstormOpts}
          onOptsChange={onBrainstormOptsChange}
          onOpen={onOpenSeed}
          onPromote={onPromoteSeed}
        />
        <IdeasBar
          ideas={ideas}
          streamingContexts={streamingContexts}
          runningAutoIdeas={runningAutoIdeas}
          isOpen={ideasOpen}
          onToggle={() => setIdeasOpen(v => !v)}
          onAdd={onAddIdea}
          onSetActive={onSetActiveIdea}
          onRemove={onRemoveIdea}
          onReveal={onRevealIdea}
          onDuplicate={onDuplicateIdea}
          onSetColor={onSetIdeaColor}
        />
      </div>

      <div className={s.footer}>
        <button className={s.footerBtn} onClick={onOpenSettings} title="LLM settings">
          ⚙️ <span>Settings</span>
        </button>
        <button
          className={s.footerIconBtn}
          onClick={onToggleTheme}
          title={theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}
        >
          {theme === 'dark' ? '☀️' : '🌙'}
        </button>
      </div>
    </aside>
  )
}
