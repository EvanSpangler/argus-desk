import { test, expect } from 'claude-code/testing'

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
const cmdOf = (r: Run) => r.argv.find(a => ['file', 'list', 'grant', 'deny', 'modify', 'consume', 'capture', 'stats'].includes(String(a)))

const DRAFT = 'Hi Sam,\n\nThe broker is disabled, so the connector reports unhealthy. Re-enable it under Settings > Connectors.\n\nThanks!\n'
const ITEM = { md5: 'a1b2c3d4e5f60718293a4b5c6d7e8f90', ticket: '123456', state: 'pending', filed: 1, age_s: 120, chars: 120, text: DRAFT,
  checks: { verdict: 'clear', hits: [], warnings: ['no-thread-captured'], thread: { status: 'no-thread' }, claims: { checkable: 2, credited: 0, uncovered: 2, echo: 0, hits: ['uncovered'], shadow: true } } }

test('an outward post with no grant is refused and told what to do', async ($, on) => {
  const calls = fake(on, r => (cmdOf(r) === 'consume' ? { ok: false, tried: [], undecided: [ITEM.md5] } : { ok: true, items: [ITEM] }))
  const r = await $.tool.call({ tool: 'mcp__local-argus__invoke_proxy', method: 'POST', path: '/api/v2/tickets/123456/comments', body: { comment: { body: DRAFT, public: true } } } as any)
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
  const first = await $.tool.call({ tool: 'mcp__local-argus__invoke_proxy', method: 'POST', path: '/api/v2/tickets/123456/comments', body: { comment: { body: DRAFT } } } as any)
  expect(first.deny).toBeUndefined()
  const second = await $.tool.call({ tool: 'mcp__local-argus__invoke_proxy', method: 'POST', path: '/api/v2/tickets/123456/comments', body: { comment: { body: DRAFT } } } as any)
  expect(second.deny).toMatch(/argus-desk/)
  expect(calls.filter(c => cmdOf(c) === 'consume').length).toBe(2)
})

test('a read of the ticket comments passes untouched and is captured as the live thread', async ($, on) => {
  const calls = fake(on, () => ({ ok: true, items: [] }))
  const payload = { comments: [{ id: 1, author_role: 'end-user', created_at: '2026-10-14T10:00:00Z', plain_body: 'Scans fail. Which version?' }, { id: 2, author_role: 'agent', created_at: '2026-10-14T11:00:00Z', plain_body: 'Please send the logs.' }], count: 2 }
  on('tool.call', { tool: 'mcp__local-argus__invoke_proxy' }, async () => ({ result: payload, text: JSON.stringify(payload) }))
  const r = await $.tool.call({ tool: 'mcp__local-argus__invoke_proxy', method: 'GET', path: '/api/v2/tickets/123456/comments.json' } as any)
  expect(r.deny).toBeUndefined()
  const cap = calls.find(c => cmdOf(c) === 'capture')
  expect(cap).toBeDefined()
  const body = JSON.parse(cap!.stdin!)
  expect(body.ticket).toBe('123456')
  expect(body.count).toBe(2)
  expect(body.messages[1].who).toBe('us')
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
