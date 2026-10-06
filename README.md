# cc-cold-cache-guard

A Claude Code plugin that catches prompts sent to a session whose prompt cache has likely expired, so you don't pay for a full cache rewrite by accident. Instead, it offers a free handoff summarized by a local model.

## What It Does

When you send a message to a cold session, the message is held and Claude Code's own multiple-choice dialog asks what to do (no model call is made to ask):

- **Generate a handoff with a local model**: offered only when a model is actually loaded in LM Studio (or any OpenAI-compatible server). Afterwards, you can start a fresh session with the handoff (`/clear` plus an `@`-mention of the file), copy it to the clipboard, or just keep the file. Your held message is appended to the handoff.
- **Send anyway**: the message goes through untouched and pays for the cache write.
- **Don't send** (or Esc): your message goes back to the input box.

It also adds a `/handoff` command for generating a handoff any time, and shows a toast when you `--resume` a cold session.

The transcript is only ever read; condensing happens on an in-memory copy. Handoffs are saved to `~/.claude/handoffs/`.

## Try It

Requires `python3` and LM Studio's server running (`lms server start`) with a model loaded.

```sh
git clone https://github.com/jefflinse/cc-cold-cache-guard.git
claude --plugin-dir ./cc-cold-cache-guard

# force the guard to trip after a minute of idleness:
STALE_GUARD_IDLE_MINUTES=1 claude --plugin-dir ./cc-cold-cache-guard
```

## Config

Defaults are in `config.json`; override them in `~/.claude/stale-guard.json` or via env vars.

| Key | Default | Notes |
|---|---|---|
| `idle_minutes` | `"auto"` | `auto` = cache TTL detected from the transcript (1h or 5m) minus `auto_margin_minutes`. Env: `STALE_GUARD_IDLE_MINUTES` |
| `llm_base_url` | `http://localhost:1234` | Env: `STALE_GUARD_LLM_URL` |
| `model` | `""` | Empty = first loaded model. Env: `STALE_GUARD_MODEL` |
| `max_transcript_chars` | `100000` | ≈25k tokens; keep it under your model's context length. Env: `STALE_GUARD_MAX_CHARS` |
| `max_output_tokens` | `16384` | Reasoning models spend part of this on hidden thinking |
| `request_timeout_seconds` | `540` | Must stay below the hooks module's 10-minute limit |
| `handoff_dir` | `~/.claude/handoffs` | |

The local model's summarization rules are in `prompts/handoff-system.md`.
