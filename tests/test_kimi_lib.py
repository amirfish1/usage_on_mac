import datetime as dt
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import _kimi_lib


def _payload():
    return {
        "user": {"membership": {"level": "LEVEL_ADVANCED"}, "region": "REGION_OVERSEA"},
        "usage": {"limit": "100", "used": "15", "remaining": "85",
                  "resetTime": "2026-07-28T13:52:21.141644Z"},
        "limits": [
            {"window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
             "detail": {"limit": "100", "remaining": "96",
                        "resetTime": "2026-07-22T09:52:21.141644Z"}},
            {"window": {"duration": 7, "timeUnit": "TIME_UNIT_DAY"},
             "detail": {"limit": "100", "remaining": "85",
                        "resetTime": "2026-07-28T13:52:21.141644Z"}},
        ],
        "boosterWallet": {
            "balance": {"type": "BOOSTER", "amount": "1000000000", "unit": "UNIT_CURRENCY"},
            "status": "STATUS_ACTIVE",
            "monthlyChargeLimit": {"currency": "USD", "priceInCents": "10000"},
            "monthlyUsed": {"currency": "USD", "priceInCents": "1000"},
            "monthlyChargeLimitEnabled": True,
        },
    }


class KimiUsageTest(unittest.TestCase):
    def test_successful_fetch_parses_all_sections(self):
        with (
            mock.patch.object(_kimi_lib, "_load_access_token", return_value="tok"),
            mock.patch.object(_kimi_lib, "_fetch", return_value=_payload()) as fetch,
            mock.patch.object(_kimi_lib, "_write_cache"),
        ):
            usage = _kimi_lib.read_usage()

        fetch.assert_called_once_with(f"{_kimi_lib.BASE_URL}/usages", "tok")
        self.assertFalse(usage["from_cache"])
        # weekly
        self.assertEqual(15.0, usage["weekly"]["pct"])
        self.assertEqual(15, usage["weekly"]["used"])
        self.assertEqual(100, usage["weekly"]["limit"])
        expected_reset = dt.datetime(2026, 7, 28, 13, 52, 21,
                                     tzinfo=dt.timezone.utc).timestamp() + 0.141644
        self.assertAlmostEqual(expected_reset, usage["weekly"]["resets_at"], places=3)
        # session: the 300-minute window, used derived from remaining
        self.assertEqual(4.0, usage["session"]["pct"])
        self.assertEqual(4, usage["session"]["used"])
        # booster wallet: amount 1_000_000_000 → 1000 cents
        extra = usage["extra"]
        self.assertEqual(1000, extra["total_cents"])
        self.assertIsNone(extra["balance_cents"])
        self.assertEqual(1000, extra["monthly_used_cents"])
        self.assertEqual(10000, extra["monthly_limit_cents"])
        self.assertTrue(extra["monthly_limit_enabled"])
        self.assertEqual("USD", extra["currency"])
        # membership level prettified
        self.assertEqual("Advanced", usage["plan_type"])

    def test_weekly_used_derived_from_remaining_when_used_absent(self):
        payload = _payload()
        del payload["usage"]["used"]
        with (
            mock.patch.object(_kimi_lib, "_load_access_token", return_value="tok"),
            mock.patch.object(_kimi_lib, "_fetch", return_value=payload),
            mock.patch.object(_kimi_lib, "_write_cache"),
        ):
            usage = _kimi_lib.read_usage()

        self.assertEqual(15, usage["weekly"]["used"])
        self.assertEqual(15.0, usage["weekly"]["pct"])

    def test_http_error_falls_back_to_cache(self):
        cached = {
            "weekly": {"pct": 42.0, "used": 42, "limit": 100, "resets_at": 1_790_000_000},
            "session": None,
            "extra": None,
            "plan_type": "Advanced",
            "fetched_at": 1_780_000_000,
            "from_cache": False,
        }
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "kimi-usage.json"
            cache_path.write_text(json.dumps(cached))
            err = urllib.error.HTTPError(
                url="https://api.kimi.com/coding/v1/usages", code=401,
                msg="Unauthorized", hdrs=None, fp=None)
            with (
                mock.patch.object(_kimi_lib, "CACHE", cache_path),
                mock.patch.object(_kimi_lib, "_load_access_token", return_value="tok"),
                mock.patch.object(_kimi_lib, "_fetch", side_effect=err),
            ):
                usage = _kimi_lib.read_usage()

        self.assertTrue(usage["from_cache"])
        self.assertEqual(42.0, usage["weekly"]["pct"])

    def test_no_credentials_and_no_cache_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope.json"
            with (
                mock.patch.object(_kimi_lib, "CREDENTIALS", missing),
                mock.patch.object(_kimi_lib, "CACHE", missing),
            ):
                self.assertIsNone(_kimi_lib.read_usage())

    def test_no_booster_wallet_means_no_extra(self):
        payload = _payload()
        payload["boosterWallet"] = None
        with (
            mock.patch.object(_kimi_lib, "_load_access_token", return_value="tok"),
            mock.patch.object(_kimi_lib, "_fetch", return_value=payload),
            mock.patch.object(_kimi_lib, "_write_cache"),
        ):
            usage = _kimi_lib.read_usage()

        self.assertIsNone(usage["extra"])

    def test_session_falls_back_to_smallest_window(self):
        payload = _payload()
        payload["limits"] = [
            {"window": {"duration": 2, "timeUnit": "TIME_UNIT_HOUR"},
             "detail": {"limit": "50", "remaining": "25",
                        "resetTime": "2026-07-22T09:52:21Z"}},
            {"window": {"duration": 7, "timeUnit": "TIME_UNIT_DAY"},
             "detail": {"limit": "100", "remaining": "85",
                        "resetTime": "2026-07-28T13:52:21Z"}},
        ]
        with (
            mock.patch.object(_kimi_lib, "_load_access_token", return_value="tok"),
            mock.patch.object(_kimi_lib, "_fetch", return_value=payload),
            mock.patch.object(_kimi_lib, "_write_cache"),
        ):
            usage = _kimi_lib.read_usage()

        self.assertEqual(50.0, usage["session"]["pct"])
        self.assertEqual(50, usage["session"]["limit"])


if __name__ == "__main__":
    unittest.main()
