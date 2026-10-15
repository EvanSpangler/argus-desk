export type DeskChecks = {
  verdict: 'hold' | 'clear'
  hits: string[]
  warnings: string[]
  lint?: string[]
  thread?: { status: string; messages?: number; console_count?: number; capture?: string[]; mechanical?: string[]; open_questions?: string[] }
  claims?: { checkable: number; credited: number; uncovered: number; echo: number; hits: string[]; shadow: boolean }
}

export type DeskItem = {
  md5: string
  ticket: string
  state: 'pending' | 'open' | 'held'
  source?: string
  filed: number
  age_s: number
  chars?: number
  checks: DeskChecks
  text: string
  supersedes?: string | null
}

export type QueueRow = {
  ticket: string
  state: 'new' | 'reopened' | 'drafting' | 'waiting' | 'stale' | 'sent' | 'done' | 'gone' | 'skipped'
  sla?: string | null
  subject?: string
  age_s: number
  status?: string
}

export type WallAgent = {
  id: string
  ticket: string
  role: string
  label: string
  status: string
  started: number
  last: string
  lines: string[]
}

export type RunAgent = { id: string; agentId?: string; role: string; label: string; model?: string; status: string; params?: string; summary?: string; started: number; ended?: number }
export type Finding = { id: string; agent: string; role: string; kind: string; text: string; receipts?: string[]; about?: string | null; ts?: number }
export type Receipt = { id: string; kind: string; ticket: string; quote: string; source?: string; bound?: string[]; minted?: string; page_origin?: 'fetch' | 'search' }
export type Claim = { text: string; classes?: string[]; receipts?: string[]; problems?: string[]; echo?: boolean }

export type ThreadMessage = { id?: string; who: string; at?: number; text: string }

export type Trace = {
  ok: boolean
  run: { ticket: string; phase: string; rounds?: number; agents?: RunAgent[]; started?: number }
  findings?: Finding[]
  receipts?: Receipt[]
  events?: Array<{ agent: string; kind: string; text: string; ts?: number }>
  claims?: Claim[]
  drafts?: DeskItem[]
  thread?: { id?: string; source?: string; fetched_at?: number; messages: ThreadMessage[] }
  thread_count?: number
}

export type DeskView = {
  items: DeskItem[]
  queue: QueueRow[]
  wall: WallAgent[]
  selected?: string
  ticket?: string
  trace?: Trace
  mode: 'list' | 'deny' | 'modify'
  notice?: string
  poll: 'on' | 'paused' | 'off'
  lastPoll?: number
  pollError?: string
  lastResult?: string
  /** The filter text per view, applied on Enter. */
  filter?: { wall?: string; queue?: string; ticket?: string; decide?: string }
  /** The agent whose tail the Wall shows, instead of the list. */
  tail?: string
  /** The Ticket view's folded sections. */
  folded?: string[]
}

declare module 'claude-code' {
  interface PluginState {
    'argus-desk': { view: DeskView }
  }
}
