"""Weekly trace refresh: builder order, one-look gate, atomic swap, retries and pruning, without real training."""
import datetime as dt
import json
import os
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import init_home
from mr import experience as xp
from mr import trace_weekly as tw
from mr.schedule import ScheduleError


class FakeBuilder:
    """Stands in for intent_build.py: records each step and writes the files the runner inspects."""

    def __init__(self, ci_high=0.1, fail_step=None, cases=3):
        self.calls, self.ci_high, self.fail_step, self.cases = [], ci_high, fail_step, cases

    def __call__(self, argv, **kw):
        home, step = Path(argv[4]), argv[5]
        self.calls.append(step)
        if step == self.fail_step:
            return subprocess.CompletedProcess(argv, 2)
        home.mkdir(parents=True, exist_ok=True)
        if step == "evaluate":
            (home / "research").mkdir(exist_ok=True)
            (home / "research" / "test-results.json").write_text(json.dumps({
                "best_baseline": "context", "bootstrap": {"primary_is_baseline": False, "mrr": {
                    "ci_low": self.ci_high - 0.05, "ci_high": self.ci_high, "mean_diff": self.ci_high - 0.025,
                    "n_families": 12}}}), encoding="utf-8")
        if step == "refit":
            out = Path(argv[argv.index("--output") + 1])
            out.mkdir(parents=True)
            rows = [{"id": "c%d" % i, "input": {"seed": "build the page"}, "target": {"text": "keep going"},
                     "historical_response": {"assistant_text": ""}} for i in range(self.cases)]
            (out / "cases.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
            (out / "manifest.json").write_text("{}", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0)


class TraceWeeklyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(os.path.realpath(self.tmp.name))
        for name in ("claude", "codex", "code"):
            (self.root / name).mkdir()
        self.cache = self.root / "old-embedding-cache.sqlite"
        self.cache.write_bytes(b"cache")
        self.plan = tw.plan(self.root / "trace", "/usr/bin/python3",
                            ["claude=%s" % (self.root / "claude"), "codex=%s" % (self.root / "codex"),
                             "kimi=%s" % (self.root / "missing")],
                            [str(self.root / "code")], {"path": str(self.root / "enc"), "model": "m", "revision": "r"},
                            seed_cache=str(self.cache), weekday=6, hour=3, minute=30, timezone="America/Toronto")
        self.spec = Path(self.plan["plan"])

    def tearDown(self):
        self.tmp.cleanup()

    def run_week(self, day, builder):
        return tw.run(self.spec, today=day, run_command=builder)

    def test_plan_writes_files_only(self):
        self.assertFalse(self.plan["installed"])
        plist = plistlib.loads(Path(self.plan["launchd_plist"]).read_bytes())
        self.assertEqual(plist["StartCalendarInterval"], {"Weekday": 0, "Hour": 3, "Minute": 30})
        self.assertFalse(plist["RunAtLoad"])
        self.assertEqual(oct(os.stat(self.plan["runner"]).st_mode & 0o777), "0o700")
        self.assertFalse((self.root / "trace" / "current").exists())
        with self.assertRaises(ScheduleError):
            tw.plan(self.root / "trace", "/usr/bin/python3", ["claude=%s" % (self.root / "claude")], [],
                    {"path": "e", "model": "m", "revision": "other"})
        with self.assertRaises(ScheduleError):
            tw.plan(self.root / "other", "/usr/bin/python3", ["slack=/x"], [], {"path": "e", "model": "m", "revision": "r"})

    def test_successful_week_swaps_current_and_seeds_the_cache(self):
        fake = FakeBuilder()
        result = self.run_week(dt.date(2026, 11, 1), fake)
        self.assertEqual(result["status"], "swapped", result)
        self.assertEqual(fake.calls, ["ingest", "git", "prepare", "encode", "train", "seal", "evaluate", "refit"])
        current = self.root / "trace" / "current"
        self.assertTrue(current.is_symlink())
        self.assertEqual(os.path.realpath(current), str(self.root / "trace" / "candidates" / "2026-W44"))
        self.assertEqual(result["cache_from"], str(self.cache))
        self.assertTrue(result["fresh_look"]["pass"])
        state = json.loads((self.root / "trace" / "state" / "latest.json").read_text())
        self.assertEqual(state["status"], "swapped")
        again = self.run_week(dt.date(2026, 11, 1), FakeBuilder())
        self.assertEqual(again["status"], "already_ran")

    def test_missing_sources_are_skipped(self):
        argv = dict(tw.steps(json.loads(self.spec.read_text()), self.root / "s", self.root / "c"))["ingest"]
        joined = " ".join(argv)
        self.assertIn("claude=", joined)
        self.assertNotIn("kimi=", joined)

    def test_failed_gate_keeps_the_previous_candidate(self):
        self.run_week(dt.date(2026, 11, 1), FakeBuilder())
        fake = FakeBuilder(ci_high=-0.01)
        result = self.run_week(dt.date(2026, 11, 8), fake)
        self.assertEqual(result["status"], "kept_previous")
        self.assertNotIn("refit", fake.calls)
        self.assertFalse(result["fresh_look"]["pass"])
        self.assertEqual(os.path.realpath(self.root / "trace" / "current"),
                         str(self.root / "trace" / "candidates" / "2026-W44"))

    def test_failure_keeps_current_and_retries_from_scratch(self):
        self.run_week(dt.date(2026, 11, 1), FakeBuilder())
        failed = self.run_week(dt.date(2026, 11, 8), FakeBuilder(fail_step="train"))
        self.assertEqual((failed["status"], failed["failed_step"]), ("failed", "train"))
        self.assertEqual(os.path.realpath(self.root / "trace" / "current"),
                         str(self.root / "trace" / "candidates" / "2026-W44"))
        retry = self.run_week(dt.date(2026, 11, 8), FakeBuilder())
        self.assertEqual((retry["status"], retry["attempts"]), ("swapped", 2))

    def test_attempts_are_capped(self):
        for _ in range(tw.MAX_ATTEMPTS):
            self.run_week(dt.date(2026, 11, 8), FakeBuilder(fail_step="ingest"))
        capped = self.run_week(dt.date(2026, 11, 8), FakeBuilder())
        self.assertEqual(capped["reason"], "attempt limit reached this week")

    def test_old_studies_and_candidates_are_pruned_but_previous_kept(self):
        for day in (dt.date(2026, 11, 1), dt.date(2026, 11, 8), dt.date(2026, 11, 15)):
            self.assertEqual(self.run_week(day, FakeBuilder())["status"], "swapped")
        trace = self.root / "trace"
        self.assertEqual(sorted(p.name for p in (trace / "studies").iterdir()), ["2026-W46"])
        self.assertEqual(sorted(p.name for p in (trace / "candidates").iterdir()), ["2026-W45", "2026-W46"])

    def test_experience_summary_follows_the_swap(self):
        paths, cfg = init_home(self.root / "home")
        cfg["candidate"] = str(self.root / "trace" / "current")
        with mock.patch.dict(os.environ):
            os.environ.pop("MIND_READING_CANDIDATE", None)
            self.run_week(dt.date(2026, 11, 1), FakeBuilder(cases=3))
            self.assertEqual(xp.load_summary(paths, cfg)["replies"], 3)
            self.run_week(dt.date(2026, 11, 8), FakeBuilder(cases=5))
            self.assertEqual(xp.load_summary(paths, cfg)["replies"], 5)


if __name__ == "__main__":
    unittest.main()
