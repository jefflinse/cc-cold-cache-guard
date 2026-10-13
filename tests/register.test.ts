import type { ModelCompleteRequest, ModelCompleteResult, On, ProcessRunResult, PromptFillResult, ToolCallResult } from 'claude-code'
import { describe, expect, mock, test, type Engine } from 'claude-code/testing'

// The world beneath the plugin: stale_guard.py, the model, the dialogs and the
// session, each answered from memory and recorded.

const SESSION = 'sess-1234abcd'
const HANDOFF_PATH = '/handoffs/20261006-120000-sess-123.md'
const HANDOFF_TEXT = '# Handoff from a previous Claude Code session\n\n## Goal\nShip it.\n'

type Fakes = {
  check?: Record<string, unknown>
  prepare?: Record<string, unknown>
  complete?: Record<string, unknown>
  reply?: ModelCompleteResult
  /** Picks the answer to each `$.ui.ask`; default: the first option. */
  pick?: (question: string, options: readonly string[]) => string
}

const STALE = {
  stale: true,
  idle_minutes: 75,
  idle_text: '1h 15m',
  ttl_minutes: 60,
  context_tokens: 150_000,
  provider: 'local',
  handoff_model: 'google/gemma-4-26b-a4b-qat',
  handoff_unavailable_reason: null,
  auto_continue_choice: false,
}

const PREPARED = {
  ok: true,
  provider: 'anthropic',
  model: 'haiku',
  effort: 'low',
  max_output_tokens: 16384,
  timeout_seconds: 540,
  system: 'SYSTEM PROMPT',
  prompt: 'CONDENSED TRANSCRIPT',
  condensed_chars: 20,
}

const ANSWERED: ModelCompleteResult = {
  isAnswered: true,
  text: '## Goal\nShip it.',
  usage: { input_tokens: 5000, output_tokens: 800, cache_creation_input_tokens: 0, cache_read_input_tokens: 0 },
} as ModelCompleteResult

function world(on: On, fakes: Fakes = {}) {
  const w = {
    asked: [] as { question: string; options: readonly string[] }[],
    runs: [] as { command: string; argv: readonly string[]; stdin: string; env?: Record<string, string> }[],
    completions: [] as ModelCompleteRequest[],
    commands: [] as string[],
    filled: [] as string[],
    submitted: [] as { text: string; origin: unknown }[],
    logs: [] as string[],
    toasts: [] as string[],
    /** The input box. */
    box: '',
    /** What the input box held when the model was asked for the summary. */
    boxAtSummary: undefined as string | undefined,
  }
  const scripted: Record<string, unknown> = {
    config: {
      ok: true,
      settings: [
        { key: 'provider', value: 'anthropic', source: 'default' },
        { key: 'auto_continue_choice', value: true, source: '/config' },
      ],
    },
    check: fakes.check ?? STALE,
    prepare: fakes.prepare ?? PREPARED,
    complete: fakes.complete ?? { ok: true, text: '## Goal\nShip it.' },
    save: { ok: true, path: HANDOFF_PATH, text: HANDOFF_TEXT },
  }

  on('session.id', async () => ({ value: SESSION }))
  on('session.cwd', async () => ({ value: '/repo' }))
  on('env.get', async () => ({ value: undefined }))
  on('settings.read', async () => ({
    value: { enabledPlugins: { 'cc-cold-cache-guard@cc-cold-cache-guard': true } },
  }))
  on('process.run', async (_$, e) => {
    const command = e.argv[2] ?? ''
    w.runs.push({ command, argv: e.argv, stdin: e.init?.stdin ?? '', env: e.init?.env })
    const stdout = JSON.stringify(scripted[command])
    return { value: { exitCode: 0, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } as ProcessRunResult }
  })
  on('model.complete', async (_$, e) => {
    w.completions.push(e)
    w.boxAtSummary = w.box
    return { value: fakes.reply ?? ANSWERED }
  })
  // $.ui.ask is a tool.call of AskUserQuestion.
  on('tool.call', { tool: 'AskUserQuestion' }, async (_$, e) => {
    const q = (e as unknown as { questions: { question: string; options: { label: string }[] }[] }).questions[0]!
    const options = q.options.map(o => o.label)
    w.asked.push({ question: q.question, options })
    const answer = fakes.pick ? fakes.pick(q.question, options) : options[0]!
    return { result: { questions: [q], answers: { [q.question]: answer } } } as unknown as ToolCallResult
  })
  on('command.run', async (_$, e) => {
    w.commands.push(e.args ? `${e.command} ${e.args}` : e.command)
    return { text: '' }
  })
  on('prompt.read', async () => ({ value: { text: w.box, cursor: w.box.length } }))
  on('prompt.fill', async (_$, e) => {
    w.filled.push(e.text)
    w.box = e.text
    return { isFilled: true } as PromptFillResult
  })
  on('prompt.submit', async (_$, e) => {
    w.submitted.push({ text: e.text, origin: e.origin })
    return { text: e.text }
  })
  on('ui.log', async (_$, e) => {
    w.logs.push(e.text)
    return { value: undefined }
  })
  on('ui.toast', async (_$, e) => {
    w.toasts.push(e.text)
    return { value: undefined }
  })
  on('ui.status', async () => ({ value: undefined }))
  return w
}

const typed = (text: string) => ({ text, origin: { kind: 'composer' as const }, wait: false })

/** Sends a typed prompt; as in Claude Code 2.1.296 and later, a dropped one goes back in the input box. */
async function send($: Engine, w: ReturnType<typeof world>, text: string) {
  const r = await $.prompt.submit(typed(text))
  if (r.drop !== undefined) w.box = text
  return r
}

const pickContaining = (needle: string) => (_q: string, options: readonly string[]) =>
  options.find(o => o.includes(needle)) ?? options[0]!

describe('the cold-cache dialog', () => {
  test('lets a warm session through without asking', async ($, on) => {
    mock.clock(on, { now: 10 * 60_000 })
    const w = world(on, { check: { ...STALE, stale: false } })
    const r = await $.prompt.submit(typed('hello'))
    expect(r.drop).toBeUndefined()
    expect(w.asked).toEqual([])
  })

  test('offers handoff, send and hold, with no auto option by default', async ($, on) => {
    mock.clock(on, { now: 10 * 60_000 })
    const w = world(on, { pick: pickContaining('Send anyway') })
    await $.prompt.submit(typed('hello'))
    expect(w.asked[0]!.options).toEqual([
      'Generate a handoff prompt using gemma-4-26b-a4b-qat (local, free)',
      'Send anyway (re-caches ~150k tokens)',
      "Don't send (put it back in the input box)",
    ])
  })

  test('adds the auto option second when auto_continue_choice is on', async ($, on) => {
    mock.clock(on, { now: 10 * 60_000 })
    const w = world(on, {
      check: { ...STALE, provider: 'anthropic', handoff_model: 'haiku', auto_continue_choice: true },
      pick: pickContaining('Send anyway'),
    })
    await $.prompt.submit(typed('hello'))
    expect(w.asked[0]!.options).toEqual([
      'Generate a handoff prompt using haiku (Anthropic)',
      'Generate a handoff using haiku (Anthropic) and continue in a fresh session with it right away',
      'Send anyway (re-caches ~150k tokens)',
      "Don't send (put it back in the input box)",
    ])
  })

  test('says why when no handoff model is available', async ($, on) => {
    mock.clock(on, { now: 10 * 60_000 })
    const w = world(on, {
      check: { ...STALE, handoff_model: null, handoff_unavailable_reason: 'LM Studio is running but no model is loaded', auto_continue_choice: true },
      pick: pickContaining("Don't send"),
    })
    const r = await $.prompt.submit(typed('hello'))
    expect(w.asked[0]!.options).toHaveLength(2)
    expect(w.asked[0]!.question).toContain('Handoff unavailable: LM Studio is running but no model is loaded')
    expect(r.drop).toContain('Not sent')
  })
})

describe('generating the handoff', () => {
  test('Anthropic: completes in-process with the configured model and effort', async ($, on) => {
    const clock = mock.clock(on, { now: 10 * 60_000 })
    const w = world(on, { check: { ...STALE, provider: 'anthropic', handoff_model: 'haiku' }, pick: pickContaining('Neither') })
    const r = await send($, w, 'next thing')
    expect(r.drop).toContain('Held: generating a handoff')
    await clock.settle()

    expect(w.completions).toEqual([
      { model: 'haiku', system: 'SYSTEM PROMPT', prompt: 'CONDENSED TRANSCRIPT', effort: 'low', maxTokens: 16384, timeoutMs: 540_000 },
    ])
    expect(w.runs.map(r => r.command)).toEqual(['check', 'prepare', 'save'])
    const save = w.runs[2]!
    expect(save.argv).toEqual(expect.arrayContaining(['--provider', 'anthropic', '--model', 'haiku']))
    expect(JSON.parse(save.stdin)).toEqual({ body: '## Goal\nShip it.', held: 'next thing' })
    expect(w.logs.some(l => l.includes('Handoff ready') && l.includes('5000 in / 800 out tokens'))).toBe(true)
    // The manual option still asks what to do next.
    expect(w.asked[1]!.question).toContain('Your handoff is ready')
    expect(w.commands).toEqual([])
    // The held message is in the handoff, so it leaves the input box.
    expect(w.boxAtSummary).toBe('')
    expect(w.box).toBe('')
  })

  test('local: completes through stale_guard.py and never calls the Anthropic API', async ($, on) => {
    const clock = mock.clock(on, { now: 10 * 60_000 })
    const w = world(on, {
      prepare: { ...PREPARED, provider: 'local', model: 'google/gemma-4-26b-a4b-qat' },
      pick: pickContaining('Neither'),
    })
    await send($, w, 'next thing')
    await clock.settle()

    expect(w.completions).toEqual([])
    expect(w.runs.map(r => r.command)).toEqual(['check', 'prepare', 'complete', 'save'])
    expect(w.runs[2]!.argv).toEqual(expect.arrayContaining(['--model', 'google/gemma-4-26b-a4b-qat']))
    expect(JSON.parse(w.runs[2]!.stdin)).toEqual({ system: 'SYSTEM PROMPT', prompt: 'CONDENSED TRANSCRIPT' })
  })

  test('"start fresh" clears and pre-fills an @-mention of the file', async ($, on) => {
    const clock = mock.clock(on, { now: 10 * 60_000 })
    const w = world(on, { pick: (q, o) => (q.includes('ready') ? o.find(x => x.startsWith('Start a fresh'))! : o[0]!) })
    await send($, w, 'next thing')
    await clock.settle()

    expect(w.commands).toEqual(['clear'])
    expect(w.filled).toEqual(['', `@${HANDOFF_PATH} `])
    expect(w.submitted.filter(s => (s.origin as { kind: string }).kind === 'plugin')).toEqual([])
  })

  test('auto: skips the follow-up question, clears and submits the handoff as the user', async ($, on) => {
    const clock = mock.clock(on, { now: 10 * 60_000 })
    const w = world(on, {
      check: { ...STALE, provider: 'anthropic', handoff_model: 'haiku', auto_continue_choice: true },
      pick: pickContaining('right away'),
    })
    const r = await send($, w, 'next thing')
    expect(r.drop).toContain('then continuing in a fresh session')
    await clock.settle()

    expect(w.asked).toHaveLength(1)
    expect(w.commands).toEqual(['clear'])
    expect(w.boxAtSummary).toBe('')
    expect(w.box).toBe('')
    const sent = w.submitted.filter(s => (s.origin as { kind: string }).kind === 'plugin')
    expect(sent).toHaveLength(1)
    expect(sent[0]!.text).toBe(HANDOFF_TEXT)
    expect(sent[0]!.origin).toMatchObject({ asUser: true })
  })

  test('an Anthropic API error gives the held message back and skips the LM Studio hint', async ($, on) => {
    const clock = mock.clock(on, { now: 10 * 60_000 })
    const w = world(on, {
      check: { ...STALE, provider: 'anthropic', handoff_model: 'haiku', auto_continue_choice: true },
      reply: {
        isAnswered: false,
        reason: 'api-error',
        status: 529,
        error: 'overloaded',
        usage: { input_tokens: 0, output_tokens: 0, cache_creation_input_tokens: 0, cache_read_input_tokens: 0 },
      } as ModelCompleteResult,
      pick: pickContaining('right away'),
    })
    await send($, w, 'next thing')
    await clock.settle()

    const failure = w.logs.find(l => l.startsWith('✗'))
    expect(failure).toContain('API error (overloaded, HTTP 529)')
    expect(failure).not.toContain('LM Studio')
    expect(w.filled).toEqual(['', 'next thing'])
    expect(w.box).toBe('next thing')
    expect(w.commands).toEqual([])
    expect(w.runs.map(r => r.command)).not.toContain('save')
  })

  test('a local failure keeps the LM Studio hint', async ($, on) => {
    const clock = mock.clock(on, { now: 10 * 60_000 })
    const w = world(on, {
      prepare: { ...PREPARED, provider: 'local', model: 'gemma' },
      complete: { ok: false, error: 'local model at http://localhost:1234: timed out' },
    })
    await send($, w, 'next thing')
    await clock.settle()

    expect(w.logs.find(l => l.startsWith('✗'))).toContain('lms server start')
    expect(w.filled).toEqual(['', 'next thing'])
  })

  test('leaves the input box alone once the person has typed over the held message', async ($, on) => {
    const clock = mock.clock(on, { now: 10 * 60_000 })
    const w = world(on, { pick: pickContaining('Neither') })
    await send($, w, 'next thing')
    w.box = 'something else'
    await clock.settle()

    expect(w.filled).toEqual([])
    expect(w.box).toBe('something else')
  })

  test('works on Claude Code before 2.1.296, which empties the box on a drop', async ($, on) => {
    const clock = mock.clock(on, { now: 10 * 60_000 })
    const w = world(on, { pick: pickContaining('Neither') })
    await $.prompt.submit(typed('next thing'))
    await clock.settle()

    expect(w.filled).toEqual([])
    expect(w.runs.map(r => r.command)).toContain('save')
  })

  test('/handoff asks what to do next and has no held message', async ($, on) => {
    const clock = mock.clock(on, { now: 10 * 60_000 })
    const w = world(on, { pick: pickContaining('Neither') })
    await $.command.run({ command: 'handoff', args: '', origin: { kind: 'composer' }, presentation: { isFullscreen: false, columns: 80 } })
    await clock.settle()

    expect(JSON.parse(w.runs.find(r => r.command === 'save')!.stdin)).toEqual({ body: '## Goal\nShip it.', held: '' })
    expect(w.asked.map(a => a.question)).toEqual(['Your handoff is ready. What do you want to do with it?'])
  })
})

const RUN = { args: '', origin: { kind: 'composer' as const }, presentation: { isFullscreen: false, columns: 80 } }

describe('settings', () => {
  test('the settings reach stale_guard.py', { options: { anthropic_model: 'sonnet' } }, async ($, on) => {
    mock.clock(on, { now: 10 * 60_000 })
    const w = world(on, { pick: pickContaining('Send anyway') })
    await $.prompt.submit(typed('hello'))
    const options = JSON.parse(w.runs.find(r => r.command === 'check')!.env!.STALE_GUARD_PLUGIN_OPTIONS!)
    expect(options.anthropic_model).toBe('sonnet')
    expect(options.provider).toBe('anthropic')
  })

  test("/cold-cache-guard-configure lists the settings and opens the plugin's settings screen", async ($, on) => {
    const clock = mock.clock(on, { now: 10 * 60_000 })
    const w = world(on)
    const r = await $.command.run({ command: 'cold-cache-guard-configure', ...RUN })
    await clock.settle()
    expect(r.text).toContain('provider              "anthropic"\n')
    expect(r.text).toContain('auto_continue_choice  true  (from /config)')
    expect(r.text).toContain('Change them with /plugin configure cc-cold-cache-guard@cc-cold-cache-guard')
    expect(w.commands).toContain('plugin configure cc-cold-cache-guard@cc-cold-cache-guard')
  })
})
