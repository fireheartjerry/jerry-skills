#!/usr/bin/env python3
"""Behavioral checks for the templates in references/long-running.md.

Every bash block must parse. The launch, identity, poll and stop blocks are
then run against staged states in a temp directory: a real background job for
the launch and stop blocks, a real process carrying a distinctive argv marker
for the identity block, and staged files plus live and dead PIDs for the poll
block (which also serves as the condition monitor's script). Stdlib only; spends no tokens; skips
when bash is absent. Works with bash 3.2 (macOS) and Linux bash.

Run:  python3 -I skills/high-agency/tests/test_templates.py
"""
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
LONG_RUNNING_MD = os.path.join(SKILL_DIR, "references", "long-running.md")

BASH = shutil.which("bash")
PKILL = shutil.which("pkill")
WORD_BUDGET = 1400
RUN_TIMEOUT = 60      # seconds; every template here finishes in well under that


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def put(path, text):
    with open(path, "w") as fh:
        fh.write(text)


def set_old(path):
    """Push a file's mtime far into the past (2001)."""
    os.utime(path, (1000000000, 1000000000))


def bash_blocks(text):
    return re.findall(r"```bash\n(.*?)```", text, re.S)


def fenced_blocks(text):
    """Every fenced block, whatever its language tag."""
    return re.findall(r"```[a-z]*\n(.*?)```", text, re.S)


def one_block(blocks, needle, label):
    hits = [b for b in blocks if needle in b]
    if len(hits) != 1:
        raise AssertionError("expected exactly one %s block (containing %r), found %d"
                             % (label, needle, len(hits)))
    return hits[0]


def bracketed(word):
    """The pgrep self-exclusion form of a word: first character in brackets."""
    return "[" + word[0] + "]" + word[1:]


def substitute_job(block, token, replacement, marker="job"):
    """Replace the token on every line marked with a trailing '# <marker>'
    comment, in both plain and bracketed form. Returns the new block and the
    number of lines changed."""
    out = []
    changed = 0
    for line in block.splitlines():
        if re.search(r"#\s*%s\s*$" % marker, line):
            new = line.replace(bracketed(token), bracketed(replacement))
            new = new.replace(token, replacement)
            if new != line:
                changed += 1
            line = new
        out.append(line)
    return "\n".join(out) + "\n", changed


def dead_pid():
    """A PID that belonged to a process which has already exited."""
    p = subprocess.Popen(["true"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    p.wait()
    return p.pid


def kill_tree(pid):
    """Terminate a background job and its children; ignore the already-dead."""
    if PKILL:
        subprocess.run([PKILL, "-P", str(pid)], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass


def alive(pid):
    """True while *pid* runs and is not a zombie (a reaped-later child of ours)."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout
    except OSError:
        return True
    return bool(out.strip()) and not out.strip().startswith("Z")


def run_script(script_text, cwd, timeout=RUN_TIMEOUT):
    path = os.path.join(cwd, "script-%s.sh" % uuid.uuid4().hex[:8])
    put(path, script_text)
    return subprocess.run([BASH, path], cwd=cwd, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=timeout)


class Templates(object):
    """Lazily parsed pieces of long-running.md shared by the test classes."""

    def __init__(self):
        self.text = read(LONG_RUNNING_MD)
        self.blocks = bash_blocks(self.text)
        self.launch = one_block(self.blocks, "nohup", "launch")
        self.identity = one_block(self.blocks, "pgrep -f", "identity")
        self.poll_block = one_block(self.blocks, "deadline=", "poll")
        self.stop = one_block(self.blocks, "pkill -P", "stop")
        m = re.search(r"^fail_re=.*$", self.poll_block, re.M)
        if not m:
            raise AssertionError("poll block has no fail_re= line")
        self.fail_re_line = m.group(0)
        # The script under test runs from fail_re= to the end of the block.
        self.poll = self.poll_block[self.poll_block.index("fail_re="):].rstrip() + "\n"

    def poll_script(self, work):
        """The poll loop with sleep shadowed and the deadline set to now."""
        loop = re.sub(r"\+ \d+ \)\)", "+ 0 ))", self.poll, count=1)
        if "+ 0 ))" not in loop:
            raise AssertionError("could not rewrite the deadline in the poll loop")
        return "sleep() { :; }\ncd '%s' || exit 97\n%s" % (work, loop)

    def artifact(self):
        m = re.search(r"\[ ! -e (\S+) \] \|\| mv", self.launch)
        if not m:
            raise AssertionError("launch block does not move an earlier artifact aside")
        return m.group(1)

    def launch_token(self):
        m = re.search(r"echo '([^']+)' > run\.cmd", self.launch)
        if not m:
            raise AssertionError("launch block does not record the command in run.cmd")
        return m.group(1)

    def identity_token(self):
        m = re.search(r"pgrep -f '\[(.)\]([^']*)'", self.identity)
        if not m:
            raise AssertionError("identity block has no bracketed pgrep pattern")
        return m.group(1) + m.group(2)


_T = None


def templates():
    global _T
    if _T is None:
        _T = Templates()
    return _T


class TestDocument(unittest.TestCase):
    """Text-level checks. These read the file directly so a block that fails
    to parse does not mask an unrelated finding."""

    def setUp(self):
        self.text = read(LONG_RUNNING_MD)

    def test_word_budget(self):
        words = len(self.text.split())
        self.assertLessEqual(words, WORD_BUDGET, "long-running.md is %d words" % words)

    def test_harness_table(self):
        text = self.text
        start = text.index("## What each harness offers")
        rest = text[start + 1:]
        m = re.search(r"^## ", rest, re.M)
        section = rest[:m.start()] if m else rest
        self.assertRegex(section, r"Claude Code \d+\.\d+(\.\d+)?", "no Claude Code version")
        self.assertRegex(section, r"Codex CLI \d+\.\d+(\.\d+)?", "no Codex CLI version")
        self.assertRegex(section, r"\d{4}-\d{2}-\d{2}", "no YYYY-MM-DD date")
        self.assertIn("missing from your tool list", section)
        rows = [l for l in section.splitlines() if l.startswith("|")]
        self.assertGreaterEqual(len(rows), 8, "table needs a header, a rule and six rows")
        header = [c.strip() for c in rows[0].strip("|").split("|")]
        self.assertEqual(header, ["Capability", "Claude Code", "Codex", "Plain shell"])
        self.assertRegex(rows[1], r"^\|(\s*:?-+:?\s*\|)+$", "second row must be the rule")
        body = rows[2:]
        self.assertGreaterEqual(len(body), 6)
        for row in body:
            cells = [c.strip() for c in row.strip("|").split("|")]
            self.assertEqual(len(cells), 4, "row has %d cells: %r" % (len(cells), row))
            for c in cells:
                self.assertTrue(c, "empty cell in %r" % row)
        names = " ".join(r.split("|")[1] for r in body).lower()
        for cap in ("checklist", "detached launch", "condition monitor",
                    "interval re-check", "scheduled wake-up", "stop a job", "headless"):
            self.assertIn(cap, names, "table lacks the %r row" % cap)
        for note in ("cannot see a local PID", "per-call timeout", "stock macOS"):
            self.assertIn(note, section, "note %r missing under the table" % note)

    def test_handoff_template(self):
        blocks = [b for b in fenced_blocks(self.text) if "Running:" in b]
        self.assertEqual(len(blocks), 1, "expected one handoff template")
        for field in ("Running:", "Log:", "Check:", "Done when:", "Failed when:",
                      "Expected finish:", "Next action", "Nothing is watching"):
            self.assertIn(field, blocks[0])

    def test_fail_re_is_explained(self):
        text = self.text
        t = templates()
        self.assertRegex(re.sub(r"\s+", " ", text),
                         r"failure prefix to `fail_re`, anchored with `\^`, regex characters escaped")
        why = text[text.index("Why each line is there"):]
        for needle in ("`fail_re`", "liveness exit", "whole seconds", "run.pid", "children"):
            self.assertIn(needle, why)
        self.assertNotIn("'Traceback|ERROR'", text, "the fixed grep must be gone")
        self.assertNotIn("-nt run.pid ]", t.poll, "bash 3.2 compares -nt in whole seconds")
        self.assertNotIn("until ", "\n".join(t.blocks), "one watch script (the poll block), not two")
        for guard in ("NO_FAIL_RE", "NO_PID", "BAD_FAIL_RE"):
            self.assertIn(guard, t.poll)

    def test_placeholders_are_explained(self):
        flat = re.sub(r"\s+", " ", self.text)
        self.assertIn("lines marked `# job` hold the example command", flat)
        self.assertIn("`# artifact`", flat)
        t = templates()
        art = t.artifact()
        for block in (t.launch, t.poll):
            for line in block.splitlines():
                if art in line:
                    self.assertRegex(line, r"#\s*artifact\s*$", "unmarked artifact line: %r" % line)

    def test_job_markers(self):
        t = templates()
        _, n = substitute_job(t.launch, t.launch_token(), "X")
        self.assertEqual(n, 2, "launch block: the run.cmd and nohup lines must carry '# job'")
        _, n = substitute_job(t.identity, t.identity_token(), "X")
        self.assertEqual(n, 1, "identity block: the pgrep line must carry '# job'")
        self.assertIn(t.identity_token(), t.launch_token(),
                      "identity and launch blocks must name the same job")


@unittest.skipUnless(BASH, "bash not available")
class TestBlocksParse(unittest.TestCase):
    def test_bash_n_every_block(self):
        t = templates()
        self.assertGreaterEqual(len(t.blocks), 4)
        work = tempfile.mkdtemp(prefix="ha-syntax-")
        try:
            for i, block in enumerate(t.blocks):
                path = os.path.join(work, "block%d.sh" % i)
                put(path, block)
                p = subprocess.run([BASH, "-n", path], capture_output=True, text=True)
                self.assertEqual(p.returncode, 0, "block %d: %s" % (i, p.stderr.strip()))
        finally:
            shutil.rmtree(work, ignore_errors=True)


@unittest.skipUnless(BASH, "bash not available")
class TestFailSignature(unittest.TestCase):
    """fail_re against the system grep: BSD grep on macOS, GNU grep on Linux."""

    POSITIVE = ["Traceback (most recent call last):", "ERROR: boom", "FATAL something",
                "CRITICAL: disk", "Error: no such file"]
    NEGATIVE = ["found 0 ERRORS in 12 files", "no error here", "ERRORS", "ERRORS: 0",
                "  ERROR indented is not a line start", "0 errors, 0 warnings"]

    def greps(self):
        found = []
        for cand in ("/usr/bin/grep", "/bin/grep", shutil.which("grep")):
            if cand and os.path.exists(cand) and cand not in found:
                found.append(cand)
        return found

    def test_signature_matches_failures_and_ignores_counts(self):
        t = templates()
        work = tempfile.mkdtemp(prefix="ha-grep-")
        try:
            log = os.path.join(work, "run.log")
            for grep in self.greps():
                for line, expect in ([(l, 0) for l in self.POSITIVE] +
                                     [(l, 1) for l in self.NEGATIVE]):
                    put(log, line + "\n")
                    script = '%s\n"%s" -qE "$fail_re" run.log\n' % (t.fail_re_line, grep)
                    p = run_script(script, work)
                    self.assertEqual(p.returncode, expect,
                                     "%s on %r: rc=%d %s" % (grep, line, p.returncode, p.stderr))
        finally:
            shutil.rmtree(work, ignore_errors=True)


@unittest.skipUnless(BASH, "bash not available")
class TestLaunchBlock(unittest.TestCase):
    def setUp(self):
        self.t = templates()
        self.work = tempfile.mkdtemp(prefix="ha-launch-")
        self.addCleanup(shutil.rmtree, self.work, True)

    def launch(self, job):
        block, n = substitute_job(self.t.launch, self.t.launch_token(), job)
        self.assertEqual(n, 2)
        p = run_script(block, self.work)
        pid_path = os.path.join(self.work, "run.pid")
        pid = int(read(pid_path).strip()) if os.path.exists(pid_path) else None
        if pid:
            self.addCleanup(kill_tree, pid)
        return p, pid

    def test_launch_starts_and_records_the_job(self):
        p, pid = self.launch('sh -c "echo started; sleep 20"')
        self.assertIsNotNone(pid, p.stderr)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(p.stdout.rstrip().endswith("STARTED"), p.stdout)
        os.kill(pid, 0)   # raises if the job is not alive
        self.assertIn("started", read(os.path.join(self.work, "run.log")))
        self.assertIn("echo started", read(os.path.join(self.work, "run.cmd")))
        self.assertIn("started", p.stdout, "the block must print the head of the log")

    def test_launch_moves_a_stale_artifact_aside(self):
        art = os.path.join(self.work, self.t.artifact())
        os.makedirs(os.path.dirname(art))
        put(art, '{"stale": true}')
        p, pid = self.launch('sh -c "sleep 20"')
        self.assertFalse(os.path.exists(art), "a stale artifact left in place reads as DONE")
        moved = [n for n in os.listdir(os.path.dirname(art)) if n.startswith(os.path.basename(art) + ".prev")]
        self.assertEqual(len(moved), 1, os.listdir(os.path.dirname(art)))
        poll = run_script(self.t.poll_script(self.work), self.work)
        self.assertEqual(poll.stdout.strip(), "TIMEOUT", "a live job with no new artifact is not done")

    def test_launch_reports_a_job_that_died(self):
        p, _ = self.launch('sh -c "echo boom; exit 3"')
        self.assertTrue(p.stdout.rstrip().endswith("NOT_RUNNING"), p.stdout)

    def test_fast_job_in_the_launch_second_is_done(self):
        # bash 3.2 compares -nt in whole seconds: an artifact written 50 ms
        # after run.pid used to read as EXITED_NO_RESULT.
        p, _ = self.launch('sh -c "mkdir -p out; echo {} > %s"' % self.t.artifact())
        poll = run_script(self.t.poll_script(self.work), self.work)
        self.assertEqual(poll.stdout.strip(), "DONE", poll.stdout + poll.stderr)


@unittest.skipUnless(BASH and PKILL, "bash and pkill needed")
class TestStopBlock(unittest.TestCase):
    def test_stop_kills_the_job_and_its_children(self):
        t = templates()
        work = tempfile.mkdtemp(prefix="ha-stop-")
        self.addCleanup(shutil.rmtree, work, True)
        marker = "ha-stop-%s" % uuid.uuid4().hex[:12]
        block, _ = substitute_job(t.launch, t.launch_token(),
                                  'sh -c "sleep 31 & echo \\$! > child.pid; wait; : %s"' % marker)
        p = run_script(block, work)
        self.assertIn("STARTED", p.stdout, p.stdout + p.stderr)
        pid = int(read(os.path.join(work, "run.pid")).strip())
        child = int(read(os.path.join(work, "child.pid")).strip())
        self.addCleanup(kill_tree, pid)
        self.addCleanup(lambda: os.kill(child, signal.SIGKILL) if alive(child) else None)
        self.assertTrue(alive(pid) and alive(child))
        run_script(t.stop, work)
        deadline = time.time() + 5
        while time.time() < deadline and (alive(pid) or alive(child)):
            time.sleep(0.1)
        self.assertFalse(alive(pid), "the job survived the stop block")
        self.assertFalse(alive(child), "the job's child survived the stop block")


@unittest.skipUnless(BASH and shutil.which("pgrep") and shutil.which("ps"),
                     "bash, pgrep and ps needed")
class TestIdentityBlock(unittest.TestCase):
    def setUp(self):
        self.t = templates()
        self.work = tempfile.mkdtemp(prefix="ha-ident-")
        self.addCleanup(shutil.rmtree, self.work, True)

    def test_identity_check_finds_only_the_job(self):
        marker = "ha-ident-%s" % uuid.uuid4().hex[:12]
        # Two commands so the shell forks sleep instead of exec'ing it, which
        # keeps the marker in this process's argv for ps and pgrep to see.
        proc = subprocess.Popen(["sh", "-c", "sleep 20; : %s" % marker],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        self.addCleanup(proc.wait)          # cleanups run last-in first-out:
        self.addCleanup(kill_tree, proc.pid)  # kill first, then reap
        put(os.path.join(self.work, "run.pid"), "%d\n" % proc.pid)
        put(os.path.join(self.work, "run.cmd"), "sh -c 'sleep 20; : %s'\n" % marker)
        block, n = substitute_job(self.t.identity, self.t.identity_token(), marker)
        self.assertEqual(n, 1)
        p = run_script(block, self.work)
        self.assertEqual(p.returncode, 0, p.stderr)
        lines = [l.strip() for l in p.stdout.splitlines() if l.strip()]
        ps_lines = [l for l in lines if not l.isdigit()]
        pgrep_pids = sorted(int(l) for l in lines if l.isdigit())
        self.assertTrue(any(marker in l for l in ps_lines), "ps did not print the marker: %r" % lines)
        self.assertEqual(pgrep_pids, [proc.pid],
                         "pgrep must find exactly the job (pid %d), not the test shell: %r"
                         % (proc.pid, pgrep_pids))
        self.assertNotIn(os.getpid(), pgrep_pids)

    def test_identity_check_is_quiet_with_no_pid_file(self):
        block, _ = substitute_job(self.t.identity, self.t.identity_token(),
                                  "ha-absent-%s" % uuid.uuid4().hex[:12])
        p = run_script(block, self.work)
        self.assertEqual(p.stdout.strip(), "")
        self.assertEqual(p.stderr.strip(), "", "missing run.pid must not print an error")


@unittest.skipUnless(BASH, "bash not available")
class TestPollLoop(unittest.TestCase):
    """The poll block, which is also the condition monitor's script: exactly
    one output line for every staged state."""

    def setUp(self):
        self.t = templates()
        self.work = tempfile.mkdtemp(prefix="ha-poll-")
        self.addCleanup(shutil.rmtree, self.work, True)
        self.result = os.path.join(self.work, self.t.artifact())
        os.makedirs(os.path.dirname(self.result))
        self.pid = os.path.join(self.work, "run.pid")
        self.log = os.path.join(self.work, "run.log")
        self.script = self.t.poll_script(self.work)

    def run_loop(self, script=None):
        p = run_script(script or self.script, self.work)
        self.assertEqual(p.stderr.strip(), "", "the loop must stay quiet on stderr: %s" % p.stderr)
        self.assertEqual(len(p.stdout.split()), 1, "exactly one output line: %r" % p.stdout)
        return p.stdout.strip()

    def stage(self, pid, result=False, log=None):
        """pid: a live or dead PID, or None for no run.pid. result: whether
        this run's artifact exists. log: text or None."""
        for path in (self.result, self.log, self.pid):
            if os.path.exists(path):
                os.unlink(path)
        if pid is not None:
            put(self.pid, "%d\n" % pid)
        if result:
            put(self.result, "{}")
        if log is not None:
            put(self.log, log)

    def test_live_job_without_artifact_times_out(self):
        self.stage(os.getpid(), log="")
        self.assertEqual(self.run_loop(), "TIMEOUT")

    def test_fresh_artifact_is_done(self):
        self.stage(os.getpid(), result=True, log="")
        self.assertEqual(self.run_loop(), "DONE")

    def test_dead_job_without_result_or_log(self):
        self.stage(dead_pid())
        self.assertEqual(self.run_loop(), "EXITED_NO_RESULT")

    def test_live_job_without_log_keeps_waiting_quietly(self):
        self.stage(os.getpid())
        self.assertEqual(self.run_loop(), "TIMEOUT")

    def test_traceback_fails(self):
        self.stage(os.getpid(), log="Traceback (most recent call last):\n  File x\n")
        self.assertEqual(self.run_loop(), "FAILED")

    def test_error_at_line_start_fails(self):
        self.stage(os.getpid(), log="step 1 ok\nERROR: boom\n")
        self.assertEqual(self.run_loop(), "FAILED")

    def test_benign_counts_do_not_fail(self):
        self.stage(os.getpid(), log="found 0 ERRORS in 12 files\nno error here\n")
        self.assertEqual(self.run_loop(), "TIMEOUT")

    def test_dead_job_with_fresh_result_is_done(self):
        self.stage(dead_pid(), result=True)
        self.assertEqual(self.run_loop(), "DONE")

    def test_failure_wins_over_timeout_but_not_over_result(self):
        # The artifact is checked before the log, so a job that logged an
        # error line and still produced its result reports DONE.
        self.stage(os.getpid(), result=True, log="ERROR: retried once\n")
        self.assertEqual(self.run_loop(), "DONE")

    def test_missing_run_pid_stops_instead_of_reading_dead_or_done(self):
        # The run_in_background path once never wrote run.pid: the loop read
        # that as a dead job, or as DONE beside a leftover artifact.
        for result in (False, True):
            self.stage(None, result=result, log="")
            self.assertEqual(self.run_loop(), "NO_PID", "result=%r" % result)
        put(self.pid, "")
        self.assertEqual(self.run_loop(), "NO_PID", "an empty run.pid is no PID")

    def test_unset_fail_re_stops_loudly(self):
        # An empty pattern matches every line; the guard must stop the loop
        # with a named line instead of reporting FAILED for a healthy job.
        self.stage(os.getpid(), log="benchmark: 10/240s, 0 ERRORS\n")
        script = self.script.replace(self.t.fail_re_line, "unset fail_re", 1)
        self.assertIn("unset fail_re", script)
        self.assertEqual(self.run_loop(script), "NO_FAIL_RE")

    def test_malformed_fail_re_stops_loudly(self):
        # grep exits 2 on a bad regex, which `if grep` reads as "no failure".
        self.stage(os.getpid(), log="FATAL (code 7): disk full\n")
        for bad in ("^FATAL (code", "[unclosed"):
            script = self.script.replace(self.t.fail_re_line, "fail_re='%s'" % bad, 1)
            self.assertEqual(self.run_loop(script), "BAD_FAIL_RE", bad)
        fixed = self.script.replace(self.t.fail_re_line, "fail_re='^FATAL \\(code'", 1)
        self.assertEqual(self.run_loop(fixed), "FAILED", "the escaped form must still match")


if __name__ == "__main__":
    unittest.main(verbosity=1)
