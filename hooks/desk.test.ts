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
const cmdOf = (r: Run) => r.argv.find(a => ['file', 'list', 'grant', 'deny', 'modify', 'consume', 'capture', 'stats', 'sync', 'queue', 'mark'].includes(String(a)))

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

test('/desk poll searches the open set through the outward tool, folds it into the queue and pulls the next ticket', async ($, on) => {
  const clock = mock.clock(on)
  const calls = fake(on, r => (cmdOf(r) === 'sync' ? { ok: true, rows: [NEXT], next: NEXT, busy: [] } : cmdOf(r) === 'queue' ? { ok: true, rows: [NEXT] } : { ok: true, items: [] }))
  const prompts: string[] = []
  on('prompt.submit', async (_$: any, e: any) => { prompts.push(e.text); return { text: e.text } })
  const searches: any[] = []
  on('tool.call', { tool: 'mcp__local-argus__invoke_proxy' }, async (_$: any, e: any) => { searches.push(e); return { result: SEARCH, text: JSON.stringify(SEARCH) } })
  await $.tool.call({ tool: 'mcp__local-argus__invoke_proxy', proxy: 'zendesk-full', endpoint: 'zendesk_get_ticket', arguments: { ticket_id: 1 } } as any)
  searches.length = 0
  const r = await $.command.run({ command: 'desk', args: 'poll' })
  expect(String(r.text)).toMatch(/Polled 2/)
  expect(String(r.text)).toMatch(/pulled #123457/)
  expect(searches[0].endpoint).toBe('zendesk_search_tickets')
  expect(searches[0].proxy).toBe('zendesk-full')
  const sync = calls.find(c => cmdOf(c) === 'sync')
  expect(sync).toBeDefined()
  const tickets = JSON.parse(sync!.stdin!).tickets
  expect(tickets.map((t: any) => t.id)).toEqual(['123456', '123457'])
  expect(tickets[1].sla).toBe('2026-10-15T09:00:00Z')
  const mark = calls.find(c => cmdOf(c) === 'mark')
  expect(mark).toBeDefined()
  expect(mark!.argv).toContain('123457')
  expect(mark!.argv).toContain('drafting')
  await clock.advance(5)
  expect(prompts.join(' || ')).toMatch(/#123457[^|]*file_draft[^|]*skip_ticket/)
  // the gate saw the search as a read: no consume
  expect(calls.filter(c => cmdOf(c) === 'consume').length).toBe(0)
})

test('the poller runs on the clock while polling is on, and not while paused', async ($, on) => {
  const clock = mock.clock(on)
  fake(on, r => (cmdOf(r) === 'sync' ? { ok: true, rows: [], next: null, busy: [] } : { ok: true, items: [], rows: [] }))
  let searches = 0
  on('tool.call', { tool: 'mcp__local-argus__invoke_proxy' }, async () => { searches++; return { result: { results: [] }, text: '{"results":[]}' } })
  await $.tool.call({ tool: 'mcp__local-argus__invoke_proxy', proxy: 'zendesk-full', endpoint: 'zendesk_get_ticket', arguments: { ticket_id: 1 } } as any)
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
