import datetime as dt
import plistlib
import types
import unittest
from unittest import mock
from pathlib import Path

import helpers
from mr.config import read_json, write_json
from mr.schedule import ScheduleError, _lock, plan, run_weekly, subscription_environment
from mr.preferences import PreferenceError


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = helpers.tmpdir()
        self.paths, self.cfg = helpers.init_home(self.tmp / "live")
        self.p = plan(self.paths, self.cfg, self.tmp / "weekly", timezone="America/Toronto", max_calls=4)

    def test_plan_has_no_install_and_never_writes_live_target(self):
        spec = read_json(self.p["plan"])
        self.assertFalse(self.p["installed"])
        self.assertEqual(self.p["maximum_engine_calls"], 12)
        self.assertEqual(spec["promotion"], "never automatic")
        self.assertFalse((self.paths.home / "CANDIDATE.json").exists())
        with Path(self.p["launchd_plist"]).open("rb") as fh:
            plist = plistlib.load(fh)
        self.assertFalse(plist["RunAtLoad"])
        self.assertEqual(plist["StartCalendarInterval"], {"Weekday": 1, "Hour": 9, "Minute": 0})
        self.assertEqual(plist["EnvironmentVariables"]["PATH"], spec["executable_path"])
        self.assertEqual(plan(self.paths, self.cfg, self.tmp / "weekly", timezone="America/Toronto", max_calls=4)["label"], self.p["label"])
        with self.assertRaises(ScheduleError):
            plan(self.paths, self.cfg, self.paths.home / "bad")
        with self.assertRaises(ScheduleError):
            plan(self.paths, self.cfg, self.paths.home / "missing" / ".." / "nested")

    def test_weekly_success_is_pending_review_and_blocks_duplicate_work(self):
        calls = []
        def run(cmd, **kwargs):
            calls.append(cmd)
            candidate = Path(cmd[cmd.index("--candidate-home") + 1])
            write_json(candidate / "CANDIDATE.json", {"status": "awaiting_human_review"})
            return types.SimpleNamespace(returncode=0)
        first = run_weekly(self.p["plan"], run_command=run, today=dt.date(2026, 10, 22))
        self.assertEqual(first["status"], "awaiting_human_review")
        self.assertIn("--max-calls", calls[0])
        self.assertEqual(calls[0][calls[0].index("--concurrency") + 1], "1")
        self.assertEqual(calls[0][calls[0].index("--train-before") + 1], "2026-10-22")
        second = run_weekly(self.p["plan"], run_command=run, today=dt.date(2026, 10, 29))
        self.assertEqual(second["status"], "awaiting_user")
        self.assertEqual(len(calls), 1)
        write_json(Path(first["candidate"]) / "CANDIDATE.json", {"status": "dismissed_by_user"})
        third = run_weekly(self.p["plan"], run_command=run, today=dt.date(2026, 10, 22))
        self.assertEqual(third["status"], "already_ran")
        self.assertEqual(len(calls), 1)

    def test_failure_and_incomplete_artifact_never_count_as_success(self):
        def incomplete(cmd, **kwargs):
            candidate = Path(cmd[cmd.index("--candidate-home") + 1])
            write_json(candidate / "CANDIDATE.json", {"status": "incomplete"})
            return types.SimpleNamespace(returncode=0)
        s = run_weekly(self.p["plan"], run_command=incomplete, today=dt.date(2026, 10, 22))
        self.assertEqual(s["status"], "failed")
        self.assertEqual(read_json(Path(self.p["plan"]).parent / "state" / "latest.json")["status"], "failed")

    def test_failed_launch_without_candidate_is_not_repeated_in_same_week(self):
        calls = []
        def failed(cmd, **kwargs):
            calls.append(cmd)
            return types.SimpleNamespace(returncode=2)
        self.assertEqual(run_weekly(self.p["plan"], run_command=failed, today=dt.date(2026, 10, 22))["status"], "failed")
        self.assertEqual(run_weekly(self.p["plan"], run_command=failed, today=dt.date(2026, 10, 22))["status"], "already_attempted")
        self.assertEqual(len(calls), 1)

    def test_kernel_lock_prevents_overlapping_job_and_releases(self):
        lock = self.tmp / "running.json"
        handle = _lock(lock)
        try:
            with self.assertRaisesRegex(ScheduleError, "already running"):
                _lock(lock)
        finally:
            handle.close()
        with _lock(lock):
            pass

    def test_weekly_environment_excludes_api_billing_and_endpoint_overrides(self):
        env = {"PATH": "/usr/bin", "HOME": "/home/example", "ANTHROPIC_API_KEY": "secret",
               "ANTHROPIC_AUTH_TOKEN": "secret", "ANTHROPIC_BASE_URL": "https://endpoint.example",
               "CLAUDE_CODE_USE_BEDROCK": "1", "CLAUDE_CODE_USE_VERTEX": "1", "OPENAI_API_KEY": "secret",
               "OPENAI_API_BASE": "https://endpoint.example", "AZURE_OPENAI_API_KEY": "secret"}
        self.assertEqual(subscription_environment(env), {"PATH": "/usr/bin", "HOME": "/home/example"})

    def test_plan_rejects_unrelated_or_symlinked_runner_without_writing_spec(self):
        original = self.paths.model / "profile.md"
        original.write_text("live model")
        for linked in (False, True):
            out = self.tmp / ("linked-output" if linked else "regular-output")
            out.mkdir()
            runner = out / "weekly.py"
            if linked:
                runner.symlink_to(original)
            else:
                runner.write_text("personal script")
            with self.assertRaises((PreferenceError, ScheduleError)):
                plan(self.paths, self.cfg, out)
            self.assertFalse((out / "weekly.json").exists())
            self.assertEqual(original.read_text(), "live model")
            self.assertEqual(runner.read_text(), "live model" if linked else "personal script")

    def test_plan_rejects_output_ancestor_and_tmp_symlinks(self):
        actual = self.tmp / "actual-output"
        actual.mkdir()
        linked = self.tmp / "linked-output"
        linked.symlink_to(actual, target_is_directory=True)
        with self.assertRaises(PreferenceError):
            plan(self.paths, self.cfg, linked / "weekly")
        self.assertEqual(list(actual.iterdir()), [])
        outside = self.tmp / "personal.json"
        outside.write_text("personal data")
        out = self.tmp / "tmp-output"
        out.mkdir()
        (out / "weekly.json.tmp").symlink_to(outside)
        with self.assertRaises(PreferenceError):
            plan(self.paths, self.cfg, out)
        self.assertEqual(outside.read_text(), "personal data")
        self.assertFalse((out / "weekly.py").exists())

    def test_weekly_rejects_symlinked_log_or_state_before_lock_or_launch(self):
        spec = read_json(self.p["plan"])
        state = Path(spec["state_dir"])
        state.mkdir()
        outside = self.paths.model / "profile.md"
        outside.write_text("live model")
        for name in ("2026-W43.log", "latest.json", "running.json"):
            linked = state / name
            linked.symlink_to(outside)
            with self.assertRaises(PreferenceError):
                run_weekly(self.p["plan"], run_command=lambda *_a, **_kw: self.fail("must not launch"), today=dt.date(2026, 10, 22))
            self.assertEqual(outside.read_text(), "live model")
            self.assertFalse((Path(spec["candidate_root"])).exists())
            linked.unlink()

    def test_kernel_lock_preserves_unrelated_regular_file(self):
        lock = self.tmp / "personal-lock.json"
        lock.write_text('private notes')
        with self.assertRaises((ScheduleError, ValueError)):
            _lock(lock)
        self.assertEqual(lock.read_text(), 'private notes')

    def test_real_cli_lookup_is_frozen_for_minimal_scheduler_environment(self):
        import os
        binary_dir = self.tmp / "user-bin"
        binary_dir.mkdir()
        binary = binary_dir / "claude"
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o700)
        with mock.patch.dict(os.environ, {"PATH": str(binary_dir)}, clear=False):
            planned = plan(self.paths, self.cfg, self.tmp / "real-cli-plan", engine="claude")
        spec = read_json(planned["plan"])
        self.assertEqual(spec["engine_executable"], str(binary.absolute()))
        self.assertEqual(spec["executable_path"].split(os.pathsep)[0], str(binary_dir.absolute()))
        captured = []
        def run(cmd, **kwargs):
            captured.append(kwargs["env"])
            candidate = Path(cmd[cmd.index("--candidate-home") + 1])
            write_json(candidate / "CANDIDATE.json", {"status": "awaiting_human_review"})
            return types.SimpleNamespace(returncode=0)
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin", "ANTHROPIC_API_KEY": "not-for-billing"}, clear=False):
            result = run_weekly(planned["plan"], run_command=run, today=dt.date(2026, 10, 22))
        self.assertEqual(result["status"], "awaiting_human_review")
        self.assertEqual(captured[0]["PATH"], spec["executable_path"])
        self.assertNotIn("ANTHROPIC_API_KEY", captured[0])

    def test_unavailable_cli_refuses_plan_before_creating_outputs(self):
        out = self.tmp / "missing-cli-plan"
        with mock.patch("mr.schedule.shutil.which", return_value=None):
            with self.assertRaisesRegex(ScheduleError, "executable is not available"):
                plan(self.paths, self.cfg, out, engine="claude")
        self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
