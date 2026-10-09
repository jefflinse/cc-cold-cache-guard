# cc-cold-cache-guard

A Claude Code plugin that catches prompts sent to a session whose prompt cache has likely expired, so you don't pay for a full cache rewrite by accident. Instead, it offers a cheap handoff summarized by a small Claude model (a fraction of the re-cache cost) or a local model (free).

## What It Does

When you send a message to a cold session, the message is held and Claude Code's own multiple-choice dialog asks what to do (no model call is made to ask):

- **Generate a handoff prompt**: a condensed copy of the transcript is summarized by the configured provider (see below). Afterwards, you can start a fresh session with the handoff (`/clear` plus an `@`-mention of the file), copy it to the clipboard, or just keep the file. Your held message is appended to the handoff.
- **Generate a handoff and continue right away** (off by default; turn on with `auto_continue_choice`): the same, but it doesn't ask what to do next. It runs `/clear` and sends the handoff, including your held message, as the first message of the fresh session.
- **Send anyway**: the message goes through untouched and pays for the cache write.
- **Don't send** (or Esc): your message goes back to the input box.

The guard stays out of the way when Claude Code's own idle compaction ran after the last message ("Compacted while idle, before the prompt cache expired"). The session is already down to its summary, so re-caching it is cheap.

It also adds a `/handoff` command for generating a handoff any time, a `/cold-cache-guard-configure` command that lists the current settings and opens the plugin's settings screen, and shows a toast when you `--resume` a cold session.

A handoff has four parts:

1. **Original request**, quoted verbatim: your first message in the session. A session started from a handoff carries that handoff's original request forward.
2. **The summary**: goal, decisions, current state, files, open questions and next steps.
3. **Last exchanges**, quoted verbatim: your last three messages and Claude's final reply to each.
4. **Your held message**, if there is one.

Quoted messages are text only, without tool calls or the slash and `!` commands you ran, and each is capped at 4,000 characters. The summarizer still sees the whole condensed transcript.

The transcript is only ever read; condensing happens on an in-memory copy. Handoffs are saved to `~/.claude/handoffs/`.

## Providers

- **`anthropic`** (default): Claude, called in-process through the session's own API client and credentials (`$.model.complete`). Nothing is spawned and no extra session is recorded. Defaults to `haiku`; Haiku 4.5 has no effort setting, so `anthropic_effort` only matters if you switch to a model that does (e.g. `sonnet`). A handoff takes about 30 seconds with Haiku. The request is uncached, but it only carries the condensed transcript (up to ≈177k tokens at the default `max_transcript_chars`), not the full conversation.
- **`local`**: LM Studio, or any OpenAI-compatible server, for free handoffs. The handoff options only appear when a model is actually loaded. Load it with a long context (200k tokens fits the default `max_transcript_chars`). A long session can take minutes to read locally, and `request_timeout_seconds` (540) is the limit.

## Install

Requires `python3`.

```sh
claude plugin install cc-cold-cache-guard \
  --marketplace https://github.com/jefflinse/cc-cold-cache-guard.git
```

Then restart Claude Code, or run `/reload-plugins` in an open session. The install notes that the options aren't set; each one falls back to its default, and the settings screen names the defaults. Run `/cold-cache-guard-configure` to see the current values and change them, or set some at install time with `--config`, e.g. `--config provider=local`.

To update:

```sh
claude plugin marketplace update cc-cold-cache-guard
claude plugin update cc-cold-cache-guard@cc-cold-cache-guard
```

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

Every setting can be changed on the plugin's settings screen (`/plugin configure cc-cold-cache-guard@cc-cold-cache-guard`) or in `/config`, and the plugin asks for them when you install it. `/cold-cache-guard-configure` lists the current values, says where each came from, and opens the settings screen.

Settings are layered, each layer winning over the ones before it:

1. `config.json`: the defaults.
2. The settings screen (`/plugin configure` or `/config`): stored in `settings.json` under `pluginConfigs`.
3. `STALE_GUARD_*` env vars, where listed below.

| Key | Default | Notes |
|---|---|---|
| `idle_minutes` | `"auto"` | `auto` = cache TTL detected from the transcript (1h or 5m) minus `auto_margin_minutes`. Env: `STALE_GUARD_IDLE_MINUTES` |
| `provider` | `"anthropic"` | `anthropic` or `local`. Env: `STALE_GUARD_PROVIDER` |
| `auto_margin_minutes` | `2` | With `idle_minutes` set to `auto`: how many minutes before the cache TTL to start holding prompts |
| `fallback_ttl_minutes` | `60` | Cache TTL assumed when the transcript doesn't show one |
| `auto_continue_choice` | `false` | Adds the "generate and continue right away" option to the dialog |
| `llm_base_url` | `http://localhost:1234` | Local only. Env: `STALE_GUARD_LLM_URL` |
| `model` | `""` | Local only. Empty = first loaded model. Env: `STALE_GUARD_MODEL` |
| `temperature` | `0.2` | Local only |
| `local_thinking` | `false` | Local only. Off asks the server to skip a reasoning model's thinking (`reasoning_effort: "none"`, which LM Studio honors, plus `chat_template_kwargs.enable_thinking: false` for llama.cpp and vLLM), and retries without them if the server rejects them. On sends neither, so the server's own setting applies |
| `anthropic_model` | `"haiku"` | Anthropic only. An alias or full model id, as `--model` takes. Env: `STALE_GUARD_ANTHROPIC_MODEL` |
| `anthropic_effort` | `"low"` | Anthropic only. `low`, `medium`, `high`, `xhigh` or `max`; ignored by models without an effort setting, including Haiku 4.5. Env: `STALE_GUARD_ANTHROPIC_EFFORT` |
| `max_transcript_chars` | `620000` | ≈177k tokens, sized for a 200k-token context with room for the summarizer's instructions and `max_output_tokens`. Checked against the model: Haiku's 200k, a local model's loaded context as LM Studio reports it, or 200k when the server doesn't say. A value that won't fit makes the handoff unavailable and says what would. The 1M-token Claude models aren't checked. Env: `STALE_GUARD_MAX_CHARS` |
| `max_output_tokens` | `16384` | Reasoning models spend part of this on hidden thinking. Anthropic caps it at 64000 |
| `request_timeout_seconds` | `540` | Must stay below the hooks module's 10-minute limit |
| `handoff_dir` | `~/.claude/handoffs` | |

Both providers use the same summarization rules, in `prompts/handoff-system.md`.

## Tests

```sh
claude plugin test .                    # hooks module (tests/*.test.ts)
python3 -m unittest discover tests      # bin/stale_guard.py
```
