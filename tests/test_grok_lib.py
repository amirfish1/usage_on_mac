import json
import os
import tempfile
import unittest
from pathlib import Path

import _grok_lib


class GrokLibTest(unittest.TestCase):
    def test_read_from_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_p = Path(tmp) / "unified.jsonl"
            cache_p = Path(tmp) / "cache.json"

            sample = {
                "ts": "2026-09-01T08:47:35.889Z",
                "src": "shell",
                "pid": 60018,
                "ver": "1.0.13",
                "lvl": "info",
                "msg": "billing: fetched credits config",
                "ctx": {
                    "config": {
                        "creditUsagePercent": 8.0,
                        "currentPeriod": {
                            "type": "USAGE_PERIOD_TYPE_WEEKLY",
                            "start": "2026-08-27T16:22:10.838963+00:00",
                            "end": "2026-09-03T16:22:10.838963+00:00"
                        },
                        "onDemandCap": {"val": 0},
                        "onDemandUsed": {"val": 0},
                        "prepaidBalance": {"val": 1000},
                        "isUnifiedBillingUser": True,
                        "billingPeriodStart": "2026-08-27T16:22:10.838963+00:00",
                        "billingPeriodEnd": "2026-09-03T16:22:10.838963+00:00",
                        "historyLen": 0
                    },
                    "onDemandEnabled": None,
                    "subscriptionTier": "SuperGrok"
                }
            }
            log_p.write_text(json.dumps(sample) + "\n")

            orig_log = _grok_lib.LOG_FILE
            orig_cache = _grok_lib.CACHE
            try:
                _grok_lib.LOG_FILE = log_p
                _grok_lib.CACHE = cache_p

                usage = _grok_lib.read_usage()
                self.assertIsNotNone(usage)
                self.assertEqual(usage["weekly"]["pct"], 8.0)
                self.assertEqual(usage["weekly"]["resets_at"], "2026-09-03T16:22:10.838963+00:00")
                self.assertEqual(usage["plan_type"], "SuperGrok")
                self.assertEqual(usage["extra"]["balance_cents"], 1000)
                self.assertFalse(usage["from_cache"])

                # Second read should hit cache if log is missing
                log_p.unlink()
                usage_cached = _grok_lib.read_usage()
                self.assertIsNotNone(usage_cached)
                self.assertEqual(usage_cached["weekly"]["pct"], 8.0)
                self.assertTrue(usage_cached["from_cache"])
            finally:
                _grok_lib.LOG_FILE = orig_log
                _grok_lib.CACHE = orig_cache


if __name__ == "__main__":
    unittest.main()
