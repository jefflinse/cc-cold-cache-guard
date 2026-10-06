import type { EngineInterface, Register } from 'claude-code'

// The heavy lifting (reading the transcript, condensing it, calling LM Studio)
// lives in bin/stale_guard.py; this module owns the interaction.

type Check = {
  stale: boolean
  idle_minutes: number | null
  idle_text?: string
  ttl_minutes: number | null
  context_tokens: number | null
  /** Only present when stale: the local model a handoff would use, or why there is none. */
  local_model?: string | null
  local_unavailable_reason?: string | null
  error?: string
}

type Handoff =
  | {
      ok: true
      path: string
      model: string
      seconds: number
      condensed_chars: number
      text: string
    }
  | { ok: false; error: string }

const HANDOFF_TIMEOUT_MS = 600_000 // $.process.run's ceiling

// A prompt sent this soon after the last turn cannot have a cold cache (the
// shortest TTL is 5 minutes), so skip spawning the check.
const WARM_MS = 3 * 60_000

const kTokens = (n: number | null) => (n ? `~${Math.round(n / 1000)}k tokens` : 'the whole conversation')

// Module state; a hot reload resets it, which is harmless.
let lastTurnAt = 0
let handoffRunning = false

function script($: EngineInterface) {
  return `${$.plugin.root}/bin/stale_guard.py`
}

async function check($: EngineInterface): Promise<Check | null> {
  const sessionId = await $.session.id()
  const { stdout } = await $.process.run(['python3', script($), 'check', '--session-id', sessionId])
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

const FRESH = 'Start a fresh session here with the handoff (runs /clear, pre-fills your prompt)'
const COPY = 'Copy the handoff to the clipboard'
const KEEP = 'Neither, just keep the file'

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

  if (choice === FRESH) {
    const previous = await $.session.id()
    await $.command.run({ command: 'clear' })
    lastTurnAt = 0
    // An @-mention typed by the person is expanded into the file's contents on Enter.
    await $.prompt.fill({ text: `@${path} ` })
    $.ui.log(`Started fresh from the handoff. The previous session is kept: claude --resume ${previous}`)
    $.ui.toast('Review the prompt and press Enter to continue in the new session.', { timeoutMs: 8000 })
  }
}

async function runHandoff($: EngineInterface, held: string, model?: string) {
  if (handoffRunning) {
    $.ui.toast('A local handoff is already being generated.')
    return
  }
  handoffRunning = true
  const started = await $.clock.now()
  const tick = $.clock.every(1000, () => {
    void $.clock.now().then(now => {
      $.ui.status(`⏳ Local handoff: summarizing with ${model ? shortModel(model) : 'local model'}… ${Math.round((now - started) / 1000)}s`)
    })
  })
  try {
    const sessionId = await $.session.id()
    const cwd = await $.session.cwd()
    const { stdout, stderr } = await $.process.run(
      ['python3', script($), 'handoff', '--session-id', sessionId, '--cwd', cwd],
      { stdin: held, timeoutMs: HANDOFF_TIMEOUT_MS },
    )
    let result: Handoff
    try {
      result = JSON.parse(stdout) as Handoff
    } catch {
      result = { ok: false, error: (stderr || stdout).trim().slice(0, 500) || 'no output' }
    }
    if (!result.ok) {
      $.ui.log(`✗ Local handoff failed: ${result.error}\n  Is LM Studio's server running with a model loaded? (lms server start)`)
      $.ui.toast('Local handoff failed — see the transcript for why.')
      if (held) await $.prompt.fill({ text: held })
      return
    }
    tick.cancel()
    $.ui.status(undefined)
    $.ui.log(`✓ Handoff ready (${shortModel(result.model)}, ${result.seconds}s): ${result.path}`)
    try {
      await offerNextStep($, result.path, result.text)
    } catch (err) {
      $.ui.log(`Handoff is saved at ${result.path}, but the follow-up step failed: ${err instanceof Error ? err.message : String(err)}`)
    }
  } catch (err) {
    $.ui.log(`✗ Local handoff failed: ${err instanceof Error ? err.message : String(err)}`)
    if (held) await $.prompt.fill({ text: held })
  } finally {
    tick.cancel()
    $.ui.status(undefined)
    handoffRunning = false
  }
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'handoff',
      description: 'Summarize this session with your local LLM into a handoff for a new session',
    })
    return next(e)
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
    const override = Number(await $.env.get('STALE_GUARD_IDLE_MINUTES'))
    const warmMs = override > 0 ? Math.min(WARM_MS, override * 60_000) : WARM_MS
    if ((await $.clock.now()) - lastTurnAt < warmMs) return next(e)

    const status = await check($)
    if (!status?.stale) return next(e)

    const model = status.local_model ?? null
    const handoff = model ? `Generate a handoff prompt using ${shortModel(model)} (local, free)` : null
    const send = `Send anyway (re-caches ${kTokens(status.context_tokens)})`
    const hold = "Don't send (put it back in the input box)"
    const options = handoff ? [handoff, send, hold] : [send, hold]
    const noLocal = model ? '' : ` (Local handoff unavailable: ${status.local_unavailable_reason ?? 'no local model'}.)`
    let choice: string
    try {
      choice = await $.ui.ask(
        `This session has been idle ${status.idle_text}, so its prompt cache (~${status.ttl_minutes}m) has ` +
          `likely expired.${noLocal} What do you want to do with your message?`,
        { header: 'Cold cache', options },
      )
    } catch {
      choice = hold // dismissed with Esc
    }

    if (choice === send) return next(e)

    if (handoff && model && choice === handoff) {
      $.clock.after(0, () => void runHandoff($, e.text, model))
      return { drop: 'Held: generating a local handoff (progress in the status line).' }
    }

    // Anything else (including free text typed under "Other"): give the message back.
    $.clock.after(50, () => void $.prompt.fill({ text: e.text }))
    return { drop: 'Not sent. Your message is back in the input box.' }
  })
}
