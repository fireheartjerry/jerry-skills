import json
import os
import plistlib
import unittest
from unittest import mock

import helpers
from mr import schedule
from mr.config import load_config


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = helpers.tmpdir()
        self.paths, self.cfg = helpers.init_home(self.tmp / "home")
        self.agents = self.tmp / "LaunchAgents"
        self.env = mock.patch.dict(os.environ, {"MIND_READING_LAUNCH_AGENTS_DIR": str(self.agents), "MIND_READING_NO_LAUNCHCTL": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_install_writes_a_weekly_low_priority_agent_for_this_home(self):
        res = schedule.install(self.paths, self.cfg, weekday=1, hour=7, minute=30, platform="darwin", log=helpers.quiet)
        self.assertTrue(res["installed"])
        target = self.agents / "local.mind-reading.refresh.plist"
        data = plistlib.loads(target.read_bytes())
        args = data["ProgramArguments"]
        self.assertEqual(args[1], "-I")
        self.assertTrue(args[2].endswith("scripts/mind_reading.py"))
        self.assertEqual(args[3:], ["--home", str(self.paths.home), "refresh", "--auto"])
        self.assertEqual(data["StartCalendarInterval"], {"Weekday": 1, "Hour": 7, "Minute": 30})
        self.assertEqual(data["StandardOutPath"], str(self.paths.logs / "schedule.log"))
        self.assertEqual((data["ProcessType"], data["LowPriorityIO"], data["RunAtLoad"]), ("Background", True, False))
        self.assertIn("/usr/bin", data["EnvironmentVariables"]["PATH"].split(":"))
        self.assertEqual(load_config(self.paths)["schedule"]["weekday"], 1)
        st = schedule.status(self.paths, load_config(self.paths), platform="darwin")
        self.assertTrue(st["installed"] and st["loaded"] and st["home_matches"])
        self.assertIn("weekly on Monday at 07:30", schedule.format_status_line(st))
        self.assertTrue(schedule.uninstall(self.paths, self.cfg, platform="darwin", log=helpers.quiet)["removed"])
        self.assertFalse(target.exists())

    def test_daily_dry_run_and_linux(self):
        res = schedule.install(self.paths, self.cfg, daily=True, dry_run=True, platform="darwin", log=helpers.quiet)
        self.assertFalse(res["installed"])
        self.assertFalse(self.agents.exists())
        self.assertNotIn("Weekday", plistlib.loads(res["body"].encode())["StartCalendarInterval"])
        lin = schedule.install(self.paths, self.cfg, hour=6, minute=5, platform="linux", log=helpers.quiet)
        self.assertFalse(lin["installed"])
        self.assertTrue(lin["cron"].startswith("5 6 * * 0 "))
        self.assertIn("refresh --auto >> ", lin["cron"])
        self.assertIn("cron (not installed by this tool)", schedule.format_status_line(schedule.status(self.paths, self.cfg, platform="linux")))

    def test_cli(self):
        env = {"MIND_READING_LAUNCH_AGENTS_DIR": str(self.agents), "MIND_READING_NO_LAUNCHCTL": "1"}
        r = helpers.run_cli(self.paths.home, "--json", "schedule", "install", "--dry-run", "--platform", "darwin", env_extra=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("refresh", json.loads(r.stdout)["body"])
        r = helpers.run_cli(self.paths.home, "schedule", "status", "--platform", "darwin", env_extra=env)
        self.assertIn("schedule: not installed", r.stdout)
        self.assertIn("auto refresh: never run", r.stdout)


if __name__ == "__main__":
    unittest.main()
