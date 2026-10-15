import { test, expect, mock } from 'claude-code/testing'

// The desk's state lives in desk.py; here a fake answers $.process.run so the mod's own logic is what runs.
type Run = { argv: readonly string[]; stdin?: string }
const fake = (on: any, answer: (r: Run) => Record<string, unknown>) => {
  const calls: Run[] = []
  on('process.run', async (_$: any, e: any) => {
    const r = { argv: e.argv as readonly string[], stdin: e.init?.stdin as string | undefined }
    calls.push(r)
    return { value: { exitCode: 0, stdout: JSON.stringify(answer(r)), stderr: '' } }
  })
  return calls
}
const cmdOf = (r: Run) => r.argv.find(a => ['file', 'list', 'grant', 'deny', 'modify', 'consume', 'capture', 'stats', 'sync', 'queue', 'mark', 'run', 'event', 'finding', 'receipt', 'page', 'cite', 'call', 'audit', 'verify', 'trace', 'wait'].includes(String(a)))

const DRAFT = 'Hi Sam,\n\nThe broker is disabled, so the connector reports unhealthy. Re-enable it under Settings > Connectors.\n\nThanks!\n'
const ITEM = { md5: 'a1b2c3d4e5f60718293a4b5c6d7e8f90', ticket: '123456', state: 'pending', filed: 1, age_s: 120, chars: 120, text: DRAFT,
  checks: { verdict: 'clear', hits: [], warnings: ['no-thread-captured'], thread: { status: 'no-thread' }, claims: { checkable: 2, credited: 0, uncovered: 2, echo: 0, hits: ['uncovered'], shadow: true } } }

test('an outward post with no grant is refused and told what to do', async ($, on) => {
  const calls = fake(on, r => (cmdOf(r) === 'consume' ? { ok: false, tried: [], undecided: [ITEM.md5] } : { ok: true, items: [ITEM] }))
  const r = await $.tool.call({ tool: 'mcp__local-argus__invoke_proxy', proxy: 'ticketing-write', endpoint: 'ticketing_update_ticket', arguments: { ticket_id: 123456, comment: { body: DRAFT, public: true } } } as any)
  expect(r.deny).toMatch(/argus-desk/)
  expect(r.deny).toMatch(/file_draft/)
  expect(r.deny).toMatch(/a1b2c3d4/)
  const consume = calls.find(c => cmdOf(c) === 'consume')
  expect(consume).toBeDefined()
  expect(JSON.parse(consume!.stdin!).candidates).toContain(DRAFT)
})

test('an outward post passes once the exact text has an open grant, and the grant is spent through desk.py', async ($, on) => {
  let consumed = 0
  const calls = fake(on, r => (cmdOf(r) === 'consume' ? { ok: ++consumed === 1, md5: ITEM.md5 } : { ok: true, items: [] }))
  on('tool.call', { tool: 'mcp__local-argus__invoke_proxy' }, async () => ({ result: { posted: true } }))
  const first = await $.tool.call({ tool: 'mcp__local-argus__invoke_proxy', proxy: 'ticketing-write', endpoint: 'ticketing_update_ticket', arguments: JSON.stringify({ ticket_id: 123456, comment: { body: DRAFT } }) } as any)
  expect(first.deny).toBeUndefined()
  const second = await $.tool.call({ tool: 'mcp__local-argus__invoke_proxy', proxy: 'ticketing-write', endpoint: 'ticketing_update_ticket', arguments: JSON.stringify({ ticket_id: 123456, comment: { body: DRAFT } }) } as any)
  expect(second.deny).toMatch(/argus-desk/)
  expect(calls.filter(c => cmdOf(c) === 'consume').length).toBe(2)
})

test('a read of the ticket comments passes untouched and is captured as the live thread', async ($, on) => {
  const calls = fake(on, () => ({ ok: true, items: [] }))
  const payload = { comments: [{ id: 1, author_role: 'end-user', created_at: '2026-10-14T10:00:00Z', plain_body: 'Scans fail. Which version?' }, { id: 2, author_role: 'agent', created_at: '2026-10-14T11:00:00Z', plain_body: 'Please send the logs.' }, { id: 3, author_role: 'agent', public: false, created_at: '2026-10-14T11:05:00Z', plain_body: 'AI classification: Troubleshooting.' }], count: 3 }
  on('tool.call', { tool: 'mcp__local-argus__invoke_proxy' }, async () => ({ result: payload, text: JSON.stringify(payload) }))
  const r = await $.tool.call({ tool: 'mcp__local-argus__invoke_proxy', proxy: 'ticketing-full', endpoint: 'ticketing_get_ticket_comments', arguments: { ticket_id: 123456 } } as any)
  expect(r.deny).toBeUndefined()
  const cap = calls.find(c => cmdOf(c) === 'capture')
  expect(cap).toBeDefined()
  const body = JSON.parse(cap!.stdin!)
  expect(body.ticket).toBe('123456')
  expect(body.count).toBe(3)
  expect(body.messages[1].who).toBe('us')
  expect(body.messages[2].who).toBe('staff')
})

test('fails closed: a write on any proxy, or a call with no endpoint, needs a grant; a named read does not', async ($, on) => {
  const calls = fake(on, r => (cmdOf(r) === 'consume' ? { ok: false, tried: [], undecided: [] } : { ok: true, items: [] }))
  on('tool.call', { tool: 'mcp__local-argus__invoke_proxy' }, async () => ({ result: { ok: true }, text: '{}' }))
  const chat = await $.tool.call({ tool: 'mcp__local-argus__invoke_proxy', proxy: 'chat-out', endpoint: 'chat_post_message', arguments: { channel: 'C1', text: DRAFT } } as any)
  expect(chat.deny).toMatch(/argus-desk/)
  const bare = await $.tool.call({ tool: 'mcp__local-argus__invoke_proxy', proxy: 'ticketing-full' } as any)
  expect(bare.deny).toMatch(/argus-desk/)
  const read = await $.tool.call({ tool: 'mcp__local-argus__invoke_proxy', proxy: 'ticketing-full', endpoint: 'ticketing_search_tickets', arguments: '{"query":"status:open"}' } as any)
  expect(read.deny).toBeUndefined()
  expect(calls.filter(c => cmdOf(c) === 'consume').length).toBe(2)
})

test('file_draft files through desk.py and tells the model to wait', async ($, on) => {
  const calls = fake(on, r => (cmdOf(r) === 'file' ? { ok: true, md5: ITEM.md5, ticket: '123456', verdict: 'clear', hits: [], warnings: ['no-thread-captured'], claims: { checkable: 2, credited: 0 } } : { ok: true, items: [ITEM] }))
  const r = await $.tool.call({ tool: 'mcp__argus-desk__file_draft', ticket: '123456', draft: DRAFT } as any)
  expect(r.deny).toBeUndefined()
  expect(String(r.result)).toMatch(/Filed draft a1b2c3d4/)
  expect(String(r.result)).toMatch(/waiting for the operator/)
  const filed = calls.find(c => cmdOf(c) === 'file')
  expect(JSON.parse(filed!.stdin!).draft).toBe(DRAFT)
})

test('the pane shows the draft with its checks and the three decisions on every surface', async ($, on) => {
  fake(on, () => ({ ok: true, items: [ITEM] }))
  await $.tool.call({ tool: 'mcp__argus-desk__file_draft', ticket: '123456', draft: DRAFT } as any)
  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ plugin: 'argus-desk', surface, component: 'Pane', requestId: 'argus-desk', props: { title: 'Desk', isFocused: true, bodyColumns: 100, placement: 'dock' } } as any)
    expect(await ui.find({ type: 'Text', text: /1 waiting/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /NOT captured/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /0 of 2 receipted/ })).toBeDefined()
    expect(await ui.find({ key: 'send' })).toBeDefined()
    expect(await ui.find({ key: 'modify' })).toBeDefined()
    expect(await ui.find({ key: 'deny' })).toBeDefined()
    await ui.unmount()
  }
})

test('Send grants through desk.py and wakes the model to post; Deny records the reason', async ($, on) => {
  const calls = fake(on, r => (cmdOf(r) === 'grant' || cmdOf(r) === 'deny' ? { ok: true, md5: ITEM.md5 } : { ok: true, items: [ITEM] }))
  const prompts: string[] = []
  on('prompt.submit', async (_$: any, e: any) => { prompts.push(e.text); return { text: e.text } })
  await $.tool.call({ tool: 'mcp__argus-desk__file_draft', ticket: '123456', draft: DRAFT } as any)
  const ui = await $.ui.mount({ plugin: 'argus-desk', surface: 'terminal', component: 'Pane', requestId: 'argus-desk', props: { title: 'Desk', isFocused: true, bodyColumns: 100, placement: 'dock' } } as any)
  await ui.press({ key: 'send' })
  const grant = calls.find(c => cmdOf(c) === 'grant')
  expect(grant).toBeDefined()
  expect(grant!.argv).toContain(ITEM.md5)
  expect(prompts.some(p => /approved draft a1b2c3d4/.test(p))).toBe(true)
  await ui.press({ key: 'deny' })
  await ui.input({ key: 'deny-reason', text: 'wrong ask' })
  const deny = calls.find(c => cmdOf(c) === 'deny')
  expect(deny).toBeDefined()
  expect(deny!.argv).toContain('wrong ask')
  await ui.unmount()
})

const SEARCH = { results: [{ id: 123456, status: 'open', subject: 'Scan stuck', updated_at: '2026-10-15T01:00:00Z' }, { id: 123457, status: 'new', subject: 'SLA one', sla_next_breach_at: '2026-10-15T09:00:00Z', updated_at: '2026-10-15T02:00:00Z' }], count: 2 }
const NEXT = { ticket: '123457', state: 'new', sla: '2026-10-15T09:00:00Z', age_s: 5 }

const RUN = (phase: string, agents: any[] = [], rounds = 0) => ({ ok: true, run: { ticket: '123457', phase, agents, rounds }, findings: [], receipts: [], drafts: [] })

test('/desk poll searches the open set, folds it into the queue and starts a run; each turn.complete advances the run', async ($, on) => {
  const clock = mock.clock(on)
  // desk.py as a fake with a tiny run store, so advance() reads back what it wrote
  const store: { phase: string; agents: any[]; rounds: number } = { phase: 'new', agents: [], rounds: 0 }
  const calls = fake(on, r => {
    const c = cmdOf(r)
    if (c === 'sync') return { ok: true, rows: [NEXT], next: NEXT, busy: [] }
    if (c === 'queue') return { ok: true, rows: [NEXT] }
    if (c === 'run') {
      const b = JSON.parse(r.stdin!)
      if (b.phase) { store.phase = b.phase; if (b.phase === 'review') store.rounds++ }
      if (b.agent?.id) { const prev = store.agents.find(a => a.id === b.agent.id) ?? {}; store.agents = [...store.agents.filter(a => a.id !== b.agent.id), { ...prev, ...b.agent }] }
      return { ok: true }
    }
    if (c === 'trace') return { ok: true, run: { ticket: '123457', phase: store.phase, agents: store.agents, rounds: store.rounds }, findings: [], receipts: [], drafts: [],
      // inv1 and inv3 minted receipts; inv2 minted none, so the floor sends it back in once (as inv2b)
      events: [{ agent: 'inv1', kind: 'receipt', text: 'a' }, { agent: 'inv1', kind: 'receipt', text: 'b' }, { agent: 'inv3', kind: 'receipt', text: 'c' }, { agent: 'inv3', kind: 'receipt', text: 'd' }, { agent: 'inv2b', kind: 'receipt', text: 'e' }, { agent: 'inv2b', kind: 'receipt', text: 'f' }] }
    return { ok: true, items: [] }
  })
  const spawns: any[] = []
  on('agent.spawn', async (_$: any, e: any) => { spawns.push(e); return { agentId: `a${spawns.length}`, model: e.model ?? 'sonnet' } })
  on('turn.complete', async (_$: any, e: any) => ({ text: e.answer }))
  const searches: any[] = []
  on('tool.call', { tool: 'mcp__local-argus__invoke_proxy' }, async (_$: any, e: any) => { searches.push(e); return { result: SEARCH, text: JSON.stringify(SEARCH) } })
  await $.tool.call({ tool: 'mcp__local-argus__invoke_proxy', proxy: 'zendesk-full', endpoint: 'zendesk_get_ticket', arguments: { ticket_id: 123456 } } as any)
  searches.length = 0
  const r = await $.command.run({ command: 'desk', args: 'poll' })
  expect(String(r.text)).toMatch(/Polled 2/)
  expect(String(r.text)).toMatch(/pulled #123457/)
  expect(searches[0].endpoint).toBe('zendesk_search_tickets')
  const sync = calls.find(c => cmdOf(c) === 'sync')
  expect(JSON.parse(sync!.stdin!).tickets.map((t: any) => t.id)).toEqual(['123456', '123457'])
  // the run starts from the driver's timer: three investigators first
  await clock.advance(2_100)
  expect(calls.find(c => cmdOf(c) === 'mark')!.argv).toContain('drafting')
  expect(spawns.map(s => s.description)).toEqual(['investigator #123457 inv1', 'investigator #123457 inv2', 'investigator #123457 inv3'])
  expect(spawns[0].prompt).toMatch(/investigate Zendesk ticket #123457/)
  expect(spawns[0].prompt).toMatch(/desk_receipt/)
  const finish = async (label: string, answer: string) => { const id = store.agents.find(a => a.label === label && a.status === 'running')!.id; await $.turn.complete({ agentId: id, answer, reason: 'answer', durationMs: 1, isAborted: false, turnId: `t-${id}` } as any); await clock.advance(2_100) }
  await finish('inv1', 'cause: label'); await finish('inv2', 'evidence thin')
  expect(spawns.length).toBe(3, 'nothing moves until every investigator is done')
  await finish('inv3', 'history clean')
  // the evidence floor: inv2 recorded no receipts, so it goes back in once before the adversary
  expect(spawns[3].description).toBe('investigator #123457 inv2b')
  expect(spawns[3].prompt).toMatch(/recorded 0 receipts/)
  expect(spawns[3].prompt).toMatch(/evidence thin/)
  await finish('inv2b', 'evidence: two receipts now')
  expect(spawns[4].description).toBe('adversary #123457 adv')
  await finish('adv', 'counter: GitHub-only step')
  expect(spawns[5].description).toBe('drafter #123457 draft')
  expect(spawns[5].prompt).toMatch(/file_draft/)
  expect(spawns[5].prompt).toMatch(/counter: GitHub-only step/)
  await finish('draft', 'md5 abc filed')
  // two independent reviewers per round, different checklists
  expect(spawns.slice(6, 8).map(s => s.description)).toEqual(['reviewer #123457 rev', 'reviewer #123457 revB'])
  expect(spawns[6].prompt).toMatch(/Checklist A/)
  expect(spawns[7].prompt).toMatch(/Checklist B/)
  await finish('rev', 'NO OBJECTIONS')
  expect(spawns.length).toBe(8, 'the round waits for both reviewers')
  await finish('revB', 'Objection: the wizexclude step does not apply to ADO')
  expect(spawns[8].description).toBe('drafter #123457 draft2')
  expect(spawns[8].prompt).toMatch(/Revise/)
  await finish('draft2', 'md5 def filed')
  expect(spawns.slice(9, 11).map(s => s.description)).toEqual(['reviewer #123457 rev2', 'reviewer #123457 rev2B'])
  await finish('rev2', 'NO OBJECTIONS'); await finish('rev2B', 'NO OBJECTIONS')
  expect(spawns.length).toBe(11)
  expect(store.phase).toBe('done')
  // accountability: the exact prompt and the full answer travel to the record, and every proxy read is a call row
  const runRows = calls.filter(c => cmdOf(c) === 'run').map(c => JSON.parse(c.stdin!))
  expect(runRows.find(r => r.agent?.label === 'inv1' && r.agent.prompt)?.agent.prompt).toBe(spawns[0].prompt)
  expect(runRows.find(r => r.agent?.id === store.agents.find(a => a.label === 'revB')!.id && r.agent.answer)?.agent.answer).toMatch(/wizexclude/)
  const callRows = calls.filter(c => cmdOf(c) === 'call').map(c => JSON.parse(c.stdin!))
  // (the poll's own search names no ticket and no agent, so it is not a run record; the ticket read above is)
  expect(callRows.length).toBe(1)
  expect(callRows[0].tool).toBe('mcp__local-argus__invoke_proxy')
  expect(callRows[0].args.endpoint).toBe('zendesk_get_ticket')
  expect(callRows[0].ticket).toBe('123456')
  expect(callRows[0].agent).toBe('main')
  expect(typeof callRows[0].result).toBe('string')
  expect(store.agents.find(a => a.label === 'revB').summary).toMatch(/Objection/)
  expect(calls.filter(c => cmdOf(c) === 'consume').length).toBe(0)
})

test('/desk run N holds the turn through desk.py wait and prints the trace', async ($, on) => {
  const clock = mock.clock(on)
  const calls = fake(on, r => (cmdOf(r) === 'wait' ? { ok: true, phase: 'done' } : cmdOf(r) === 'trace' ? RUN('done', [{ id: 'a1', role: 'drafter', label: 'draft', model: 'opus', status: 'completed', summary: 'filed', started: 1 }]) : { ok: true, items: [], rows: [] }))
  on('agent.spawn', async () => ({ agentId: 'a1', model: 'sonnet' }))
  const r = await $.command.run({ command: 'desk', args: 'run 123457' })
  expect(String(r.text)).toMatch(/Run on #123457/)
  expect(String(r.text)).toMatch(/drafter draft \(opus\) completed: filed/)
  const w = calls.find(c => cmdOf(c) === 'wait')
  expect(w).toBeDefined()
  expect(w!.argv).toContain('123457')
  await clock.advance(5)
})

test('the poller runs on the clock while polling is on, and not while paused', async ($, on) => {
  const clock = mock.clock(on)
  fake(on, r => (cmdOf(r) === 'sync' ? { ok: true, rows: [], next: null, busy: [] } : { ok: true, items: [], rows: [] }))
  let searches = 0
  on('tool.call', { tool: 'mcp__local-argus__invoke_proxy' }, async () => { searches++; return { result: { results: [] }, text: '{"results":[]}' } })
  await $.tool.call({ tool: 'mcp__local-argus__invoke_proxy', proxy: 'zendesk-full', endpoint: 'zendesk_get_ticket', arguments: { ticket_id: 123456 } } as any)
  searches = 0
  await $.command.run({ command: 'desk', args: 'resume' })
  await clock.advance(5 * 60_000 + 10)
  expect(searches).toBe(1)
  await $.command.run({ command: 'desk', args: 'pause' })
  await clock.advance(5 * 60_000)
  expect(searches).toBe(1)
  await $.command.run({ command: 'desk', args: 'resume' })
  await clock.advance(5 * 60_000)
  expect(searches).toBe(2)
})

test('a granted post without the chosen status is refused with the grant left open; with it, the post runs and the status is read back', async ($, on) => {
  let consumes = 0
  const calls = fake(on, r => {
    if (cmdOf(r) !== 'consume') return { ok: true, items: [], rows: [] }
    consumes++
    const flat = JSON.parse(r.stdin!).flat as string
    return /"status":"pending"/.test(flat) ? { ok: true, md5: ITEM.md5, ticket: '123456', status: 'pending' } : { ok: false, code: 'status-missing', md5: ITEM.md5, status: 'pending' }
  })
  const seen: any[] = []
  on('tool.call', { tool: 'mcp__local-argus__invoke_proxy' }, async (_$: any, e: any) => {
    seen.push(e)
    return e.endpoint === 'zendesk_get_ticket' ? { result: {}, text: '{"ticket":{"id":123456,"status":"pending"}}' } : { result: { posted: true }, text: '{"ok":true}' }
  })
  const bare = await $.tool.call({ tool: 'mcp__local-argus__invoke_proxy', proxy: 'zendesk-write', endpoint: 'zendesk_update_ticket', arguments: { ticket_id: 123456, comment: { body: DRAFT, public: true } } } as any)
  expect(bare.deny).toMatch(/status set to "pending"/)
  expect(seen.length).toBe(0)
  const full = await $.tool.call({ tool: 'mcp__local-argus__invoke_proxy', proxy: 'zendesk-write', endpoint: 'zendesk_update_ticket', arguments: { ticket_id: 123456, ticket: { status: 'pending', comment: { body: DRAFT, public: true } } } } as any)
  expect(full.deny).toBeUndefined()
  expect(consumes).toBe(2)
  expect(seen.map(e => e.endpoint)).toEqual(['zendesk_update_ticket', 'zendesk_get_ticket'])
  expect(calls.filter(c => cmdOf(c) === 'consume').length).toBe(2)
})

test('skip_ticket records the reason and frees the queue', async ($, on) => {
  const calls = fake(on, () => ({ ok: true, items: [], rows: [] }))
  const r = await $.tool.call({ tool: 'mcp__argus-desk__skip_ticket', ticket: '123456', reason: 'customer answered their own question' } as any)
  expect(String(r.result)).toMatch(/needs no reply/)
  const mark = calls.find(c => cmdOf(c) === 'mark')
  expect(mark!.argv).toContain('skipped')
  expect(mark!.argv).toContain('customer answered their own question')
})

test('a poll that gets an error body back names the proxy, the endpoint and the proxy\'s own words instead of "Polled 0"', async ($, on) => {
  fake(on, () => ({ ok: true, items: [], rows: [], next: null, busy: [] }))
  let answer = '{"error":"HTTP 404: Client error \'404 Not Found\' for url \'https://zendesk-full.example/tickets/search\'"}'
  on('tool.call', { tool: 'mcp__local-argus__invoke_proxy' }, async () => ({ result: {}, text: answer }))
  await $.tool.call({ tool: 'mcp__local-argus__invoke_proxy', proxy: 'zendesk-full', endpoint: 'zendesk_get_ticket', arguments: { ticket_id: 123456 } } as any)
  const r = await $.command.run({ command: 'desk', args: 'poll' })
  expect(String(r.text)).toMatch(/Poll failed: zendesk-full zendesk_search_tickets for query ".*" answered: .*HTTP 404/)
  // an empty but healthy answer is reported with the proxy's words too, so a wrong query shows
  answer = '{"results":[],"count":0}'
  const r2 = await $.command.run({ command: 'desk', args: 'poll' })
  expect(String(r2.text)).toMatch(/Polled 0 open ticket\(s\)\. For query ".*" the proxy answered: \{"results":\[\],"count":0\}/)
})

test('a documentation search is on the record and captured as a page per official URL, so a docs receipt can quote it', async ($, on) => {
  const calls = fake(on, () => ({ ok: true, items: [], rows: [] }))
  on('tool.call', { tool: 'mcp__argus__enterprise_search' }, async () => ({ result: {}, text: 'Kubernetes connector: the Secrets Manager limit is 100 per account. Source: https://docs.wiz.io/docs/k8s-connector' }))
  const r = await $.tool.call({ tool: 'mcp__argus__enterprise_search', query: 'secrets manager limit', data_source: 'wiz_docs_public' } as any)
  expect(r.deny).toBeUndefined()
  const page = calls.find(c => cmdOf(c) === 'page')
  expect(page).toBeDefined()
  expect(JSON.parse(page!.stdin!).source).toBe('https://docs.wiz.io/docs/k8s-connector')
  expect(JSON.parse(page!.stdin!).body).toMatch(/limit is 100/)
})

test('the poller\'s pull spawns under the driver a /desk command created; /desk queue switches the tab without a key', async ($, on) => {
  const clock = mock.clock(on)
  const store: any[] = []
  fake(on, r => {
    const c = cmdOf(r)
    if (c === 'sync') return { ok: true, rows: [NEXT], next: NEXT, busy: [] }
    if (c === 'queue') return { ok: true, rows: [NEXT] }
    if (c === 'trace') return RUN('new', store)
    if (c === 'run') { const b = JSON.parse(r.stdin!); if (b.agent?.id) store.push(b.agent); return { ok: true } }
    return { ok: true, items: [], rows: [] }
  })
  const spawns: any[] = []
  on('agent.spawn', async (_$: any, e: any) => { spawns.push(e); return { agentId: `a${spawns.length}`, model: 'sonnet' } })
  on('tool.call', { tool: 'mcp__local-argus__invoke_proxy' }, async () => ({ result: SEARCH, text: JSON.stringify(SEARCH) }))
  await $.tool.call({ tool: 'mcp__local-argus__invoke_proxy', proxy: 'zendesk-full', endpoint: 'zendesk_get_ticket', arguments: { ticket_id: 123456 } } as any)
  // /desk resume is a command: it creates the driver; the poll on the clock then pulls, and the driver spawns
  await $.command.run({ command: 'desk', args: 'resume' })
  await clock.advance(5 * 60_000 + 10)
  await clock.advance(4_000)
  expect(spawns.map(s => s.description)).toEqual(['investigator #123457 inv1', 'investigator #123457 inv2', 'investigator #123457 inv3'])
  const r = await $.command.run({ command: 'desk', args: 'queue' })
  expect(String(r.text)).toMatch(/Desk: queue tab/)
  expect(String(r.text)).toMatch(/ctrl\+x then tab/)
})

test('/desk query sets the search the poller sends, kept in the plugin store, and the poll line names it', async ($, on) => {
  const kv: Record<string, unknown> = {}
  on('store.get', async (_$: any, e: any) => ({ value: kv[e.key] }))
  on('store.set', async (_$: any, e: any) => { kv[e.key] = e.value; return { value: undefined } })
  on('store.delete', async (_$: any, e: any) => { delete kv[e.key]; return { value: undefined } })
  fake(on, () => ({ ok: true, items: [], rows: [], next: null, busy: [] }))
  const searches: any[] = []
  on('tool.call', { tool: 'mcp__local-argus__invoke_proxy' }, async (_$: any, e: any) => { searches.push(e); return { result: {}, text: '{"results":[],"count":0}' } })
  await $.tool.call({ tool: 'mcp__local-argus__invoke_proxy', proxy: 'zendesk-full', endpoint: 'zendesk_get_ticket', arguments: { ticket_id: 123456 } } as any)
  const set = await $.command.run({ command: 'desk', args: 'query type:ticket status<pending assignee:25873958858140' })
  expect(String(set.text)).toMatch(/Search query now: type:ticket status<pending assignee:25873958858140/)
  searches.length = 0
  const r = await $.command.run({ command: 'desk', args: 'poll' })
  expect(searches[0].arguments.query).toBe('type:ticket status<pending assignee:25873958858140')
  expect(String(r.text)).toMatch(/For query "type:ticket status<pending assignee:25873958858140" the proxy answered/)
  const show = await $.command.run({ command: 'desk', args: 'query' })
  expect(String(show.text)).toMatch(/Search query: type:ticket status<pending assignee:25873958858140/)
})

test('agentTools=mcp attaches the desk tools to each agent type as an MCP server, so a call inside an agent is served by a process, not a hook', { options: { agentTools: 'mcp', stateDir: '/tmp/desk-mcp-test' } }, async ($, on) => {
  const specs: any[] = []
  on('agent.register', async (_$: any, e: any) => { specs.push(e); return { value: { agent: `argus-desk:${e.name}` } } })
  fake(on, () => ({ ok: true, items: [], rows: [], next: null, busy: [] }))
  on('tool.call', { tool: 'mcp__local-argus__invoke_proxy' }, async () => ({ result: {}, text: '{}' }))
  // the agent types are registered in session.start and again by the first /desk command (the test host fires no session.start)
  await $.command.run({ command: 'desk', args: 'pause' })
  expect(specs.map(s => s.name)).toEqual(['investigator', 'adversary', 'drafter', 'reviewer'])
  const inv = specs[0]
  expect(inv.mcpServers).toHaveLength(1)
  const srv = inv.mcpServers[0].desk
  expect(srv.args.join(' ')).toMatch(/checks\/desk_mcp\.py --root \/tmp\/desk-mcp-test --role investigator --docs-hosts docs\.wiz\.io/)
  expect(String(inv.prompt)).toMatch(/served by the MCP server named desk/)
  expect(inv.tools).toBeUndefined()
})

test('by default the agent types carry no MCP server and the hook-served desk tools answer', async ($, on) => {
  const specs: any[] = []
  on('agent.register', async (_$: any, e: any) => { specs.push(e); return { value: { agent: `argus-desk:${e.name}` } } })
  fake(on, () => ({ ok: true, id: 'f-1', items: [], rows: [], next: null, busy: [] }))
  on('tool.call', { tool: 'mcp__local-argus__invoke_proxy' }, async () => ({ result: {}, text: '{}' }))
  // the agent types are registered in session.start and again by the first /desk command (the test host fires no session.start)
  await $.command.run({ command: 'desk', args: 'pause' })
  expect(specs.length).toBe(4)
  expect(specs.every(s => s.mcpServers === undefined)).toBe(true)
  const r = await $.tool.call({ tool: 'mcp__argus-desk__desk_finding', ticket: '123456', kind: 'hypothesis', text: 'x' } as any)
  expect(String(r.result)).toMatch(/Recorded f-1 \(hypothesis\)/)
})
