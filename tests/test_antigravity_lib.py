import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _antigravity_lib


MONITORING_RESPONSE = {
    "timeSeries": [
        {"metric": {"labels": {"type": "input"}},
         "points": [{"value": {"int64Value": "60000000"}}]},
        {"metric": {"labels": {"type": "output"}},
         "points": [{"value": {"int64Value": "500000"}}]},
    ]
}


class AntigravityVertexTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.settings = root / "settings.json"
        self.config = root / "antigravity.json"
        self.cache = root / "cache.json"
        self.settings.write_text(json.dumps({"gcp": {"project": "test-project"}}))
        self.patches = [
            mock.patch.object(_antigravity_lib, "AGY_SETTINGS", self.settings),
            mock.patch.object(_antigravity_lib, "CONFIG", self.config),
            mock.patch.object(_antigravity_lib, "CACHE", self.cache),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_parse_vertex_tokens_sums_by_type(self):
        self.assertEqual(_antigravity_lib._parse_vertex_tokens(MONITORING_RESPONSE),
                         {"input": 60000000, "output": 500000})
        self.assertIsNone(_antigravity_lib._parse_vertex_tokens({"error": {"code": 403}}))

    def test_vertex_mode_without_budget_reports_cost_not_pct(self):
        with mock.patch.object(_antigravity_lib, "_fetch_vertex_tokens",
                               return_value={"input": 60000000, "output": 500000}):
            data = _antigravity_lib.read_usage()
        self.assertEqual(data["billing"], "vertex")
        self.assertIsNone(data["gemini"]["weekly"]["pct"])
        # 60M in * $2/M + 0.5M out * $10/M
        self.assertAlmostEqual(data["vertex"]["est_cost_usd"], 125.0)

    def test_vertex_mode_pct_of_weekly_budget(self):
        self.config.write_text(json.dumps({"weekly_budget_usd": 250}))
        with mock.patch.object(_antigravity_lib, "_fetch_vertex_tokens",
                               return_value={"input": 60000000, "output": 500000}):
            data = _antigravity_lib.read_usage()
        self.assertAlmostEqual(data["gemini"]["weekly"]["pct"], 50.0)

    def test_vertex_mode_ignores_subscription_era_cache(self):
        self.cache.write_text(json.dumps(
            {"gemini": {"weekly": {"pct": 0.01, "resets_at": "2026-10-23T02:09:27Z"}}}))
        with mock.patch.object(_antigravity_lib, "_fetch_vertex_tokens", return_value=None):
            self.assertIsNone(_antigravity_lib.read_usage())

    def test_vertex_mode_falls_back_to_vertex_cache(self):
        with mock.patch.object(_antigravity_lib, "_fetch_vertex_tokens",
                               return_value={"input": 1000000, "output": 0}):
            _antigravity_lib.read_usage()
        with mock.patch.object(_antigravity_lib, "_fetch_vertex_tokens", return_value=None):
            data = _antigravity_lib.read_usage()
        self.assertTrue(data["from_cache"])
        self.assertEqual(data["vertex"]["input_tokens"], 1000000)


if __name__ == "__main__":
    unittest.main()
