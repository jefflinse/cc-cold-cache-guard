# local-compaction-and-handoff

A Claude Code plugin that stops you from accidentally paying for a full prompt-cache rewrite when you come back to a stale session, and offers a free local-LLM handoff instead.

## What it does

- **Cold prompt**: when you send a message to a session whose last API call is older than its prompt-cache TTL, the message is held and Claude Code's own multiple-choice dialog asks what to do (no model call is made to ask):
  - **Generate a handoff prompt using <model>** (offered only when a local model is loaded): the transcript is condensed in memory and summarized by your local model. Progress shows in the status line. When done, a second dialog asks whether to start a fresh session here with it (runs `/clear` and pre-fills `@<handoff file>`; the old session stays resumable), copy it to the clipboard, or just keep the file. Your held message is appended to the handoff.
  - **Send anyway**: the original message goes through untouched (attachments included) and pays for the cache write.
  - **Don't send** (or Esc): nothing is sent and your message is put back in the input box.
- **Resume**: `--resume` on a cold session shows a toast up front.
- **`/handoff`**: generate a handoff any time.

The transcript `.jsonl` is only ever read. Trimming (dropping thinking, clipping tool output, eliding the middle if needed) happens on an in-memory copy.

Handoffs are saved to `~/.claude/handoffs/` (configurable).

## Layout

- `hooks/register.ts`: the Claude Code hooks module (UI, dialog, status line, clipboard)
- `bin/stale_guard.py`: `check` and `handoff` subcommands (transcript reading, condensing, LM Studio call); stdlib Python only
- `prompts/handoff-system.md`: the local model's summarization rules

## Try it

```sh
claude --plugin-dir ~/dev/local-compaction-and-handoff
# force the guard to trip after a minute of idleness:
STALE_GUARD_IDLE_MINUTES=1 claude --plugin-dir ~/dev/local-compaction-and-handoff
```

Requires `python3` and LM Studio's server running (`lms server start`) with a model loaded.

## Config

Defaults are in `config.json`; override in `~/.claude/stale-guard.json` or via env vars.

| Key | Default | Notes |
|---|---|---|
| `idle_minutes` | `"auto"` | `auto` = TTL detected from the transcript's cache usage (1h or 5m) minus `auto_margin_minutes`. Env: `STALE_GUARD_IDLE_MINUTES` |
| `llm_base_url` | `http://localhost:1234` | Env: `STALE_GUARD_LLM_URL` |
| `model` | `""` | Empty = first model LM Studio reports as loaded. Env: `STALE_GUARD_MODEL` |
| `max_transcript_chars` | `100000` | ≈25k tokens. Keep it under your loaded model's context length minus prompt + output. Env: `STALE_GUARD_MAX_CHARS` |
| `max_output_tokens` | `16384` | Reasoning models (e.g. Gemma 4) spend part of this on hidden thinking |
| `request_timeout_seconds` | `540` | Must stay below the hooks module's 10-minute process limit |
| `handoff_dir` | `~/.claude/handoffs` | |

The summarization rules are in `prompts/handoff-system.md`.
