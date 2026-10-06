#!/usr/bin/env python3
"""Cold-cache guard engine: transcript inspection, condensing and handoff writing.

Called by the plugin's hooks module (hooks/register.ts), which owns the UI and,
for the Anthropic provider, the model call itself ($.model.complete):

    stale_guard.py check   --session-id ID
        -> {"stale", "idle_minutes", "threshold_minutes", "ttl_minutes", "context_tokens", "transcript",
            "provider", "handoff_model", "handoff_unavailable_reason", "offer_auto_handoff"}
           (the handoff_* and offer_* keys only when stale)
    stale_guard.py prepare --session-id ID --cwd DIR
        -> {"ok": true, "provider", "model", "effort", "max_output_tokens", "timeout_seconds",
            "system", "prompt", "condensed_chars"}
    stale_guard.py complete --model M          ({"system", "prompt"} JSON on stdin; local provider only)
        -> {"ok": true, "text"}
    stale_guard.py save --session-id ID --cwd DIR --provider P --model M   ({"body", "held"} JSON on stdin)
        -> {"ok": true, "path", "text"}

Every command answers {"ok": false, "error"} on failure.

The session transcript (.jsonl) is only ever read; trimming happens in memory on
a condensed copy that is sent to the summarizing model.
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
USER_CONFIG = CLAUDE_DIR / "stale-guard.json"


# ---------------------------------------------------------------- config

def load_config():
    cfg = {}
    for path in (PLUGIN_ROOT / "config.json", USER_CONFIG):
        if path.is_file():
            cfg.update(json.loads(path.read_text()))
    env = {
        "STALE_GUARD_IDLE_MINUTES": "idle_minutes",
        "STALE_GUARD_LLM_URL": "llm_base_url",
        "STALE_GUARD_MODEL": "model",
        "STALE_GUARD_MAX_CHARS": "max_transcript_chars",
        "STALE_GUARD_PROVIDER": "provider",
        "STALE_GUARD_ANTHROPIC_MODEL": "anthropic_model",
        "STALE_GUARD_ANTHROPIC_EFFORT": "anthropic_effort",
    }
    for var, key in env.items():
        if os.environ.get(var):
            cfg[key] = os.environ[var]
    return cfg


def as_bool(value):
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


# ---------------------------------------------------------------- transcript reading

def iter_entries(transcript_path):
    with open(transcript_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def parse_ts(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def tail_entries(transcript_path, chunk=256 * 1024):
    """Yield entries from the end of the file backwards, reading only as much as needed."""
    with open(transcript_path, "rb") as f:
        f.seek(0, os.SEEK_END)
        pos, leftover = f.tell(), b""
        while pos > 0:
            step = min(chunk, pos)
            pos -= step
            f.seek(pos)
            buf = f.read(step) + leftover
            lines = buf.split(b"\n")
            leftover = lines.pop(0) if pos > 0 else b""
            for line in reversed(lines):
                if line.strip():
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue
        if leftover.strip():
            try:
                yield json.loads(leftover)
            except json.JSONDecodeError:
                pass


def session_activity(transcript_path):
    """Return (last_api_activity, ttl_minutes_or_None, context_tokens_or_None).

    The cache is refreshed by every API call, so the last main-thread assistant
    entry is the best proxy for "when the cache was last touched".
    """
    last_ts, ctx, seen = None, None, 0
    for e in tail_entries(transcript_path):
        if e.get("type") != "assistant" or e.get("isSidechain"):
            continue
        usage = (e.get("message") or {}).get("usage") or {}
        if last_ts is None:
            last_ts = parse_ts(e.get("timestamp"))
            if last_ts is None:
                continue
            if usage:
                ctx = (usage.get("input_tokens") or 0) + (usage.get("cache_read_input_tokens") or 0) \
                    + (usage.get("cache_creation_input_tokens") or 0)
        # The TTL shows only on entries that wrote cache; look back a little for one.
        cc = usage.get("cache_creation") or {}
        if cc.get("ephemeral_1h_input_tokens"):
            return last_ts, 60, ctx
        if cc.get("ephemeral_5m_input_tokens"):
            return last_ts, 5, ctx
        seen += 1
        if seen >= 200:
            break
    return last_ts, None, ctx


def find_transcript(session_id):
    matches = sorted((CLAUDE_DIR / "projects").glob(f"*/{session_id}.jsonl"), key=lambda p: p.stat().st_mtime)
    return matches[-1] if matches else None


def threshold_minutes(cfg, detected_ttl):
    setting = cfg.get("idle_minutes", "auto")
    if str(setting).lower() != "auto":
        return float(setting)
    ttl = detected_ttl or float(cfg.get("fallback_ttl_minutes", 60))
    return max(1.0, ttl - float(cfg.get("auto_margin_minutes", 2)))


def fmt_duration(minutes):
    minutes = int(minutes)
    d, rem = divmod(minutes, 1440)
    h, m = divmod(rem, 60)
    parts = [f"{d}d"] if d else []
    if h or d:
        parts.append(f"{h}h")
    if not d:
        parts.append(f"{m}m")
    return " ".join(parts)


# ---------------------------------------------------------------- condensing (in memory only)

REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)


def clip(text, limit):
    text = text.strip()
    if len(text) <= limit:
        return text
    head = int(limit * 0.7)
    tail = limit - head
    return f"{text[:head]} …[{len(text) - limit} chars trimmed]… {text[-tail:]}"


def block_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def summarize_tool_input(name, inp, limit):
    if not isinstance(inp, dict):
        return clip(str(inp), limit)
    for key in ("command", "file_path", "path", "pattern", "url", "query", "description", "prompt"):
        if key in inp and isinstance(inp[key], str):
            extra = ""
            if name in ("Edit", "MultiEdit") and "new_string" in inp:
                extra = f" (new text: {clip(inp['new_string'], limit // 2)})"
            return clip(f"{key}={inp[key]}", limit) + extra
    return clip(json.dumps(inp, ensure_ascii=False), limit)


def collect_chunks(transcript_path, caps):
    """Turn the transcript into a list of text chunks, starting after the last compaction."""
    chunks = []
    tool_names = {}
    for e in iter_entries(transcript_path):
        etype = e.get("type")
        if e.get("isSidechain"):
            continue
        if etype == "system" and e.get("subtype") == "compact_boundary":
            chunks = []  # everything before is represented by the compact summary that follows
            continue
        if etype not in ("user", "assistant") or e.get("isMeta"):
            continue
        content = (e.get("message") or {}).get("content")

        if etype == "user":
            if e.get("isCompactSummary"):
                chunks.append("[EARLIER SUMMARY]\n" + clip(block_text(content), caps["summary"]))
                continue
            if isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_result":
                        name = tool_names.get(b.get("tool_use_id"), "tool")
                        res = block_text(b.get("content")) or str(b.get("content") or "")
                        flag = " (error)" if b.get("is_error") else ""
                        chunks.append(f"← RESULT {name}{flag}: {clip(REMINDER_RE.sub('', res), caps['tool_result'])}")
                    elif b.get("type") == "text":
                        t = REMINDER_RE.sub("", b.get("text", "")).strip()
                        if t:
                            chunks.append("USER: " + clip(t, caps["user"]))
            else:
                t = REMINDER_RE.sub("", content or "").strip()
                if t:
                    chunks.append("USER: " + clip(t, caps["user"]))

        else:  # assistant
            for b in content if isinstance(content, list) else []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and b.get("text", "").strip():
                    chunks.append("ASSISTANT: " + clip(b["text"], caps["assistant"]))
                elif b.get("type") == "tool_use":
                    tool_names[b.get("id")] = b.get("name", "tool")
                    chunks.append(f"→ TOOL {b.get('name')}: {summarize_tool_input(b.get('name'), b.get('input'), caps['tool_input'])}")
                # thinking / images are dropped
    return chunks


def condense(transcript_path, max_chars):
    normal = dict(user=6000, assistant=3000, tool_input=400, tool_result=600, summary=20000)
    tight = dict(user=3000, assistant=1200, tool_input=160, tool_result=160, summary=10000)
    chunks = collect_chunks(transcript_path, normal)
    if sum(len(c) + 1 for c in chunks) > max_chars:
        chunks = collect_chunks(transcript_path, tight)
    if sum(len(c) + 1 for c in chunks) <= max_chars:
        return "\n".join(chunks)

    # Still too big: keep the opening (goal-setting) and as much of the end (current state) as fits.
    head_budget, tail_budget = int(max_chars * 0.15), int(max_chars * 0.85)
    head, used = [], 0
    for c in chunks:
        if used + len(c) > head_budget:
            break
        head.append(c)
        used += len(c) + 1
    tail, used = [], 0
    for c in reversed(chunks[len(head):]):
        if used + len(c) > tail_budget:
            break
        tail.append(c)
        used += len(c) + 1
    tail.reverse()
    omitted = len(chunks) - len(head) - len(tail)
    return "\n".join(head + [f"[… {omitted} entries omitted …]"] + tail)


# ---------------------------------------------------------------- local LLM

def http_json(url, payload=None, timeout=10):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def probe_model(cfg, timeout=1.5):
    """Return (model_id, None) if a usable model is available, else (None, reason).

    Prefers LM Studio's native API, which says which models are loaded; falls back
    to the OpenAI-compatible /v1/models for other servers (llama.cpp, Ollama, ...).
    """
    base = cfg["llm_base_url"].rstrip("/")
    wanted = cfg.get("model") or ""
    try:
        models = http_json(f"{base}/api/v0/models", timeout=timeout).get("data", [])
        llms = [m for m in models if m.get("type") in (None, "llm", "vlm")]
        if wanted:
            if any(m.get("id") == wanted for m in llms):
                return wanted, None
            return None, f"configured model {wanted} is not available in LM Studio"
        loaded = [m["id"] for m in llms if m.get("state") == "loaded"]
        return (loaded[0], None) if loaded else (None, "LM Studio is running but no model is loaded")
    except urllib.error.HTTPError:
        pass  # not LM Studio; try the generic endpoint
    except (urllib.error.URLError, TimeoutError, OSError):
        return None, f"no local LLM server at {base}"
    except ValueError:
        pass
    try:
        ids = [m.get("id") for m in http_json(f"{base}/v1/models", timeout=timeout).get("data", [])]
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return None, f"no local LLM server at {base} ({exc})"
    if wanted:
        return (wanted, None) if wanted in ids else (None, f"configured model {wanted} is not served at {base}")
    return (ids[0], None) if ids else (None, f"the server at {base} has no models")


def run_local_llm(cfg, model, system_prompt, user_content):
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_content}],
        "temperature": float(cfg.get("temperature", 0.2)),
        "max_tokens": int(cfg.get("max_output_tokens", 4096)),
        "stream": False,
    }
    resp = http_json(f"{cfg['llm_base_url'].rstrip('/')}/v1/chat/completions", body,
                     timeout=float(cfg.get("request_timeout_seconds", 840)))
    choice = resp["choices"][0]
    text = choice["message"].get("content") or ""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()  # reasoning models
    if not text:
        reasoning = len(choice["message"].get("reasoning_content") or "")
        raise RuntimeError(
            f"{model} returned no answer (finish_reason={choice.get('finish_reason')}, "
            f"{reasoning} chars of reasoning). If finish_reason is 'length', raise max_output_tokens."
        )
    return text


# ---------------------------------------------------------------- providers

PROVIDERS = ("local", "anthropic")
EFFORTS = ("low", "medium", "high", "xhigh", "max")


def provider_of(cfg):
    return str(cfg.get("provider", "anthropic")).strip().lower()


def handoff_backend(cfg, timeout=1.5):
    """Return (provider, model, None) when a handoff can be generated, else (provider, None, reason).

    The local provider is probed; the Anthropic one goes through the session's own
    API client, so it is always available (the engine rejects a model it won't send).
    """
    provider = provider_of(cfg)
    if provider == "anthropic":
        effort = str(cfg.get("anthropic_effort", "low")).lower()
        if effort not in EFFORTS:
            return provider, None, f"anthropic_effort must be one of {', '.join(EFFORTS)}, not {effort!r}"
        return provider, cfg.get("anthropic_model") or "haiku", None
    if provider == "local":
        model, reason = probe_model(cfg, timeout=timeout)
        return provider, model, reason
    return provider, None, f"provider must be one of {', '.join(PROVIDERS)}, not {provider!r}"


def summarizer_label(provider, model):
    return f"Claude ({model})" if provider == "anthropic" else f"a local model ({model})"


# ---------------------------------------------------------------- commands

def cmd_check(cfg, args):
    transcript = find_transcript(args.session_id)
    out = {"stale": False, "idle_minutes": None, "threshold_minutes": None, "ttl_minutes": None,
           "context_tokens": None, "transcript": str(transcript) if transcript else None}
    if not transcript:
        return out
    last_ts, ttl, ctx = session_activity(transcript)
    if not last_ts:
        return out
    idle = (datetime.now(timezone.utc) - last_ts).total_seconds() / 60
    limit = threshold_minutes(cfg, ttl)
    out.update(stale=idle >= limit, idle_minutes=round(idle, 1), idle_text=fmt_duration(idle),
               threshold_minutes=limit, ttl_minutes=ttl or float(cfg.get("fallback_ttl_minutes", 60)),
               context_tokens=ctx)
    if out["stale"]:  # only probe when the answer will be shown
        out["provider"], out["handoff_model"], out["handoff_unavailable_reason"] = handoff_backend(cfg)
        out["offer_auto_handoff"] = as_bool(cfg.get("offer_auto_handoff", False))
    return out


def read_stdin_json():
    return json.loads(sys.stdin.read() or "{}")


def cmd_prepare(cfg, args):
    transcript = find_transcript(args.session_id)
    if not transcript:
        return {"ok": False, "error": f"no transcript found for session {args.session_id}"}
    provider, model, reason = handoff_backend(cfg, timeout=10)
    if not model:
        return {"ok": False, "error": reason}

    last_ts, _, _ = session_activity(transcript)
    idle_note = fmt_duration((datetime.now(timezone.utc) - last_ts).total_seconds() / 60) + " ago" if last_ts else "unknown"
    condensed = condense(transcript, int(cfg.get("max_transcript_chars", 100000)))
    return {
        "ok": True,
        "provider": provider,
        "model": model,
        "effort": str(cfg.get("anthropic_effort", "low")).lower(),
        "max_output_tokens": int(cfg.get("max_output_tokens", 4096)),
        "timeout_seconds": float(cfg.get("request_timeout_seconds", 540)),
        "system": (PLUGIN_ROOT / "prompts" / "handoff-system.md").read_text(),
        "prompt": (
            f"Project directory: {args.cwd}\n"
            f"Last activity in session: {idle_note}\n\n"
            f"<transcript>\n{condensed}\n</transcript>\n\n"
            "Write the handoff document now, following the required format exactly."
        ),
        "condensed_chars": len(condensed),
    }


def cmd_complete(cfg, args):
    req = read_stdin_json()
    try:
        text = run_local_llm(cfg, args.model, req["system"], req["prompt"])
    except (urllib.error.URLError, TimeoutError, OSError, RuntimeError, KeyError, ValueError) as exc:
        return {"ok": False, "error": f"local model at {cfg['llm_base_url']}: {exc}"}
    return {"ok": True, "text": text}


def cmd_save(cfg, args):
    req = read_stdin_json()
    body, held = (req.get("body") or "").strip(), (req.get("held") or "").strip()
    if not body:
        return {"ok": False, "error": "the model's handoff was empty"}
    transcript = find_transcript(args.session_id)
    handoff = (
        "# Handoff from a previous Claude Code session\n\n"
        f"You are continuing work from an earlier Claude Code session in `{args.cwd}` that was summarized by "
        f"{summarizer_label(args.provider, args.model)} to avoid re-reading its full history. Treat this as your "
        "working context, but verify file and repo state before relying on specifics.\n\n"
        + (f"If you need an exact detail, the full original transcript is at `{transcript}` "
           "(JSONL, read-only — search it rather than reading it whole).\n\n" if transcript else "")
        + body + "\n"
    )
    if held:
        handoff += f"\n## The user's next message\n\n{held}\n"

    out_dir = Path(os.path.expanduser(cfg.get("handoff_dir", "~/.claude/handoffs")))
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{datetime.now():%Y%m%d-%H%M%S}-{args.session_id[:8]}.md"
    out_file.write_text(handoff)
    return {"ok": True, "path": str(out_file), "text": handoff}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check")
    check.add_argument("--session-id", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--session-id", required=True)
    prepare.add_argument("--cwd", default=os.getcwd())
    complete = sub.add_parser("complete")
    complete.add_argument("--model", required=True)
    save = sub.add_parser("save")
    save.add_argument("--session-id", required=True)
    save.add_argument("--cwd", default=os.getcwd())
    save.add_argument("--provider", required=True, choices=PROVIDERS)
    save.add_argument("--model", required=True)
    args = parser.parse_args()

    commands = {"check": cmd_check, "prepare": cmd_prepare, "complete": cmd_complete, "save": cmd_save}
    cfg = load_config()
    try:
        result = commands[args.command](cfg, args)
    except Exception as exc:  # report, never traceback, so the caller can show it
        result = {"ok": False, "stale": False, "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
