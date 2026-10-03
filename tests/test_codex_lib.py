import datetime as dt
import json
import os
import tempfile
import time
from pathlib import Path
import unittest
from unittest import mock

import _codex_lib


class CodexUsageTest(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(_codex_lib, "_read_live_rate_limits", return_value=None)
        patch.start()
        self.addCleanup(patch.stop)
    def test_weekly_window_can_be_primary_when_session_window_is_absent(self):
        rate_limits = {
            "primary": {
                "used_percent": 7.0,
                "resets_at": int(time.time()) + 86400,
                "window_minutes": 10080,
            },
            "secondary": None,
            "plan_type": "prolite",
        }

        with (
            mock.patch.object(_codex_lib, "_latest_snapshot", return_value=(dt.datetime.now(dt.timezone.utc).isoformat(), rate_limits)),
            mock.patch.object(_codex_lib, "_read_cache", return_value=None),
            mock.patch.object(_codex_lib, "_write_cache"),
        ):
            usage = _codex_lib.read_usage()

        self.assertEqual(7.0, usage["weekly"]["pct"])
        self.assertIsNone(usage["session"])
        self.assertFalse(usage["from_cache"])


class CodexFreshnessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.object(_codex_lib, "CACHE", Path(self.tmp.name) / "cache.json")
        patch.start()
        self.addCleanup(patch.stop)

    def rate_limits(self, pct=4, minutes=10080):
        return {"primary": {"usedPercent": pct, "windowDurationMins": minutes,
                            "resetsAt": int(time.time()) + 86400}, "planType": "prolite"}

    def test_live_weekly_primary_overrides_old_95_percent(self):
        with mock.patch.object(_codex_lib, "_read_live_rate_limits", return_value=self.rate_limits()), mock.patch.object(_codex_lib, "_latest_snapshot") as logs:
            usage = _codex_lib.read_usage()
        self.assertEqual(usage["weekly"]["pct"], 4)
        self.assertEqual(usage["source"], "app-server")
        self.assertFalse(usage["from_cache"])
        logs.assert_not_called()
        self.assertEqual(_codex_lib._read_cache()["weekly"]["pct"], 4)

    def test_expired_95_percent_log_and_cache_are_not_displayed(self):
        ts = dt.datetime.fromtimestamp(time.time()-86400, dt.timezone.utc).isoformat()
        rl = self.rate_limits(95)
        rl["primary"]["resetsAt"] = int(time.time()) - 1
        _codex_lib._write_cache(_codex_lib._snapshot_data(ts, rl, "rollout"))
        with mock.patch.object(_codex_lib, "_read_live_rate_limits", return_value=None), mock.patch.object(_codex_lib, "_latest_snapshot", return_value=(ts, rl)):
            self.assertIsNone(_codex_lib.read_usage())

    def test_older_log_cannot_replace_newer_live_cache(self):
        now = dt.datetime.now(dt.timezone.utc)
        _codex_lib._write_cache(_codex_lib._snapshot_data(now.isoformat(), self.rate_limits(), "app-server"))
        with mock.patch.object(_codex_lib, "_read_live_rate_limits", return_value=None), mock.patch.object(_codex_lib, "_latest_snapshot", return_value=((now-dt.timedelta(hours=1)).isoformat(), self.rate_limits(95))):
            usage = _codex_lib.read_usage()
        self.assertEqual(usage["weekly"]["pct"], 4)
        self.assertTrue(usage["from_cache"])

    def test_old_log_retains_observation_time_and_is_stale(self):
        ts = (dt.datetime.now(dt.timezone.utc)-dt.timedelta(hours=1)).isoformat()
        with mock.patch.object(_codex_lib, "_read_live_rate_limits", return_value=None), mock.patch.object(_codex_lib, "_latest_snapshot", return_value=(ts, self.rate_limits(12))):
            usage = _codex_lib.read_usage()
        self.assertTrue(usage["from_cache"])
        self.assertEqual(usage["fetched_at"], _codex_lib._epoch(ts))

    def test_latest_snapshot_skips_other_buckets_and_malformed_windows(self):
        now = dt.datetime.now(dt.timezone.utc)
        records = [
            {"timestamp": now.isoformat(), "payload": {"rate_limits": self.rate_limits(4)}},
            {"timestamp": (now+dt.timedelta(seconds=1)).isoformat(), "payload": {"rate_limits": {**self.rate_limits(95), "limit_id": "codex_other"}}},
            {"timestamp": (now+dt.timedelta(seconds=2)).isoformat(), "payload": {"rate_limits": self.rate_limits("bad")}},
        ]
        log = Path(self.tmp.name) / "rollout.jsonl"
        log.write_text('\n'.join(json.dumps(r) for r in records) + '\n{"rate_limits":')
        with mock.patch.object(_codex_lib, "_iter_recent_rollouts", return_value=[log]):
            self.assertEqual(_codex_lib._latest_snapshot()[0], now.isoformat())

    def test_weekly_secondary_and_expired_session(self):
        rl = self.rate_limits(11, 300)
        rl["secondary"] = self.rate_limits(4)["primary"]
        rl["primary"]["resetsAt"] = int(time.time()) - 1
        data = _codex_lib.valid_usage(_codex_lib._snapshot_data(dt.datetime.now(dt.timezone.utc).isoformat(), rl, "app-server"))
        self.assertEqual(data["weekly"]["pct"], 4)
        self.assertIsNone(data["session"])

    def test_invalid_live_result_falls_back_to_cache(self):
        ts = dt.datetime.now(dt.timezone.utc).isoformat()
        _codex_lib._write_cache(_codex_lib._snapshot_data(ts, self.rate_limits(4), "app-server"))
        with mock.patch.object(_codex_lib, "_read_live_rate_limits", return_value=self.rate_limits("bad")), mock.patch.object(_codex_lib, "_latest_snapshot", return_value=None):
            self.assertEqual(_codex_lib.read_usage()["weekly"]["pct"], 4)

    def test_nonfinite_usage_is_rejected(self):
        self.assertIsNone(_codex_lib._window_for_duration(self.rate_limits(float("nan")), 10080))

    def test_live_protocol_and_bucket_selection_with_fake_cli(self):
        cli = Path(self.tmp.name) / "codex"
        result = {"rateLimits": self.rate_limits(95), "rateLimitsByLimitId": {"codex": self.rate_limits(4)}}
        cli.write_text("#!/usr/bin/env python3\nimport sys,json\n"
                       "for line in sys.stdin:\n"
                       " m=json.loads(line)\n"
                       " if m.get('id')==1: print(json.dumps({'id':1,'result':{}}),flush=True)\n"
                       " if m.get('id')==2: print(json.dumps({'id':2,'result':" + repr(result) + "}),flush=True)\n")
        cli.chmod(0o755)
        with mock.patch.dict(os.environ, {"CODEX_USAGE_LIVE": "1", "CODEX_USAGE_BIN": str(cli)}):
            self.assertEqual(_codex_lib._read_live_rate_limits(), result["rateLimitsByLimitId"]["codex"])

    def test_unresponsive_cli_is_bounded(self):
        cli = Path(self.tmp.name) / "codex"
        cli.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(60)\n")
        cli.chmod(0o755)
        started = time.monotonic()
        with mock.patch.dict(os.environ, {"CODEX_USAGE_LIVE": "1", "CODEX_USAGE_BIN": str(cli)}), mock.patch.object(_codex_lib, "LIVE_TIMEOUT", 0.1):
            self.assertIsNone(_codex_lib._read_live_rate_limits())
        self.assertLess(time.monotonic()-started, 2)


if __name__ == "__main__":
    unittest.main()
