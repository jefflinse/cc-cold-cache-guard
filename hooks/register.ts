import type { EngineInterface, ModelEffort, PluginOptions, Register } from 'claude-code'

// The heavy lifting (reading the transcript, condensing it, calling LM Studio,
// writing the handoff file) lives in bin/stale_guard.py; this module owns the
// interaction, and makes the model call itself when the provider is Anthropic.

type Provider = 'local' | 'anthropic'

type Check = {
  stale: boolean
  idle_minutes: number | null
  idle_text?: string
  ttl_minutes: number | null
  context_tokens: number | null
  /** Idle long enough, but Claude Code compacted the session before its cache expired. */
  idle_compacted?: boolean
  /** Only present when stale: the model a handoff would use, or why there is none. */
  provider?: Provider
  handoff_model?: string | null
  handoff_unavailable_reason?: string | null
  auto_continue_choice?: boolean
  error?: string
}

type Failure = { ok: false; error: string }

type Prepared =
  | {
      ok: true
      provider: Provider
      model: string
      effort: ModelEffort
      max_output_tokens: number
      timeout_seconds: number
      system: string
      prompt: string
      condensed_chars: number
    }
  | Failure

type Completed = { ok: true; text: string; note?: string } | Failure

type Saved = { ok: true; path: string; text: string } | Failure

type Settings = { ok: true; settings: { key: string; value: unknown; source: string }[] } | Failure

const CONFIGURE = 'cold-cache-guard-configure'

const HANDOFF_TIMEOUT_MS = 600_000 // $.process.run's ceiling

// A prompt sent this soon after the last turn cannot have a cold cache (the
// shortest TTL is 5 minutes), so skip spawning the check.
const WARM_MS = 3 * 60_000

const kTokens = (n: number | null) => (n ? `~${Math.round(n / 1000)}k tokens` : 'the whole conversation')

// Module state; a hot reload resets it, which is harmless.
let lastTurnAt = 0
let handoffRunning = false
// The settings screen's values, defaults filled in; register runs again whenever one changes.
let pluginOptions: PluginOptions = {}

/** The id `/plugin configure` takes, as enabledPlugins names it. */
async function pluginId($: EngineInterface) {
  const { enabledPlugins } = (await $.settings.read()) as { enabledPlugins?: Record<string, unknown> }
  // `<name>@<marketplace>` when installed, `<name>` or `<name>@inline` from --plugin-dir.
  const name = $.plugin.name
  return Object.keys(enabledPlugins ?? {}).find(key => key === name || key.startsWith(`${name}@`)) ?? name
}

function script($: EngineInterface) {
  return `${$.plugin.root}/bin/stale_guard.py`
}

// stale_guard.py layers these over config.json; STALE_GUARD_* env vars still win.
function scriptEnv() {
  return { STALE_GUARD_PLUGIN_OPTIONS: JSON.stringify(pluginOptions) }
}

/** Runs one stale_guard.py command; output that isn't JSON becomes a failure. */
async function engine<T extends { ok: boolean }>(
  $: EngineInterface,
  args: string[],
  stdin = '',
): Promise<T | Failure> {
  const { stdout, stderr } = await $.process.run(['python3', script($), ...args], {
    stdin,
    env: scriptEnv(),
    timeoutMs: HANDOFF_TIMEOUT_MS,
  })
  try {
    return JSON.parse(stdout) as T
  } catch {
    return { ok: false, error: (stderr || stdout).trim().slice(0, 500) || 'no output' }
  }
}

async function check($: EngineInterface): Promise<Check | null> {
  const sessionId = await $.session.id()
  const { stdout } = await $.process.run(['python3', script($), 'check', '--session-id', sessionId], { env: scriptEnv() })
  const result = JSON.parse(stdout) as Check
  if (result.error) {
    $.ui.log(`stale-guard: check failed (prompt allowed through): ${result.error}`, { to: 'debug' })
    return null
  }
  return result
}

// "google/gemma-4-26b-a4b-qat" -> "gemma-4-26b-a4b-qat"
function shortModel(id: string) {
  return id.split('/').pop() || id
}

function describeModel(provider: Provider | undefined, model: string) {
  return provider === 'anthropic' ? `${model} (Anthropic)` : `${shortModel(model)} (local, free)`
}

async function summarize($: EngineInterface, p: Extract<Prepared, { ok: true }>): Promise<Completed> {
  if (p.provider === 'local') {
    return engine<Completed>($, ['complete', '--model', p.model], JSON.stringify({ system: p.system, prompt: p.prompt }))
  }
  // Through the session's own API client: no child `claude` process, so no
  // plugins, MCP servers or CLAUDE.md loaded, and no stray session written.
  const r = await $.model.complete({
    model: p.model,
    system: p.system,
    prompt: p.prompt,
    effort: p.effort,
    maxTokens: p.max_output_tokens,
    timeoutMs: p.timeout_seconds * 1000,
  })
  if (r.isAnswered) {
    const { input_tokens, output_tokens } = r.usage
    return { ok: true, text: r.text, note: `${input_tokens} in / ${output_tokens} out tokens` }
  }
  const error =
    r.reason === 'api-error'
      ? `API error (${r.error}${r.status ? `, HTTP ${r.status}` : ''})`
      : r.reason === 'aborted'
        ? `timed out or was cancelled (limit ${p.timeout_seconds}s)`
        : `${p.model} replied with no text`
  return { ok: false, error }
}

const FRESH = 'Start a fresh session here with the handoff (runs /clear, pre-fills your prompt)'
const COPY = 'Copy the handoff to the clipboard'
const KEEP = 'Neither, just keep the file'

async function startFresh($: EngineInterface, path: string, text: string, auto: boolean) {
  const previous = await $.session.id()
  await $.command.run({ command: 'clear' })
  lastTurnAt = 0
  const kept = `The previous session is kept: claude --resume ${previous}`
  if (auto) {
    // @-mentions are not expanded in a plugin's prompt, so send the handoff itself.
    $.ui.log(`Continuing in a fresh session from the handoff at ${path}. ${kept}`)
    await $.prompt.submit({ text, asUser: true })
    return
  }
  // An @-mention typed by the person is expanded into the file's contents on Enter.
  await $.prompt.fill({ text: `@${path} ` })
  $.ui.log(`Started fresh from the handoff. ${kept}`)
  $.ui.toast('Review the prompt and press Enter to continue in the new session.', { timeoutMs: 8000 })
}

async function offerNextStep($: EngineInterface, path: string, text: string) {
  let choice: string
  try {
    choice = await $.ui.ask('Your handoff is ready. What do you want to do with it?', {
      header: 'Handoff',
      options: [FRESH, COPY, KEEP],
    })
  } catch {
    choice = KEEP // dismissed
  }

  if (choice === COPY) {
    const copied = await $.ui.copy({ text })
    $.ui.toast(copied.isCopied ? 'Handoff copied to clipboard.' : `Couldn't copy (${copied.reason}); it's at ${path}`)
    return
  }

  if (choice === FRESH) await startFresh($, path, text, false)
}

/** `auto`: skip the follow-up question and continue in a fresh session with the handoff. */
async function runHandoff($: EngineInterface, held: string, auto = false) {
  if (handoffRunning) {
    $.ui.toast('A handoff is already being generated.')
    return
  }
  handoffRunning = true
  let stage = 'condensing the transcript'
  const started = await $.clock.now()
  const tick = $.clock.every(1000, () => {
    void $.clock.now().then(now => {
      $.ui.status(`⏳ Handoff: ${stage}… ${Math.round((now - started) / 1000)}s`)
    })
  })
  let provider: Provider | undefined
  const fail = async (error: string) => {
    const hint = provider === 'local' ? "\n  Is LM Studio's server running with a model loaded? (lms server start)" : ''
    $.ui.log(`✗ Handoff failed: ${error}${hint}`)
    $.ui.toast('Handoff failed — see the transcript for why.')
    if (held) await $.prompt.fill({ text: held })
  }
  try {
    const sessionId = await $.session.id()
    const cwd = await $.session.cwd()
    const prepared = await engine<Prepared>($, ['prepare', '--session-id', sessionId, '--cwd', cwd])
    if (!prepared.ok) return await fail(prepared.error)
    provider = prepared.provider

    stage = `summarizing with ${describeModel(provider, prepared.model)}`
    const summary = await summarize($, prepared)
    if (!summary.ok) return await fail(summary.error)

    const saved = await engine<Saved>(
      $,
      ['save', '--session-id', sessionId, '--cwd', cwd, '--provider', provider, '--model', prepared.model],
      JSON.stringify({ body: summary.text, held }),
    )
    if (!saved.ok) return await fail(saved.error)

    tick.cancel()
    $.ui.status(undefined)
    const seconds = Math.round(((await $.clock.now()) - started) / 1000)
    const note = summary.note ? `, ${summary.note}` : ''
    $.ui.log(`✓ Handoff ready (${shortModel(prepared.model)}, ${seconds}s${note}): ${saved.path}`)
    try {
      if (auto) await startFresh($, saved.path, saved.text, true)
      else await offerNextStep($, saved.path, saved.text)
    } catch (err) {
      $.ui.log(`Handoff is saved at ${saved.path}, but the follow-up step failed: ${err instanceof Error ? err.message : String(err)}`)
    }
  } catch (err) {
    await fail(err instanceof Error ? err.message : String(err))
  } finally {
    tick.cancel()
    $.ui.status(undefined)
    handoffRunning = false
  }
}

/** The effective settings, one per line, with where each came from. */
async function describeSettings($: EngineInterface, configure: string): Promise<string> {
  const result = await engine<Settings>($, ['config'])
  if (!result.ok) return `Couldn't read the settings: ${result.error}`
  const width = Math.max(...result.settings.map(s => s.key.length))
  const lines = result.settings.map(({ key, value, source }) => {
    const from = source === 'default' ? '' : `  (from ${source})`
    return `  ${key.padEnd(width)}  ${JSON.stringify(value)}${from}`
  })
  return [
    'Current settings:',
    ...lines,
    '',
    `Change them with ${configure} (opening it now). STALE_GUARD_* env vars still win over it.`,
  ].join('\n')
}

export const register: Register = (on, options) => {
  pluginOptions = options

  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'handoff',
      description: "Summarize this session into a handoff for a new session (local LLM or Claude, per the plugin's settings)",
    })
    await $.command.register({
      name: CONFIGURE,
      description: "Show cc-cold-cache-guard's settings and open its settings screen",
    })
    return next(e)
  })

  on('command.run', { command: CONFIGURE }, async $ => {
    // The plugin's own settings screen, not the whole /config menu.
    const args = `configure ${await pluginId($)}`
    const text = await describeSettings($, `/plugin ${args}`)
    // Queued: it opens once this command's output is shown.
    $.clock.after(0, () => void $.command.run({ command: 'plugin', args }).catch(() => {}))
    return { text }
  })

  on('command.run', { command: 'handoff' }, async $ => {
    void runHandoff($, '')
    return {}
  })

  on('turn.complete', async ($, e, next) => {
    lastTurnAt = await $.clock.now()
    return next(e)
  })

  // Resuming an old session: say so before anything is typed.
  on('classic.SessionStart', async ($, e, next) => {
    if (e.source === 'resume' && e.prompt_cache_likely_expired) {
      const idleMin = (e.seconds_since_last_response ?? 0) / 60
      $.ui.toast(
        `This session's prompt cache has likely expired (idle ${Math.round(idleMin / 60)}h, ${kTokens(e.context_tokens ?? null)}). ` +
          `Your next message will ask before re-caching it.`,
        { timeoutMs: 10_000 },
      )
    }
    return next(e)
  })

  on('prompt.submit', async ($, e, next) => {
    // Only guard what the person typed (terminal or Remote Control).
    if (e.origin.kind !== 'composer' && e.origin.kind !== 'bridge') return next(e)
    const override = Number((await $.env.get('STALE_GUARD_IDLE_MINUTES')) || pluginOptions.idle_minutes)
    const warmMs = override > 0 ? Math.min(WARM_MS, override * 60_000) : WARM_MS
    if ((await $.clock.now()) - lastTurnAt < warmMs) return next(e)

    const status = await check($)
    if (status?.idle_compacted) {
      $.ui.log('stale-guard: Claude Code compacted this session while it was idle; prompt allowed through', { to: 'debug' })
    }
    if (!status?.stale) return next(e)

    const model = status.handoff_model ?? null
    const who = model ? describeModel(status.provider, model) : null
    const handoff = who ? `Generate a handoff prompt using ${who}` : null
    const autoContinue =
      who && status.auto_continue_choice ? `Generate a handoff using ${who} and continue in a fresh session with it right away` : null
    const send = `Send anyway (re-caches ${kTokens(status.context_tokens)})`
    const hold = "Don't send (put it back in the input box)"
    const options = [handoff, autoContinue, send, hold].filter((o): o is string => o !== null)
    const unavailable = who ? '' : ` (Handoff unavailable: ${status.handoff_unavailable_reason ?? 'no model'}.)`
    let choice: string
    try {
      choice = await $.ui.ask(
        `This session has been idle ${status.idle_text}, so its prompt cache (~${status.ttl_minutes}m) has ` +
          `likely expired.${unavailable} What do you want to do with your message?`,
        { header: 'Cold cache', options },
      )
    } catch {
      choice = hold // dismissed with Esc
    }

    if (choice === send) return next(e)

    if (choice === handoff || choice === autoContinue) {
      const auto = choice === autoContinue
      $.clock.after(0, () => void runHandoff($, e.text, auto))
      return {
        drop: auto
          ? 'Held: generating a handoff, then continuing in a fresh session (progress in the status line).'
          : 'Held: generating a handoff (progress in the status line).',
      }
    }

    // Anything else (including free text typed under "Other"): give the message back.
    $.clock.after(50, () => void $.prompt.fill({ text: e.text }))
    return { drop: 'Not sent. Your message is back in the input box.' }
  })
}
