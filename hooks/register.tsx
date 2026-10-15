import { atom, read, update } from 'claude-code'
import type { Register } from 'claude-code'

import type { DeskItem, DeskView } from '../types'

// argus-desk: the review desk. Three controls, each mechanical:
//   1. file_draft   a tool the model calls with the reply it wants to send; the desk checks it and files it.
//   2. the pane     the operator reads the draft with its checks and presses Send, Modify or Deny.
//   3. the gate     any outward tool call must carry text whose md5 the operator granted; one grant, one send.
// State and checks live in checks/desk.py (stdlib Python), one process per call, so the rules are the
// tested reference implementations and the mod stays a thin binding.

const PANE = 'argus-desk'
const TOOL = 'file_draft'
const view = atom({ plugin: 'argus-desk', key: 'view' } as const, { items: [], mode: 'list' } as DeskView)

type Opts = { stateDir: string; python: string; outwardTool: string }

const opts = (o: Record<string, unknown>): Opts => ({
  stateDir: String(o.stateDir ?? '~/.argus-desk'),
  python: String(o.python ?? 'python3'),
  outwardTool: String(o.outwardTool ?? 'invoke_proxy'),
})

// invoke_proxy as Argus shows it (2026-10-15): required proxy (an enum of 49 services) and endpoint (a name such
// as zendesk_get_ticket, not a path), optional arguments (object or JSON string). There is no method field, so
// the endpoint name is the only sign of a read. Anything not plainly a read is outward, on every proxy.
const READ_ENDPOINT = /^[a-z0-9]+_(get|list|search|read|show|fetch|count|describe)(_|$)/i

const RESERVED = new Set(['tool', 'tool_use_id', 'agentId', 'consent', 'requestMeta'])

/** Every string an outward call carries, nested or not: the candidates for a granted md5. */
const strings = (v: unknown, out: string[] = [], depth = 0): string[] => {
  if (depth > 6) return out
  if (typeof v === 'string') {
    if (v.length >= 20) out.push(v)
  } else if (Array.isArray(v)) {
    for (const x of v) strings(x, out, depth + 1)
  } else if (v && typeof v === 'object') {
    for (const [k, x] of Object.entries(v)) if (!RESERVED.has(k)) strings(x, out, depth + 1)
  }
  return out
}

/** arguments may arrive as a JSON string; parse it so the reply text and the ticket id are visible. */
const normalize = (args: Record<string, unknown>): Record<string, unknown> => {
  if (typeof args.arguments !== 'string') return args
  try {
    return { ...args, arguments: JSON.parse(args.arguments) as unknown }
  } catch {
    return args
  }
}

/**
 * A call that can write: any endpoint whose name is not plainly a read, on any proxy. Fails closed: no endpoint,
 * or a write method or body in the old path-and-method shape, is outward too.
 */
const isOutward = (args: Record<string, unknown>): boolean => {
  const endpoint = typeof args.endpoint === 'string' ? args.endpoint : ''
  if (!endpoint || !READ_ENDPOINT.test(endpoint)) return true
  const flat = JSON.stringify(args).toLowerCase()
  return /"(?:method|verb|http_method)"\s*:\s*"(post|put|patch|delete)"/.test(flat)
}

const ticketOf = (args: Record<string, unknown>): string | undefined => {
  const flat = JSON.stringify(args)
  const m = flat.match(/tickets?\/(\d{4,})/) ?? flat.match(/"ticket(?:_id|Id)?"\s*:\s*"?(\d{4,})/)
  return m?.[1]
}


async function desk($: any, o: Opts, args: string[], stdin?: string): Promise<Record<string, unknown> & { ok?: unknown }> {
  const r = await $.process.run([o.python, '-I', `${$.plugin.root}/checks/desk.py`, '--root', o.stateDir, ...args], { stdin, timeoutMs: 60_000 })
  try {
    return { ...(JSON.parse(r.stdout) as Record<string, unknown>), exitCode: r.exitCode }
  } catch {
    return { ok: false, reason: `desk.py exit ${r.exitCode}: ${(r.stderr || r.stdout).slice(0, 300)}`, exitCode: r.exitCode }
  }
}

async function refresh($: any, o: Opts, notice?: string): Promise<DeskItem[]> {
  const r = await desk($, o, ['list'])
  const items = (r.items as DeskItem[] | undefined) ?? []
  await update($, view, v => ({ ...v, items, notice: notice ?? (r.ok ? undefined : String(r.reason)) }))
  try {
    $.ui.status(items.length ? `desk: ${items.length} waiting` : undefined)
  } catch {
    /* no status line on this surface */
  }
  return items
}

async function decide($: any, o: Opts, md5: string, decision: 'send' | 'deny' | 'modify', note: string, pressId: string): Promise<void> {
  const item = (await read($, view)).items.find(i => i.md5 === md5)
  if (!item) return
  if (decision === 'send') {
    const r = await desk($, o, ['grant', '--md5', md5, '--press-id', pressId])
    if (!r.ok) {
      await refresh($, o, `send refused: ${String(r.reason)}`)
      return
    }
    await refresh($, o, `granted ${md5.slice(0, 8)}`)
    void $.prompt.submit({ text: `The operator approved draft ${md5} for #${item.ticket}. Post exactly that text now, unchanged, with the posting tool.` }).catch(() => undefined)
  } else if (decision === 'deny') {
    await desk($, o, ['deny', '--md5', md5, '--reason', note])
    await refresh($, o, `denied ${md5.slice(0, 8)}`)
    void $.prompt.submit({ text: `The operator denied draft ${md5} for #${item.ticket}${note ? `: ${note}` : ''}. Do not post it. Decide what the ticket needs next and say so; file any new reply with ${TOOL}.` }).catch(() => undefined)
  } else {
    await desk($, o, ['modify', '--md5', md5, '--note', note])
    await refresh($, o, `returned ${md5.slice(0, 8)}`)
    void $.prompt.submit({ text: `The operator sent draft ${md5} for #${item.ticket} back with this note: ${note || '(no note)'}. Revise it and file the new text with ${TOOL}, supersedes=${md5}.` }).catch(() => undefined)
  }
  await update($, view, v => ({ ...v, mode: 'list', selected: undefined }))
}

export const register: Register = (on, options) => {
  const o = opts(options as Record<string, unknown>)

  on('session.start', async ($, e, next) => {
    await $.command.register({ name: 'desk', description: 'Open the review desk (drafts waiting for Send, Modify or Deny); /desk stats for the numbers', argumentHint: '[stats]' })
    await $.tool.register({
      name: TOOL,
      description:
        'File a customer-facing reply for the operator to review before it is posted. Required before any outward post: ' +
        'the posting tool refuses text the operator has not granted. Give the exact text that would be sent. ' +
        'When revising a draft the operator sent back, pass supersedes with the md5 you were given.',
      inputSchema: {
        type: 'object',
        properties: {
          ticket: { type: 'string', description: 'The ticket id the reply goes to' },
          draft: { type: 'string', description: 'The exact reply text that would be posted' },
          supersedes: { type: 'string', description: 'md5 of the draft this one replaces, when revising' },
        },
        required: ['ticket', 'draft'],
      },
      isDeferred: false,
    })
    if (e.isInteractive) {
      await refresh($, o)
      if (e.surface === 'terminal') void $.ui.open({ id: PANE, title: 'Desk' })
    }
    return next(e)
  })

  on('command.run', { command: 'desk' }, async ($, e) => {
    if (/^\s*stats\b/.test(e.args ?? '')) {
      const r = await $.process.run([o.python, '-I', `${$.plugin.root}/checks/desk.py`, '--root', o.stateDir, 'stats', '--text'], { timeoutMs: 60_000 })
      return { text: r.exitCode === 0 ? r.stdout.trim() : `desk.py failed: ${r.stderr.slice(0, 300)}` }
    }
    const items = await refresh($, o)
    await $.ui.open({ id: PANE, title: 'Desk', focus: true })
    return { text: items.length ? `${items.length} draft(s) waiting at the desk.` : 'Nothing waiting at the desk.' }
  })

  // 1. filing: the model hands over the exact text; the desk checks and records it.
  on('tool.call', { tool: 'mcp__argus-desk__file_draft' }, async ($, e) => {
    const a = e as unknown as { ticket?: string; draft?: string; supersedes?: string }
    const r = await desk($, o, ['file'], JSON.stringify({ ticket: String(a.ticket ?? ''), draft: String(a.draft ?? ''), supersedes: a.supersedes, source: 'argus' }))
    await refresh($, o)
    if (!r.ok) return { result: `Not filed: ${String(r.reason)}${r.md5 ? ` (md5 ${String(r.md5)})` : ''}` }
    const hits = (r.hits as string[]) ?? []
    const warn = (r.warnings as string[]) ?? []
    const claims = r.claims as { checkable: number; credited: number } | undefined
    return {
      result:
        `Filed draft ${String(r.md5)} for #${String(r.ticket)}. Checks: ${String(r.verdict)}` +
        (hits.length ? ` (${hits.join(', ')})` : '') +
        (warn.length ? `; warnings: ${warn.join(', ')}` : '') +
        (claims ? `; claims: ${claims.credited} of ${claims.checkable} receipted` : '') +
        '. It is waiting for the operator at the desk. Do not post it until you are told it was approved; ' +
        'if it comes back with a note, revise and file again with supersedes set to this md5.',
    }
  }).catch(() => ({ deny: 'argus-desk: filing failed; nothing was recorded, try again' }))

  // 2. the gate: an outward call passes only by spending an open grant on the exact text it carries.
  on('tool.call', async ($, e, next) => {
    if (!String(e.tool).includes(o.outwardTool)) return next(e)
    const args = normalize(Object.fromEntries(Object.entries(e as Record<string, unknown>).filter(([k]) => !RESERVED.has(k))))
    if (!isOutward(args)) {
      // A read: let it run, and when it looks like the ticket's comments, capture them as the live thread.
      const ran = await next(e)
      const ticket = ticketOf(args)
      const text = (ran as { text?: string }).text
      if (ticket && text && /"comments"\s*:\s*\[/.test(text)) {
        try {
          const parsed = JSON.parse(text) as { comments?: Array<Record<string, unknown>>; count?: number }
          const comments = parsed.comments ?? []
          const messages = comments.map((c, i) => ({
            id: String(c.id ?? i),
            who: c.author_role === 'agent' || c.via_agent === true || c.public === false ? 'us' : 'customer',
            at: typeof c.created_at === 'string' ? Date.parse(c.created_at) / 1000 : undefined,
            text: String(c.plain_body ?? c.body ?? ''),
          }))
          await desk($, o, ['capture'], JSON.stringify({ ticket, messages, count: parsed.count ?? comments.length }))
        } catch {
          /* not a comments payload after all */
        }
      }
      return ran
    }
    const r = await desk($, o, ['consume'], JSON.stringify({ candidates: strings(args), what: `${String(e.tool)} ${JSON.stringify(args).slice(0, 120)}` }))
    if (r.ok) {
      const ran = await next(e)
      await refresh($, o, `sent ${String(r.md5).slice(0, 8)}`)
      return ran
    }
    const undecided = (r.undecided as string[] | undefined) ?? []
    return {
      deny:
        `argus-desk: this call would post to the customer and carries no text the operator has granted. ` +
        (undecided.length ? `Waiting at the desk: ${undecided.map(m => m.slice(0, 8)).join(', ')}. ` : 'Nothing is filed. ') +
        `File the exact reply with ${TOOL} and wait for the operator's Send.`,
    }
  }).catch(($, e, next) => {
    if (!String(e.tool).includes(o.outwardTool)) return next.called ? next(e) : next(e)
    return next.called ? next(e) : { deny: 'argus-desk: the gate could not decide, so the post is refused.' }
  })

  // 3. the pane: the forced decision.
  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button, Input } = $.ui.resolve(e) as any
    const v = await read($, view)
    const width = Math.max(40, e.props.bodyColumns ?? 80)
    const rows = Math.max(8, (e.viewport?.rows ?? 30) - 4)
    const sel = v.items.find(i => i.md5 === v.selected) ?? v.items[0]
    const preview = (t: string, n: number) => t.split('\n').filter(l => l.trim()).slice(0, n).map(l => l.slice(0, width - 2))
    return (
      <Box flexDirection="column">
        <Text bold>Desk: {v.items.length} waiting{v.notice ? `  (${v.notice})` : ''}</Text>
        {v.items.length === 0 && <Text dimColor>No drafts filed. The model files one with {TOOL}; nothing posts without a Send here.</Text>}
        {v.items.map(i => (
          <Button key={`pick-${i.md5}`} plain onPress={() => void update($, view, x => ({ ...x, selected: i.md5, mode: 'list' }))}>
            {i.md5 === sel?.md5 ? '> ' : '  '}#{i.ticket} {i.md5.slice(0, 8)} {i.checks.verdict === 'hold' ? `HOLD ${i.checks.hits.join(',')}` : 'clear'} {i.state} {Math.round(i.age_s / 60)}m
          </Button>
        ))}
        {sel && (
          <Box flexDirection="column" marginTop={1}>
            <Text dimColor>
              thread: {sel.checks.thread?.status === 'captured' ? `${sel.checks.thread.messages} of ${sel.checks.thread.console_count} comments recorded` : 'NOT captured'}
              {' | '}claims: {sel.checks.claims ? `${sel.checks.claims.credited} of ${sel.checks.claims.checkable} receipted` : 'n/a'}
              {sel.checks.warnings.length ? ` | ${sel.checks.warnings.join(', ')}` : ''}
            </Text>
            {(sel.checks.thread?.open_questions ?? []).slice(0, 4).map(q => (
              <Text key={`q-${q.slice(0, 20)}`} color="yellow">customer asked: {q.slice(0, width - 18)}</Text>
            ))}
            {preview(sel.text, rows - 8 - v.items.length).map((l, n) => (
              <Text key={`l-${n}`}>{l}</Text>
            ))}
            {v.mode === 'list' && (
              <Box marginTop={1}>
                <Button key="send" variant="primary" hotkey="s" onPress={p => void decide($, o, sel.md5, 'send', '', `${p.requestId}-${sel.md5.slice(0, 8)}-${Date.now()}`)}>Send</Button>
                <Button key="modify" hotkey="m" onPress={() => void update($, view, x => ({ ...x, selected: sel.md5, mode: 'modify' }))}>Modify</Button>
                <Button key="deny" hotkey="d" onPress={() => void update($, view, x => ({ ...x, selected: sel.md5, mode: 'deny' }))}>Deny</Button>
              </Box>
            )}
            {v.mode === 'modify' && (
              <Input key="modify-note" label="what to change" placeholder="one line for the drafter" autoFocus submitLabel="return" onSubmit={(t, p) => void decide($, o, sel.md5, 'modify', t, p.requestId)} />
            )}
            {v.mode === 'deny' && (
              <Input key="deny-reason" label="why" placeholder="recorded with the deny" autoFocus submitLabel="deny" onSubmit={(t, p) => void decide($, o, sel.md5, 'deny', t, p.requestId)} />
            )}
          </Box>
        )}
      </Box>
    )
  })
}
