"""Black-box tests for bin/stale_guard.py: run it as the hooks module does, against a
synthetic transcript in a throwaway CLAUDE_CONFIG_DIR.

    python3 -m unittest discover tests
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
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
        self.assertEqual(got["max_transcript_chars"], (100000, "default"))  # not passed at all

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

    def test_check_transcript_limit_too_big_for_haiku_says_why(self):
        self.configure(provider="anthropic", max_transcript_chars=1_000_000, max_output_tokens=20000)
        out = self.run_cmd("check", "--session-id", SESSION)
        self.assertIsNone(out["handoff_model"])
        self.assertIn("won't fit haiku's 200k-token context", out["handoff_unavailable_reason"])
        self.assertIn("set max_transcript_chars to 620000 or less", out["handoff_unavailable_reason"])

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
