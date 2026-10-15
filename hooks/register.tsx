import { atom, read, update } from 'claude-code'
import type { Register } from 'claude-code'

import type { DeskItem, DeskView, QueueRow, Trace, WallAgent } from '../types'

// argus-desk: the review desk and the investigation it reviews.
//   1. the queue     a timer searches the open set and pulls one ticket at a time.
//   2. the run       a tree of subagents per ticket: investigators from three angles, an adversary, a drafter,
//                    a reviewer that may only object. Everything they read is a receipt; everything they conclude
//                    is a finding; every step is a line on the wall.
//   3. file_draft    the drafter files the exact reply with its citations; the desk checks it.
//   4. the pane      Wall, Queue, Ticket (the trace), Decide: Send, Modify or Deny.
//   5. the gate      an outward call passes only by spending a grant on the exact text, with the status chosen.
// State and checks live in checks/desk.py (stdlib Python), one process per call; the mod is the binding.

const PANE = 'argus-desk'
const TOOL = 'file_draft'
const SKIP = 'skip_ticket'
const FINDING = 'desk_finding'
const RECEIPT = 'desk_receipt'
const STATUSES = ['pending', 'solved', 'open', 'on-hold']
const ROLES = ['investigator', 'adversary', 'drafter', 'reviewer'] as const
type Role = (typeof ROLES)[number]

const view = atom({ plugin: 'argus-desk', key: 'view' } as const, { items: [], queue: [], wall: [], mode: 'list', poll: 'off', tab: 'decide', scroll: 0 } as DeskView)

type Opts = {
  stateDir: string; python: string; outwardTool: string
  pollMinutes: number; readProxy: string; writeProxy: string; searchEndpoint: string; searchQuery: string
  commentsEndpoint: string; ticketEndpoint: string; updateEndpoint: string; sendStatus: string
  docsHosts: string; investigatorModel: string; adversaryModel: string; drafterModel: string; reviewerModel: string; maxRounds: number
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
  docsHosts: String(o.docsHosts ?? 'docs.wiz.io'),
  investigatorModel: String(o.investigatorModel ?? 'sonnet'),
  adversaryModel: String(o.adversaryModel ?? 'opus'),
  drafterModel: String(o.drafterModel ?? 'opus'),
  reviewerModel: String(o.reviewerModel ?? 'sonnet'),
  maxRounds: Math.max(0, Math.min(3, Number(o.maxRounds ?? 1) || 0)),
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
  const ticket = v.ticket ?? items[0]?.ticket ?? queue[0]?.ticket
  const trace = ticket ? ((await desk($, o, ['trace', '--ticket', ticket])) as unknown as Trace) : undefined
  await update($, view, x => ({ ...x, items, queue, ticket, trace: trace?.ok ? trace : x.trace, notice: notice ?? (r.ok ? undefined : String(r.reason)) }))
  try {
    const open = queue.filter(x => x.state !== 'skipped').length
    $.ui.status(items.length ? `desk: ${items.length} waiting` : active.size ? `desk: ${active.size} agents on ${new Set([...active.values()].map(a => a.ticket)).size} ticket(s)` : open ? `desk: ${open} open` : undefined)
  } catch {
    /* no status line on this surface */
  }
  return items
}

async function wallLine($: any, o: Opts, agentId: string, kind: string, text: string): Promise<void> {
  const a = active.get(agentId)
  if (!a) return
  await desk($, o, ['event'], JSON.stringify({ ticket: a.ticket, agent: a.label, kind, text }))
  await update($, view, v => {
    const wall: WallAgent[] = v.wall.filter(w => w.id !== agentId)
    const prev = v.wall.find(w => w.id === agentId)
    wall.push({ id: agentId, ticket: a.ticket, role: a.role, label: a.label, status: prev?.status ?? 'running', started: prev?.started ?? Date.now(), last: `${kind}: ${text.slice(0, 160)}`, lines: [...(prev?.lines ?? []).slice(-5), `${kind}: ${text.slice(0, 160)}`] })
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
      `The operator approved draft ${md5} for #${item.ticket}. Post exactly that text now, unchanged, as a public comment with ` +
      `${o.outwardTool} (proxy ${o.writeProxy}, endpoint ${o.updateEndpoint}), and in the same call set the ticket status to "${status}". ` +
      `The gate refuses the post if the status is missing.` }).catch(() => undefined)
  } else if (decision === 'deny') {
    await desk($, o, ['deny', '--md5', md5, '--reason', note])
    await desk($, o, ['finding'], JSON.stringify({ ticket: item.ticket, agent: 'operator', role: 'operator', kind: 'objection', text: `Denied: ${note || '(no reason)'}`, about: md5 }))
    await refresh($, o, `denied ${md5.slice(0, 8)}`)
    void $.prompt.submit({ text: `The operator denied draft ${md5} for #${item.ticket}${note ? `: ${note}` : ''}. Do not post it. Run /desk run ${item.ticket} to investigate again with that objection on record, or say what the ticket needs instead.` }).catch(() => undefined)
  } else {
    await desk($, o, ['modify', '--md5', md5, '--note', note])
    await desk($, o, ['finding'], JSON.stringify({ ticket: item.ticket, agent: 'operator', role: 'operator', kind: 'objection', text: `Modify: ${note || '(no note)'}`, about: md5 }))
    await refresh($, o, `returned ${md5.slice(0, 8)}`)
    $.clock.after(1, () => void redraft($, o, item.ticket, md5, note).catch(() => undefined))
  }
  await update($, view, v => ({ ...v, mode: 'list', selected: undefined }))
}

// ---- the run: one investigation, a tree of subagents, awaited through the Agent tool so the turn that started
// it holds until the drafter has filed (a `claude -p` session stays alive for it; a timer dispatch too).

const toolNames = (o: Opts): string[] => [outwardName ?? `mcp__argus__${o.outwardTool}`, `mcp__argus-desk__${FINDING}`, `mcp__argus-desk__${RECEIPT}`, `mcp__argus-desk__${TOOL}`, `mcp__argus-desk__${SKIP}`]

const readingRules = (o: Opts, ticket: string): string =>
  `Read with ${o.outwardTool}: proxy ${o.readProxy}, endpoint ${o.commentsEndpoint} with arguments {"ticket_id":"${ticket}"} for every comment, ` +
  `endpoint ${o.ticketEndpoint} for the ticket, and the docs or product proxies for anything else. You may not post, comment, or change anything. ` +
  `Every fact you will rely on must be receipted the moment you read it: call ${RECEIPT} with kind "docs" and the sentence verbatim from the ` +
  `page plus its URL, or kind "material" and the customer's words verbatim. A receipt that is refused means you did not actually read it; do not rely on it. ` +
  `Record what you conclude with ${FINDING} (kind hypothesis, evidence, counter), naming the receipt ids that carry it.`

const prompts: Record<Role, (o: Opts, ticket: string, angle: string, context: string) => string> = {
  investigator: (o, ticket, angle, context) =>
    `You investigate Zendesk ticket #${ticket} for the support engineer who will answer it. Your angle: ${angle}\n` +
    `${readingRules(o, ticket)}\n${context}\n` +
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
    `Rules for the reply: answer what the customer needs, in plain words, under 220 words. No dashes of any kind. Never promise a time. ` +
    `Do not repeat what we already told them. Every factual sentence must carry a receipt; a claim with no receipt is removed or turned into a question for the customer. ` +
    `File it with ${TOOL}: ticket, draft (the exact text), and citations as a list of {sentence, receipt} covering every factual sentence. ` +
    `If the ticket needs no reply from us right now, call ${SKIP} with the reason instead. Do not post anything. Answer with the md5 ${TOOL} returned and one line on what the reply does.`,
  reviewer: (o, ticket, angle, context) =>
    `You review the filed reply on Zendesk ticket #${ticket} before the operator sees it. You may only object; you do not score and you do not praise.\n` +
    (angle ? `${angle}\n` : '') +
    `${readingRules(o, ticket)}\n${context}\n` +
    `Object when: a customer question is left unanswered; a sentence contradicts something we said earlier on the thread; a step does not apply to their platform or setup; ` +
    `a factual sentence has no receipt or its receipt does not say that; the reply repeats what they already tried; the reply hedges on something the evidence settles. ` +
    `Record each objection with ${FINDING} kind objection, quoting the sentence. If there is nothing to object to, record one objection with the text "NO OBJECTIONS" and why you looked and found none. Answer with the list.`,
}

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
  await desk($, o, ['run'], JSON.stringify({ ticket, agent: { id, agentId: id, role, label, model: String(r.model ?? model), status: 'running', params: angle.slice(0, 80) } }))
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
      await go('adversary', 'adversary')
      await spawnRole($, o, ticket, 'adversary', 'adv', '', o.adversaryModel)
    } else if (phase === 'adversary' && !live('adversary') && of('adversary').length) {
      await go('draft', 'draft')
      await spawnRole($, o, ticket, 'drafter', 'draft', '', o.drafterModel)
    } else if (phase === 'draft' && !live('drafter') && of('drafter').length) {
      await go('review', `review ${rounds + 1}`)
      await spawnRole($, o, ticket, 'reviewer', rounds ? `rev${rounds + 1}` : 'rev', '', o.reviewerModel)
    } else if (phase === 'review' && !live('reviewer') && of('reviewer').length) {
      const last = of('reviewer').slice(-1)[0]
      const objected = !/NO OBJECTIONS/i.test(last.summary ?? '')
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
  await desk($, o, ['run'], JSON.stringify({ ticket, phase: 'draft' }))
  await spawnRole($, o, ticket, 'drafter', `redraft-${Date.now() % 1000}`, `Revise draft ${md5}. The operator's note: ${note || '(none)'}. File the new text with supersedes=${md5}.`, o.drafterModel)
}

/** Start the run on one ticket from a timer dispatch of its own. */
function pull($: any, o: Opts, ticket: string, why: string): void {
  $.clock.after(1, () => {
    void (async () => {
      await desk($, o, ['mark', '--ticket', ticket, '--state', 'drafting'])
      await desk($, o, ['run'], JSON.stringify({ ticket, phase: 'new' }))
      await desk($, o, ['event'], JSON.stringify({ ticket, agent: 'desk', kind: 'phase', text: `pulled (${why})` }))
      await advance($, o, ticket)
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
async function tick($: any, o: Opts, force = false): Promise<{ polled: number; pulled?: string; error?: string }> {
  let text = ''
  try {
    const name = await findOutward($, o)
    const r = await $.tool.call({ tool: name, proxy: o.readProxy, endpoint: o.searchEndpoint, arguments: { query: o.searchQuery, sort_by: 'created_at', sort_order: 'asc' } })
    if (r.deny || r.isError) return { polled: 0, error: String(r.deny ?? r.text ?? 'search failed').slice(0, 160) }
    text = String(r.text ?? (typeof r.result === 'string' ? r.result : JSON.stringify(r.result ?? '')))
  } catch (err) {
    return { polled: 0, error: String(err).slice(0, 160) }
  }
  const tickets = ticketsIn(text)
  const v = await desk($, o, ['sync'], JSON.stringify({ tickets }))
  await update($, view, x => ({ ...x, lastPoll: Date.now(), pollError: undefined }))
  const next = v.next as QueueRow | undefined
  if (v.ok && next && (force || (await read($, view)).poll === 'on')) {
    pull($, o, next.ticket, next.sla ? `SLA ${next.sla}` : next.state === 'reopened' ? 'the customer answered' : next.state === 'stale' ? 'pulled before, nothing filed' : `open ${Math.round(next.age_s / 60)} min`)
    return { polled: tickets.length, pulled: next.ticket }
  }
  await refresh($, o)
  return { polled: tickets.length }
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

/** After a granted post: read the ticket back and say whether the status moved. */
async function verify($: any, o: Opts, toolName: string, ticket: string, status: string): Promise<void> {
  try {
    const r = await $.tool.call({ tool: toolName, proxy: o.readProxy, endpoint: o.ticketEndpoint, arguments: { ticket_id: ticket } })
    const text = String(r.text ?? JSON.stringify(r.result ?? ''))
    const m = text.match(/"status"\s*:\s*"([a-z-]+)"/)
    const now = m?.[1]
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
  if (t.receipts?.length) out.push('RECEIPTS:', ...t.receipts.map(r => `  ${r.id} ${r.kind}${r.source ? ` ${r.source}` : ''}: "${r.quote.slice(0, 160)}"`))
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

export const register: Register = (on, options) => {
  const o = opts(options as Record<string, unknown>)

  on('session.start', async ($, e, next) => {
    await $.command.register({ name: 'desk', description: 'The review desk: pane with Wall, Queue, Ticket, Decide; /desk run N, trace N, poll, pull N, pause, resume, stats', argumentHint: '[run N | trace N | poll | pull N | pause | resume | stats]' })
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
    await findOutward($, o)
    const tools = toolNames(o)
    for (const role of ROLES) {
      try {
        await $.agent.register({ name: role, description: `argus-desk ${role}: spawned by the desk on a ticket run`, prompt: `You are the ${role} of the argus-desk review workflow. Follow the task exactly; use only the tools named in it.`, tools, model: role === 'investigator' ? o.investigatorModel : role === 'adversary' ? o.adversaryModel : role === 'drafter' ? o.drafterModel : o.reviewerModel, maxTurns: 40 })
      } catch {
        /* a host without agent types: the role prompt rides in the task and the gate still holds */
      }
    }
    if (e.isInteractive) {
      await update($, view, v => ({ ...v, poll: o.pollMinutes > 0 ? 'on' : 'off' }))
      await refresh($, o)
      if (e.surface === 'terminal') void $.ui.open({ id: PANE, title: 'Desk' })
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
      await desk($, o, ['run'], JSON.stringify({ ticket: a.ticket, agent: { id, status, summary: text.slice(0, 600) } }))
      await update($, view, v => ({ ...v, wall: v.wall.map(w => (w.id === id ? { ...w, status, last: text.slice(0, 160) } : w)) }))
      $.clock.after(1, () => void advance($, o, a.ticket).catch(() => undefined))
    }
    return next(e)
  })

  on('command.run', { command: 'desk' }, async ($, e) => {
    const args = (e.args ?? '').trim()
    if (/^stats\b/.test(args)) {
      const r = await $.process.run([o.python, '-I', `${$.plugin.root}/checks/desk.py`, '--root', o.stateDir, 'stats', '--text'], { timeoutMs: 60_000 })
      return { text: r.exitCode === 0 ? r.stdout.trim() : `desk.py failed: ${r.stderr.slice(0, 300)}` }
    }
    if (/^run\s+#?\d+$/.test(args)) {
      const t = args.replace(/^run\s+#?/, '')
      pull($, o, t, 'run by the operator')
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
      await update($, view, v => ({ ...v, ticket: t, tab: 'ticket', scroll: 0 }))
      return { text: tr.ok ? traceText(tr) : `no trace for #${t}` }
    }
    if (/^pull\s+#?\d+$/.test(args)) {
      const t = args.replace(/^pull\s+#?/, '')
      pull($, o, t, 'pulled by the operator')
      return { text: `Pulled #${t}: the run starts now; watch the Wall.` }
    }
    if (args === 'poll') {
      const t = await tick($, o, true)
      return { text: t.error ? `Poll failed: ${t.error}` : `Polled ${t.polled} open ticket(s)${t.pulled ? `, pulled #${t.pulled}` : ''}.` }
    }
    if (args === 'pause' || args === 'resume') {
      await update($, view, v => ({ ...v, poll: args === 'pause' ? 'paused' : 'on' }))
      if (args === 'resume') startPolling($, o)
      return { text: args === 'pause' ? 'Polling paused; /desk resume to start it again.' : 'Polling on.' }
    }
    const items = await refresh($, o)
    await $.ui.open({ id: PANE, title: 'Desk', focus: true })
    return { text: items.length ? `${items.length} draft(s) waiting at the desk.` : 'Nothing waiting at the desk.' }
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
    return { result: r.ok ? `Receipt ${String(r.id)}.` : `Refused: ${String(r.why ?? r.reason)}. Only a sentence you actually read, verbatim, can be receipted${r.why === 'page-not-captured' ? '; fetch the page through the proxy first' : ''}.` }
  }).catch(() => ({ deny: 'argus-desk: the receipt was not minted, try again' }))

  // 2. the gate: an outward call passes only by spending an open grant on the exact text it carries.
  on('tool.call', async ($, e, next) => {
    if (!String(e.tool).includes(o.outwardTool)) return next(e)
    outwardName = String(e.tool)
    const agentId = (e as { agentId?: string }).agentId
    const args = normalize(Object.fromEntries(Object.entries(e as Record<string, unknown>).filter(([k]) => !RESERVED.has(k))))
    if (!isOutward(args)) {
      // A read: let it run; capture comments as the live thread, and documentation pages for receipts.
      const ran = await next(e)
      const ticket = ticketOf(args)
      const text = (ran as { text?: string }).text
      if (agentId) await wallLine($, o, agentId, 'read', `${String(args.proxy ?? '')} ${String(args.endpoint ?? '')} ${JSON.stringify(args.arguments ?? {}).slice(0, 100)}`)
      if (ticket && text && /"comments"\s*:\s*\[/.test(text)) {
        try {
          const parsed = JSON.parse(text) as { comments?: Array<Record<string, unknown>>; count?: number }
          const comments = parsed.comments ?? []
          const messages = comments.map((c, i) => ({
            id: String(c.id ?? i),
            // Internal notes are staff: bots post several on every new ticket, and counting them as our replies held
            // every draft on cadence (too-many). Only a public comment from our side is a reply.
            who: c.public === false ? 'staff' : c.author_role === 'agent' || c.via_agent === true ? 'us' : 'customer',
            at: typeof c.created_at === 'string' ? Date.parse(c.created_at) / 1000 : undefined,
            text: String(c.plain_body ?? c.body ?? ''),
          }))
          await desk($, o, ['capture'], JSON.stringify({ ticket, messages, count: parsed.count ?? comments.length }))
        } catch {
          /* not a comments payload after all */
        }
      }
      if (text) for (const url of docsUrls(o, JSON.stringify(args), text.slice(0, 4000))) await desk($, o, ['page'], JSON.stringify({ source: url, body: text }))
      return ran
    }
    const flat = JSON.stringify(args)
    const r = await desk($, o, ['consume'], JSON.stringify({ candidates: strings(args), what: `${String(e.tool)} ${flat.slice(0, 120)}`, flat }))
    if (r.ok) {
      const ran = await next(e)
      await refresh($, o, `sent ${String(r.md5).slice(0, 8)}`)
      if (r.ticket && r.status) await verify($, o, String(e.tool), String(r.ticket), String(r.status))
      return ran
    }
    if (r.code === 'status-missing') {
      return {
        deny:
          `argus-desk: the operator approved this text with the ticket status set to "${String(r.status)}", and this call does not set it. ` +
          `Repeat the call with the same text and status "${String(r.status)}" in the ticket update. The approval is still open.`,
      }
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

  // 3. the pane: Wall, Queue, Ticket, Decide.
  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button, Input } = $.ui.resolve(e) as any
    const v = await read($, view)
    const width = Math.max(40, e.props.bodyColumns ?? 80)
    const rows = Math.max(10, (e.viewport?.rows ?? 30) - 4)
    const cut = (t: string, n = width - 4) => t.replace(/\s+/g, ' ').slice(0, n)
    const sel = v.items.find(i => i.md5 === v.selected) ?? v.items.find(i => i.ticket === v.ticket) ?? v.items[0]
    const lines: Array<{ key: string; text: string; color?: string; dim?: boolean }> = []
    const add = (key: string, text: string, color?: string, dim?: boolean) => lines.push({ key, text, color, dim })
    const t = v.trace
    if (v.tab === 'wall') {
      const byTicket = new Map<string, WallAgent[]>()
      for (const w of v.wall) byTicket.set(w.ticket, [...(byTicket.get(w.ticket) ?? []), w])
      if (!byTicket.size) add('w0', 'No agents yet. A pull or /desk run N starts a run.', undefined, true)
      for (const [ticket, agents] of byTicket) {
        add(`w-${ticket}`, `#${ticket}  ${t?.run?.ticket === ticket ? `phase ${t.run.phase}` : ''}`, 'cyan')
        for (const a of agents.slice(-12)) {
          add(`w-${ticket}-${a.id}`, `  ${a.status === 'running' ? '*' : a.status === 'pending' ? '.' : a.status === 'failed' ? 'x' : 'v'} ${a.role} ${a.label} ${Math.round((Date.now() - a.started) / 60000)}m  ${cut(a.last, width - 30)}`, a.status === 'failed' ? 'red' : undefined, a.status !== 'running')
          for (const l of a.lines.slice(-2)) add(`w-${ticket}-${a.id}-${l.slice(0, 12)}`, `      ${cut(l, width - 8)}`, undefined, true)
        }
      }
    } else if (v.tab === 'queue') {
      add('q0', `poll ${v.poll}${v.lastPoll ? `, last ${Math.round((Date.now() - v.lastPoll) / 60000)} min ago` : ', none yet'}${v.pollError ? `, failed: ${v.pollError}` : ''}`, undefined, true)
      if (!v.queue.length) add('q1', 'No open tickets seen.', undefined, true)
      for (const q of v.queue) add(`q-${q.ticket}`, `#${q.ticket} ${q.state}${q.sla ? ` SLA ${q.sla.slice(5, 16)}` : ''} ${Math.round(q.age_s / 60)}m  ${cut(q.subject ?? '', width - 36)}`, undefined, q.state === 'skipped' || q.state === 'sent')
    } else if (v.tab === 'ticket') {
      if (!t?.ok) add('t0', 'Pick a ticket in Queue or Decide; the trace shows here.', undefined, true)
      else {
        add('t1', `#${t.run.ticket}  phase ${t.run.phase}  rounds ${t.run.rounds ?? 0}  agents ${t.run.agents?.length ?? 0}`, 'cyan')
        for (const a of t.run.agents ?? []) add(`ta-${a.id}`, `  ${a.status === 'completed' ? 'v' : a.status === 'failed' ? 'x' : '*'} ${a.role} ${a.label} (${a.model})${a.params ? ` ${cut(a.params, 50)}` : ''}${a.summary ? `: ${cut(a.summary, width - 40)}` : ''}`, undefined, true)
        for (const k of ['hypothesis', 'evidence', 'counter', 'objection', 'answer', 'summary']) {
          const fs = (t.findings ?? []).filter(f => f.kind === k)
          if (!fs.length) continue
          add(`tk-${k}`, k.toUpperCase(), k === 'counter' || k === 'objection' ? 'yellow' : 'green')
          for (const f of fs) add(`tf-${f.id}`, `  - (${f.agent}) ${cut(f.text, width - 8)}${f.receipts?.length ? ` <${f.receipts.map(r => r.replace(/^rcpt:/, '')).join(',')}>` : ''}`)
        }
        if (t.receipts?.length) {
          add('tr', 'RECEIPTS', 'green')
          for (const r of t.receipts) add(`tr-${r.id}`, `  ${r.id.replace(/^rcpt:/, '')} ${r.kind}${r.source ? ` ${cut(r.source, 50)}` : ''}: "${cut(r.quote, width - 30)}"`, undefined, true)
        }
        if (t.claims?.length) {
          add('tc', 'CLAIMS IN THE DRAFT', 'green')
          for (const c of t.claims) add(`tc-${c.text.slice(0, 24)}`, `  ${c.problems?.length ? c.problems.join(',') : c.echo ? 'echo' : 'ok'}: ${cut(c.text, width - 20)}`, c.problems?.length ? 'yellow' : undefined)
        }
      }
    } else {
      for (const i of v.items) add(`d-${i.md5}`, `${i.md5 === sel?.md5 ? '> ' : '  '}#${i.ticket} ${i.md5.slice(0, 8)} ${i.checks.verdict === 'hold' ? `HOLD ${i.checks.hits.join(',')}` : 'clear'} ${i.state} ${Math.round(i.age_s / 60)}m`)
      if (!v.items.length) add('d0', `No drafts filed. The drafter files one with ${TOOL}; nothing posts without a Send here.`, undefined, true)
      if (sel) {
        add('ds', `thread: ${sel.checks.thread?.status === 'captured' ? `${sel.checks.thread.messages} of ${sel.checks.thread.console_count} comments recorded` : 'NOT captured'} | claims: ${sel.checks.claims ? `${sel.checks.claims.credited} of ${sel.checks.claims.checkable} receipted` : 'n/a'}${sel.checks.warnings.length ? ` | ${sel.checks.warnings.join(', ')}` : ''}`, undefined, true)
        for (const q of (sel.checks.thread?.open_questions ?? []).slice(0, 4)) add(`dq-${q.slice(0, 16)}`, `customer asked: ${cut(q, width - 18)}`, 'yellow')
        const objections = (t?.findings ?? []).filter(f => f.kind === 'objection' && !/NO OBJECTIONS/i.test(f.text))
        for (const f of objections.slice(-4)) add(`do-${f.id}`, `objection (${f.agent}): ${cut(f.text, width - 24)}`, 'yellow')
        for (const [n, l] of sel.text.split('\n').entries()) add(`dl-${n}`, cut(l))
      }
    }
    const body = Math.max(4, rows - 5)
    const scroll = Math.max(0, Math.min(v.scroll, Math.max(0, lines.length - body)))
    const shown = lines.slice(scroll, scroll + body)
    const tab = (id: DeskView['tab'], label: string, hotkey: string) => (
      <Button key={`tab-${id}`} {...(v.tab !== id ? { plain: true } : {})} hotkey={hotkey} onPress={() => void update($, view, x => ({ ...x, tab: id, scroll: 0 }))}>{v.tab === id ? `[${label}]` : ` ${label} `}</Button>
    )
    return (
      <Box flexDirection="column">
        <Box>
          {tab('wall', '1 Wall', '1')}{tab('queue', '2 Queue', '2')}{tab('ticket', '3 Ticket', '3')}{tab('decide', '4 Decide', '4')}
          <Text dimColor>  {v.items.length} waiting, {active.size} agents{v.notice ? `  (${v.notice})` : ''}</Text>
        </Box>
        {shown.map(l => (
          <Text key={l.key} color={l.color} dimColor={l.dim}>{l.text}</Text>
        ))}
        {lines.length > body && <Text dimColor>lines {scroll + 1}-{Math.min(lines.length, scroll + body)} of {lines.length}</Text>}
        <Box marginTop={0}>
          <Button key="up" plain hotkey="k" onPress={() => void update($, view, x => ({ ...x, scroll: Math.max(0, x.scroll - Math.max(1, body - 2)) }))}>k up</Button>
          <Button key="down" plain hotkey="j" onPress={() => void update($, view, x => ({ ...x, scroll: x.scroll + Math.max(1, body - 2) }))}>j down</Button>
          {v.items.length > 1 && <Button key="next" plain hotkey="n" onPress={() => void update($, view, x => { const i = x.items.findIndex(it => it.md5 === (sel?.md5 ?? '')); const nx = x.items[(i + 1) % x.items.length]; return { ...x, selected: nx?.md5, ticket: nx?.ticket, scroll: 0 } })}>n next draft</Button>}
          {sel && v.tab === 'decide' && v.mode === 'list' && (
            <Box>
              <Button key="send" variant="primary" hotkey="s" onPress={p => void decide($, o, sel.md5, 'send', '', `${p.requestId}-${sel.md5.slice(0, 8)}-${Date.now()}`)}>s Send, {o.sendStatus}</Button>
              <Button key="solve" hotkey="v" onPress={p => void decide($, o, sel.md5, 'send', '', `${p.requestId}-${sel.md5.slice(0, 8)}-${Date.now()}`, 'solved')}>v Send, solved</Button>
              <Button key="modify" hotkey="m" onPress={() => void update($, view, x => ({ ...x, selected: sel.md5, mode: 'modify' }))}>m Modify</Button>
              <Button key="deny" hotkey="d" onPress={() => void update($, view, x => ({ ...x, selected: sel.md5, mode: 'deny' }))}>d Deny</Button>
            </Box>
          )}
        </Box>
        {sel && v.mode === 'modify' && (
          <Input key="modify-note" label="what to change" placeholder="one line for the drafter" autoFocus submitLabel="return" onSubmit={(txt: string, p: any) => void decide($, o, sel.md5, 'modify', txt, p.requestId)} />
        )}
        {sel && v.mode === 'deny' && (
          <Input key="deny-reason" label="why" placeholder="recorded with the deny" autoFocus submitLabel="deny" onSubmit={(txt: string, p: any) => void decide($, o, sel.md5, 'deny', txt, p.requestId)} />
        )}
      </Box>
    )
  })
}
