import { atom, read, update } from 'claude-code'
import type { Register } from 'claude-code'

import type { DeskItem, DeskView, QueueRow, Trace, TreeNode, TreeResult, WallAgent } from '../types'

// argus-desk: the review desk and the investigation it reviews.
//   1. the queue     a timer searches the open set and pulls one ticket at a time.
//   2. the run       a tree of subagents per ticket: investigators from three angles, an adversary, a drafter,
//                    a reviewer that may only object. Everything they read is a receipt; everything they conclude
//                    is a finding; every step is a line on the wall.
//   3. file_draft    the drafter files the exact reply with its citations; the desk checks it.
//   4. the pane      Wall, Queue, Ticket (the trace), Decide: Send, Modify or Deny.
//   5. the gate      an outward call passes only by spending a grant on the exact text, with the status chosen.
// State and checks live in checks/desk.py (stdlib Python), one process per call; the mod is the binding.

// One engine pane holds the three views. The engine refuses `ui.open({ focus })` while the person holds a pane, so a key
// pressed inside one pane can never raise another: with a pane per view, 1 to 3 did nothing on a real terminal (the
// plugin test host does not model the refusal). The panes 0.8.0 opened are closed on the way in.
const PANE = 'desk'
const OLD_PANES = ['desk-review', 'desk-tickets', 'desk-wall']
type View = 'review' | 'tickets' | 'wall'
const VIEWS: View[] = ['review', 'tickets', 'wall']
const TITLES: Record<View, string> = { review: 'Review', tickets: 'Tickets', wall: 'Wall' }
/** Old names still typed by habit or by the model. */
const ALIASES: Record<string, View> = { decide: 'review', queue: 'tickets', ticket: 'tickets' }
/** The element the focus ring holds in each pane, from ui.focus: what a hotkey acts on (p pulls the ticket under the ring). */
const ring = new Map<string, string>()
const TOOL = 'file_draft'
const SKIP = 'skip_ticket'
const FINDING = 'desk_finding'
const RECEIPT = 'desk_receipt'
const MISMATCH = 'desk_mismatch'
const STATUSES = ['pending', 'solved', 'open', 'on-hold']
const ROLES = ['investigator', 'adversary', 'drafter', 'reviewer'] as const
type Role = (typeof ROLES)[number]

const view = atom({ plugin: 'argus-desk', key: 'view' } as const, { items: [], queue: [], wall: [], mode: 'list', poll: 'off' } as DeskView)

type Opts = {
  stateDir: string; python: string; outwardTool: string
  pollMinutes: number; readProxy: string; writeProxy: string; searchEndpoint: string; searchQuery: string
  commentsEndpoint: string; ticketEndpoint: string; updateEndpoint: string; sendStatus: string
  docsTool: string; docsSource: string; docsHosts: string; agentTools: 'hooks' | 'mcp'; investigatorModel: string; adversaryModel: string; drafterModel: string; reviewerModel: string; maxRounds: number
  guardRecord: boolean
}

const opts = (o: Record<string, unknown>): Opts => ({
  stateDir: String(o.stateDir ?? '~/.argus-desk'),
  python: String(o.python ?? 'python3'),
  outwardTool: String(o.outwardTool ?? 'invoke_proxy'),
  pollMinutes: Math.max(0, Number(o.pollMinutes ?? 5) || 0),
  readProxy: String(o.readProxy ?? 'zendesk-full'),
  writeProxy: String(o.writeProxy ?? 'zendesk-write'),
  searchEndpoint: String(o.searchEndpoint ?? 'zendesk_search_tickets'),
  searchQuery: String(o.searchQuery ?? 'type:ticket status<pending assignee:me'),
  commentsEndpoint: String(o.commentsEndpoint ?? 'zendesk_list_comments'),
  ticketEndpoint: String(o.ticketEndpoint ?? 'zendesk_get_ticket'),
  updateEndpoint: String(o.updateEndpoint ?? 'zendesk_update_ticket'),
  sendStatus: STATUSES.includes(String(o.sendStatus)) ? String(o.sendStatus) : 'pending',
  docsTool: String(o.docsTool ?? 'enterprise_search'),
  docsSource: String(o.docsSource ?? 'wiz_docs_public'),
  docsHosts: String(o.docsHosts ?? 'docs.wiz.io'),
  // 2026-10-15: mcp by default since 0.6.2, so a host where hooks do not answer inside agents needs no settings edit.
  agentTools: String(o.agentTools ?? 'mcp') === 'hooks' ? 'hooks' : 'mcp',
  investigatorModel: String(o.investigatorModel ?? 'sonnet'),
  adversaryModel: String(o.adversaryModel ?? 'opus'),
  drafterModel: String(o.drafterModel ?? 'opus'),
  reviewerModel: String(o.reviewerModel ?? 'sonnet'),
  maxRounds: Math.max(0, Math.min(3, Number(o.maxRounds ?? 2) || 0)),
  guardRecord: String(o.guardRecord ?? 'true') !== 'false',
})

// invoke_proxy as Argus shows it (2026-10-15): required proxy (an enum of 49 services) and endpoint (a name such
// as zendesk_get_ticket, not a path), optional arguments (object or JSON string). There is no method field, so
// the endpoint name is the only sign of a read. Anything not plainly a read is outward, on every proxy.
const READ_ENDPOINT = /^[a-z0-9]+_(get|list|search|read|show|fetch|count|describe)(_|$)/i

const RESERVED = new Set(['tool', 'tool_use_id', 'agentId', 'consent', 'requestMeta'])

/** The fields of an update that carry text to the customer. The gate reads these and nothing else: a granted
 *  text in a tag, a subject or a custom field is not a granted post. */
const BODY_KEY = /^(body|html_body|plain_body|text|message|content|comment|note|description)$/i

/** The comment bodies an outward call carries, with whether the comment around each is marked not public. */
const bodies = (v: unknown, out: Array<{ text: string; internal: boolean }> = [], depth = 0, internal = false): Array<{ text: string; internal: boolean }> => {
  if (depth > 6) return out
  if (Array.isArray(v)) {
    for (const x of v) bodies(x, out, depth + 1, internal)
  } else if (v && typeof v === 'object') {
    const o = v as Record<string, unknown>
    const here = internal || o.public === false
    for (const [k, x] of Object.entries(o)) {
      if (RESERVED.has(k)) continue
      if (typeof x === 'string') {
        if (BODY_KEY.test(k) && x.trim()) out.push({ text: x, internal: here })
      } else bodies(x, out, depth + 1, here)
    }
  }
  return out
}

/** The ticket status an update sets: on the ticket object or at the top of the arguments, not in a nested field. */
const statusOf = (args: Record<string, unknown>): string | undefined => {
  const a = (args.arguments && typeof args.arguments === 'object' ? args.arguments : args) as Record<string, unknown>
  const t = a.ticket && typeof a.ticket === 'object' ? (a.ticket as Record<string, unknown>) : undefined
  const s = t?.status ?? a.status
  return typeof s === 'string' ? s : undefined
}

/** The desk's own files, as a shell command or a file path would name them: the record only the desk may write. */
const recordNames = (o: Opts): string[] => [o.stateDir.replace(/^~\//, '').replace(/\/+$/, ''), 'checks/desk.py', 'checks/desk_mcp.py', 'desk.py', 'desk_mcp.py']
const GUARDED = /^(bash|write|edit|multiedit|notebookedit)$/i
const namesRecord = (o: Opts, args: Record<string, unknown>): string | undefined => {
  const text = [args.command, args.file_path, args.path, args.notebook_path].filter(x => typeof x === 'string').join('\n')
  return recordNames(o).find(n => n && text.includes(n))
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

/** The comments array of a read result, wherever the proxy put it: at the top, under data/result/response, or as a JSON
 *  string inside the JSON. Returns the array and the count beside it when the payload carries one. */
const commentsIn = (v: unknown, depth = 0): { comments: Array<Record<string, unknown>>; count?: number } | undefined => {
  if (depth > 5 || v == null) return undefined
  if (typeof v === 'string') {
    if (depth === 0 || !/"comments"\s*:\s*\[/.test(v)) return undefined
    try {
      return commentsIn(JSON.parse(v), depth + 1)
    } catch {
      return undefined
    }
  }
  if (Array.isArray(v)) {
    for (const x of v) {
      const f = commentsIn(x, depth + 1)
      if (f) return f
    }
    return undefined
  }
  if (typeof v === 'object') {
    const rec = v as Record<string, unknown>
    if (Array.isArray(rec.comments)) {
      const count = typeof rec.count === 'number' ? rec.count : typeof rec.comment_count === 'number' ? rec.comment_count : undefined
      return { comments: rec.comments.filter(c => c && typeof c === 'object') as Array<Record<string, unknown>>, count }
    }
    for (const x of Object.values(rec)) {
      const f = commentsIn(x, depth + 1)
      if (f) return f
    }
  }
  return undefined
}

/** Our side of a comment: the proxy may say it as author_role, via_agent, or an author object with a role. */
const isOurs = (c: Record<string, unknown>): boolean => {
  const author = c.author as Record<string, unknown> | undefined
  const role = String(c.author_role ?? author?.role ?? '').toLowerCase()
  return c.via_agent === true || role === 'agent' || role === 'admin'
}

/** The text of a comment: plain first; html only when it is all the proxy filled, with its tags dropped. */
const commentText = (c: Record<string, unknown>): string => {
  for (const k of ['plain_body', 'body', 'text', 'content', 'message']) if (typeof c[k] === 'string' && (c[k] as string).trim()) return c[k] as string
  if (typeof c.html_body === 'string') return c.html_body.replace(/<br\s*\/?>|<\/p>|<\/div>|<\/li>/gi, '\n').replace(/<[^>]+>/g, '').replace(/&nbsp;/g, ' ').replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&#39;/g, "'").trim()
  return ''
}

const ticketOf = (args: Record<string, unknown>): string | undefined => {
  const flat = JSON.stringify(args)
  const m = flat.match(/tickets?\/(\d{4,})/) ?? flat.match(/"ticket(?:_id|Id)?"\s*:\s*"?(\d{4,})/)
  return m?.[1]
}

/** The requester's name in a ticket read: requester.name, requester_name, or the sideloaded user whose id is requester_id. */
const requesterIn = (v: unknown, depth = 0): string | undefined => {
  if (depth > 6 || !v) return undefined
  if (typeof v === 'string') {
    if (!/requester/.test(v)) return undefined
    try { return requesterIn(JSON.parse(v), depth + 1) } catch { return undefined }
  }
  if (Array.isArray(v)) { for (const x of v) { const f = requesterIn(x, depth + 1); if (f) return f } return undefined }
  if (typeof v !== 'object') return undefined
  const rec = v as Record<string, unknown>
  const req = rec.requester as Record<string, unknown> | undefined
  if (req && typeof req === 'object' && typeof req.name === 'string' && req.name.trim()) return req.name.trim()
  if (typeof rec.requester_name === 'string' && rec.requester_name.trim()) return rec.requester_name.trim()
  const tk = (rec.ticket ?? rec) as Record<string, unknown>
  if (tk && typeof tk === 'object' && tk.requester_id != null && Array.isArray(rec.users)) {
    const u = (rec.users as Array<Record<string, unknown>>).find(x => x && String(x.id) === String(tk.requester_id))
    if (u && typeof u.name === 'string' && u.name.trim()) return u.name.trim()
  }
  for (const x of Object.values(rec)) { const f = requesterIn(x, depth + 1); if (f) return f }
  return undefined
}

/** The documentation URLs a read names, on the official hosts, so its result can stand as that page. */
const docsUrls = (o: Opts, ...texts: string[]): string[] => {
  const hosts = o.docsHosts.split(',').map(h => h.trim()).filter(Boolean)
  const out = new Set<string>()
  for (const t of texts) for (const m of t.matchAll(/https?:\/\/([a-z0-9.-]+)(\/[^\s"'<>\\)]*)?/gi)) if (hosts.includes(m[1].toLowerCase())) out.add(m[0].replace(/[.,;:]+$/, ''))
  return [...out].slice(0, 8)
}

async function desk($: any, o: Opts, args: string[], stdin?: string): Promise<Record<string, unknown> & { ok?: unknown }> {
  const r = await $.process.run([o.python, '-I', `${$.plugin.root}/checks/desk.py`, '--root', o.stateDir, ...args], {
    stdin, timeoutMs: args[0] === 'wait' ? 590_000 : 60_000, env: { ARGUS_DESK_DOCS_HOSTS: o.docsHosts },
  })
  try {
    return { ...(JSON.parse(r.stdout) as Record<string, unknown>), exitCode: r.exitCode }
  } catch {
    return { ok: false, reason: `desk.py exit ${r.exitCode}: ${(r.stderr || r.stdout).slice(0, 300)}`, exitCode: r.exitCode }
  }
}

let oldClosed = false
/** The row the ring lands on when a view opens: the tree's first row, the selected ticket's, the first agent's. */
function firstRow(v: DeskView, which: View): string | undefined {
  const f = (v.filter?.[which] ?? '').trim().toLowerCase()
  const has = (...parts: Array<string | undefined>) => !f || parts.some(p => p && p.toLowerCase().includes(f))
  if (which === 'review') {
    const zoomId = (v.zoom ?? [])[(v.zoom ?? []).length - 1]
    const root = v.tree?.ok ? v.tree.tree : undefined
    if (!root) return undefined
    const n = zoomId ? findNode(root, zoomId) : root
    const c = n?.children.find(x => !f || hasMatch(x, f))
    return c ? nodeKey(c.id) : undefined
  }
  if (which === 'tickets') {
    const rows = v.queue.filter(q => has(q.ticket, q.subject, q.state))
    const t = rows.find(q => q.ticket === v.ticket) ?? rows[0]
    return t ? `ticket:${t.ticket}` : undefined
  }
  if (v.tail) return 'back'
  const a = v.wall.find(w => has(w.ticket, w.role, w.label, w.last, w.status))
  return a ? `agent:${a.id}` : undefined
}

/** Show one view in the desk pane: the view is state, so a key pressed inside the pane switches it with no focus move.
 *  A press passes its requestId, and the ring moves to the view's first row: left on the key pressed, Enter pressed it again. */
async function openView($: any, v: View, focus = true, requestId?: string): Promise<void> {
  await update($, view, x => ({ ...x, which: v }))
  if (requestId) {
    const key = firstRow(await read($, view), v)
    if (key) await $.ui.focus({ requestId, key }).catch(() => undefined)
  }
  try {
    if (!oldClosed) {
      oldClosed = true
      for (const id of OLD_PANES) await $.ui.close({ id }).catch(() => undefined)
    }
    await $.ui.open({ id: PANE, title: 'Desk', ...(focus ? { focus: true } : {}) })
  } catch {
    /* a host without panes: the state is set for when one draws */
  }
}
/** Open the pane on the view it last showed, without asking for the keys. */
async function openDesk($: any): Promise<void> {
  try {
    await $.ui.open({ id: PANE, title: 'Desk' })
  } catch {
    /* as above */
  }
}

/** The agents running now, by id: which ticket and role each belongs to, for the wall and the receipts. */
const active = new Map<string, { ticket: string; role: Role; label: string }>()
/** Step text per agent, flushed to the tail at the end of each step. */
const partial = new Map<string, string>()

async function refresh($: any, o: Opts, notice?: string): Promise<DeskItem[]> {
  const r = await desk($, o, ['list'])
  const items = (r.items as DeskItem[] | undefined) ?? []
  const q = await desk($, o, ['queue'])
  const queue = ((q.rows as QueueRow[] | undefined) ?? []).filter(x => x.state !== 'done' && x.state !== 'gone')
  const v = await read($, view)
  const selected = v.selected && items.some(i => i.md5 === v.selected) ? v.selected : items[0]?.md5
  const ticket = v.ticket ?? items.find(i => i.md5 === selected)?.ticket ?? queue[0]?.ticket
  const trace = ticket ? ((await desk($, o, ['trace', '--ticket', ticket])) as unknown as Trace) : undefined
  const tree = selected ? await loadTree($, o, selected) : undefined
  await update($, view, x => ({
    ...x, items, queue, ticket, selected, trace: trace?.ok ? trace : x.trace, notice: notice ?? (r.ok ? undefined : String(r.reason)),
    tree: tree ?? undefined,
    // a different draft starts from the default view of its tree
    ...(tree?.md5 && tree.md5 !== x.tree?.md5 ? { expanded: DEFAULT_OPEN, zoom: [], bodies: {}, last: undefined } : {}),
  }))
  try {
    const open = queue.filter(x => x.state !== 'skipped').length
    $.ui.status(items.length ? `desk: ${items.length} waiting` : active.size ? `desk: ${active.size} agents on ${new Set([...active.values()].map(a => a.ticket)).size} ticket(s)` : open ? `desk: ${open} open` : undefined)
  } catch {
    /* no status line on this surface */
  }
  return items
}

/** What the desk holds, said in the refusal, so a refused verbatim quote reads as what it is (2026-10-15 receipt fault). */
const refusalDetail = (r: Record<string, unknown>): string => {
  const why = String(r.why ?? '')
  if (why === 'page-not-captured') {
    const cap = Array.isArray(r.captured) ? (r.captured as unknown[]).map(String) : []
    return `; fetch the page through the proxy first, then quote it with the URL as the result names it (captured so far: ${cap.length ? cap.join(', ') : 'none'})`
  }
  if (why === 'no-thread-captured') return `; ${String(r.hint ?? '')}`
  if (why === 'quote-not-in-material') {
    const t = (r.thread ?? {}) as Record<string, unknown>
    return `; the captured thread holds ${String(t.customer_messages ?? '?')} customer message(s), ${String(t.customer_chars ?? '?')} chars; quote the customer's words as the proxy returned them`
  }
  if (why === 'quote-not-from-customer') return `; those words are in a ${String(r.found_in ?? '')} message, not the customer's`
  return ''
}

async function wallLine($: any, o: Opts, agentId: string, kind: string, text: string): Promise<void> {
  const a = active.get(agentId)
  if (!a) return
  await desk($, o, ['event'], JSON.stringify({ ticket: a.ticket, agent: a.label, kind, text }))
  await update($, view, v => {
    const wall: WallAgent[] = v.wall.filter(w => w.id !== agentId)
    const prev = v.wall.find(w => w.id === agentId)
    wall.push({ id: agentId, ticket: a.ticket, role: a.role, label: a.label, status: prev?.status ?? 'running', started: prev?.started ?? Date.now(), last: `${kind}: ${text.slice(0, 160)}`, lines: [...(prev?.lines ?? []).slice(-79), `${kind}: ${text.slice(0, 400)}`] })
    return { ...v, wall }
  })
}

async function decide($: any, o: Opts, md5: string, decision: 'send' | 'deny' | 'modify', note: string, pressId: string, status = o.sendStatus): Promise<void> {
  const item = (await read($, view)).items.find(i => i.md5 === md5)
  if (!item) return
  if (decision === 'send') {
    const r = await desk($, o, ['grant', '--md5', md5, '--press-id', pressId, '--status', status])
    if (!r.ok) {
      await refresh($, o, `send refused: ${String(r.reason)}`)
      return
    }
    await refresh($, o, `granted ${md5.slice(0, 8)}, status ${status}`)
    void $.prompt.submit({ text:
      // the text itself: a subagent drafted it, so this session may never have seen it (and after a restart it has seen nothing)
      `The operator approved draft ${md5} for #${item.ticket}. Post exactly the text between the two marker lines below, unchanged, ` +
      `as a public comment with ${o.outwardTool} (proxy ${o.writeProxy}, endpoint ${o.updateEndpoint}), and in the same call set the ticket ` +
      `status to "${status}". The marker lines are not part of it. The gate refuses the post if the text differs or the status is missing.\n` +
      `----- BEGIN APPROVED DRAFT ${md5.slice(0, 8)} -----\n${item.text}\n----- END APPROVED DRAFT ${md5.slice(0, 8)} -----` }).catch(() => undefined)
  } else if (decision === 'deny') {
    await desk($, o, ['deny', '--md5', md5, '--reason', note])
    await desk($, o, ['finding'], JSON.stringify({ ticket: item.ticket, agent: 'operator', role: 'operator', kind: 'objection', text: `Denied: ${note || '(no reason)'}`, about: md5 }))
    await refresh($, o, `denied ${md5.slice(0, 8)}`)
    void $.prompt.submit({ text: `The operator denied draft ${md5} for #${item.ticket}${note ? `: ${note}` : ''}. Do not post it. Tell the operator in one line that \`/desk run ${item.ticket}\` investigates again with that objection on record (it is the operator's command; you cannot run it), or say what the ticket needs instead.` }).catch(() => undefined)
  } else {
    await desk($, o, ['modify', '--md5', md5, '--note', note])
    await desk($, o, ['finding'], JSON.stringify({ ticket: item.ticket, agent: 'operator', role: 'operator', kind: 'objection', text: `Modify: ${note || '(no note)'}`, about: md5 }))
    await refresh($, o, `returned ${md5.slice(0, 8)}`)
    $.clock.after(1, () => void redraft($, o, item.ticket, md5, note).catch(() => undefined))
  }
  await update($, view, v => ({ ...v, mode: 'list', selected: undefined }))
  // the next draft opens with the ring on its first row: left where the engine puts it, the ring sat on Send, and the
  // Enter that submitted a Deny's reason, pressed once more, would have sent the next draft
  await ringToFirstRow($)
}

/** The ring onto the Review's first row, now and again once the engine has re-seated it: when the field that held the
 *  ring is gone, the engine moves the ring to the nearest element (a decision key) after this call has run. */
async function ringToFirstRow($: any): Promise<void> {
  const go = async () => {
    const key = firstRow(await read($, view), 'review')
    if (key) await $.ui.focus({ requestId: PANE, key }).catch(() => undefined)
  }
  await go()
  try {
    $.clock.after(150, () => void go())
  } catch {
    /* a host without a clock: the first move stands */
  }
}

// ---- the run: one investigation, a tree of subagents, awaited through the Agent tool so the turn that started
// it holds until the drafter has filed (a `claude -p` session stays alive for it; a timer dispatch too).

// 2026-10-15, workstation: a `tools` list on the agent type left the subagents without the proxy tool (its real name is only known
// once the session lists its tools), so the types name none and inherit every tool the session has; the gate holds on the outward one.

const readingRules = (o: Opts, ticket: string): string =>
  `Read with ${o.outwardTool}: proxy ${o.readProxy}, endpoint ${o.commentsEndpoint} with arguments {"ticket_id":"${ticket}"} for every comment, ` +
  `endpoint ${o.ticketEndpoint} for the ticket. For documentation call ${o.docsTool} with data source ${o.docsSource}; a docs receipt may cite only a page URL that appears in that result. You may not post, comment, or change anything. ` +
  `Every fact you will rely on must be receipted the moment you read it: call ${RECEIPT} with kind "docs" and the sentence verbatim from the ` +
  `page plus its URL, or kind "material" and the customer's words verbatim. A receipt that is refused means you did not actually read it; do not rely on it. ` +
  `Record what you conclude with ${FINDING} (kind hypothesis, evidence, counter), naming the receipt ids that carry it.`

const prompts: Record<Role, (o: Opts, ticket: string, angle: string, context: string) => string> = {
  investigator: (o, ticket, angle, context) =>
    `You investigate Zendesk ticket #${ticket} for the support engineer who will answer it. Your angle: ${angle}\n` +
    `${readingRules(o, ticket)}\n${context}\n` +
    `The bar (the operator's own): a cause needs two legs, the mechanism from the official documentation (the governing prose sentence, never a YAML or code example) ` +
    `and the customer's own data showing it applies to them. Open and read every attachment and log the customer gave before you say anything is missing or ask for it. ` +
    `Read the whole thread with every comment, not the first page. Record at least ${RECEIPT_FLOOR} receipts; a docs receipt for every product behaviour you assert, a material receipt for every fact about their setup. ` +
    `Fewer than ${RECEIPT_FLOOR} receipts means you have not investigated and the desk sends you back in. ` +
    `Finish with one ${FINDING} of kind summary (cause, confidence low/medium/high, what the customer must be asked, what you could not verify), ` +
    `and answer in under 200 words with the same.`,
  adversary: (o, ticket, _angle, context) =>
    `You are the adversary on Zendesk ticket #${ticket}. The investigators' findings are below. Assume the leading explanation is wrong and find what would make it wrong: ` +
    `a platform mismatch, a step that does not apply to this customer's setup, a thing we already told them, a question of theirs nobody answered, evidence that cuts the other way.\n` +
    `${readingRules(o, ticket)}\n${context}\n` +
    `Record each counter with ${FINDING} kind counter, with receipts where you have them. If nothing holds up against the findings, record one counter saying so and why. Answer in under 200 words.`,
  drafter: (o, ticket, angle, context) =>
    `You draft the public reply on Zendesk ticket #${ticket}. The findings, counters and receipts are below; read the thread yourself as well.\n` +
    (angle ? `${angle}\n` : '') +
    `${readingRules(o, ticket)}\n${context}\n` +
    `Rules for the reply, the operator's own: answer the customer's need in the first paragraph, in plain words; prefer a documentation link to a list of steps, and give numbered steps only when they asked for steps or the link alone will not do. ` +
    `No dashes of any kind. Never promise a time. No closing offer, no sign-off, no greeting line beyond the first (Zendesk appends the signature). ` +
    `Do not restate the customer's words back to them; a sentence that echoes them is not an answer and is counted against the draft. Do not repeat what we already told them. ` +
    `There is no "unconfirmed", "I suspect" or "I believe" wording that makes an unverified sentence acceptable: a claim is verified with a receipt or it is cut; if you cannot verify the answer, ask for exactly what would settle it and assert nothing. ` +
    `Every factual sentence must carry a receipt that says what the sentence says. When the reply escalates to engineering, say "I've passed along your request to the WizOS engineering team." and only when there is a documented mismatch between the docs and what the product did, recorded first with ${MISMATCH} (the docs receipt, the material or test receipt, one line on how they differ); an escalation with no mismatch on record is held. ` +
    `Greet the customer by name only with the name in the ticket's requester field; if the ticket read showed no requester name, use no name. ` +
    `File it with ${TOOL}: ticket, draft (the exact text), and citations as a list of {sentence, receipt} covering every factual sentence. ` +
    `If the ticket needs no reply from us right now, call ${SKIP} with the reason instead. Do not post anything. Answer with the md5 ${TOOL} returned and one line on what the reply does.`,
  reviewer: (o, ticket, angle, context) =>
    `You review the filed reply on Zendesk ticket #${ticket} before the operator sees it. You may only object; you do not score and you do not praise.\n` +
    (angle ? `${angle}\n` : '') +
    `${readingRules(o, ticket)}\n${context}\n` +
    `You are a checker, not a writer: do not suggest wording, do not grade style. Every quote you give must be copied exactly from the thread or the draft; a quote that is not there is discarded and counted against you. ` +
    `Record each objection with ${FINDING} kind objection, quoting the draft sentence and the customer's or our earlier words it fails against. ` +
    `If there is nothing to object to after working the whole checklist, record one objection with the text "NO OBJECTIONS" and, for each checklist item, what you checked and found. Answer with the list.`,
}

/** Receipts an investigator must mint before its pass counts; below it the desk sends it back in once. */
const RECEIPT_FLOOR = 2

/** Two reviewers per round, independent, different checklists; both must find nothing for the round to clear. */
const REVIEW_ANGLES = [
  `Checklist A, the thread. 1. QUESTIONS: list every question or request in the customer's latest messages, including ones without a question mark; for each decide exactly one: ` +
  `answered (a draft sentence directly answers it with real information; quote it), asked_back (the draft asks a genuine question of ours needed to answer it), ` +
  `unknown_stated (the draft says plainly we do not have the answer yet AND what we are doing about it: a reproduction, an escalation, a follow-up), or not_answered (talking around it, ` +
  `answering a different or only part of the question, or restating their question back to them). Every not_answered is an objection. ` +
  `2. CONSTRAINTS: anything they said they want, need, cannot or will not do; does the draft recommend, assume or require the opposite? Their guess about the cause is not a constraint. ` +
  `3. OUR EARLIER REPLIES: does the draft silently reverse a factual or instructional statement we made earlier on the thread? An explained change is not silent. ` +
  `4. RE-ASKS: does the draft ask for something the customer already gave, the same item, not a more precise check of it? 5. USES WHAT THEY SAID: does the draft ignore a fact, file or log they already gave?`,
  `Checklist B, the evidence and the need. 1. RECEIPTS: for each factual sentence in the draft, does its receipt actually say that, from the official docs for product behaviour and from the customer's own material for their setup? ` +
  `A sentence with no receipt, a receipt that says something weaker, or an inference from a receipt is an objection. 2. PLATFORM: does every step and control apply to this customer's platform, product version and setup as the thread shows it? ` +
  `3. THE NEED: does the first paragraph answer what the customer actually needs, or does it restate, hedge, hold ("still checking" with no action) or defer? ` +
  `4. ESCALATION: if the draft says engineering is looking, is there a documented mismatch between the docs and the product on record, or is it a promise? 5. COMMITMENTS: any time, date or outcome the draft promises.`,
]

const ANGLES = [
  'documentation first: find the official page that answers this and what it says applies to this customer\'s platform; note every step that is platform-specific.',
  'evidence first: the support package, logs, product state and ids the customer gave; what they prove, what is missing, what we must ask for.',
  'thread first: the whole history, what we already said, what they already tried, every question of theirs still open, and the risk of repeating ourselves.',
]

async function contextFor($: any, o: Opts, ticket: string, what: 'findings' | 'all'): Promise<string> {
  const t = (await desk($, o, ['trace', '--ticket', ticket])) as unknown as Trace
  if (!t.ok) return ''
  const f = (t.findings ?? []).map(x => `- [${x.kind}] (${x.role} ${x.agent}) ${x.text}${x.receipts?.length ? ` <${x.receipts.join(',')}>` : ''}`).join('\n')
  const a = (t.run?.agents ?? []).filter(x => x.summary && x.status === 'completed').map(x => `- ${x.role} ${x.label}: ${x.summary}`).join('\n')
  const r = (t.receipts ?? []).map(x => `- ${x.id} ${x.kind}${x.source ? ` ${x.source}` : ''}: "${x.quote.slice(0, 200)}"`).join('\n')
  const d = what === 'all' && t.drafts?.length ? `\nFILED DRAFT ${t.drafts[t.drafts.length - 1].md5}:\n${t.drafts[t.drafts.length - 1].text}\nCLAIM CHECK:\n${(t.claims ?? []).map(c => `- ${c.problems?.length ? c.problems.join(',') : 'ok'}: ${c.text}`).join('\n')}` : ''
  return `AGENTS SO FAR:\n${a || '(none)'}\nFINDINGS SO FAR:\n${f || '(none)'}\nRECEIPTS:\n${r || '(none)'}${d}`
}

/** The run as a state machine in run.json: each agent's turn.complete advances it from a timer dispatch of its own
 *  (a hook that spawns never sees its children's events, so the spawner must not be a hook that listens). */
const ORDER: Record<string, string> = { new: 'investigate', investigate: 'adversary', adversary: 'draft', draft: 'review' }

async function spawnRole($: any, o: Opts, ticket: string, role: Role, label: string, angle: string, model: string): Promise<string | undefined> {
  const context = await contextFor($, o, ticket, role === 'reviewer' ? 'all' : 'findings')
  const prompt = prompts[role](o, ticket, angle, context)
  const description = `${role} #${ticket} ${label}`
  let r: Record<string, unknown>
  try {
    r = (await $.agent.spawn({ prompt, description, subagentType: `argus-desk:${role}`, model })) as Record<string, unknown>
    if (r.deny) r = (await $.agent.spawn({ prompt, description, model })) as Record<string, unknown>
  } catch (err) {
    r = { deny: String(err) }
  }
  // Core sets agentId; a host that answered the spawn without starting a loop (a test's bottom) leaves it out.
  const id = typeof r.agentId === 'string' ? r.agentId : !r.deny && r.model ? `${role}-${label}-${Date.now()}` : undefined
  if (!id) {
    await desk($, o, ['run'], JSON.stringify({ ticket, agent: { id: `${role}-${label}-failed-${Date.now()}`, role, label, model, status: 'failed', summary: `spawn refused: ${String(r.deny ?? '')}`.slice(0, 300) } }))
    return undefined
  }
  active.set(id, { ticket, role, label })
  await desk($, o, ['run'], JSON.stringify({ ticket, agent: { id, agentId: id, role, label, model: String(r.model ?? model), status: 'running', params: angle.slice(0, 80), prompt } }))
  await update($, view, v => ({ ...v, wall: [...v.wall.filter(w => w.id !== id), { id, ticket, role, label, status: 'running', started: Date.now(), last: 'started', lines: [] }] }))
  return id
}

async function advance($: any, o: Opts, ticket: string): Promise<void> {
  const t = (await desk($, o, ['trace', '--ticket', ticket])) as unknown as Trace
  if (!t.ok) return
  const run = t.run
  const agents = run.agents ?? []
  const of = (role: string) => agents.filter(a => a.role === role)
  const live = (role: string) => of(role).some(a => a.status === 'running' || a.status === 'pending')
  const rounds = run.rounds ?? 0
  const phase = run.phase
  const go = async (next: string, msg: string) => {
    await desk($, o, ['run'], JSON.stringify({ ticket, phase: next }))
    await desk($, o, ['event'], JSON.stringify({ ticket, agent: 'desk', kind: 'phase', text: msg }))
    await refresh($, o, `#${ticket}: ${msg}`)
  }
  try {
    if (phase === 'new' || phase === 'failed' && !agents.length) {
      await go('investigate', 'investigate')
      await Promise.all(ANGLES.map((angle, i) => spawnRole($, o, ticket, 'investigator', `inv${i + 1}`, angle, o.investigatorModel)))
    } else if (phase === 'investigate' && !live('investigator') && of('investigator').length) {
      // the evidence floor: an investigator that completed with fewer receipts than the floor goes back in once,
      // told what it recorded, before the adversary sees anything (receipts from an earlier run of the ticket do not count)
      const since = run.started ?? 0
      const receipts = (label: string) => (t.events ?? []).filter(e => e.kind === 'receipt' && e.agent === label && (e.ts ?? since) >= since).length
      const retry = of('investigator').filter(a => a.status === 'completed' && !a.label.endsWith('b') && receipts(a.label) < RECEIPT_FLOOR && !of('investigator').some(b => b.label === `${a.label}b`))
      if (retry.length) {
        await desk($, o, ['event'], JSON.stringify({ ticket, agent: 'desk', kind: 'phase', text: `below the evidence floor: ${retry.map(a => `${a.label} (${receipts(a.label)} receipts)`).join(', ')}; sending back in` }))
        await Promise.all(retry.map(a => {
          const i = Number((a.label.match(/\d+/) ?? ['1'])[0]) - 1
          return spawnRole($, o, ticket, 'investigator', `${a.label}b`, `${ANGLES[i] ?? ANGLES[0]} Your first pass recorded ${receipts(a.label)} receipts and concluded: ${a.summary ?? '(nothing)'}. That is below the floor of ${RECEIPT_FLOOR}: read the comments endpoint for the whole thread, open the attachments, fetch the docs page that governs the behaviour, and receipt what you read before you conclude.`, o.investigatorModel)
        }))
        return
      }
      await go('adversary', 'adversary')
      await spawnRole($, o, ticket, 'adversary', 'adv', '', o.adversaryModel)
    } else if (phase === 'adversary' && !live('adversary') && of('adversary').length) {
      await go('draft', 'draft')
      await spawnRole($, o, ticket, 'drafter', 'draft', '', o.drafterModel)
    } else if (phase === 'draft' && !live('drafter') && of('drafter').length) {
      await go('review', `review ${rounds + 1}`)
      const base = rounds ? `rev${rounds + 1}` : 'rev'
      await Promise.all(REVIEW_ANGLES.map((angle, i) => spawnRole($, o, ticket, 'reviewer', i ? `${base}${String.fromCharCode(65 + i)}` : base, angle, o.reviewerModel)))
    } else if (phase === 'review' && !live('reviewer') && of('reviewer').length) {
      // this round's reviewers are the last REVIEW_ANGLES.length spawned; the round clears only when every one found nothing
      const round = of('reviewer').slice(-REVIEW_ANGLES.length)
      const objectors = round.filter(a => !/NO OBJECTIONS/i.test(a.summary ?? ''))
      const last = { summary: objectors.length ? objectors.map(a => `${a.label}: ${a.summary ?? ''}`).join(' | ') : round[0]?.summary }
      const objected = objectors.length > 0
      if (objected && rounds <= o.maxRounds) {
        await go('draft', `redraft ${rounds + 1} after objections`)
        const prev = t.drafts?.slice(-1)[0]?.md5
        await spawnRole($, o, ticket, 'drafter', `draft${rounds + 1}`, `Revise: the reviewer objected (see the objections). File the new text with supersedes=${prev ?? 'the previous md5'}.`, o.drafterModel)
      } else {
        await go('done', `done: ${last.summary?.slice(0, 160) ?? ''}`)
      }
    }
  } catch (err) {
    await go('failed', `failed: ${String(err).slice(0, 300)}`)
  }
}

/** After Modify: one more drafter pass with the operator's note, then the usual review. */
async function redraft($: any, o: Opts, ticket: string, md5: string, note: string): Promise<void> {
  await desk($, o, ['run'], JSON.stringify({ ticket, phase: 'adversary', agent: { id: `operator-${Date.now()}`, role: 'adversary', label: 'operator', status: 'completed', summary: `Modify: ${note || '(none)'}; revise draft ${md5} with supersedes=${md5}.` } }))
  startDriver($, o)
  pending.add(ticket)
}

/**
 * Runs that need advancing, and the driver that advances them. Every spawn is made from the driver's own timer
 * dispatch: a subagent spawned from inside a hook's dispatch (a turn.complete, through a timer it set) ran with
 * none of this plugin's hooks answering its tool calls (seen 2026-10-15: "no tool.call hook answered"), while one
 * spawned from a timer set by a command ran with all of them. So no hook spawns; hooks only mark, and the driver,
 * started by a command or the session start, does the spawning.
 */
const pending = new Set<string>()
let driver: { cancel: () => void } | undefined
let driving = false

function startDriver($: any, o: Opts, lineage: 'command' | 'hook' = 'hook'): void {
  if (driver) return
  // 2026-10-15, workstation: a driver created in session.start spawned agents whose desk tool calls no hook answered
  // ("no tool.call hook answered this call"), the same lineage fault as a spawn from turn.complete. Only a command's
  // dispatch creates the driver; a hook only marks, and the next /desk command starts it.
  if (lineage !== 'command') return
  driver = $.clock.every(2_000, () => {
    void (async () => {
      if (driving || !pending.size) return
      driving = true
      try {
        for (const ticket of [...pending]) {
          pending.delete(ticket)
          await advance($, o, ticket)
        }
      } finally {
        driving = false
      }
    })().catch(() => { driving = false })
  })
}

/** Start the run on one ticket: mark it, set the phase, and leave the first spawn to the driver. */
function pull($: any, o: Opts, ticket: string, why: string, lineage: 'command' | 'hook' = 'hook'): void {
  startDriver($, o, lineage)
  $.clock.after(1, () => {
    void (async () => {
      await desk($, o, ['mark', '--ticket', ticket, '--state', 'drafting'])
      await desk($, o, ['run'], JSON.stringify({ ticket, phase: 'new' }))
      await desk($, o, ['event'], JSON.stringify({ ticket, agent: 'desk', kind: 'phase', text: `pulled (${why})` }))
      pending.add(ticket)
    })().catch(() => undefined)
  })
}

/** The outward tool's full name: looked up when the session lists its tools, else as last seen at the gate. */
let outwardName: string | undefined

async function findOutward($: any, o: Opts): Promise<string | undefined> {
  try {
    const tools = (await $.tool.list()) as Array<{ name: string }>
    const name = tools.map(t => t.name).find(n => n.includes(o.outwardTool))
    if (name) outwardName = name
  } catch {
    /* no tool list on this host; the gate fills it in on the first call it sees */
  }
  return outwardName ?? `mcp__argus__${o.outwardTool}`
}

/** One poll: the open set, folded into the queue; then the next ticket, if nothing is being worked. */
/** The search query: the operator's `/desk query ...` override (kept in the plugin store across sessions), else the option. */
async function queryOf($: any, o: Opts): Promise<string> {
  try {
    const q = await $.store.get('searchQuery')
    if (typeof q === 'string' && q.trim()) return q.trim()
  } catch {
    /* a host without a store */
  }
  return o.searchQuery
}

async function tick($: any, o: Opts, force = false): Promise<{ polled: number; pulled?: string; error?: string; head?: string; query?: string }> {
  let text = ''
  const query = await queryOf($, o)
  try {
    const name = await findOutward($, o)
    const r = await $.tool.call({ tool: name, proxy: o.readProxy, endpoint: o.searchEndpoint, arguments: { query, sort_by: 'created_at', sort_order: 'asc' } })
    if (r.deny || r.isError) return { polled: 0, error: `${o.readProxy} ${o.searchEndpoint}: ${String(r.deny ?? r.text ?? 'search failed').replace(/\s+/g, ' ').slice(0, 200)}` }
    text = String(r.text ?? (typeof r.result === 'string' ? r.result : JSON.stringify(r.result ?? '')))
  } catch (err) {
    return { polled: 0, error: `${o.readProxy} ${o.searchEndpoint}: ${String(err).slice(0, 200)}` }
  }
  const head = text.replace(/\s+/g, ' ').slice(0, 200)
  const tickets = ticketsIn(text)
  // 2026-10-15, workstation: the proxy answered an error body as a normal result and the desk said "Polled 0". The proxy's
  // own words, with the proxy and endpoint tried, are the result the operator needs.
  if (!tickets.length && /"error"|HTTP \d{3}|not found|unauthori|forbidden|denied|invalid|unknown endpoint/i.test(text)) {
    await update($, view, x => ({ ...x, lastPoll: Date.now(), pollError: head, lastResult: head }))
    return { polled: 0, error: `${o.readProxy} ${o.searchEndpoint} for query "${query}" answered: ${head}`, query }
  }
  const v = await desk($, o, ['sync'], JSON.stringify({ tickets }))
  await update($, view, x => ({ ...x, lastPoll: Date.now(), pollError: undefined, lastResult: head }))
  const next = v.next as QueueRow | undefined
  if (v.ok && next && (force || (await read($, view)).poll === 'on')) {
    pull($, o, next.ticket, next.sla ? `SLA ${next.sla}` : next.state === 'reopened' ? 'the customer answered' : next.state === 'stale' ? 'pulled before, nothing filed' : `open ${Math.round(next.age_s / 60)} min`)
    return { polled: tickets.length, pulled: next.ticket }
  }
  await refresh($, o)
  return { polled: tickets.length, head, query }
}

/** The tickets in a search result, whatever the proxy wraps them in. */
const ticketsIn = (text: string): Array<{ id: string; status?: string; sla?: string | null; subject?: string; updated?: string }> => {
  let parsed: unknown
  try {
    parsed = JSON.parse(text)
  } catch {
    return []
  }
  const rows: unknown[] = Array.isArray(parsed) ? parsed
    : (parsed as Record<string, unknown>)?.results instanceof Array ? ((parsed as Record<string, unknown>).results as unknown[])
    : (parsed as Record<string, unknown>)?.tickets instanceof Array ? ((parsed as Record<string, unknown>).tickets as unknown[])
    : []
  return rows
    .filter((t): t is Record<string, unknown> => !!t && typeof t === 'object' && /^\d+$/.test(String((t as Record<string, unknown>).id ?? '')))
    .map(t => ({
      id: String(t.id),
      status: typeof t.status === 'string' ? t.status : undefined,
      sla: typeof t.sla_next_breach_at === 'string' ? t.sla_next_breach_at : typeof t.sla === 'string' ? t.sla : null,
      subject: typeof t.subject === 'string' ? t.subject : undefined,
      updated: typeof t.updated_at === 'string' ? t.updated_at : undefined,
    }))
}

/** The ticket's own status and updated_at in a readback, whatever the proxy wraps the ticket in. */
const ticketFields = (text: string): { status?: string; updated?: string } => {
  try {
    const p = JSON.parse(text) as Record<string, unknown>
    const t = (p.ticket && typeof p.ticket === 'object' ? p.ticket : p) as Record<string, unknown>
    return { status: typeof t.status === 'string' ? t.status : undefined, updated: typeof t.updated_at === 'string' ? t.updated_at : undefined }
  } catch {
    return { status: text.match(/"status"\s*:\s*"([a-z-]+)"/)?.[1] }
  }
}

/** After a granted post: read the ticket back, record its updated_at as the line a customer answer must cross,
 *  and say whether the status moved. */
async function verify($: any, o: Opts, toolName: string, ticket: string, status: string): Promise<void> {
  try {
    const r = await $.tool.call({ tool: toolName, proxy: o.readProxy, endpoint: o.ticketEndpoint, arguments: { ticket_id: ticket } })
    const text = String(r.text ?? JSON.stringify(r.result ?? ''))
    const { status: now, updated } = ticketFields(text)
    if (updated) await desk($, o, ['mark', '--ticket', ticket, '--state', 'sent', '--updated', updated])
    if (now === status) {
      await refresh($, o, `#${ticket} posted, status ${status}`)
    } else {
      await refresh($, o, `#${ticket} posted but status reads ${now ?? 'unknown'}, wanted ${status}`)
      void $.prompt.submit({ text: `The reply on #${ticket} posted, but the ticket status reads "${now ?? 'unknown'}" and the operator wanted "${status}". Set the status to "${status}" with ${o.outwardTool} (no comment) and confirm.` }).catch(() => undefined)
    }
  } catch {
    await refresh($, o, `#${ticket} posted; status not verified`)
  }
}

/** A text trace of one ticket for the transcript (and for sessions with no pane). */
const traceText = (t: Trace): string => {
  const out: string[] = []
  out.push(`#${t.run.ticket} phase ${t.run.phase}, rounds ${t.run.rounds ?? 0}`)
  for (const a of t.run.agents ?? []) out.push(`  ${a.role} ${a.label} (${a.model}) ${a.status}${a.params ? ` [${a.params}]` : ''}${a.summary ? `: ${a.summary.slice(0, 300)}` : ''}`)
  for (const k of ['hypothesis', 'evidence', 'counter', 'objection', 'answer', 'summary']) {
    const rows = (t.findings ?? []).filter(f => f.kind === k)
    if (rows.length) out.push(`${k.toUpperCase()}:`, ...rows.map(f => `  - (${f.agent}) ${f.text}${f.receipts?.length ? ` <${f.receipts.join(',')}>` : ''}`))
  }
  if (t.receipts?.length) out.push('RECEIPTS:', ...t.receipts.map(r => `  ${r.id} ${r.kind}${r.source ? ` ${r.source}${r.page_origin === 'search' ? ' (search result)' : ''}` : ''}: "${r.quote.slice(0, 160)}"`))
  if (t.drafts?.length) {
    const d = t.drafts[t.drafts.length - 1]
    out.push(`DRAFT ${d.md5} ${d.checks.verdict}${d.checks.hits.length ? ` (${d.checks.hits.join(', ')})` : ''}:`, d.text)
    out.push('CLAIMS:', ...(t.claims ?? []).map(c => `  ${c.problems?.length ? c.problems.join(',') : c.echo ? 'echo' : `ok <${(c.receipts ?? []).join(',')}>`}: ${c.text}`))
  }
  return out.join('\n')
}

/** The poll timer, one per loaded module; a reload starts over. */
let timer: { cancel: () => void } | undefined

function startPolling($: any, o: Opts): void {
  if (timer || o.pollMinutes <= 0) return
  timer = $.clock.every(o.pollMinutes * 60_000, () => {
    void (async () => {
      if ((await read($, view)).poll !== 'on') return
      const t = await tick($, o)
      if (t.error) await update($, view, v => ({ ...v, pollError: t.error }))
    })()
  })
}

/** The agent types, registered once per module load: from session.start, and again from the first /desk command in case
 * that registration was lost (a reload, a host that fires no session.start). Idempotent by a module flag. */
let agentsRegistered = false
async function ensureAgents($: any, o: Opts): Promise<void> {
  if (agentsRegistered) return
  agentsRegistered = true
  for (const role of ROLES) {
    try {
      // 2026-10-15, workstation: a desk tool called from inside a spawned agent reached no hook there. With agentTools=mcp
      // the agent type carries the desk's tools as an MCP server (checks/desk_mcp.py, the same desk.py record), served by
      // a process, not a hook. The main session keeps the hook-served tools either way.
      const mcpServers = o.agentTools === 'mcp'
        ? [{ desk: { command: o.python, args: ['-I', `${$.plugin.root}/checks/desk_mcp.py`, '--root', o.stateDir, '--role', role, '--docs-hosts', o.docsHosts] } }]
        : undefined
      await $.agent.register({ name: role, description: `argus-desk ${role}: spawned by the desk on a ticket run`, prompt: `You are the ${role} of the argus-desk review workflow. Follow the task exactly; use only the tools named in it.${mcpServers ? ` The desk tools (${TOOL}, ${SKIP}, ${FINDING}, ${RECEIPT}, ${MISMATCH}) are served by the MCP server named desk.` : ''}`, ...(mcpServers ? { mcpServers } : {}), model: role === 'investigator' ? o.investigatorModel : role === 'adversary' ? o.adversaryModel : role === 'drafter' ? o.drafterModel : o.reviewerModel, maxTurns: 40 })
    } catch {
      /* a host without agent types: the role prompt rides in the task and the gate still holds */
    }
  }
}

export const register: Register = (on, options) => {
  const o = opts(options as Record<string, unknown>)

  on('session.start', async ($, e, next) => {
    await $.command.register({ name: 'desk', description: 'The review desk: the Review, Tickets and Wall panes; /desk review|tickets|wall opens one, /desk show [md5 | N] [node] prints the proof tree, /desk run N, trace N, poll, pull N, pause, resume, stats, audit, verify', argumentHint: '[review | tickets | wall | show | run N | trace N | poll | pull N | pause | resume | stats]' })
    await $.tool.register({
      name: TOOL,
      description:
        'File a customer-facing reply for the operator to review before it is posted. Required before any outward post: ' +
        'the posting tool refuses text the operator has not granted. Give the exact text that would be sent, and citations: ' +
        'for every factual sentence, the receipt id (from desk_receipt) that carries it. When revising a draft the operator sent back, pass supersedes with the md5 you were given.',
      inputSchema: {
        type: 'object',
        properties: {
          ticket: { type: 'string', description: 'The ticket id the reply goes to' },
          draft: { type: 'string', description: 'The exact reply text that would be posted' },
          citations: { type: 'array', items: { type: 'object', properties: { sentence: { type: 'string' }, receipt: { type: 'string' } }, required: ['sentence', 'receipt'] }, description: 'Each factual sentence of the draft with the receipt id that carries it' },
          supersedes: { type: 'string', description: 'md5 of the draft this one replaces, when revising' },
        },
        required: ['ticket', 'draft'],
      },
      isDeferred: false,
    })
    await $.tool.register({
      name: SKIP,
      description: 'Record that a ticket the desk pulled needs no reply from us right now, with the reason. The operator sees it in the desk.',
      inputSchema: { type: 'object', properties: { ticket: { type: 'string' }, reason: { type: 'string' } }, required: ['ticket', 'reason'] },
      isDeferred: false,
    })
    await $.tool.register({
      name: FINDING,
      description: 'Record a finding on a ticket investigation: kind hypothesis, evidence, counter (evidence against), objection (a reviewer\'s), answer (to an objection) or summary. Name the receipt ids that carry it.',
      inputSchema: { type: 'object', properties: { ticket: { type: 'string' }, kind: { type: 'string' }, text: { type: 'string' }, receipts: { type: 'array', items: { type: 'string' } }, about: { type: 'string', description: 'the finding id this answers or objects to' } }, required: ['ticket', 'kind', 'text'] },
      isDeferred: false,
    })
    await $.tool.register({
      name: RECEIPT,
      description: 'Mint a receipt for something you actually read on this ticket: kind "docs" with the sentence verbatim from an official documentation page and its URL (the page must have been fetched in this session), or kind "material" with the customer\'s own words verbatim from the thread. Refused when the quote is not on the page or not in the thread.',
      inputSchema: { type: 'object', properties: { ticket: { type: 'string' }, kind: { type: 'string' }, quote: { type: 'string' }, source: { type: 'string', description: 'the page URL, for docs' } }, required: ['ticket', 'kind', 'quote'] },
      isDeferred: false,
    })
    await $.tool.register({
      name: MISMATCH,
      description: 'Record the basis an escalation needs: the docs receipt id (what the product should do), the material or test receipt id (what it did), and one line on how they differ. Both receipts must be on this ticket and fresh.',
      inputSchema: { type: 'object', properties: { ticket: { type: 'string' }, documented: { type: 'string' }, observed: { type: 'string' }, summary: { type: 'string' } }, required: ['ticket', 'documented', 'observed', 'summary'] },
      isDeferred: false,
    })
    await findOutward($, o)
    await ensureAgents($, o)
    if (e.isInteractive) {
      await update($, view, v => ({ ...v, poll: o.pollMinutes > 0 ? 'on' : 'off' }))
      await refresh($, o)
      if (e.surface === 'terminal') void openDesk($)
      startPolling($, o)
    }
    return next(e)
  })

  for (const role of ROLES) on('agent.offer', { agent: `argus-desk:${role}` }, () => ({ isOffered: false }))

  // The tail: each step's text and tool names, per agent, flushed when the step stops.
  on('turn.step', async function* ($, e, next) {
    const id = (e as { agentId?: string }).agentId
    if (!id || !active.has(id)) return yield* next(e)
    const chunks = next(e)
    for (;;) {
      const n = await chunks.next()
      if (n.done) {
        const text = partial.get(id)
        partial.delete(id)
        if (text?.trim()) await wallLine($, o, id, 'said', text.trim().replace(/\s+/g, ' '))
        return n.value
      }
      const c = n.value as { kind: string; text?: string; name?: string }
      if (c.kind === 'text' && c.text) partial.set(id, (partial.get(id) ?? '') + c.text)
      else if (c.kind === 'tool' && c.name) await wallLine($, o, id, 'tool', c.name.replace(/^mcp__[^_]+__/, ''))
      yield c as any
    }
  })

  on('turn.complete', async ($, e, next) => {
    const id = (e as { agentId?: string }).agentId
    const a = id ? active.get(id) : undefined
    if (id && a) {
      const ev = e as { answer?: string; text?: string; reason?: string; isAborted?: boolean }
      const text = String(ev.answer ?? ev.text ?? '')
      const status = ev.isAborted || ev.reason === 'error' || ev.reason === 'refusal' ? 'failed' : 'completed'
      await wallLine($, o, id, 'done', text.replace(/\s+/g, ' ').slice(0, 200))
      active.delete(id)
      await desk($, o, ['run'], JSON.stringify({ ticket: a.ticket, agent: { id, status, summary: text.slice(0, 600), answer: text, reason: ev.reason } }))
      await update($, view, v => ({ ...v, wall: v.wall.map(w => (w.id === id ? { ...w, status, last: text.slice(0, 160) } : w)) }))
      pending.add(a.ticket)
    }
    return next(e)
  })

  on('command.run', { command: 'desk' }, async ($, e) => {
    const args = (e.args ?? '').trim()
    await ensureAgents($, o)
    startDriver($, o, 'command')
    if (/^stats\b/.test(args)) {
      const r = await $.process.run([o.python, '-I', `${$.plugin.root}/checks/desk.py`, '--root', o.stateDir, 'stats', '--text'], { timeoutMs: 60_000 })
      return { text: r.exitCode === 0 ? r.stdout.trim() : `desk.py failed: ${r.stderr.slice(0, 300)}` }
    }
    if (/^audit\s+\S+$/.test(args)) {
      const a = args.replace(/^audit\s+#?/, '')
      const sel = /^[0-9a-f]{32}$/.test(a) ? ['--md5', a] : ['--ticket', a]
      const r = await $.process.run([o.python, '-I', `${$.plugin.root}/checks/desk.py`, '--root', o.stateDir, 'audit', ...sel, '--text'], { timeoutMs: 60_000 })
      return { text: r.exitCode === 0 ? r.stdout.trim() : `desk.py failed: ${r.stderr.slice(0, 300)}` }
    }
    if (/^verify$/.test(args)) {
      const r = (await desk($, o, ['verify'])) as { ok?: boolean; files?: Record<string, { rows: number; ok: boolean; problem?: string }> }
      const lines = Object.entries(r.files ?? {}).map(([f, v]) => `${v.ok ? 'ok ' : 'BAD'} ${f}: ${v.rows} rows${v.problem ? `, ${v.problem}` : ''}`)
      return { text: `${r.ok ? 'Every chained record verifies.' : 'A record does not verify.'}\n${lines.join('\n')}` }
    }
    if (/^run\s+#?\d+$/.test(args)) {
      const t = args.replace(/^run\s+#?/, '')
      pull($, o, t, 'run by the operator', 'command')
      // Hold the turn until the run ends: a $ call in flight costs the hook nothing, and the host caps one process
      // at ten minutes, so the wait is a loop of them (up to ~45 min).
      let w: Record<string, unknown> = {}
      for (let i = 0; i < 5; i++) {
        w = await desk($, o, ['wait', '--ticket', t, '--timeout', '540'])
        if (w.ok || w.reason !== 'timeout') break
      }
      const tr = (await desk($, o, ['trace', '--ticket', t])) as unknown as Trace
      return { text: `Run on #${t}: phase ${String(w.phase ?? '?')}${w.ok ? '' : ` (${String(w.reason ?? 'not finished')})`}\n\n${tr.ok ? traceText(tr) : ''}` }
    }
    if (/^trace\s+#?\d+$/.test(args)) {
      const t = args.replace(/^trace\s+#?/, '')
      const tr = (await desk($, o, ['trace', '--ticket', t])) as unknown as Trace
      await update($, view, v => ({ ...v, ticket: t, trace: tr.ok ? tr : v.trace, open: [...new Set([...(v.open ?? []), t])] }))
      await openView($, 'tickets')
      return { text: tr.ok ? traceText(tr) : `no trace for #${t}` }
    }
    if (/^pull\s+#?\d+$/.test(args)) {
      const t = args.replace(/^pull\s+#?/, '')
      pull($, o, t, 'pulled by the operator', 'command')
      return { text: `Pulled #${t}: the run starts now; watch the Wall.` }
    }
    if (args === 'poll') {
      const t = await tick($, o, true)
      return { text: t.error ? `Poll failed: ${t.error}` : `Polled ${t.polled} open ticket(s)${t.pulled ? `, pulled #${t.pulled}` : ''}.${t.polled ? '' : ` For query "${t.query ?? ''}" the proxy answered: ${t.head ?? '(empty)'}. If the proxy runs under a service account, assignee:me is not you: /desk query type:ticket status<pending assignee:<your Zendesk user id>`}` }
    }
    if (/^query(\s|$)/.test(args)) {
      const q = args.replace(/^query\s*/, '').trim()
      if (!q) return { text: `Search query: ${await queryOf($, o)}\n/desk query <zendesk search> sets it for every session on this workstation; /desk query default restores the option.` }
      try {
        if (q === 'default') await $.store.delete('searchQuery')
        else await $.store.set('searchQuery', q)
      } catch (err) {
        return { text: `Not kept: ${String(err).slice(0, 200)}` }
      }
      return { text: `Search query now: ${await queryOf($, o)}. /desk poll to try it.` }
    }
    if (args === 'pause' || args === 'resume') {
      await update($, view, v => ({ ...v, poll: args === 'pause' ? 'paused' : 'on' }))
      if (args === 'resume') startPolling($, o)
      return { text: args === 'pause' ? 'Polling paused; /desk resume to start it again.' : 'Polling on.' }
    }
    if (/^show(\s|$)/.test(args)) {
      const [, what, node] = args.split(/\s+/)
      const v = await read($, view)
      const sel = what && /^[0-9a-f]{32}$/.test(what) ? ['--md5', what] : what ? ['--ticket', what.replace(/^#/, '')] : v.selected ? ['--md5', v.selected] : undefined
      if (!sel) return { text: 'Nothing selected: /desk show <md5> or /desk show <ticket>.' }
      const r = await $.process.run([o.python, '-I', `${$.plugin.root}/checks/desk.py`, '--root', o.stateDir, 'tree', ...sel, '--text', ...(node ? ['--node', node] : [])], { timeoutMs: 60_000 })
      return { text: r.exitCode === 0 ? r.stdout.trim() : `desk.py failed: ${(r.stderr || r.stdout).slice(0, 300)}` }
    }
    if (/^(review|tickets|wall|decide|queue)$/.test(args) || /^ticket(\s+#?\d+)?$/.test(args)) {
      const word = args.split(/\s+/)[0]
      const which = (ALIASES[word] ?? word) as View
      const t = args.replace(/^ticket\s+#?/, '')
      await update($, view, v => ({ ...v, tail: undefined, ...(word === 'ticket' && /^\d+$/.test(t) ? { ticket: t, open: [...new Set([...(v.open ?? []), t])] } : {}) }))
      await refresh($, o)
      await openView($, which)
      return { text: `Desk: ${TITLES[which]}. The pane takes the keys after ctrl+x then tab, or a click; every key is drawn as a button on it. 1 to 3 switch views, Tab walks the rows, the arrows scroll, Enter opens one, f finds.` }
    }
    const items = await refresh($, o)
    await openView($, 'review')
    return { text: `${items.length ? `${items.length} draft(s) waiting at the desk.` : 'Nothing waiting at the desk.'}${pending.size ? ` ${pending.size} run(s) start now.` : ''}` }
  })

  // 1. filing: the drafter hands over the exact text and its citations; the desk checks and records it.
  on('tool.call', { tool: `mcp__argus-desk__${TOOL}` }, async ($, e) => {
    const a = e as unknown as { ticket?: string; draft?: string; supersedes?: string; citations?: Array<{ sentence: string; receipt: string }> }
    const ticket = String(a.ticket ?? '')
    let cited = ''
    if (Array.isArray(a.citations) && a.citations.length) {
      const c = await desk($, o, ['cite'], JSON.stringify({ ticket, citations: a.citations }))
      cited = `; citations bound: ${String(c.bound ?? 0)}${(c.unknown as string[] | undefined)?.length ? `, unknown receipts: ${(c.unknown as string[]).join(', ')}` : ''}`
    }
    const r = await desk($, o, ['file'], JSON.stringify({ ticket, draft: String(a.draft ?? ''), supersedes: a.supersedes, source: 'argus' }))
    if (r.ok) await desk($, o, ['mark', '--ticket', String(r.ticket), '--state', 'drafting'])
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
        (claims ? `; claims: ${claims.credited} of ${claims.checkable} receipted` : '') + cited +
        '. It is waiting for the operator at the desk. Do not post it until you are told it was approved; ' +
        'if it comes back with a note, revise and file again with supersedes set to this md5.',
    }
  }).catch(() => ({ deny: 'argus-desk: filing failed; nothing was recorded, try again' }))

  on('tool.call', { tool: `mcp__argus-desk__${SKIP}` }, async ($, e) => {
    const a = e as unknown as { ticket?: string; reason?: string }
    await desk($, o, ['mark', '--ticket', String(a.ticket ?? ''), '--state', 'skipped', '--note', String(a.reason ?? '')])
    await desk($, o, ['finding'], JSON.stringify({ ticket: String(a.ticket ?? ''), agent: active.get(String((e as { agentId?: string }).agentId ?? ''))?.label ?? 'model', role: 'drafter', kind: 'summary', text: `No reply now: ${String(a.reason ?? '')}` }))
    await refresh($, o, `#${String(a.ticket)} skipped: ${String(a.reason ?? '').slice(0, 60)}`)
    return { result: `Recorded: #${String(a.ticket)} needs no reply now. The desk will pull the next open ticket.` }
  }).catch(() => ({ deny: 'argus-desk: the skip was not recorded, try again' }))

  on('tool.call', { tool: `mcp__argus-desk__${FINDING}` }, async ($, e) => {
    const a = e as unknown as { ticket?: string; kind?: string; text?: string; receipts?: string[]; about?: string; agentId?: string }
    const who = active.get(String(a.agentId ?? ''))
    const r = await desk($, o, ['finding'], JSON.stringify({ ticket: String(a.ticket ?? ''), agent: who?.label ?? 'model', role: who?.role ?? 'model', kind: String(a.kind ?? ''), text: String(a.text ?? ''), receipts: a.receipts, about: a.about }))
    if (a.agentId) await wallLine($, o, a.agentId, String(a.kind ?? 'finding'), String(a.text ?? ''))
    return { result: r.ok ? `Recorded ${String(r.id)} (${String(a.kind)}).` : `Not recorded: ${String(r.reason)}` }
  }).catch(() => ({ deny: 'argus-desk: the finding was not recorded, try again' }))

  on('tool.call', { tool: `mcp__argus-desk__${RECEIPT}` }, async ($, e) => {
    const a = e as unknown as { ticket?: string; kind?: string; quote?: string; source?: string; agentId?: string }
    const who = active.get(String(a.agentId ?? ''))
    const r = await desk($, o, ['receipt'], JSON.stringify({ ticket: String(a.ticket ?? ''), kind: String(a.kind ?? ''), quote: String(a.quote ?? ''), source: a.source, by: who?.label }))
    // a refused receipt is on the record too: the agent claimed to have read something the capture does not hold
    if (a.agentId && !r.ok) await wallLine($, o, a.agentId, 'receipt-refused', `${String(a.kind)} ${String(r.why ?? r.reason)}: "${String(a.quote ?? '').slice(0, 120)}"${a.source ? ` (${String(a.source)})` : ''}`)
    return { result: r.ok ? `Receipt ${String(r.id)}.` : `Refused: ${String(r.why ?? r.reason)}. Only a sentence you actually read, verbatim, can be receipted${refusalDetail(r)}.` }
  }).catch(() => ({ deny: 'argus-desk: the receipt was not minted, try again' }))

  on('tool.call', { tool: `mcp__argus-desk__${MISMATCH}` }, async ($, e) => {
    const a = e as unknown as { ticket?: string; documented?: string; observed?: string; summary?: string; agentId?: string }
    const who = active.get(String(a.agentId ?? ''))
    const r = await desk($, o, ['mismatch'], JSON.stringify({ ticket: String(a.ticket ?? ''), documented: String(a.documented ?? ''), observed: String(a.observed ?? ''), summary: String(a.summary ?? ''), by: who?.label }))
    if (a.agentId) await wallLine($, o, a.agentId, r.ok ? 'mismatch' : 'receipt-refused', `${String(r.id ?? r.why ?? r.reason)}: ${String(a.summary ?? '').slice(0, 120)}`)
    return { result: r.ok ? `Mismatch ${String(r.id)}.` : `Refused: ${String(r.why ?? r.reason)}.` }
  }).catch(() => ({ deny: 'argus-desk: the mismatch was not recorded, try again' }))

  // 2. the gate: an outward call passes only by spending an open grant on the exact text it carries.
  // A documentation search (enterprise_search by default): the result is a call on the record and a page per official URL it
  // carries, so a docs receipt can quote it. No outbound internet on the workstation; this is the docs leg there.
  // The record guard: the grants and the chained record are written only by the desk through $.process.run. A shell
  // command or a file edit from the model that names desk.py or the state directory could mint a grant or rewrite
  // the record, so it is refused (option guardRecord=false turns this off, for working on the desk itself).
  on('tool.call', async ($, e, next) => {
    const tool = String(e.tool)
    if (o.guardRecord && GUARDED.test(tool.replace(/^mcp__[^_]+__/, ''))) {
      const hit = namesRecord(o, e as unknown as Record<string, unknown>)
      if (hit) {
        const who = active.get(String((e as { agentId?: string }).agentId ?? ''))
        if (who) await desk($, o, ['event'], JSON.stringify({ ticket: who.ticket, agent: who.label, kind: 'guard', text: `${tool} refused: names ${hit}` }))
        return { deny: `argus-desk: ${tool} names ${hit}, which only the desk may write (its grants and its record). The operator runs desk commands in a terminal, not through the model.` }
      }
    }
    if (o.docsTool && tool.includes(o.docsTool) && !tool.includes(o.outwardTool)) {
      const ran = (await next(e)) as { text?: string; result?: unknown; isError?: boolean }
      try {
        const agentId = (e as { agentId?: string }).agentId
        const who = agentId ? active.get(agentId) : undefined
        const args = Object.fromEntries(Object.entries(e as Record<string, unknown>).filter(([k]) => !RESERVED.has(k)))
        const text = ran?.text ?? (typeof ran?.result === 'string' ? ran.result : ran?.result ? JSON.stringify(ran.result) : '')
        if (agentId) await wallLine($, o, agentId, 'docs', JSON.stringify(args).slice(0, 100))
        if (who) await desk($, o, ['call'], JSON.stringify({ ticket: who.ticket, agent: who.label, tool, args, result: text || JSON.stringify(ran ?? null), ok: !ran?.isError }))
        // a search result is kept under each official URL it names, marked as a search result, not the page
        if (text) for (const url of docsUrls(o, text)) await desk($, o, ['page'], JSON.stringify({ source: url, body: text, origin: 'search' }))
      } catch {
        /* the search ran; a failure to record it must not run it again */
      }
      return ran
    }
    if (!tool.includes(o.outwardTool)) return next(e)
    outwardName = tool
    const agentId = (e as { agentId?: string }).agentId
    const args = normalize(Object.fromEntries(Object.entries(e as Record<string, unknown>).filter(([k]) => !RESERVED.has(k))))
    if (!isOutward(args)) {
      // A read: let it run; capture comments as the live thread, and documentation pages for receipts.
      const ran = await next(e)
      try {
        const ticket = ticketOf(args)
        const text = (ran as { text?: string }).text
        if (agentId) await wallLine($, o, agentId, 'read', `${String(args.proxy ?? '')} ${String(args.endpoint ?? '')} ${JSON.stringify(args.arguments ?? {}).slice(0, 100)}`)
        // the call on the record, whole: who read what, with what arguments, and a hash of what came back
        const who = agentId ? active.get(agentId) : undefined
        if (ticket || who) await desk($, o, ['call'], JSON.stringify({ ticket: who?.ticket ?? ticket, agent: who?.label ?? 'main', tool, args, result: text ?? JSON.stringify(ran ?? null), ok: !(ran as { deny?: unknown }).deny }))
        // 2026-10-15 (receipt fault): the comments read is the thread whatever the proxy wraps it in (a data or result envelope, a
        // JSON string inside JSON), whatever the agent named the ticket argument (the agent's own ticket stands in), and
        // whichever body field the proxy filled. A read that reached the hook but was not recognised left the desk with no
        // thread, and every verbatim customer quote was then refused as "quote-not-in-material".
        const forTicket = ticket ?? who?.ticket
        if (forTicket && text && /\\?"comments\\?"\s*:\s*\[/.test(text)) {
          try {
            const found = commentsIn(JSON.parse(text))
            if (found) {
              const messages = found.comments.map((c, i) => ({
                id: String(c.id ?? i),
                // Internal notes are staff: bots post several on every new ticket, and counting them as our replies held
                // every draft on cadence (too-many). Only a public comment from our side is a reply.
                who: c.public === false ? 'staff' : isOurs(c) ? 'us' : 'customer',
                at: typeof c.created_at === 'string' ? Date.parse(c.created_at) / 1000 : undefined,
                text: commentText(c),
              }))
              await desk($, o, ['capture'], JSON.stringify({ ticket: forTicket, messages, count: found.count ?? found.comments.length }))
            }
          } catch {
            /* not a comments payload after all */
          }
        }
        // the salutation check (the fleet's requester-name gate) needs the name as the ticket's requester field shows it
        const requester = forTicket && text ? requesterIn(text) : undefined
        if (forTicket && requester) await desk($, o, ['requester', '--ticket', forTicket, '--name', requester])
        if (text) {
          // a URL the call itself named was fetched: the body is that page; one only named in the result is not
          const asked = new Set(docsUrls(o, JSON.stringify(args)))
          for (const url of docsUrls(o, JSON.stringify(args), text.slice(0, 4000))) await desk($, o, ['page'], JSON.stringify({ source: url, body: text, origin: asked.has(url) ? 'fetch' : 'search' }))
        }
      } catch {
        /* the read ran; a failure to record it must not run it again */
      }
      return ran
    }
    const flat = JSON.stringify(args)
    const who = agentId ? active.get(agentId) : undefined
    const found = bodies(args)
    const refuse = async (note: string, deny: string) => {
      const t = who?.ticket ?? ticketOf(args)
      if (t) await desk($, o, ['call'], JSON.stringify({ ticket: t, agent: who?.label ?? 'main', tool, args, ok: false, note }))
      return { deny }
    }
    if (!found.length) return refuse('POST refused: no comment body', `argus-desk: this call would post to the customer and carries no comment body (a field named body, html_body or plain_body). Only a public comment whose body is the exact text the operator granted can go out. File the reply with ${TOOL} and wait for the operator's Send.`)
    if (found.some(b => b.internal)) return refuse('POST refused: comment not public', 'argus-desk: the approved reply must be a public comment to the customer; this call marks it public: false. Repeat the call with public: true. The approval is still open.')
    const r = await desk($, o, ['consume'], JSON.stringify({ candidates: found.map(b => b.text), what: `${tool} ${flat.slice(0, 120)}`, status: statusOf(args) }))
    const postTicket = (r.ticket as string | undefined) ?? who?.ticket ?? ticketOf(args)
    if (r.ok) {
      const ran = await next(e)
      try {
        if (postTicket) await desk($, o, ['call'], JSON.stringify({ ticket: postTicket, agent: who?.label ?? 'main', tool, args, result: (ran as { text?: string }).text ?? JSON.stringify(ran ?? null), ok: !(ran as { deny?: unknown }).deny, note: `POST granted ${String(r.md5)}` }))
        await refresh($, o, `sent ${String(r.md5).slice(0, 8)}`)
        if (r.ticket && r.status) await verify($, o, tool, String(r.ticket), String(r.status))
      } catch {
        /* the post ran and the grant is spent; a failure to record it must never run it again */
      }
      return ran
    }
    if (r.code === 'status-missing') {
      return refuse(`POST refused: status-missing (wanted ${String(r.status)}, given ${String(r.given ?? 'none')})`,
        `argus-desk: the operator approved this text with the ticket status set to "${String(r.status)}", and this call sets ${r.given ? `"${String(r.given)}"` : 'no status'} on the ticket. ` +
        `Repeat the call with the same text and status "${String(r.status)}" on the ticket update. The approval is still open.`)
    }
    if (r.code === 'body-mismatch') {
      return refuse('POST refused: body-mismatch', 'argus-desk: this call carries the granted text and another body text besides it. A granted post carries the granted text as its one comment body and nothing else to the customer. Repeat the call with only the approved text. The approval is still open.')
    }
    const undecided = (r.undecided as string[] | undefined) ?? []
    return refuse(`POST refused: ${String(r.code ?? r.reason ?? 'no grant')}`,
      `argus-desk: this call would post to the customer and carries no text the operator has granted. ` +
      (undecided.length ? `Waiting at the desk: ${undecided.map(m => m.slice(0, 8)).join(', ')}. ` : 'Nothing is filed. ') +
      `File the exact reply with ${TOOL} and wait for the operator's Send.`)
  }).catch(($, e, next) => {
    if (!String(e.tool).includes(o.outwardTool)) return next(e)
    // Nothing after next(e) can throw above, so next.called here means the post ran: never run it again.
    return next.called
      ? { deny: 'argus-desk: the post ran and its grant is spent, but the desk could not record it. Do not repeat the post; tell the operator.' }
      : { deny: 'argus-desk: the gate could not decide, so the post is refused.' }
  })

  // 3. the pane: Wall, Queue, Ticket, Decide.
  on('ui.focus', ($, e, next) => {
    if (e.element && e.requestId === PANE) ring.set(e.requestId, e.element)
    return next(e)
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => drawPane($, o, e, (await read($, view)).which ?? 'review'))
}

// ---- the views: Review (the proof tree behind the selected draft), Tickets (the queue; a row opens to its thread, run
// and drafts) and Wall (every agent live). Three views in one pane, so 1 to 3 switch them from inside it (see PANE).
// Every row is a control the focus ring walks: Tab moves the ring, Enter opens, the arrows scroll; the engine keeps the
// ring in view. The operator's rule: every key that does something is drawn as a labelled button (`z: zoom`), on bars
// above the rows so they show when a view opens; nothing works that is not on screen. Arrows, Tab, Enter and Esc are the engine's: they walk the rows and act on one.

const KEYS_OFF = 'keys off: press ctrl+x then tab, or click the pane, to take the keys'
/** Every bar of keys: a gap between the buttons, wrapping onto the next row rather than running off the pane. */
const BAR = { flexWrap: 'wrap', columnGap: 3 } as const
const DEFAULT_OPEN = ['issues', 'reply']

const nodeKey = (id: string) => `n:${id}`
const idOf = (key: string | undefined): string | undefined => (key && key.startsWith('n:') ? key.slice(2) : undefined)

function findNode(n: TreeNode | undefined, id: string): TreeNode | undefined {
  if (!n) return undefined
  if (n.id === id) return n
  for (const c of n.children) {
    const hit = findNode(c, id)
    if (hit) return hit
  }
  return undefined
}
function pathTo(n: TreeNode | undefined, id: string, acc: string[] = []): string[] | undefined {
  if (!n) return undefined
  if (n.id === id) return [...acc, id]
  for (const c of n.children) {
    const p = pathTo(c, id, [...acc, n.id])
    if (p) return p
  }
  return undefined
}
const descendants = (n: TreeNode): string[] => [n.id, ...n.children.flatMap(descendants)]
const matches = (n: TreeNode, f: string): boolean => n.label.toLowerCase().includes(f) || (n.body ?? '').toLowerCase().includes(f)
const hasMatch = (n: TreeNode, f: string): boolean => matches(n, f) || n.children.some(c => hasMatch(c, f))

async function loadTree($: any, o: Opts, md5: string, node?: string): Promise<TreeResult> {
  return (await desk($, o, ['tree', '--md5', md5, '--status', o.sendStatus, '--via', `${o.writeProxy}/${o.updateEndpoint}`, ...(node ? ['--node', node] : [])])) as unknown as TreeResult
}

/** Make one draft the Review's: selected, its tree loaded, the pane opened. */
async function selectDraft($: any, o: Opts, md5: string, open = true, requestId?: string): Promise<void> {
  const v = await read($, view)
  const item = v.items.find(i => i.md5 === md5)
  await update($, view, x => ({ ...x, selected: md5, ticket: item?.ticket ?? x.ticket, mode: 'list' }))
  await refresh($, o)
  if (open) await openView($, 'review', true, requestId)
}

/** Expand the path down to a node, leave any zoom, and put the ring on it. */
async function reveal($: any, target: string, requestId?: string): Promise<void> {
  const v = await read($, view)
  const path = pathTo(v.tree?.tree, target)
  if (!path) return
  await update($, view, x => ({ ...x, zoom: [], expanded: [...new Set([...(x.expanded ?? DEFAULT_OPEN), ...path])], last: target }))
  if (requestId) await $.ui.focus({ requestId, key: nodeKey(target) }).catch(() => undefined)
}

async function toggleNode($: any, o: Opts, n: TreeNode): Promise<void> {
  const v = await read($, view)
  if (n.body_file && !n.body && !v.bodies?.[n.id] && v.tree?.md5) {
    const r = await loadTree($, o, v.tree.md5, n.id)
    const body = r.ok ? String(r.tree?.body ?? '') : `not loaded: ${String(r.reason ?? '')}`
    await update($, view, x => ({ ...x, bodies: { ...(x.bodies ?? {}), [n.id]: body } }))
  }
  await update($, view, x => {
    const ex = new Set(x.expanded ?? DEFAULT_OPEN)
    if (ex.has(n.id)) ex.delete(n.id)
    else ex.add(n.id)
    return { ...x, expanded: [...ex], last: n.id }
  })
}

/** The node an action acts on: the one under the focus ring, else the row last pressed. */
const acted = (v: DeskView, requestId: string): string | undefined => idOf(ring.get(requestId)) ?? v.last

async function drawPane($: any, o: Opts, e: any, which: View) {
  const { Box, Text, Button, Input } = $.ui.resolve(e) as any
  const v = await read($, view)
  const width = Math.max(40, e.props.bodyColumns ?? 80)
  const focused = e.props.isFocused === true
  const f = (v.filter?.[which] ?? '').trim().toLowerCase()
  const cut = (t: string, n = width - 4) => t.replace(/\s+/g, ' ').slice(0, n)
  const ago = (t: number) => `${Math.max(0, Math.round((Date.now() - t) / 60000))}m`
  const match = (...parts: Array<string | undefined>) => !f || parts.some(p => p && p.toLowerCase().includes(f))
  // the filter applies on Enter, and the ring leaves the field for the first row left: in the field, every key is text
  const setFilter = (text: string, requestId?: string) => void update($, view, x => ({ ...x, filter: { ...(x.filter ?? {}), [which]: text.trim() } })).then(async () => {
    const key = requestId ? firstRow(await read($, view), which) : undefined
    if (key) await $.ui.focus({ requestId, key }).catch(() => undefined)
  })
  const sel = v.items.find(i => i.md5 === v.selected) ?? v.items[0]
  /** Modify and Deny ask for one line: the field opens and takes the ring. */
  const askFor = async (requestId: string, mode: 'modify' | 'deny') => {
    await update($, view, x => ({ ...x, selected: sel.md5, mode }))
    await $.ui.focus({ requestId, key: mode === 'modify' ? 'modify-note' : 'deny-reason' }).catch(() => undefined)
  }
  let first = true
  const focusFirst = () => { if (!first) return {}; first = false; return { autoFocus: true as const } }

  const nav = (
    <Box key="nav" {...BAR}>
      {VIEWS.map((id, i) => (
        <Button key={`go-${id}`} plain hotkey={String(i + 1)} dimColor={id !== which} onPress={(p: any) => void openView($, id, true, p.requestId)}>{id === which ? `[${TITLES[id]}]` : TITLES[id]}</Button>
      ))}
      <Text dimColor>{v.items.length} waiting, {active.size} agents, poll {v.poll}{v.notice ? `  (${v.notice})` : ''}</Text>
    </Box>
  )
  const keysOff = focused ? null : <Text key="keys-off" color="yellow">{KEYS_OFF}</Text>
  const finder = (
    <Box key="finder" columnGap={1}>
      <Button key="find" plain hotkey="f" onPress={(p: any) => void $.ui.focus({ requestId: p.requestId, key: 'filter' }).catch(() => undefined)}>find</Button>
      <Input key="filter" placeholder="type, then Enter, to narrow the rows" value={v.filter?.[which] ?? ''} submitLabel="narrow" onSubmit={(txt: string, p: any) => setFilter(txt, p?.requestId)} />
      {f ? <Button key="clear" plain hotkey="c" onPress={(p: any) => setFilter('', p.requestId)}>clear</Button> : null}
    </Box>
  )
  const body: any[] = []
  /** The view's keys, drawn above its rows so they are on screen when the view opens, whatever the length of the rows. */
  const keys: any[] = []
  const text = (key: string, t: string, color?: string, dim?: boolean) => body.push(<Text key={key} color={color} dimColor={dim}>{t}</Text>)
  const row = (key: string, t: string, press: (p?: any) => void, color?: string, dim?: boolean) =>
    body.push(<Button key={key} plain dimColor={dim} {...focusFirst()} onPress={press}>{color ? <Text color={color}>{t}</Text> : t}</Button>)

  if (which === 'review') {
    const tree = v.tree?.ok ? v.tree.tree : undefined
    const zoom = v.zoom ?? []
    const zoomId = zoom[zoom.length - 1]
    const expanded = new Set(v.expanded ?? DEFAULT_OPEN)
    if (!sel) {
      text('r0', `No draft at the desk. The drafter files one with ${TOOL}; its proof shows here, and nothing posts without a Send.`, undefined, true)
    } else if (!tree) {
      // the draft is still the operator's to decide when its tree did not build: the text and the checks at filing stand in
      text('r0', `The proof tree did not build: ${String(v.tree?.reason ?? 'desk.py gave no tree')}. The draft and its checks at filing:`, 'yellow')
      text('r1', `#${sel.ticket} draft ${sel.md5.slice(0, 8)} ${sel.checks.verdict === 'hold' ? `HOLD ${sel.checks.hits.join(',')}` : 'clear'} | thread: ${sel.checks.thread?.status === 'captured' ? `${sel.checks.thread.messages} of ${sel.checks.thread.console_count} comments recorded` : 'NOT captured'} | claims: ${sel.checks.claims ? `${sel.checks.claims.credited} of ${sel.checks.claims.checkable} receipted` : 'n/a'}${sel.checks.warnings.length ? ` | ${sel.checks.warnings.join(', ')}` : ''}`, undefined, true)
      for (const [n, l] of sel.text.split('\n').entries()) text(`dl-${n}`, l || ' ')
    } else {
      const crumbs = [tree.label, ...zoom.map(id => cut(findNode(tree, id)?.label ?? id, 40))]
      text('crumbs', crumbs.join('  >  '), 'cyan')
      const walk = (n: TreeNode, depth: number) => {
        if (f && !hasMatch(n, f)) return
        const open = Boolean(f) || n.id === zoomId || expanded.has(n.id)
        // a row cut to the pane's width opens to its whole text: a customer message, an objection, a sentence
        const room = width - 6 - depth * 2
        const isCut = n.label.replace(/\s+/g, ' ').length > room
        const hidden = (n.body ?? v.bodies?.[n.id] ?? '').trim()
        const glyph = n.children.length || isCut || (hidden && !n.label.includes(hidden)) || n.body_file ? (open ? '-' : '+') : '·'
        const dim = n.state === 'answered' || n.state === 'asserts-nothing' || n.state === 'overruled'
        row(nodeKey(n.id), `${'  '.repeat(depth)}${glyph} ${cut(n.label, room)}`, () => void toggleNode($, o, n), n.color, dim)
        if (!open) return
        // the whole text in a box indented under its row, so its wrapped lines keep the row's indent
        if (isCut) body.push(<Box key={`l:${n.id}`} flexDirection="column" paddingLeft={(depth + 2) * 2}>{n.label.split('\n').map((l, i) => <Text key={`l:${n.id}:${i}`}>{l || ' '}</Text>)}</Box>)
        const b = (n.body ?? v.bodies?.[n.id] ?? '').trim()
        if (b && !n.label.includes(b)) for (const [i, l] of b.split('\n').entries()) text(`b:${n.id}:${i}`, `${'  '.repeat(depth + 2)}${l || ' '}`, undefined, true)
        for (const c of n.children) walk(c, depth + 1)
      }
      const root = zoomId ? findNode(tree, zoomId) : undefined
      if (root) walk(root, 0)
      else for (const c of tree.children) walk(c, 0)
      keys.push(
        <Box key="r-nav" {...BAR}>
          <Button key="zoom" plain hotkey="z" onPress={(p: any) => { const id = acted(v, p.requestId); if (id && id !== zoomId) void update($, view, x => ({ ...x, zoom: [...(x.zoom ?? []), id], expanded: [...new Set([...(x.expanded ?? DEFAULT_OPEN), id])] })) }}>zoom into the row</Button>
          <Button key="up" plain hotkey="u" onPress={() => void update($, view, x => ({ ...x, zoom: (x.zoom ?? []).slice(0, -1) }))}>up</Button>
          <Button key="expand-all" plain hotkey="x" onPress={(p: any) => { const id = acted(v, p.requestId); const n = id ? findNode(tree, id) : undefined; if (n) void update($, view, x => ({ ...x, expanded: [...new Set([...(x.expanded ?? DEFAULT_OPEN), ...descendants(n)])] })) }}>expand all beneath</Button>
          <Button key="follow" plain hotkey="g" onPress={(p: any) => { const id = acted(v, p.requestId); const n = id ? findNode(tree, id) : undefined; if (n?.target) void reveal($, n.target, p.requestId) }}>go to what the row is about</Button>
        </Box>,
      )
      keys.splice(0, 0,
        <Box key="r-jump" {...BAR}>
          <Button key="jump-issues" plain hotkey="i" onPress={(p: any) => void reveal($, 'issues', p.requestId)}>issues</Button>
          <Button key="jump-ask" plain hotkey="t" onPress={(p: any) => void reveal($, 'ask:thread', p.requestId)}>thread</Button>
          <Button key="jump-reply" plain hotkey="r" onPress={(p: any) => void reveal($, 'reply', p.requestId)}>reply</Button>
          <Button key="jump-run" plain hotkey="w" onPress={(p: any) => void reveal($, 'run', p.requestId)}>who did what</Button>
          <Button key="open-ticket" plain hotkey="o" onPress={(p: any) => { void update($, view, x => ({ ...x, ticket: sel.ticket, open: [...new Set([...(x.open ?? []), sel.ticket])] })).then(() => openView($, 'tickets', true, p.requestId)) }}>the ticket</Button>
          {v.items.length > 1 ? <Button key="next" plain hotkey="n" onPress={() => { const i = v.items.findIndex(it => it.md5 === sel.md5); void selectDraft($, o, v.items[(i + 1) % v.items.length].md5, false) }}>next draft</Button> : null}
        </Box>,
      )
    }
    if (sel) {
      const open = v.tree?.open ?? 0
      if (v.mode === 'list') keys.unshift(
        <Box key="r-decide" {...BAR}>
          <Button key="send" plain hotkey="s" onPress={(p: any) => void decide($, o, sel.md5, 'send', '', `${p.requestId}-${sel.md5.slice(0, 8)}-${Date.now()}`)}>Send, {o.sendStatus}{open ? ` (${open} open)` : ''}</Button>
          <Button key="solve" plain hotkey="v" onPress={(p: any) => void decide($, o, sel.md5, 'send', '', `${p.requestId}-${sel.md5.slice(0, 8)}-${Date.now()}`, 'solved')}>Send, solved</Button>
          <Button key="modify" plain hotkey="m" onPress={(p: any) => void askFor(p.requestId, 'modify')}>Modify</Button>
          <Button key="deny" plain hotkey="d" onPress={(p: any) => void askFor(p.requestId, 'deny')}>Deny</Button>
        </Box>,
      )
      // the field takes the ring as it opens, so the words typed are the note, not the view's keys; cancel (Tab, Enter) leaves
      // the draft undecided. Esc hands the keys back to the prompt and the field stays, with its cancel beside it.
      const cancel = <Button key="cancel" plain onPress={() => void update($, view, x => ({ ...x, mode: 'list' })).then(() => ringToFirstRow($))}>cancel, keep the draft undecided</Button>
      if (v.mode === 'modify') keys.unshift(
        <Box key="r-ask" {...BAR}>
          <Input key="modify-note" label="what to change" placeholder="one line for the drafter, then Enter" autoFocus submitLabel="return" onSubmit={(txt: string, p: any) => void decide($, o, sel.md5, 'modify', txt, p.requestId)} />
          {cancel}
        </Box>,
      )
      if (v.mode === 'deny') keys.unshift(
        <Box key="r-ask" {...BAR}>
          <Input key="deny-reason" label="why" placeholder="recorded with the deny, then Enter" autoFocus submitLabel="deny" onSubmit={(txt: string, p: any) => void decide($, o, sel.md5, 'deny', txt, p.requestId)} />
          {cancel}
        </Box>,
      )
    }
  } else if (which === 'tickets') {
    text('q0', `poll ${v.poll}${v.lastPoll ? `, last ${ago(v.lastPoll)} ago` : ', none yet'}${v.pollError ? `, failed: ${v.pollError}` : ''}${!driver && pending.size ? `; ${pending.size} run(s) waiting: type /desk` : ''}`, undefined, true)
    if (v.lastResult && !v.pollError) text('q0r', `  proxy answered: ${cut(v.lastResult, width - 20)}`, undefined, true)
    const opened = new Set(v.open ?? [])
    const rows = v.queue.filter(q => match(q.ticket, q.subject, q.state))
    if (!v.queue.length) text('q1', 'No open tickets seen. /desk poll asks now; /desk query shows the search.', undefined, true)
    else if (!rows.length) text('q1', `No ticket matches "${f}".`, undefined, true)
    for (const q of rows) {
      const isOpen = opened.has(q.ticket)
      row(`ticket:${q.ticket}`, `${isOpen ? '-' : '+'} #${q.ticket} ${q.state}${q.sla ? ` SLA ${q.sla.slice(5, 16)}` : ''} ${Math.round(q.age_s / 60)}m  ${cut(q.subject ?? '', width - 36)}`,
        () => void update($, view, x => ({ ...x, ticket: q.ticket, open: isOpen ? (x.open ?? []).filter(t => t !== q.ticket) : [...(x.open ?? []), q.ticket] })), undefined, q.state === 'skipped' || q.state === 'sent')
      if (!isOpen) continue
      text(`th:${q.ticket}`, `    thread: ${q.thread ? `${q.thread.messages} of ${q.thread.count ?? '?'} comments recorded${q.thread.fetched_at ? ` ${ago(q.thread.fetched_at * 1000)} ago` : ''}` : 'not recorded'}`, undefined, true)
      text(`run:${q.ticket}`, `    run: ${q.run ? `phase ${q.run.phase}, rounds ${q.run.rounds ?? 0}, agents ${q.run.agents ?? 0}` : 'none'}`, undefined, true)
      const drafts = v.items.filter(i => i.ticket === q.ticket)
      if (!drafts.length) text(`d0:${q.ticket}`, '    drafts: none waiting', undefined, true)
      // one line per draft, cut to the pane: a long HOLD list wrapped the row into the next one
      for (const i of drafts) row(`draft:${i.md5}`, `    ${cut(`draft ${i.md5.slice(0, 8)} ${i.checks.verdict === 'hold' ? `HOLD ${i.checks.hits.join(',')}` : 'clear'} ${i.state} ${Math.round(i.age_s / 60)}m  ${i.text}`, width - 8)}`, (p: any) => void selectDraft($, o, i.md5, true, p?.requestId))
    }
    const under = (requestId: string): string | undefined => {
      const k = ring.get(requestId)
      const t = k?.startsWith('ticket:') ? k.slice(7) : k?.startsWith('draft:') ? v.items.find(i => i.md5 === k.slice(6))?.ticket : undefined
      return t ?? v.ticket
    }
    keys.push(
      <Box key="q-actions" {...BAR}>
        <Button key="pull" plain hotkey="p" onPress={(p: any) => { const id = under(p.requestId); if (id) pull($, o, id, 'pulled by the operator at the desk') }}>pull the ticket under the ring</Button>
        <Button key="reread" plain hotkey="r" onPress={(p: any) => { const id = under(p.requestId); if (id) void $.prompt.submit({ text: `Read every comment on ticket #${id} again now with ${o.outwardTool} (proxy ${o.readProxy}, endpoint ${o.commentsEndpoint}, arguments {"ticket_id":"${id}"}), so the desk records the thread afresh. Do not post anything.` }).catch(() => undefined) }}>re-read its thread</Button>
        <Button key="open-review" plain hotkey="o" onPress={(p: any) => { const id = under(p.requestId); const d = v.items.find(i => i.ticket === id); if (d) void selectDraft($, o, d.md5, true, p.requestId) }}>its draft in Review</Button>
      </Box>,
    )
  } else {
    const tail = v.tail ? v.wall.find(w => w.id === v.tail) : undefined
    if (tail) {
      body.push(
        <Box key="tail-head">
          <Button key="back" plain hotkey="b" autoFocus onPress={() => void update($, view, x => ({ ...x, tail: undefined }))}>back</Button>
          <Text color="cyan">  #{tail.ticket} {tail.role} {tail.label}  {tail.status}  {ago(tail.started)}</Text>
        </Box>,
      )
      if (!tail.lines.length) text('tail-0', 'Nothing said yet.', undefined, true)
      tail.lines.forEach((l, i) => text(`tail-${i}`, l))
    } else {
      const byTicket = new Map<string, WallAgent[]>()
      for (const w of v.wall) byTicket.set(w.ticket, [...(byTicket.get(w.ticket) ?? []), w])
      if (!byTicket.size) text('w0', 'No agents yet. A poll or /desk run N starts a run; every agent shows here as it works, Enter tails one.', undefined, true)
      for (const [ticket, agents] of byTicket) {
        const rows = agents.filter(a => match(ticket, a.role, a.label, a.last, a.status))
        if (!rows.length) continue
        text(`w-${ticket}`, `#${ticket}${v.trace?.run?.ticket === ticket ? `  phase ${v.trace.run.phase}` : ''}  ${rows.filter(a => a.status === 'running').length} running`, 'cyan')
        for (const a of rows) {
          const glyph = a.status === 'running' ? '*' : a.status === 'pending' ? '.' : a.status === 'failed' ? 'x' : 'v'
          row(`agent:${a.id}`, `${glyph} ${a.role} ${a.label} ${ago(a.started)}  ${cut(a.last, width - 30)}`, () => void update($, view, x => ({ ...x, tail: a.id })), a.status === 'failed' ? 'red' : undefined, a.status !== 'running')
        }
      }
    }
  }

  return (
    <Box flexDirection="column">
      {nav}
      {keysOff}
      {finder}
      {keys}
      {body}
    </Box>
  )
}
