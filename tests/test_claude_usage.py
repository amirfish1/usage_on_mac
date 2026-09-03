import json
import importlib.util
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "claude-usage.5m.py"


def load_plugin():
    spec = importlib.util.spec_from_file_location("claude_usage", PLUGIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _UsageHandler(BaseHTTPRequestHandler):
    payload = {}

    def do_GET(self):
        body = json.dumps(self.payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


class ClaudeUsageOutputTest(unittest.TestCase):
    def test_ccc_without_claude_weekly_is_rejected(self):
        _UsageHandler.payload = {
            "ok": True,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "claude": {
                "five_hour": {"pct": None, "resets_at": None},
                "seven_day": {"pct": None, "resets_at": None},
            },
            "codex": {},
        }
        server = ThreadingHTTPServer(("127.0.0.1", 0), _UsageHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            plugin = load_plugin()
            plugin.CCC_USAGE_URL = f"http://127.0.0.1:{server.server_port}/api/usage/current"
            self.assertIsNone(plugin.fetch_from_ccc())
        finally:
            server.shutdown()
            server.server_close()

    def test_ccc_uses_expanded_dropdown(self):
        now = datetime.now(timezone.utc)
        reset = now + timedelta(days=5)
        _UsageHandler.payload = {
            "ok": True,
            "fetched_at": now.isoformat(),
            "claude": {
                "five_hour": {"pct": 11.0, "resets_at": (now + timedelta(hours=4)).isoformat()},
                "seven_day": {"pct": 19.0, "resets_at": reset.isoformat()},
                "seven_day_sonnet": {"pct": None, "resets_at": None},
                "pace": {
                    "ok": True,
                    "projected_pct": 54.0,
                    "elapsed_h": 14.0,
                    "total_h": 91.0,
                    "hours_left": 77.0,
                    "expected_pct": 15.4,
                    "delta_pp": 3.6,
                },
            },
            "codex": {
                "weekly": {
                    "pct": 15.0,
                    "resets_at": reset.isoformat(),
                    "window_minutes": 10080,
                },
                "session": {
                    "pct": 4.0,
                    "resets_at": (now + timedelta(hours=4)).isoformat(),
                    "window_minutes": 300,
                },
                "pace": {
                    "ok": True,
                    "projected_pct": 97.0,
                    "elapsed_h": 14.0,
                    "total_h": 91.0,
                    "hours_left": 77.0,
                    "expected_pct": 15.4,
                    "delta_pp": -0.4,
                },
                "plan_type": "prolite",
            },
        }
        server = ThreadingHTTPServer(("127.0.0.1", 0), _UsageHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            env = os.environ.copy()
            env["CCC_USAGE_URL"] = f"http://127.0.0.1:{server.server_port}/api/usage/current"
            # Keep the subprocess hermetic: no real transcripts.
            with tempfile.TemporaryDirectory() as tmp:
                env["CLAUDE_PROJECTS_DIR"] = tmp
                env["CLAUDE_USAGE_MODEL_CACHE"] = os.path.join(tmp, "cache.json")
                env["CODEX_SESSIONS_DIR"] = tmp
                env["CODEX_USAGE_CACHE"] = os.path.join(tmp, "codex.json")
                proc = subprocess.run(
                    [str(PLUGIN)],
                    cwd=ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=True,
                )
        finally:
            server.shutdown()
            server.server_close()

        output = proc.stdout
        self.assertIn("19% used · resets in", output)
        self.assertIn("expected 15% · Δ +4pp", output)
        self.assertIn("Worked 14.0h · 77.0h left", output)
        # weekly % shouldn't be repeated across the summary and pace-detail rows
        self.assertNotIn("Used 19%", output)
        self.assertIn("Updated ", output)
        self.assertIn("Refresh now | refresh=true", output)
        self.assertNotIn("via CCC", output)

    def test_fresh_local_codex_usage_overrides_stale_ccc_usage(self):
        plugin = load_plugin()
        now = datetime.now(timezone.utc)
        reset = now + timedelta(days=5)
        ccc = {
            "ok": True,
            "fetched_at": now.isoformat(),
            "claude": {
                "five_hour": {"pct": 11.0, "resets_at": (now + timedelta(hours=4)).isoformat()},
                "seven_day": {"pct": 19.0, "resets_at": reset.isoformat()},
                "pace": {
                    "ok": True,
                    "projected_pct": 54.0,
                    "elapsed_h": 14.0,
                    "total_h": 91.0,
                    "hours_left": 77.0,
                    "expected_pct": 15.4,
                    "delta_pp": 3.6,
                },
            },
            "codex": {
                "weekly": {"pct": 2.0, "resets_at": reset.isoformat(), "window_minutes": 10080},
                "session": {"pct": 12.0, "resets_at": (now - timedelta(days=2)).isoformat(), "window_minutes": 300},
                "pace": {"ok": True, "projected_pct": 7.0},
                "plan_type": "prolite",
            },
        }
        local_codex = {
            "weekly": {"pct": 7.0, "resets_at": int(reset.timestamp()), "window_minutes": 10080},
            "session": None,
            "plan_type": "prolite",
            "from_cache": False,
        }
        fake_codex_module = SimpleNamespace(read_usage=lambda: local_codex)
        output = StringIO()

        with (
            mock.patch.object(plugin, "fetch_from_ccc", return_value=ccc),
            mock.patch.object(plugin, "burn_shares_for_week", return_value=None),
            mock.patch.dict(sys.modules, {"_codex_lib": fake_codex_module}),
            redirect_stdout(output),
        ):
            plugin.main()

        rendered = output.getvalue()
        self.assertIn("Codex (prolite)", rendered)
        self.assertIn("  7% used", rendered)
        self.assertNotIn("  2% used", rendered)

    def test_kimi_usage_section_renders(self):
        plugin = load_plugin()
        now = datetime.now(timezone.utc)
        reset = now + timedelta(days=5)
        ccc = {
            "ok": True,
            "fetched_at": now.isoformat(),
            "claude": {
                "five_hour": {"pct": 11.0, "resets_at": (now + timedelta(hours=4)).isoformat()},
                "seven_day": {"pct": 19.0, "resets_at": reset.isoformat()},
                "pace": {
                    "ok": True,
                    "projected_pct": 54.0,
                    "elapsed_h": 14.0,
                    "total_h": 91.0,
                    "hours_left": 77.0,
                    "expected_pct": 15.4,
                    "delta_pp": 3.6,
                },
            },
            "codex": {},
        }
        local_kimi = {
            "weekly": {"pct": 15.0, "used": 15, "limit": 100,
                       "resets_at": int(reset.timestamp())},
            "session": {"pct": 4.0, "used": 4, "limit": 100,
                        "resets_at": int((now + timedelta(hours=4)).timestamp())},
            "extra": {
                "total_cents": 1000,
                "balance_cents": None,
                "monthly_used_cents": 1000,
                "monthly_limit_cents": 10000,
                "monthly_limit_enabled": True,
                "currency": "USD",
            },
            "plan_type": "Advanced",
            "from_cache": False,
        }
        fake_kimi_module = SimpleNamespace(read_usage=lambda: local_kimi)
        fake_codex_module = SimpleNamespace(read_usage=lambda: None)
        output = StringIO()

        with (
            mock.patch.object(plugin, "fetch_from_ccc", return_value=ccc),
            mock.patch.object(plugin, "burn_shares_for_week", return_value=None),
            mock.patch.dict(sys.modules, {"_codex_lib": fake_codex_module,
                                          "_kimi_lib": fake_kimi_module}),
            redirect_stdout(output),
        ):
            plugin.main()

        rendered = output.getvalue()
        self.assertIn("🌙 15%", rendered)
        self.assertIn("Kimi (Advanced)", rendered)
        self.assertIn("  15% used", rendered)
        self.assertIn("5h session: 4% used", rendered)
        self.assertIn("Extra usage (on): USD 10.00 of USD 100 (10.0%)", rendered)

    def test_web_ui_links_rendered(self):
        plugin = load_plugin()
        now = datetime.now(timezone.utc)
        reset = now + timedelta(days=5)
        ccc = {
            "ok": True,
            "fetched_at": now.isoformat(),
            "claude": {
                "five_hour": {"pct": 10.0, "resets_at": (now + timedelta(hours=4)).isoformat()},
                "seven_day": {"pct": 20.0, "resets_at": reset.isoformat()},
            },
            "codex": {},
        }
        local_codex = {
            "weekly": {"pct": 5.0, "resets_at": int(reset.timestamp()), "window_minutes": 10080},
            "session": None,
            "plan_type": "prolite",
            "from_cache": False,
        }
        local_kimi = {
            "weekly": {"pct": 10.0, "used": 10, "limit": 100, "resets_at": int(reset.timestamp())},
            "session": None,
            "plan_type": "Advanced",
            "from_cache": False,
        }
        output = StringIO()
        with (
            mock.patch.object(plugin, "fetch_from_ccc", return_value=ccc),
            mock.patch.object(plugin, "burn_shares_for_week", return_value=None),
            mock.patch.dict(sys.modules, {"_codex_lib": SimpleNamespace(read_usage=lambda: local_codex),
                                          "_kimi_lib": SimpleNamespace(read_usage=lambda: local_kimi)}),
            redirect_stdout(output),
        ):
            plugin.main()

        rendered = output.getvalue()
        # Verify direct links inside engine sections and footer section
        self.assertIn("Open Claude Web UI | href=https://claude.ai/settings/usage", rendered)
        self.assertIn("Open Codex Web UI | href=https://chatgpt.com/codex/cloud/settings/analytics#usage", rendered)
        self.assertIn("Open Kimi Web UI | href=https://www.kimi.com/membership/subscription?tab=quota", rendered)
        self.assertIn("🌐 Open Web UIs", rendered)

    def test_format_proj_dir(self):
        plugin = load_plugin()
        formatted = plugin.format_proj_dir("Users-amirfish-Apps-claude-c")
        self.assertEqual(formatted, "~/Apps-claude-c")

    def test_antigravity_and_grok_sections_render(self):
        plugin = load_plugin()
        now = datetime.now(timezone.utc)
        reset = now + timedelta(days=5)
        ccc = {
            "ok": True,
            "fetched_at": now.isoformat(),
            "claude": {
                "five_hour": {"pct": 10.0, "resets_at": (now + timedelta(hours=4)).isoformat()},
                "seven_day": {"pct": 20.0, "resets_at": reset.isoformat()},
            },
            "codex": {},
        }
        local_antigravity = {
            "gemini": {
                "weekly": {"pct": 33.0, "resets_at": reset.isoformat()},
                "session": {"pct": 12.0, "resets_at": (now + timedelta(hours=4)).isoformat()},
            },
            "third_party": {
                "weekly": {"pct": 5.0, "resets_at": reset.isoformat()},
                "session": {"pct": 0.0, "resets_at": (now + timedelta(hours=4)).isoformat()},
            },
            "from_cache": False,
        }
        local_grok = {
            "weekly": {"pct": 8.0, "resets_at": reset.isoformat()},
            "session": None,
            "extra": {"balance_cents": 1000},
            "plan_type": "SuperGrok",
            "from_cache": False,
        }
        output = StringIO()
        with (
            mock.patch.object(plugin, "fetch_from_ccc", return_value=ccc),
            mock.patch.object(plugin, "burn_shares_for_week", return_value=None),
            mock.patch.dict(sys.modules, {
                "_antigravity_lib": SimpleNamespace(read_usage=lambda: local_antigravity),
                "_grok_lib": SimpleNamespace(read_usage=lambda: local_grok),
            }),
            redirect_stdout(output),
        ):
            plugin.main()

        rendered = output.getvalue()
        # Headline symbols
        self.assertIn("✦ 33%", rendered)
        self.assertIn("✦3P 5%", rendered)
        self.assertIn("𝕏 8%", rendered)
        # Dropdown sections
        self.assertIn("Antigravity", rendered)
        self.assertIn("Gemini Models", rendered)
        self.assertIn("Claude/GPT Models", rendered)
        self.assertIn("Grok (SuperGrok)", rendered)
        self.assertIn("8% used", rendered)
        self.assertIn("Open Antigravity Home", rendered)
        self.assertIn("Open Grok Web UI", rendered)
        # Real icon images attached to headers
        self.assertIn("image=", rendered)


if __name__ == "__main__":
    unittest.main()

