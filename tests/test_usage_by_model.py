import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module():
    spec = importlib.util.spec_from_file_location(
        "usage_by_model", ROOT / "usage_by_model.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def assistant_line(msg_id, model, ts, inp=0, out=0, cw=0, cr=0):
    return json.dumps({
        "type": "assistant",
        "timestamp": ts,
        "message": {
            "id": msg_id,
            "model": model,
            "usage": {
                "input_tokens": inp,
                "output_tokens": out,
                "cache_creation_input_tokens": cw,
                "cache_read_input_tokens": cr,
            },
        },
    })


class ModelFamilyTest(unittest.TestCase):
    def test_families(self):
        m = load_module()
        self.assertEqual(m.model_family("claude-fable-5"), "fable")
        self.assertEqual(m.model_family("claude-opus-4-8"), "opus")
        self.assertEqual(m.model_family("claude-sonnet-5"), "sonnet")
        self.assertEqual(m.model_family("claude-haiku-4-5-20251001"), "haiku")
        self.assertEqual(m.model_family("<synthetic>"), "other")
        self.assertEqual(m.model_family(None), "other")


class WeightedBurnTest(unittest.TestCase):
    def test_weights(self):
        m = load_module()
        # input + 5x output + 1.25x cache_write + 0.1x cache_read
        self.assertEqual(m.weighted_burn(100, 100, 100, 100), 100 + 500 + 125 + 10)


class UsageByModelTest(unittest.TestCase):
    def setUp(self):
        self.module = load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.projects = Path(self.tmp.name) / "projects"
        (self.projects / "proj-a").mkdir(parents=True)
        self.cache = Path(self.tmp.name) / "cache.json"
        self.now = datetime.now(timezone.utc)
        self.since = (self.now - timedelta(days=7)).astimezone()

    def tearDown(self):
        self.tmp.cleanup()

    def scan(self):
        return self.module.usage_by_model(
            self.since, projects_dir=self.projects, cache_file=self.cache)

    def write_transcript(self, name, lines):
        (self.projects / "proj-a" / name).write_text("\n".join(lines) + "\n")

    def iso(self, **delta):
        return (self.now - timedelta(**delta)).isoformat().replace("+00:00", "Z")

    def test_groups_by_family_and_weights(self):
        self.write_transcript("s1.jsonl", [
            assistant_line("m1", "claude-fable-5", self.iso(hours=1), out=100),
            assistant_line("m2", "claude-opus-4-8", self.iso(hours=2), out=300),
        ])
        fams = self.scan()
        self.assertEqual(fams["fable"]["burn"], 500.0)
        self.assertEqual(fams["opus"]["burn"], 1500.0)
        self.assertAlmostEqual(fams["fable"]["share"], 0.25)
        self.assertAlmostEqual(fams["opus"]["share"], 0.75)
        self.assertEqual(fams["_total"]["burn"], 2000.0)

    def test_dedups_message_id_keeps_largest(self):
        # Streaming rewrites the same message id across lines; count it once.
        self.write_transcript("s1.jsonl", [
            assistant_line("m1", "claude-fable-5", self.iso(hours=1), out=10),
            assistant_line("m1", "claude-fable-5", self.iso(hours=1), out=40),
            assistant_line("m1", "claude-fable-5", self.iso(hours=1), out=40),
        ])
        fams = self.scan()
        self.assertEqual(fams["fable"]["output"], 40)
        self.assertEqual(fams["fable"]["burn"], 200.0)

    def test_ignores_records_before_window(self):
        self.write_transcript("s1.jsonl", [
            assistant_line("m1", "claude-fable-5", self.iso(days=30), out=100),
            assistant_line("m2", "claude-fable-5", self.iso(hours=1), out=7),
        ])
        fams = self.scan()
        self.assertEqual(fams["fable"]["output"], 7)

    def test_ignores_non_assistant_and_empty_usage(self):
        self.write_transcript("s1.jsonl", [
            json.dumps({"type": "user", "timestamp": self.iso(hours=1)}),
            assistant_line("m0", "claude-fable-5", self.iso(hours=1)),  # all-zero usage
            assistant_line("m1", "claude-sonnet-5", self.iso(hours=1), inp=3),
        ])
        fams = self.scan()
        self.assertNotIn("fable", fams)
        self.assertEqual(fams["sonnet"]["input"], 3)

    def test_cache_reused_and_invalidated_on_change(self):
        self.write_transcript("s1.jsonl", [
            assistant_line("m1", "claude-fable-5", self.iso(hours=1), out=100),
        ])
        first = self.scan()
        self.assertEqual(first["fable"]["output"], 100)
        # Rewrite with more data → sig changes → reparse picks it up
        self.write_transcript("s1.jsonl", [
            assistant_line("m1", "claude-fable-5", self.iso(hours=1), out=100),
            assistant_line("m2", "claude-fable-5", self.iso(hours=1), out=100),
        ])
        second = self.scan()
        self.assertEqual(second["fable"]["output"], 200)

    def test_empty_window(self):
        fams = self.scan()
        self.assertEqual(fams["_total"]["burn"], 0)
        self.assertEqual(
            self.module.format_split(fams), ["no usage found in window"])


if __name__ == "__main__":
    unittest.main()
