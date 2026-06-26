export interface Domain {
  id: string
  name: string
  createdAt: number
  isSelected: boolean
  codebaseUrl?: string | null
  codebaseFiles?: number
}

export interface WritingStyle {
  name: string
  scope: 'global' | 'domain'
  title: string
}

export interface Benchmark {
  name: string
  scope: 'global' | 'domain'
  title: string
}

export type ExperimentJobStatus = 'running' | 'done' | 'error' | 'cancelled' | 'interrupted'

export interface ExperimentJob {
  jobId: string
  ideaId: string
  ideaTitle: string
  hypothesisId: string
  status: ExperimentJobStatus
  startedAt: number
  updatedAt: number
  error?: string | null
}

// Live status of an `paperclaw auto` run (mirrors models.py AutoRunStatus).
export interface AutoRunStatus {
  topic: string
  status: 'running' | 'done' | 'error' | 'stopped' | 'interrupted'
  phase: string  // doctor | domain | idea | hypotheses | paper | done
  label: string
  ideaId?: string | null
  ideaTitle?: string | null
  domainId?: string | null
  domainName?: string | null
  round: number
  positives: number
  targetPositive: number
  maxHypotheses: number
  pageLimit: number
  maxDepth: number
  currentHypothesisId?: string | null
  paperReady: boolean
  startedAt: number
  updatedAt: number
  error?: string | null
}

export interface DomainSpec {
  domainId: string
  content: string
}

export interface Seed {
  id: string
  text: string
  createdAt: number
  draft?: string | null
}

export interface Idea {
  id: string
  title: string
  description?: string
  createdAt: number
  isActive: boolean
  color?: string | null   // sidebar flag: 'green' | 'yellow' | 'grey' | null
}

export interface IdeaSpec {
  ideaId: string
  content: string
}

// The domains an idea is connected to (an idea may connect to several).
export interface IdeaDomains {
  ideaId: string
  domainIds: string[]
}

// An idea's allocated experiment resources (compute + the active LLM, read-only).
export interface IdeaResources {
  ideaId: string
  experimentMode: 'simulated' | 'executed' | 'ssh' | 'cli' | null
  sshTargetId: string | null
  useReferenceCodebase: boolean
  sshTargets: SSHTarget[]
  llmProvider: string | null
  llmModel: string | null
  llmBaseUrl: string | null
  llmKeyConfigured: boolean
}
export interface IdeaResourcesUpdate {
  experimentMode?: string | null
  sshTargetId?: string | null
  useReferenceCodebase?: boolean
}

export interface PaperContent {
  ideaId: string
  content: string | null
  hasPdf: boolean
  paperFile?: string | null    // shown version's filename, e.g. paper_v2.pdf
  versionCount?: number
  versions?: number[]          // all available versions, e.g. [1, 2, 3]
}

export type MessageRole = 'user' | 'assistant' | 'system'
export type MessageStatus = 'sending' | 'sent' | 'streaming' | 'error'

export interface ChatContext {
  contextId: string
  kind: 'scratch' | 'domain' | 'seed' | 'idea'
  title: string
  messageCount: number
  lastTimestamp: number
}

export interface ChatQuestion {
  prompt: string
  options: string[]
  allowFreeText?: boolean
}

// An assistant reply is a CHRONOLOGICAL timeline of narration + tool calls
// (interleaved in arrival order, like Claude) — UI-only, not persisted.
export type MessagePart =
  | { kind: 'text'; text: string }
  | { kind: 'tool'; name: string; arg?: string; detail?: string }

// The agent's write_todos plan/checklist (deepagents), shown live and checked off.
export interface TodoItem { content: string; status: 'pending' | 'in_progress' | 'completed' }

export interface Message {
  id: string
  role: MessageRole
  content: string
  timestamp: number
  status?: MessageStatus
  statusMessage?: string | null
  specUpdated?: boolean
  mapUpdated?: boolean          // hypothesis map regenerated (refresh the map view)
  paperUpdated?: boolean        // paper.md (re)written (refresh the Paper tab)
  codebaseUpdated?: boolean     // domain reference codebase changed (refresh domains)
  servedModel?: string | null
  createdIdeaId?: string | null
  createdDomainId?: string | null
  question?: ChatQuestion | null
  thinking?: string | null     // streamed reasoning (persisted with the message)
  parts?: MessagePart[]        // interleaved text + tool timeline (persisted when there were tool calls)
  todos?: TodoItem[]           // the agent's plan/checklist (persisted with the message)
}

export type ResourceType = 'paper' | 'link' | 'dataset' | 'code'

export interface Resource {
  id: string
  type: ResourceType
  title: string
  authors?: string[]
  year?: number
  url?: string
  venue?: string
  summary?: string
  relevance?: number
}

export interface Skill {
  command: string
  description: string
  requiresIdea?: boolean
}

export interface IdeaLocation {
  ideaId: string
  path: string
  opened: boolean
}

export interface SettingsView {
  provider: string
  baseUrl?: string | null
  model: string
  apiKeyMasked: string
  hasKey: boolean
  imageBaseUrl?: string | null
  imageModel?: string | null
  imageKeyMasked?: string
  hasImageKey?: boolean
  openalexKeyMasked?: string
  hasOpenalexKey?: boolean
}

export interface SettingsUpdate {
  provider?: string
  baseUrl?: string
  model?: string
  apiKey?: string
  imageBaseUrl?: string
  imageModel?: string
  imageApiKey?: string
  openalexApiKey?: string
}

export interface EnvCheck {
  key: string
  label: string
  status: 'ok' | 'warn' | 'fail'
  detail: string
  hint?: string | null
}

export interface DoctorReport {
  ok: boolean
  checks: EnvCheck[]
}

// ── Hardware / environment (mirror paperclaw/server/models.py) ───────────
export interface SSHTarget {
  id: string
  host: string
  user: string
  port: number
  keyPath?: string | null
  label?: string | null
}

export interface GpuInfo {
  name: string
  memoryTotalMb?: number | null
}

export interface DiskInfo {
  name: string
  model?: string | null
  sizeGb?: number | null
  kind: string // SSD | HDD | NVMe | unknown
  transport?: string | null
}

export interface HardwareInfo {
  scope: 'local' | 'remote'
  label: string
  reachable: boolean
  error?: string | null
  os?: string | null
  cpuModel?: string | null
  cpuCores?: number | null
  cpuThreads?: number | null
  memTotalGb?: number | null
  gpus: GpuInfo[]
  disks: DiskInfo[]
  detectedAt: number
}

export interface RunConfig {
  experimentMode: 'simulated' | 'executed' | 'ssh' | 'cli'
  pythonPath?: string | null
  sshTargetId?: string | null
  agentCommand?: string | null  // cli mode: external headless agent command template
  maxAttempts: number
}

export interface HardwareView {
  machines: HardwareInfo[]
  sshTargets: SSHTarget[]
  runConfig: RunConfig
  markdown?: string | null
  updatedAt: number
}

// ── References / ref.bib (mirror paperclaw/server/models.py) ─────────────
export interface Reference {
  key: string
  type: string
  title: string
  authors: string[]
  year?: number | null
  doi?: string | null
  venue?: string | null
}

export type ReferenceStatus = 'VERIFIED' | 'MISMATCH' | 'NOT_FOUND' | 'UNKNOWN'

export interface ReferenceValidation {
  key: string
  status: ReferenceStatus
  detail: string
}

export interface ReferencesView {
  ideaId: string
  entries: Reference[]
  bibtex: string
}

export interface HypothesisNode {
  id: string
  statement: string
  rationale?: string | null
  test?: string | null
  status: string  // untested | supported | refuted | inconclusive | blocked
  stage?: string | null  // derived progress: untested | planned | experiment | <status>
  children: HypothesisNode[]
}

export interface HypothesisMap {
  ideaId: string
  nodes: HypothesisNode[]
  generatedAt: number
}

export interface HypothesisDetail {
  ideaId: string
  hypothesisId: string
  status: string
  plan?: string | null
  code?: string | null        // phase 1: generated run.py (executed mode)
  experiment?: string | null  // phase 2: run results
  report?: string | null
  log?: string | null
  figures: string[]
}

export interface WorkspaceEntry {
  path: string   // relative to the idea root, posix-style
  size: number
  isDir: boolean
}

export interface WorkspaceListing {
  ideaId: string
  root: string
  entries: WorkspaceEntry[]
}

export type ActivePanel = 'brainstorm' | 'ideas'
export type ResourceTab = 'all' | 'papers' | 'links' | 'datasets' | 'code'
export type RightTab = 'spec' | 'hypothesis' | 'references' | 'resources' | 'paper'

// Brainstorm generation settings — keys mirror paperclaw/prompts/ideas.py
// (BRAINSTORM_IDEA_TYPES / BRAINSTORM_EMPHASIS). Empty arrays = no constraint.
export type BrainstormIdeaType = 'application' | 'algorithm' | 'analysis' | 'benchmark'
export type BrainstormEmphasis = 'performance' | 'efficiency' | 'robustness' | 'interpretability'

export interface BrainstormOptions {
  ideaTypes: BrainstormIdeaType[]
  emphasis: BrainstormEmphasis[]
  count: number
}
