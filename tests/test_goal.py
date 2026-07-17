import importlib.util
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module():
    spec = importlib.util.spec_from_file_location("goal", ROOT / "goal.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TZ = timezone(timedelta(hours=3))  # matches the brief's +03:00 examples


class ParseGoalStringTest(unittest.TestCase):
    def setUp(self):
        self.m = load_module()
        self.now = datetime(2026, 7, 17, 3, 0, tzinfo=TZ)

    def test_full_brief_example(self):
        g = self.m.parse_goal_string(
            "100% by 2026-07-17T10:00+03:00, fable>=50%", now_local=self.now)
        self.assertEqual(g["target_pct"], 100.0)
        self.assertEqual(g["deadline"], datetime(2026, 7, 17, 10, 0, tzinfo=TZ))
        self.assertEqual(g["model_share"], {"fable": 50.0})

    def test_naive_iso_gets_local_tz(self):
        g = self.m.parse_goal_string("80 by 2026-07-18T09:30", now_local=self.now)
        self.assertEqual(g["deadline"].tzinfo, TZ)

    def test_hhmm_deadline_rolls_to_next_occurrence(self):
        g = self.m.parse_goal_string("50% by 02:00", now_local=self.now)
        self.assertEqual(g["deadline"], datetime(2026, 7, 18, 2, 0, tzinfo=TZ))
        g = self.m.parse_goal_string("50% by 10:00", now_local=self.now)
        self.assertEqual(g["deadline"], datetime(2026, 7, 17, 10, 0, tzinfo=TZ))

    def test_multiple_model_shares(self):
        g = self.m.parse_goal_string(
            "90% by 10:00, fable>=50, opus>=20%", now_local=self.now)
        self.assertEqual(g["model_share"], {"fable": 50.0, "opus": 20.0})

    def test_rejects_garbage(self):
        with self.assertRaises(ValueError):
            self.m.parse_goal_string("go fast", now_local=self.now)
        with self.assertRaises(ValueError):
            self.m.parse_goal_string("fable>=50%", now_local=self.now)  # no target


class SaveLoadTest(unittest.TestCase):
    def test_round_trip_and_clear(self):
        m = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            gf = Path(tmp) / "goal.json"
            goal = {"target_pct": 100.0,
                    "deadline": datetime(2026, 7, 17, 10, 0, tzinfo=TZ),
                    "model_share": {"fable": 50.0}}
            m.save_goal(goal, goal_file=gf)
            loaded = m.load_goal(goal_file=gf)
            self.assertEqual(loaded["target_pct"], 100.0)
            self.assertEqual(loaded["deadline"], goal["deadline"])
            self.assertEqual(loaded["model_share"], {"fable": 50.0})
            m.clear_goal(goal_file=gf)
            self.assertIsNone(m.load_goal(goal_file=gf))

    def test_load_missing_returns_none(self):
        m = load_module()
        self.assertIsNone(m.load_goal(goal_file="/nonexistent/goal.json"))


class GoalStatusTest(unittest.TestCase):
    def setUp(self):
        self.m = load_module()
        # 08:00 local, deadline 12:00 → 4 work hours left (window 7:00-20:00)
        self.now = datetime(2026, 7, 17, 8, 0, tzinfo=TZ)
        self.goal = {"target_pct": 100.0,
                     "deadline": datetime(2026, 7, 17, 12, 0, tzinfo=TZ),
                     "model_share": {"fable": 50.0}}

    def test_behind_pace(self):
        # 40% burned over 20 work hours → 2 pp/h; need (100-40)/4 = 15 pp/h
        s = self.m.goal_status(self.goal, weekly_pct=40.0, elapsed_h=20.0,
                               shares={"fable": 0.3}, now_local=self.now)
        self.assertAlmostEqual(s["hours_to_deadline"], 4.0)
        self.assertAlmostEqual(s["current_pace"], 2.0)
        self.assertAlmostEqual(s["required_pace"], 15.0)
        self.assertAlmostEqual(s["projected_pct"], 48.0)
        self.assertFalse(s["on_track"])
        self.assertFalse(s["expired"])
        fable = s["model_share"]["fable"]
        self.assertAlmostEqual(fable["current_pct"], 30.0)
        self.assertFalse(fable["met"])

    def test_on_track(self):
        # 80% over 3 work hours → 26.7 pp/h; need 5 pp/h → projected >> 100
        s = self.m.goal_status(self.goal, weekly_pct=80.0, elapsed_h=3.0,
                               shares={"fable": 0.6}, now_local=self.now)
        self.assertTrue(s["on_track"])
        self.assertGreater(s["projected_pct"], 100.0)
        self.assertTrue(s["model_share"]["fable"]["met"])

    def test_achieved(self):
        s = self.m.goal_status(self.goal, weekly_pct=100.0, elapsed_h=10.0,
                               shares=None, now_local=self.now)
        self.assertTrue(s["achieved"])
        self.assertTrue(s["on_track"])
        self.assertIsNone(s["required_pace"])

    def test_expired(self):
        late = datetime(2026, 7, 17, 13, 0, tzinfo=TZ)
        s = self.m.goal_status(self.goal, weekly_pct=60.0, elapsed_h=10.0,
                               shares=None, now_local=late)
        self.assertTrue(s["expired"])
        self.assertIsNone(s["projected_pct"])

    def test_overnight_deadline_counts_only_work_hours(self):
        # 19:00 → 10:00 next day: 1h tonight (19-20) + 3h tomorrow (7-10)
        now = datetime(2026, 7, 17, 19, 0, tzinfo=TZ)
        goal = {"target_pct": 100.0,
                "deadline": datetime(2026, 7, 18, 10, 0, tzinfo=TZ),
                "model_share": {}}
        s = self.m.goal_status(goal, weekly_pct=50.0, elapsed_h=10.0,
                               shares=None, now_local=now)
        self.assertAlmostEqual(s["hours_to_deadline"], 4.0)

    def test_no_usage_data(self):
        s = self.m.goal_status(self.goal, weekly_pct=None, elapsed_h=None,
                               shares=None, now_local=self.now)
        self.assertIsNone(s["on_track"])
        self.assertIsNone(s["projected_pct"])


if __name__ == "__main__":
    unittest.main()
