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

export type DeskView = {
  items: DeskItem[]
  selected?: string
  mode: 'list' | 'deny' | 'modify'
  notice?: string
}

declare module 'claude-code' {
  interface PluginState {
    'argus-desk': { view: DeskView }
  }
}
