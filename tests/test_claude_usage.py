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
            # Keep the subprocess hermetic: no real transcripts or goal file.
            with tempfile.TemporaryDirectory() as tmp:
                env["CLAUDE_PROJECTS_DIR"] = tmp
                env["CLAUDE_USAGE_MODEL_CACHE"] = os.path.join(tmp, "cache.json")
                env["CLAUDE_USAGE_GOAL_FILE"] = os.path.join(tmp, "goal.json")
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
        self.assertIn("Used 19% · expected 15% · Δ +4pp", output)
        self.assertIn("Worked 14.0h · 77.0h left", output)
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
            mock.patch.object(plugin, "_load_goal_lib", side_effect=ImportError),
            mock.patch.dict(sys.modules, {"_codex_lib": fake_codex_module}),
            redirect_stdout(output),
        ):
            plugin.main()

        rendered = output.getvalue()
        self.assertIn("Codex (prolite)", rendered)
        self.assertIn("  7% used", rendered)
        self.assertNotIn("  2% used", rendered)


if __name__ == "__main__":
    unittest.main()
