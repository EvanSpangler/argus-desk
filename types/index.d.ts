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

export type DeskView = {
  items: DeskItem[]
  queue: QueueRow[]
  selected?: string
  mode: 'list' | 'deny' | 'modify'
  notice?: string
  poll: 'on' | 'paused' | 'off'
  lastPoll?: number
  pollError?: string
}

declare module 'claude-code' {
  interface PluginState {
    'argus-desk': { view: DeskView }
  }
}
