import importlib.util
import sys
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
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


def load_goal_lib():
    spec = importlib.util.spec_from_file_location("goal", ROOT / "goal.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ccc_payload(now, reset):
    return {
        "ok": True,
        "fetched_at": now.isoformat(),
        "claude": {
            "five_hour": {"pct": 11.0, "resets_at": (now + timedelta(hours=4)).isoformat()},
            "seven_day": {"pct": 42.0, "resets_at": reset.isoformat()},
            "seven_day_sonnet": {"pct": None, "resets_at": None},
            "seven_day_fable": {"pct": 44.0, "resets_at": reset.isoformat()},
            "pace": {
                "ok": True,
                "projected_pct": 54.0,
                "elapsed_h": 20.0,
                "total_h": 91.0,
                "hours_left": 71.0,
                "expected_pct": 15.4,
                "delta_pp": 3.6,
            },
        },
        "codex": {},
    }


class ScopedWeeklyFromLimitsTest(unittest.TestCase):
    def test_extracts_model_scoped_weekly(self):
        plugin = load_plugin()
        usage = {"limits": [
            {"kind": "session", "group": "session", "percent": 36, "scope": None},
            {"kind": "weekly_all", "group": "weekly", "percent": 42, "scope": None},
            {"kind": "weekly_scoped", "group": "weekly", "percent": 44,
             "resets_at": "2026-07-17T07:00:00+00:00",
             "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None}},
        ]}
        scoped = plugin.scoped_weekly_from_limits(usage)
        self.assertEqual(scoped["fable"]["utilization"], 44)
        self.assertEqual(list(scoped), ["fable"])

    def test_empty_limits(self):
        plugin = load_plugin()
        self.assertEqual(plugin.scoped_weekly_from_limits({}), {})


class DropdownRenderTest(unittest.TestCase):
    def render(self, goal_cfg=None, shares=None):
        plugin = load_plugin()
        goal_lib = load_goal_lib()
        now = datetime.now(timezone.utc)
        reset = now + timedelta(days=3)
        fake_goal_lib = SimpleNamespace(
            load_goal=lambda: goal_cfg,
            goal_status=goal_lib.goal_status,
        )
        output = StringIO()
        with (
            mock.patch.object(plugin, "fetch_from_ccc",
                              return_value=ccc_payload(now, reset)),
            mock.patch.object(plugin, "burn_shares_for_week", return_value=shares),
            mock.patch.object(plugin, "_load_goal_lib", return_value=fake_goal_lib),
            mock.patch.dict(sys.modules, {"_codex_lib": SimpleNamespace(read_usage=lambda: None)}),
            redirect_stdout(output),
        ):
            plugin.main()
        return output.getvalue()

    def test_model_split_section(self):
        out = self.render(shares={"fable": 0.268, "opus": 0.393, "sonnet": 0.339})
        self.assertIn("Model split — this week", out)
        # Fable has both a share and an API cap (from seven_day_fable)
        self.assertIn("Fable     27% of burn · 44% of its cap", out)
        # Opus has a share but no API bucket
        self.assertIn("Opus      39% of burn · cap —", out)
        # sorted by share: opus first
        self.assertLess(out.index("Opus"), out.index("Sonnet"))
        self.assertLess(out.index("Sonnet"), out.index("Fable "))

    def test_goal_section_behind(self):
        goal_cfg = {
            "target_pct": 100.0,
            "deadline": datetime.now().astimezone() + timedelta(hours=2),
            "model_share": {"fable": 50.0},
        }
        out = self.render(goal_cfg=goal_cfg,
                          shares={"fable": 0.268, "opus": 0.393, "sonnet": 0.339})
        self.assertIn("🎯 Goal: 100% by", out)
        # weekly 42% at 2.1pp/h can't reach 100% in <=2 work hours
        self.assertIn("BEHIND — projected", out)
        self.assertIn("✗ fable 27% of burn (target ≥50%)", out)
        self.assertIn("Clear goal", out)

    def test_goal_section_achieved(self):
        goal_cfg = {
            "target_pct": 40.0,
            "deadline": datetime.now().astimezone() + timedelta(hours=2),
            "model_share": {},
        }
        out = self.render(goal_cfg=goal_cfg)
        self.assertIn("✅ achieved — at 42%", out)

    def test_no_goal_no_section(self):
        out = self.render(goal_cfg=None, shares=None)
        self.assertNotIn("🎯 Goal", out)
        self.assertIn("Set goal…", out)
        self.assertNotIn("Clear goal", out)
        # Fable still shown from the API bucket even without local shares
        self.assertIn("Fable   burn — · 44% of its cap", out)


if __name__ == "__main__":
    unittest.main()
