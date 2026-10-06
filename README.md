# cc-cold-cache-guard

A Claude Code plugin that catches prompts sent to a session whose prompt cache has likely expired, so you don't pay for a full cache rewrite by accident. Instead, it offers a cheap handoff summarized by a small Claude model (a fraction of the re-cache cost) or a local model (free).

## What It Does

When you send a message to a cold session, the message is held and Claude Code's own multiple-choice dialog asks what to do (no model call is made to ask):

- **Generate a handoff prompt**: a condensed copy of the transcript is summarized by the configured provider (see below). Afterwards, you can start a fresh session with the handoff (`/clear` plus an `@`-mention of the file), copy it to the clipboard, or just keep the file. Your held message is appended to the handoff.
- **Generate a handoff and continue right away** (off by default; turn on with `offer_auto_handoff`): the same, but it doesn't ask what to do next. It runs `/clear` and sends the handoff, including your held message, as the first message of the fresh session.
- **Send anyway**: the message goes through untouched and pays for the cache write.
- **Don't send** (or Esc): your message goes back to the input box.

It also adds a `/handoff` command for generating a handoff any time, and shows a toast when you `--resume` a cold session.

The transcript is only ever read; condensing happens on an in-memory copy. Handoffs are saved to `~/.claude/handoffs/`.

## Providers

- **`anthropic`** (default): Claude, called in-process through the session's own API client and credentials (`$.model.complete`). Nothing is spawned and no extra session is recorded. Defaults to `haiku` at `low` effort. The request is uncached, but it only carries the condensed transcript (≈25k tokens at the default `max_transcript_chars`), not the full conversation.
- **`local`**: LM Studio, or any OpenAI-compatible server, for free handoffs. The handoff options only appear when a model is actually loaded.

## Try It

Requires `python3`. For the `local` provider, LM Studio's server must also be running (`lms server start`) with a model loaded.

```sh
git clone https://github.com/jefflinse/cc-cold-cache-guard.git
claude --plugin-dir ./cc-cold-cache-guard

# force the guard to trip after a minute of idleness:
STALE_GUARD_IDLE_MINUTES=1 claude --plugin-dir ./cc-cold-cache-guard

# ...and summarize with a local model instead of Haiku:
STALE_GUARD_IDLE_MINUTES=1 STALE_GUARD_PROVIDER=local claude --plugin-dir ./cc-cold-cache-guard
```

## Config

Defaults are in `config.json`; override them in `~/.claude/stale-guard.json` or via env vars.

| Key | Default | Notes |
|---|---|---|
| `idle_minutes` | `"auto"` | `auto` = cache TTL detected from the transcript (1h or 5m) minus `auto_margin_minutes`. Env: `STALE_GUARD_IDLE_MINUTES` |
| `provider` | `"anthropic"` | `anthropic` or `local`. Env: `STALE_GUARD_PROVIDER` |
| `offer_auto_handoff` | `false` | Adds the "generate and continue right away" option to the dialog |
| `llm_base_url` | `http://localhost:1234` | Local only. Env: `STALE_GUARD_LLM_URL` |
| `model` | `""` | Local only. Empty = first loaded model. Env: `STALE_GUARD_MODEL` |
| `anthropic_model` | `"haiku"` | Anthropic only. An alias or full model id, as `--model` takes. Env: `STALE_GUARD_ANTHROPIC_MODEL` |
| `anthropic_effort` | `"low"` | Anthropic only. `low`, `medium`, `high`, `xhigh` or `max`; ignored by models without an effort setting. Env: `STALE_GUARD_ANTHROPIC_EFFORT` |
| `max_transcript_chars` | `100000` | ≈25k tokens; keep it under your model's context length. Env: `STALE_GUARD_MAX_CHARS` |
| `max_output_tokens` | `16384` | Reasoning models spend part of this on hidden thinking. Anthropic caps it at 64000 |
| `request_timeout_seconds` | `540` | Must stay below the hooks module's 10-minute limit |
| `handoff_dir` | `~/.claude/handoffs` | |

Both providers use the same summarization rules, in `prompts/handoff-system.md`.

## Tests

```sh
claude plugin test .                    # hooks module (tests/*.test.ts)
python3 -m unittest discover tests      # bin/stale_guard.py
```
