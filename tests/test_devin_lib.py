import json
import threading
import unittest
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import _devin_lib


class _StatusHandler(BaseHTTPRequestHandler):
    payload = {}
    last_body = None

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        _StatusHandler.last_body = self.rfile.read(length)
        body = json.dumps(self.payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


def _creds_file(tmp, url):
    p = Path(tmp) / "credentials.toml"
    p.write_text(
        f'windsurf_api_key = "devin-session-token$fake"\n'
        f'api_server_url = "{url}"\n'
    )
    return p


class DevinLibTest(unittest.TestCase):
    def _run_with(self, creds_p, cache_p, fn):
        orig_creds, orig_cache = _devin_lib.CREDENTIALS_FILE, _devin_lib.CACHE
        try:
            _devin_lib.CREDENTIALS_FILE = creds_p
            _devin_lib.CACHE = cache_p
            return fn()
        finally:
            _devin_lib.CREDENTIALS_FILE = orig_creds
            _devin_lib.CACHE = orig_cache

    def test_read_from_user_status(self):
        _StatusHandler.payload = {
            "userStatus": {
                "pro": True,
                "teamsTier": "TEAMS_TIER_DEVIN_PRO",
                "planStatus": {
                    "planInfo": {"planName": "Pro"},
                    "planStart": "2026-09-07T01:46:36Z",
                    "planEnd": "2026-10-07T01:46:36Z",
                    "dailyQuotaRemainingPercent": 76,
                    "weeklyQuotaRemainingPercent": 86,
                    "overageBalanceMicros": "8920935",
                    "dailyQuotaResetAtUnix": "1789804800",
                    "weeklyQuotaResetAtUnix": "1789891200",
                },
            }
        }
        server = ThreadingHTTPServer(("127.0.0.1", 0), _StatusHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                url = f"http://127.0.0.1:{server.server_port}"
                creds_p = _creds_file(tmp, url)
                cache_p = Path(tmp) / "cache.json"

                usage = self._run_with(creds_p, cache_p, _devin_lib.read_usage)
                self.assertIsNotNone(usage)
                self.assertEqual(usage["weekly"]["pct"], 14.0)
                self.assertEqual(usage["weekly"]["resets_at"], 1789891200)
                self.assertEqual(usage["session"]["pct"], 24.0)
                self.assertEqual(usage["session"]["resets_at"], 1789804800)
                self.assertEqual(usage["plan_type"], "Pro")
                self.assertEqual(usage["extra"]["overage_balance_micros"], "8920935")
                self.assertFalse(usage["from_cache"])

                # API key rides inside the request body's metadata envelope
                sent = json.loads(_StatusHandler.last_body)
                self.assertEqual(sent["metadata"]["apiKey"], "devin-session-token$fake")

                # Server down → falls back to cache
                server.shutdown()
                usage_cached = self._run_with(creds_p, cache_p, _devin_lib.read_usage)
                self.assertIsNotNone(usage_cached)
                self.assertEqual(usage_cached["weekly"]["pct"], 14.0)
                self.assertTrue(usage_cached["from_cache"])
        finally:
            server.server_close()

    def test_missing_credentials_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            usage = self._run_with(
                Path(tmp) / "nope.toml", Path(tmp) / "cache.json",
                _devin_lib.read_usage)
            self.assertIsNone(usage)


if __name__ == "__main__":
    unittest.main()
