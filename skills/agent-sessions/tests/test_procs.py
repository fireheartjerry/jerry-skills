"""Process identity: start times must compare exactly on every platform, and ids
recorded in the older ps-lstart form must keep matching after an upgrade."""
import os
import sys
import unittest

from helpers import SKILL, ps_lstart_under

sys.path.insert(0, os.path.join(SKILL, "scripts"))
from agent_sessions import procs  # noqa: E402


class StartTimeTests(unittest.TestCase):
    def test_own_start_time_matches_itself_on_repeated_reads(self):
        pid = os.getpid()
        first = procs.ps_lstart(pid)
        self.assertTrue(first)
        for _ in range(20):
            self.assertIs(procs.start_matches(pid, first), True)
        self.assertTrue(procs.process_matches(pid, first))

    def test_linux_ids_use_exact_proc_ticks(self):
        pid = os.getpid()
        st = procs.proc_stat(pid)
        if st is None:
            self.skipTest("no /proc on this system")
        self.assertEqual(procs.ps_lstart(pid), "proc:%s" % st[1])
        self.assertIs(procs.start_matches(pid, "proc:%d" % (int(st[1]) + 1)), False)

    def test_legacy_ps_lstart_ids_still_match(self):
        pid = os.getpid()
        legacy = ps_lstart_under("C", pid)
        if not legacy:
            self.skipTest("ps lstart unavailable")
        self.assertIs(procs.start_matches(pid, legacy), True)
        self.assertFalse(procs.owner_gone("proc:python3:%d:%s" % (pid, legacy)))

    def test_unreadable_or_empty_start_is_unknown_not_a_mismatch(self):
        self.assertIsNone(procs.start_matches(os.getpid(), ""))
        self.assertFalse(procs.process_matches(os.getpid(), None))


if __name__ == "__main__":
    unittest.main()
