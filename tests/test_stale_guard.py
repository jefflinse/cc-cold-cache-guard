"""Black-box tests for bin/stale_guard.py: run it as the hooks module does, against a
synthetic transcript in a throwaway CLAUDE_CONFIG_DIR.

    python3 -m unittest discover tests
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "bin" / "stale_guard.py"
SESSION = "0f1e2d3c-aaaa-bbbb-cccc-000000000001"
NO_SERVER = "http://127.0.0.1:9"  # discard port: nothing listens


def transcript_lines(idle_minutes):
    ts = (datetime.now(timezone.utc) - timedelta(minutes=idle_minutes)).isoformat().replace("+00:00", "Z")
    return [
        {"type": "user", "timestamp": ts, "message": {"role": "user", "content": "Please add a --verbose flag to the CLI."}},
        {"type": "assistant", "timestamp": ts, "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "Added --verbose in cli.py."}],
            "usage": {"input_tokens": 10, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 5000,
                      "cache_creation": {"ephemeral_1h_input_tokens": 5000}},
        }},
    ]


def user(text, **extra):
    return {"type": "user", "message": {"role": "user", "content": text}, **extra}


def assistant(*blocks):
    return {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}}


def text(t):
    return {"type": "text", "text": t}


def fake_lm_studio(models, strict=False):
    """An LM Studio stand-in: /api/v0/models, and /v1/chat/completions recording each request in
    `server.requests`. `strict` rejects fields it doesn't know with a 400, as some servers do.
    Returns (base_url, server)."""
    known = {"model", "messages", "temperature", "max_tokens", "stream"}

    class Handler(BaseHTTPRequestHandler):
        def reply(self, code, payload):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.reply(200, {"data": models})

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            server.requests.append(request)
            if strict and set(request) - known:
                return self.reply(400, {"error": f"unknown fields: {sorted(set(request) - known)}"})
            self.reply(200, {"choices": [{"message": {"content": "## Goal\nShip it."}, "finish_reason": "stop"}]})

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.requests = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}", server


class StaleGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.claude_dir = Path(self.tmp.name)
        self.handoff_dir = self.claude_dir / "handoffs"
        self.write_transcript(idle_minutes=120)
        self.configure()

    def write_transcript(self, idle_minutes):
        project = self.claude_dir / "projects" / "-repo"
        project.mkdir(parents=True, exist_ok=True)
        self.transcript = project / f"{SESSION}.jsonl"
        self.transcript.write_text("\n".join(json.dumps(e) for e in transcript_lines(idle_minutes)) + "\n")

    def configure(self, **overrides):
        """The plugin's settings, as the hooks module passes them."""
        self.options = {"llm_base_url": NO_SERVER, "handoff_dir": str(self.handoff_dir), **overrides}

    def run_cmd(self, *args, stdin="", env=None):
        full_env = {k: v for k, v in os.environ.items() if not k.startswith("STALE_GUARD_")}
        full_env.update(CLAUDE_CONFIG_DIR=str(self.claude_dir), STALE_GUARD_PLUGIN_OPTIONS=json.dumps(self.options),
                        **(env or {}))
        proc = subprocess.run([sys.executable, str(SCRIPT), *args], input=stdin, capture_output=True,
                              text=True, env=full_env, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    # ------------------------------------------------------------ check

    def test_check_warm_session_reports_no_backend(self):
        self.write_transcript(idle_minutes=1)
        out = self.run_cmd("check", "--session-id", SESSION)
        self.assertFalse(out["stale"])
        self.assertNotIn("provider", out)

    def test_check_defaults_to_anthropic(self):
        out = self.run_cmd("check", "--session-id", SESSION)
        self.assertEqual((out["provider"], out["handoff_model"]), ("anthropic", "haiku"))

    def test_check_local_without_server_says_why(self):
        self.configure(provider="local")
        out = self.run_cmd("check", "--session-id", SESSION)
        self.assertTrue(out["stale"])
        self.assertEqual(out["provider"], "local")
        self.assertIsNone(out["handoff_model"])
        self.assertIn("no local LLM server", out["handoff_unavailable_reason"])
        self.assertFalse(out["auto_continue_choice"])

    def test_check_anthropic_is_always_available(self):
        self.configure(provider="anthropic", auto_continue_choice=True)
        out = self.run_cmd("check", "--session-id", SESSION)
        self.assertEqual((out["provider"], out["handoff_model"], out["handoff_unavailable_reason"]),
                         ("anthropic", "haiku", None))
        self.assertTrue(out["auto_continue_choice"])

    def test_check_env_overrides_provider_and_model(self):
        out = self.run_cmd("check", "--session-id", SESSION,
                           env={"STALE_GUARD_PROVIDER": "anthropic", "STALE_GUARD_ANTHROPIC_MODEL": "sonnet"})
        self.assertEqual((out["provider"], out["handoff_model"]), ("anthropic", "sonnet"))

    def test_check_rejects_unknown_effort(self):
        self.configure(provider="anthropic", anthropic_effort="extreme")
        out = self.run_cmd("check", "--session-id", SESSION)
        self.assertIsNone(out["handoff_model"])
        self.assertIn("anthropic_effort must be one of", out["handoff_unavailable_reason"])

    # ------------------------------------------------------------ config layers

    def settings(self, env=None):
        out = self.run_cmd("config", env=env)
        self.assertTrue(out["ok"], out)
        return {s["key"]: (s["value"], s["source"]) for s in out["settings"]}

    def test_config_options_override_defaults_and_env_overrides_options(self):
        self.configure(provider="local", anthropic_model="sonnet", anthropic_effort="low")
        got = self.settings(env={"STALE_GUARD_ANTHROPIC_MODEL": "opus"})
        self.assertEqual(got["provider"], ("local", "/config"))
        self.assertEqual(got["anthropic_model"], ("opus", "STALE_GUARD_ANTHROPIC_MODEL"))
        self.assertEqual(got["anthropic_effort"], ("low", "default"))  # set, but to the default
        self.assertEqual(got["max_transcript_chars"], (620000, "default"))  # not passed at all

    def test_config_local_url_without_scheme_gets_http(self):
        self.configure(llm_base_url="localhost:1234")
        self.assertEqual(self.settings()["llm_base_url"], ("http://localhost:1234", "/config"))

    def test_config_options_reach_check(self):
        self.configure(anthropic_model="sonnet", auto_continue_choice=True)
        out = self.run_cmd("check", "--session-id", SESSION)
        self.assertEqual(out["handoff_model"], "sonnet")
        self.assertTrue(out["auto_continue_choice"])

    def test_manifest_userconfig_matches_config_json(self):
        manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
        defaults = json.loads((ROOT / "config.json").read_text())
        self.assertEqual({k: f["default"] for k, f in manifest["userConfig"].items()}, defaults)

    def test_manifest_descriptions_state_the_defaults(self):
        # The settings screen shows an unsaved field as empty, so the help text names its default.
        manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
        for key, field in manifest["userConfig"].items():
            default = field["default"]
            if isinstance(default, bool):
                shown = "on" if default else "off"
            else:
                shown = str(default) or "empty"
            with self.subTest(key=key):
                self.assertIn(f"Default: {shown}", field["description"])

    def test_check_transcript_limit_too_big_for_haiku_says_why(self):
        self.configure(provider="anthropic", max_transcript_chars=1_000_000, max_output_tokens=20000)
        out = self.run_cmd("check", "--session-id", SESSION)
        self.assertIsNone(out["handoff_model"])
        self.assertIn("won't fit haiku's 200k-token context", out["handoff_unavailable_reason"])
        self.assertIn("set max_transcript_chars to 620000 or less", out["handoff_unavailable_reason"])

    def lm_studio(self, strict=False, **model):
        url, server = fake_lm_studio([{"id": "gemma", "type": "llm", "state": "loaded", **model}], strict=strict)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)  # cleanups run last-in first-out
        self.server = server
        return url

    def test_check_local_uses_the_loaded_context_length(self):
        self.configure(provider="local", llm_base_url=self.lm_studio(loaded_context_length=32768))
        out = self.run_cmd("check", "--session-id", SESSION)
        self.assertIsNone(out["handoff_model"])
        self.assertIn("won't fit gemma's 33k-token context", out["handoff_unavailable_reason"])
        self.assertIn("or load the model with a longer context", out["handoff_unavailable_reason"])

    def test_check_local_default_fits_a_200k_context(self):
        self.configure(provider="local", llm_base_url=self.lm_studio(loaded_context_length=226304))
        self.assertEqual(self.run_cmd("check", "--session-id", SESSION)["handoff_model"], "gemma")

    def test_check_local_assumes_200k_when_the_server_doesnt_say(self):
        self.configure(provider="local", llm_base_url=self.lm_studio(), max_transcript_chars=800_000)
        out = self.run_cmd("check", "--session-id", SESSION)
        self.assertIn("won't fit gemma's 200k-token context", out["handoff_unavailable_reason"])

    def test_check_big_transcript_limit_is_fine_for_a_1m_model(self):
        self.configure(provider="anthropic", anthropic_model="sonnet", max_transcript_chars=1_000_000)
        out = self.run_cmd("check", "--session-id", SESSION)
        self.assertEqual(out["handoff_model"], "sonnet")

    def test_prepare_refuses_a_transcript_limit_too_big_for_haiku(self):
        self.configure(provider="anthropic", anthropic_model="claude-haiku-4-5", max_transcript_chars=1_000_000)
        out = self.run_cmd("prepare", "--session-id", SESSION, "--cwd", "/repo")
        self.assertFalse(out["ok"])
        self.assertIn("won't fit claude-haiku-4-5's", out["error"])

    # ------------------------------------------------------------ prepare

    def test_prepare_anthropic_returns_prompt_and_settings(self):
        self.configure(provider="anthropic", anthropic_model="claude-haiku-4-5", anthropic_effort="Medium")
        out = self.run_cmd("prepare", "--session-id", SESSION, "--cwd", "/repo")
        self.assertTrue(out["ok"], out)
        self.assertEqual((out["provider"], out["model"], out["effort"]), ("anthropic", "claude-haiku-4-5", "medium"))
        self.assertEqual(out["system"], (ROOT / "prompts" / "handoff-system.md").read_text())
        self.assertIn("Project directory: /repo", out["prompt"])
        self.assertIn("USER: Please add a --verbose flag", out["prompt"])
        self.assertIn("ASSISTANT: Added --verbose in cli.py.", out["prompt"])

    def test_prepare_unknown_provider_fails(self):
        self.configure(provider="openai")
        out = self.run_cmd("prepare", "--session-id", SESSION, "--cwd", "/repo")
        self.assertEqual(out, {"ok": False, "error": "provider must be one of local, anthropic, not 'openai'"})

    def test_prepare_local_without_server_fails(self):
        self.configure(provider="local")
        out = self.run_cmd("prepare", "--session-id", SESSION, "--cwd", "/repo")
        self.assertFalse(out["ok"])
        self.assertIn("no local LLM server", out["error"])

    def test_prepare_missing_transcript_fails(self):
        out = self.run_cmd("prepare", "--session-id", "nope", "--cwd", "/repo")
        self.assertFalse(out["ok"])

    # ------------------------------------------------------------ complete (local)

    def test_complete_without_server_fails(self):
        out = self.run_cmd("complete", "--model", "m", stdin=json.dumps({"system": "s", "prompt": "p"}))
        self.assertFalse(out["ok"])
        self.assertIn(NO_SERVER, out["error"])

    def complete(self):
        out = self.run_cmd("complete", "--model", "gemma", stdin=json.dumps({"system": "s", "prompt": "p"}))
        self.assertEqual(out, {"ok": True, "text": "## Goal\nShip it."})
        return self.server.requests

    def test_complete_asks_the_local_model_not_to_think(self):
        self.configure(provider="local", llm_base_url=self.lm_studio())
        [request] = self.complete()
        self.assertEqual(request["reasoning_effort"], "none")
        self.assertEqual(request["chat_template_kwargs"], {"enable_thinking": False})

    def test_complete_retries_without_the_switches_when_a_server_rejects_them(self):
        self.configure(provider="local", llm_base_url=self.lm_studio(strict=True))
        first, second = self.complete()
        self.assertIn("reasoning_effort", first)
        self.assertNotIn("reasoning_effort", second)
        self.assertNotIn("chat_template_kwargs", second)

    def test_complete_with_local_thinking_on_leaves_it_to_the_server(self):
        self.configure(provider="local", llm_base_url=self.lm_studio(), local_thinking=True)
        [request] = self.complete()
        self.assertNotIn("reasoning_effort", request)
        self.assertNotIn("chat_template_kwargs", request)

    # ------------------------------------------------------------ save

    def test_save_writes_handoff_with_summarizer_and_held_message(self):
        out = self.run_cmd("save", "--session-id", SESSION, "--cwd", "/repo", "--provider", "anthropic",
                           "--model", "haiku", stdin=json.dumps({"body": "## Goal\nShip it.\n", "held": "now run the tests"}))
        self.assertTrue(out["ok"], out)
        path = Path(out["path"])
        self.assertEqual(path.parent, self.handoff_dir)
        self.assertEqual(path.read_text(), out["text"])
        self.assertIn("summarized by Claude (haiku)", out["text"])
        self.assertIn(str(self.transcript), out["text"])
        self.assertTrue(out["text"].endswith("## Goal\nShip it.\n\n## The user's next message\n\nnow run the tests\n"))

    def write_entries(self, entries):
        self.transcript.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

    def save(self, held=""):
        out = self.run_cmd("save", "--session-id", SESSION, "--cwd", "/repo", "--provider", "anthropic",
                           "--model", "haiku", stdin=json.dumps({"body": "## Goal\nShip it.", "held": held}))
        self.assertTrue(out["ok"], out)
        return out["text"]

    def test_save_quotes_the_original_request_and_last_three_exchanges(self):
        self.write_entries([
            user("Build a CLI.\n\n## Not a heading"),
            assistant(text("Let me look."), {"type": "tool_use", "id": "t1", "name": "Read", "input": {}}),
            user([{"type": "tool_result", "tool_use_id": "t1", "content": "file contents"}]),
            assistant(text("Built it.")),
            user("second ask"), assistant(text("second reply")),
            user("<command-name>/reload-plugins</command-name>"), assistant(text("reply to a command")),
            user("<local-command-caveat>…</local-command-caveat>", isMeta=True),
            user("third ask"), assistant(text("third reply")),
            user("<bash-input>git push</bash-input>"),
            user("fourth ask"), assistant(text("fourth reply")),
            user("fifth ask <system-reminder>hidden</system-reminder>"), assistant(text("fifth reply")),
        ])
        handoff = self.save(held="next")
        self.assertIn("The original request and the last exchanges are quoted verbatim", handoff)
        original, rest = handoff.split("## Original request (verbatim)\n\n")[1].split("## Goal")
        self.assertEqual(original, "> Build a CLI.\n>\n> ## Not a heading\n\n")
        last = rest.split("## Last exchanges (verbatim)\n\n")[1]
        self.assertEqual(last, (
            "**User:**\n\n> third ask\n\n**Assistant:**\n\n> third reply\n\n---\n\n"
            "**User:**\n\n> fourth ask\n\n**Assistant:**\n\n> fourth reply\n\n---\n\n"
            "**User:**\n\n> fifth ask\n\n**Assistant:**\n\n> fifth reply\n\n"
            "## The user's next message\n\nnext\n"))
        for absent in ("second ask", "reply to a command", "git push", "hidden", "Let me look."):
            self.assertNotIn(absent, handoff)

    def test_save_clips_long_messages(self):
        self.write_entries([user("x" * 10_000), assistant(text("ok"))])
        handoff = self.save()
        self.assertIn("…[6000 chars trimmed]…", handoff)
        self.assertNotIn("## Last exchanges", handoff)  # the only exchange is the original request

    def test_save_carries_the_original_request_through_a_chain_of_handoffs(self):
        previous = ("# Handoff from a previous Claude Code session\n\nIntro.\n\n"
                    "## Original request (verbatim)\n\n> Build a CLI.\n\n## Goal\nOld goal.\n")
        self.write_entries([user(previous), assistant(text("Got it.")), user("now add tests"), assistant(text("Added."))])
        handoff = self.save()
        self.assertEqual(handoff.count("## Original request (verbatim)\n\n> Build a CLI.\n\n## Goal\nShip it."), 1)
        self.assertNotIn("Old goal", handoff)
        self.assertIn("**User:**\n\n> now add tests\n\n**Assistant:**\n\n> Added.", handoff)

    def test_save_local_names_the_local_model(self):
        out = self.run_cmd("save", "--session-id", SESSION, "--cwd", "/repo", "--provider", "local",
                           "--model", "gemma", stdin=json.dumps({"body": "## Goal\nx", "held": ""}))
        self.assertIn("summarized by a local model (gemma)", out["text"])
        self.assertNotIn("The user's next message", out["text"])

    def test_save_rejects_empty_body(self):
        out = self.run_cmd("save", "--session-id", SESSION, "--cwd", "/repo", "--provider", "local",
                           "--model", "m", stdin=json.dumps({"body": "  "}))
        self.assertEqual(out, {"ok": False, "error": "the model's handoff was empty"})
        self.assertFalse(self.handoff_dir.exists())


if __name__ == "__main__":
    unittest.main()
