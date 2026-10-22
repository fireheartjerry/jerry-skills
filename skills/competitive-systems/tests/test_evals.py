"""Offline tests for evals/: fixtures, grader and runner dry run. No network, no tokens."""
import filecmp
import importlib.util
import json
import os
import shutil
import re
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EVALS = os.path.join(ROOT, "evals")
MAKE = os.path.join(EVALS, "fixtures", "make_fixtures.py")
GRADE = os.path.join(EVALS, "grade.py")
RUN = os.path.join(EVALS, "run_eval.py")


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run(*argv, **kw):
    return subprocess.run([sys.executable, "-I"] + list(argv), stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True, **kw)


def tree(root):
    out = []
    for dirpath, dirnames, names in os.walk(root):
        dirnames.sort()
        for n in sorted(names):
            rel = os.path.relpath(os.path.join(dirpath, n), root)
            if rel.replace(os.sep, "/").endswith(".git/index"):
                continue                       # records file timestamps by design
            out.append(rel)
    return out


class FixturesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.a = os.path.join(cls.tmp.name, "a")
        p = run(MAKE, "--out", cls.a)
        assert p.returncode == 0, p.stderr

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_deterministic(self):
        b = os.path.join(self.tmp.name, "b")
        p = run(MAKE, "--out", b, "--only", "ci-durations,registry,packer,perf-history,suites,monorepo")
        self.assertEqual(p.returncode, 0, p.stderr)
        for name in ("ci-durations", "registry", "packer", "perf-history", "suites", "monorepo"):
            ta, tb = tree(os.path.join(self.a, name)), tree(os.path.join(b, name))
            self.assertEqual(ta, tb, name)
            _, mismatch, errors = filecmp.cmpfiles(os.path.join(self.a, name), os.path.join(b, name), ta, shallow=False)
            self.assertEqual((mismatch, errors), ([], []), name)

    def test_every_scenario_fixture_exists(self):
        with open(os.path.join(EVALS, "scenarios.json")) as f:
            for s in json.load(f):
                self.assertTrue(os.path.isdir(os.path.join(self.a, s["fixture"])), s["fixture"])

    def test_durations_facts(self):
        with open(os.path.join(self.a, "ci-durations", "durations.json")) as f:
            d = json.load(f)
        jobs = d["jobs"]
        self.assertEqual(len(jobs), 310)
        self.assertEqual(sum(jobs.values()), 160 * 60)
        self.assertEqual(sorted(jobs.values())[-2:], [840, 840])
        self.assertEqual([k for k, v in jobs.items() if v <= 0], ["test_api_002"])
        self.assertTrue(all(v >= 5 for k, v in jobs.items() if k != "test_api_002"))

    def test_sizes(self):
        a = self.a
        self.assertEqual(len(os.listdir(os.path.join(a, "registry", "registry"))), 400)
        with open(os.path.join(a, "tiny-patterns", "patterns.txt")) as f:
            self.assertEqual(len(f.read().splitlines()), 40)
        with open(os.path.join(a, "tiny-patterns", "nightly.log")) as f:
            self.assertEqual(len(f.read().splitlines()), 2000)
        with open(os.path.join(a, "perf-history", "perf.csv")) as f:
            rows = f.read().splitlines()[1:]
        self.assertEqual(len(rows), 60)
        vals = [float(r.split(",")[1]) for r in rows]
        before, after = sorted(vals[:36])[18], sorted(vals[36:])[12]
        self.assertAlmostEqual(after / before, 1.12, delta=0.03)
        self.assertGreater(vals[11], 1.25 * before)          # the c12 outlier
        with open(os.path.join(a, "suites", "suites.json")) as f:
            su = json.load(f)
        self.assertEqual(len(su["optional"]), 40)
        self.assertEqual(su["pipeline_budget_seconds"] - sum(m["p50_seconds"] for m in su["mandatory"]), 1500)
        with open(os.path.join(a, "hot-function", "profile.txt")) as f:
            self.assertIn("12.01%  example.com/indexer.normalizeKeys", f.read())

    def test_packer_overflows_and_drops_test_command(self):
        root = os.path.join(self.a, "packer")
        p = run(os.path.join(root, "packer.py"), os.path.join(root, "task.txt"), os.path.join(root, "repo"))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertGreaterEqual(len(p.stdout), 60000 * 4)
        self.assertNotIn("pytest repo/tests", p.stdout)
        with open(os.path.join(root, "repo", "svc", "pages.py")) as f:
            lines = f.read().splitlines()
        start = next(i for i, ln in enumerate(lines, 1) if ln.startswith("def paginate"))
        self.assertLess(start, 120)
        self.assertGreater(start + 8, 120)                   # straddles the 120-line chunk edge

    def test_example_files_and_privacy(self):
        for n in ("events.py", "gen_events.py", "dedup_before.py", "dedup_after.py",
                  "precondition.py", "precondition_sentinel.py"):
            self.assertTrue(os.path.isfile(os.path.join(self.a, "example-a", n)), n)
        self.assertTrue(os.path.isfile(os.path.join(self.a, "example-b", "ci-runs.json")))
        bad = re.compile(r"/Us" r"ers/|/ho" r"me/|[\w.+-]+@[\w-]+\.[a-z]{2,}")
        for dirpath, dirnames, names in os.walk(self.a):
            dirnames[:] = [d for d in dirnames if d != ".git"]
            for n in names:
                with open(os.path.join(dirpath, n), encoding="utf-8") as f:
                    m = bad.search(f.read())
                self.assertIsNone(m, "%s: %s" % (os.path.join(dirpath, n), m and m.group(0)))

    def test_unknown_fixture_is_usage_error(self):
        p = run(MAKE, "--out", os.path.join(self.tmp.name, "c"), "--only", "nope")
        self.assertEqual(p.returncode, 2)
        self.assertIn("unknown fixture", p.stderr)


TRANSCRIPT = """# skill / S-C / s1

status: ok
work: %s
model: test-model
runner: claude
user: my PR makes the report step 8%% faster

### tool_use Bash: python3 /x/scripts/dev.py aa --runs 20 -- python3 before.py

### tool_result
```
IQR in tool output only
```

### assistant
Medians 3.1 s vs 3.0 s.

### Verdict heading written by the model
### tool_use Write: /w/after.py
```
total = sum(set(rows))
```

### assistant
NO DETECTABLE CHANGE: ratio 1.02x (95%% CI 0.97x..1.08x).

### result: success, turns=3, cost_usd=0.25, duration_ms=1000
"""


class GradeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.g = load("cs_grade", GRADE)
        with open(os.path.join(EVALS, "scenarios.json")) as f:
            self.sc = dict((s["id"], s) for s in json.load(f))

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, work="none"):
        d = os.path.join(self.tmp.name, "runs", "skill")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "S-C.s1.md")
        with open(path, "w") as f:
            f.write(TRANSCRIPT % work)
        return path

    def test_parse_and_scopes(self):
        t = self.g.parse_transcript(self.write())
        self.assertEqual((t["arm"], t["sid"], t["seed"], t["cost_usd"]), ("skill", "S-C", 1, 0.25))
        self.assertEqual(len(self.g.scope_texts(t, "text")), 2)
        self.assertIn("NO DETECTABLE", self.g.scope_texts(t, "final")[0])
        self.assertIn("Verdict heading", self.g.scope_texts(t, "text")[0])   # unknown ### stays in the body
        self.assertEqual(self.g.scope_texts(t, "write_paths"), ["/w/after.py"])
        self.assertIn("sum(set(rows))", self.g.scope_texts(t, "edits")[0])
        self.assertNotIn("IQR", " ".join(self.g.scope_texts(t, "all")))       # tool output is not the model's words

    def test_score_counts_musts_minus_failures(self):
        t = self.g.parse_transcript(self.write())
        r = self.g.grade_one(t, self.sc["S-C"])
        self.assertIn("aa", r["musts_hit"])
        self.assertIn("ci", r["musts_hit"])
        self.assertIn("no-change", r["musts_hit"])
        self.assertNotIn("iqr", r["musts_hit"])
        self.assertEqual(r["failures_hit"], [])
        self.assertEqual(r["score"], len(r["musts_hit"]))
        self.assertIn("95% CI", r["evidence"]["ci"])

    def test_failure_probe_subtracts(self):
        s = dict(self.sc["S-J"])
        t = self.g.parse_transcript(self.write())
        r = self.g.grade_one(t, s)
        self.assertIn("dict-set", r["failures_hit"])
        self.assertEqual(r["score"], len(r["musts_hit"]) - len(r["failures_hit"]))

    def test_command_probe(self):
        work = os.path.join(self.tmp.name, "work")
        os.makedirs(os.path.join(work, "node_modules"))
        t = self.g.parse_transcript(self.write(work))
        state, ev = self.g.apply_probe(t, {"id": "x", "kind": "command", "command": "test -d node_modules", "must": False})
        self.assertEqual(state, "hit")
        t2 = self.g.parse_transcript(self.write("/nonexistent-dir"))
        self.assertEqual(self.g.apply_probe(t2, {"id": "x", "kind": "command", "command": "true", "must": True})[0], "skipped")

    def test_cli_writes_md_and_json_and_merge(self):
        path = self.write()
        out = os.path.join(self.tmp.name, "grades.md")
        p = run(GRADE, "--transcripts", os.path.join(self.tmp.name, "runs"), "--out", out)
        self.assertEqual(p.returncode, 0, p.stderr)
        with open(out) as f:
            md = f.read()
        self.assertIn("| S-C is-8-percent-real", md)
        self.assertIn("not run", md)                          # every other cell
        with open(out[:-3] + ".json") as f:
            self.assertEqual(json.load(f)["schema"], "cs-evals-grades/1")
        p = run(GRADE, "--prompt", "S-C", path)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("M1: yes|no", p.stdout)
        self.assertIn("NO DETECTABLE CHANGE", p.stdout)
        n_m, n_f = len(self.sc["S-C"]["musts"]), len(self.sc["S-C"]["failure_modes"])
        reply = "\n".join(["M%d: yes | \"q\"" % i for i in range(1, n_m + 1)] +
                          ["F1: yes | \"q\""] + ["F%d: no | none" % i for i in range(2, n_f + 1)])
        with open(path + ".grade.txt", "w") as f:
            f.write(reply + "\nSCORE: x\n")
        merged = os.path.join(self.tmp.name, "merged.json")
        p = run(GRADE, "--merge", out[:-3] + ".json", "--out", merged)
        self.assertEqual(p.returncode, 0, p.stderr)
        with open(merged) as f:
            row = json.load(f)["transcripts"][0]
        self.assertEqual(row["model_score"], n_m - 1)

    def test_outcome_score_leaves_out_skill_tool_checks_and_leaks_are_flagged(self):
        t = self.g.parse_transcript(self.write())
        r = self.g.grade_one(t, self.sc["S-C"])
        tool = [p["id"] for p in self.sc["S-C"]["probes"] if p.get("requires_skill_tool")]
        self.assertEqual(sorted(tool), ["aa", "measures"])
        self.assertEqual(r["outcome_score"],
                         len([p for p in r["musts_hit"] if p not in tool]) - len(r["failures_hit"]))
        self.assertIsNone(r["touched_skill"])                 # skill arm: not checked
        t["arm"] = "none"
        self.assertIsNone(self.g.grade_one(t, self.sc["S-C"])["touched_skill"])  # /x/scripts/dev.py
        t["sections"] = [("tool_use Bash: cat ~/.claude/skills/x/SKILL.md", "")]
        self.assertIn("SKILL.md", self.g.grade_one(t, self.sc["S-C"])["touched_skill"])
        t["sections"] = [("tool_use Bash: ls", "")]
        self.assertIsNone(self.g.grade_one(t, self.sc["S-C"])["touched_skill"])
        n_m = len(self.sc["S-C"]["musts"])
        reply = "\n".join("M%d: yes | q" % i for i in range(1, n_m + 1))
        g = self.g.parse_grade(reply, self.sc["S-C"])
        self.assertEqual(g["score"] - g["outcome_score"], len(self.sc["S-C"]["skill_tool_musts"]))

    def test_incomplete_model_grade_is_not_scored(self):
        g = self.g.parse_grade("M1: yes | \"q\"\n", self.sc["S-C"])
        self.assertFalse(g["complete"])

    def test_one_mode_required(self):
        p = run(GRADE)
        self.assertEqual(p.returncode, 2)
        self.assertIn("exactly one", p.stderr)


class RunEvalTest(unittest.TestCase):
    def test_dry_run_lists_every_scenario_and_spends_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "out")
            env = dict(os.environ, CLAUDE_BIN=os.path.join(tmp, "no-such-claude"), PATH="/usr/bin:/bin")
            p = run(RUN, "--dry-run", "--out", out, env=env)
            self.assertEqual(p.returncode, 0, p.stderr)
            with open(os.path.join(EVALS, "scenarios.json")) as f:
                k = len(json.load(f))
            self.assertGreaterEqual(k, 12)
            lines = [ln for ln in p.stdout.splitlines() if ln.startswith("$ ")]
            self.assertEqual(len(lines), 4 * k)
            self.assertIn("%d runs: %d scenario(s) x 2 arm(s) x 2 seed(s)" % (4 * k, k), p.stdout)
            self.assertEqual(sum("--append-system-prompt" in ln for ln in lines), 2 * k)
            self.assertTrue(all("--disable-slash-commands" in ln for ln in lines))
            self.assertFalse(os.path.exists(out))             # nothing written
            p = run(RUN, "--dry-run", "--trigger", "--out", out, env=env)
            self.assertEqual(p.returncode, 0, p.stderr)
            with open(os.path.join(EVALS, "trigger.json")) as f:
                trig = json.load(f)
            n = len(trig["should_fire"]) + len(trig["should_not_fire"])
            self.assertEqual(sum(ln.startswith("$ ") for ln in p.stdout.splitlines()), n)
            self.assertFalse(os.path.exists(out))

    def test_missing_cli_is_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, CLAUDE_BIN=os.path.join(tmp, "no-such-claude"), PATH="/usr/bin:/bin")
            p = run(RUN, "--out", os.path.join(tmp, "o"), "--scenarios", "S-C", "--seeds", "1", env=env)
            self.assertEqual(p.returncode, 3)
            self.assertIn("CLI not found", p.stderr)

    def test_prompt_is_well_formed_and_codex_home_is_isolated(self):
        r = load("cs_run_eval", RUN)
        self.assertEqual(r.user_prompt("make it fast"),
                         "make it fast.\n\nThe relevant files are in the current directory.")
        self.assertTrue(r.user_prompt("is this real?").startswith("is this real?\n\n"))
        with tempfile.TemporaryDirectory() as tmp:
            real = os.path.join(tmp, "real")
            os.makedirs(os.path.join(real, "skills", "competitive-systems"))
            for name in ("auth.json", "AGENTS.md", "config.toml"):
                with open(os.path.join(real, name), "w") as f:
                    f.write("x")
            old = os.environ.get("CODEX_HOME")
            os.environ["CODEX_HOME"] = real
            try:
                home = r.isolated_codex_home()
            finally:
                if old is None:
                    del os.environ["CODEX_HOME"]
                else:
                    os.environ["CODEX_HOME"] = old
            try:
                self.assertEqual(os.listdir(home), ["auth.json"])
                self.assertEqual(os.stat(home).st_mode & 0o777, 0o700)
                self.assertTrue(os.path.realpath(home).startswith(
                    os.path.realpath(tempfile.gettempdir())))
            finally:
                shutil.rmtree(home, ignore_errors=True)

    def test_live_runs_need_yes_and_stop_at_the_total_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = os.path.join(tmp, "claude")
            with open(fake, "w") as f:
                f.write("#!/bin/sh\nexit 0\n")
            os.chmod(fake, 0o755)
            env = dict(os.environ, CLAUDE_BIN=fake)
            p = run(RUN, "--out", os.path.join(tmp, "o"), "--scenarios", "S-C", "--seeds", "1",
                    env=env)
            self.assertEqual(p.returncode, 2, p.stderr)
            self.assertIn("worst case 1.50 USD (2 runs x 0.75 USD per-run cap)", p.stdout)
            self.assertIn("--yes", p.stderr)
            env = dict(os.environ, CODEX_BIN=fake)
            p = run(RUN, "--runner", "codex", "--out", os.path.join(tmp, "o"), "--scenarios",
                    "S-C", "--seeds", "1", "--yes", env=env)
            self.assertEqual(p.returncode, 2)
            self.assertIn("--max-runs", p.stderr)
        r = load("cs_run_eval", RUN)

        class A(object):
            jobs, max_runs, max_total_usd, runner = 1, None, 1.0, "claude"
        ran = []

        def fn(job):
            ran.append(job)
            return 0.4
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            r.run_capped(list(range(10)), fn, A(), lambda c: c)
        self.assertEqual(ran, [0, 1, 2])        # 0.4 + 0.4 + 0.4 >= 1.00: no fourth run
        self.assertIn("stopped after 3 of 10 job(s)", out.getvalue())
        A.max_runs, A.max_total_usd = 2, 100.0
        del ran[:]
        r.run_capped(list(range(10)), fn, A(), lambda c: c)
        self.assertEqual(ran, [0, 1])

    @unittest.skipUnless(os.name == "posix", "process groups are POSIX")
    def test_timeout_kills_the_agents_children(self):
        r = load("cs_run_eval", RUN)
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = os.path.join(tmp, "bg")
            _, _, status = r.run_proc(["sh", "-c", "sleep 30 & echo $! > %s; wait" % pidfile],
                                      tmp, 1)
            self.assertEqual(status, "timeout")
            import time
            time.sleep(0.2)
            with self.assertRaises(OSError):
                os.kill(int(open(pidfile).read()), 0)

    def test_sibling_decoys_are_read_live(self):
        r = load("cs_run_eval", RUN)
        with open(os.path.join(EVALS, "trigger.json")) as f:
            trig = json.load(f)
        live = [d for d in trig["decoys"] if d.get("from_skill")]
        self.assertTrue(live)
        for d in live:
            text = r.decoy_description(d)
            self.assertGreater(len(text), 50, d)
            self.assertNotIn("description:", text)
        jobs = r.trigger_jobs(type("A", (), {"runner": "claude", "model": None})())
        self.assertEqual(len(jobs), len(trig["should_fire"]) + len(trig["should_not_fire"]))
        self.assertTrue(all("iterative-improvement" in j["names"] for j in jobs))

    def test_parse_choice(self):
        r = load("cs_run_eval", RUN)
        names = ["competitive-systems", "systematic-debugging", "web-performance", "code-maintenance"]
        self.assertEqual(r.parse_choice("competitive-systems", names), "competitive-systems")
        self.assertEqual(r.parse_choice("`web-performance`.", names), "web-performance")
        self.assertEqual(r.parse_choice("none", names), "none")
        self.assertEqual(r.parse_choice("I would pick something else", names), "unparsed")


if __name__ == "__main__":
    unittest.main()
