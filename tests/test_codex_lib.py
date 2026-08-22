import unittest
from unittest import mock

import _codex_lib


class CodexUsageTest(unittest.TestCase):
    def test_weekly_window_can_be_primary_when_session_window_is_absent(self):
        rate_limits = {
            "primary": {
                "used_percent": 7.0,
                "resets_at": 1_784_666_430,
                "window_minutes": 10080,
            },
            "secondary": None,
            "plan_type": "prolite",
        }

        with (
            mock.patch.object(_codex_lib, "_latest_snapshot", return_value=("2026-07-15T04:02:55Z", rate_limits)),
            mock.patch.object(_codex_lib, "_read_cache", return_value=None),
            mock.patch.object(_codex_lib, "_write_cache"),
        ):
            usage = _codex_lib.read_usage()

        self.assertEqual(7.0, usage["weekly"]["pct"])
        self.assertIsNone(usage["session"])
        self.assertFalse(usage["from_cache"])


if __name__ == "__main__":
    unittest.main()
