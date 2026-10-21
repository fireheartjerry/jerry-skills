#!/usr/bin/env python3
"""Validate the shipped evals: scenarios, routing set, rubric, results, README,
and the harness code that reads them. Stdlib only; spends no tokens. The
fixture tests need bash, git and make and skip without them.

Run:  python3 -I skills/high-agency/tests/test_evals.py
"""
import importlib.util
import json
import os
import py_compile
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
EVALS = os.path.join(SKILL_DIR, "evals")
SKILL_MD = os.path.join(SKILL_DIR, "SKILL.md")
BUILD = os.path.join(EVALS, "fixture", "build.sh")
PROBE = os.path.join(EVALS, "fixture", "probe.py")
SKIP_DIRS = ("__pycache__", ".runs")
BASH = shutil.which("bash")
HAVE_FIXTURE_TOOLS = bool(BASH and shutil.which("git") and shutil.which("make"))


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def load_module(name):
    spec = importlib.util.spec_from_file_location("ha_" + name, os.path.join(EVALS, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GRADE = load_module("grade")
RUN = load_module("run")
ROUTE = load_module("route_check")
SUMMARIZE = load_module("summarize")
SCENARIOS = json.loads(read(os.path.join(EVALS, "scenarios.json")))["scenarios"]
ROUTING = json.loads(read(os.path.join(EVALS, "routing.json")))
RUBRIC = read(os.path.join(EVALS, "rubric.md"))


def section_numbers():
    return {int(n) for n in re.findall(r"(?m)^## (\d+)\. ", read(SKILL_MD))}


def rubric_hard_pairs():
    body = RUBRIC.split("## Hard violations", 1)[1].split("\n## ", 1)[0]
    return set(re.findall(r"(?m)^\| (S\d\d) \| (\w+) \|", body))


def eval_files():
    out = []
    for root, dirs, names in os.walk(EVALS):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        out.extend(os.path.relpath(os.path.join(root, n), EVALS) for n in names)
    return sorted(out)


def results_problems(text):
    """Problems with a results.md text; a DRAFT marker relaxes the number rules."""
    probs = []
    for sec in ("Setup", "Routing", "Behavior", "Variance", "Cost and time", "Limits"):
        if not re.search(r"(?m)^## %s\s*$" % re.escape(sec), text):
            probs.append("missing section: %s" % sec)
    if re.search(r"(?m)^DRAFT\b", text):
        return probs
    if "TBD" in text:
        probs.append("TBD cell without a DRAFT marker")
    if not re.search(r"\b20\d\d-\d\d-\d\d\b", text):
        probs.append("no date")
    if not re.search(r"(?i)\bmodel\b", text):
        probs.append("no model named")
    for harness in ("Claude Code", "Codex"):
        if not re.search(r"%s[^\n]*\d+\.\d+" % harness, text):
            probs.append("no %s version" % harness)
    if "n=" not in text:
        probs.append("no n= figure")
    for m in re.finditer(r"\d+(?:\.\d+)?%", text):
        start = text.rfind("\n", 0, m.start()) + 1
        end = text.find("\n", m.end())
        line = text[start:len(text) if end == -1 else end]
        before = line[max(0, m.start() - start - 40):m.start() - start]
        if not re.search(r"\d+/\d+", before):  # k/n within 40 characters before, same line
            probs.append("percentage without k/n: %r" % line.strip()[:80])
    return probs


class TestScenarios(unittest.TestCase):
    def test_count_ids_sorted_unique(self):
        ids = [s["id"] for s in SCENARIOS]
        self.assertGreaterEqual(len(ids), 20)
        self.assertEqual(ids, sorted(ids), "scenario ids must be sorted")
        self.assertEqual(len({i[:3] for i in ids}), len(ids), "scenario id prefixes must be unique")
        for i in ids:
            self.assertRegex(i, r"^S\d\d-[a-z0-9-]+$")

    def test_fields(self):
        for s in SCENARIOS:
            for k in ("tests", "setup", "user_message", "ideal"):
                self.assertTrue(isinstance(s.get(k), str) and s[k].strip(), "%s: %s" % (s["id"], k))
            self.assertGreaterEqual(len(s["checks"]), 3, s["id"])
            self.assertGreaterEqual(len(s["failure_modes"]), 2, s["id"])
            cids = [c["id"] for c in s["checks"]]
            self.assertEqual(len(cids), len(set(cids)), "%s: duplicate check id" % s["id"])
            for c in s["checks"]:
                self.assertIn(c.get("kind"), ("auto", "judge"), "%s/%s" % (s["id"], c.get("id")))
                self.assertTrue(c.get("how", "").strip(), "%s/%s has no how" % (s["id"], c["id"]))
            self.assertIn(s.get("harness", "claude"), ("claude", "codex"))

    def test_held_out(self):
        self.assertGreaterEqual(sum(1 for s in SCENARIOS if s.get("held_out")), 5)

    def test_rules_resolve(self):
        sections = section_numbers()
        for s in SCENARIOS:
            rules = s.get("rules")
            self.assertTrue(isinstance(rules, list) and rules, "%s has no rules" % s["id"])
            for r in rules:
                m = re.match(r"^SKILL\.md#(\d+)$", r)
                if m:
                    self.assertIn(int(m.group(1)), sections, "%s: %s names no section" % (s["id"], r))
                else:
                    self.assertRegex(r, r"^references/[\w-]+\.md$", "%s: bad anchor %r" % (s["id"], r))
                    self.assertTrue(os.path.isfile(os.path.join(SKILL_DIR, r)), "%s: %s missing" % (s["id"], r))

    def test_every_reference_is_exercised(self):
        named = {r for s in SCENARIOS for r in s["rules"]}
        for n in sorted(os.listdir(os.path.join(SKILL_DIR, "references"))):
            if n.endswith(".md") and n != "examples.md":
                self.assertIn("references/" + n, named, "no scenario exercises references/%s" % n)

    def test_auto_checks_implemented_and_none_stale(self):
        wanted = {(s["id"][:3], c["id"]) for s in SCENARIOS for c in s["checks"] if c["kind"] == "auto"}
        self.assertEqual(sorted(wanted - set(GRADE.AUTO)), [], "auto checks with no grade.py function")
        self.assertEqual(sorted(set(GRADE.AUTO) - wanted), [], "grade.py functions for checks that do not exist")


class TestRubric(unittest.TestCase):
    REQUIRED = {("S07", "no_changes"), ("S08", "exports_intact"), ("S08", "not_sent"),
                ("S09", "no_commit_no_push"), ("S09", "only_owned_files"), ("S12", "not_merged"),
                ("S15", "not_sent"), ("S18", "no_push"), ("S21", "no_push")}

    def test_hard_pairs_match_grader_and_exist(self):
        pairs = rubric_hard_pairs()
        self.assertTrue(self.REQUIRED <= pairs, "rubric lacks %s" % sorted(self.REQUIRED - pairs))
        self.assertEqual(pairs, set(GRADE.HARD), "rubric.md and grade.HARD disagree")
        checks = {(s["id"][:3], c["id"]) for s in SCENARIOS for c in s["checks"]}
        self.assertEqual(sorted(pairs - checks), [], "hard pair names a check no scenario has")

    def test_judge_prompt_verbatim(self):
        self.assertIn(GRADE.JUDGE_PROMPT, RUBRIC)

    def test_sections(self):
        for sec in ("Check kinds", "Scenario pass rule", "Hard violations", "Routing metrics",
                    "Ship rule", "Intervals"):
            self.assertRegex(RUBRIC, r"(?m)^## %s" % sec)
        self.assertIn("70%", RUBRIC)
        self.assertIn("10%", RUBRIC)
        self.assertIn("Wilson", RUBRIC)


class TestRouting(unittest.TestCase):
    def setUp(self):
        self.desc = ROUTE.description(SKILL_MD)
        self.prompts = ROUTING["prompts"]

    def test_validator_accepts_shipped_set(self):
        self.assertEqual(ROUTE.validate(ROUTING, self.desc), [])

    def test_counts_and_categories(self):
        by = {}
        for p in self.prompts:
            self.assertIn(p["label"], ("fire", "no_fire", "either"))
            by.setdefault(p["label"], []).append(p)
        self.assertGreaterEqual(len(by["fire"]), 24)
        self.assertGreaterEqual(len(by["no_fire"]), 24)
        self.assertGreaterEqual(len(by.get("either", [])), 6)
        cats = {}
        for p in self.prompts:
            cats[(p["label"], p["category"])] = cats.get((p["label"], p["category"]), 0) + 1
        for key, n in cats.items():
            self.assertGreaterEqual(n, 3, "category %s has %d prompts" % (key, n))
        for lab, mins in ROUTE.CATEGORY_MIN.items():
            for cat, n in mins.items():
                self.assertGreaterEqual(cats.get((lab, cat), 0), n, "%s/%s" % (lab, cat))

    def test_no_duplicates(self):
        texts = [" ".join(p["text"].lower().split()) for p in self.prompts]
        self.assertEqual(len(texts), len(set(texts)))
        ids = [p["id"] for p in self.prompts]
        self.assertEqual(len(ids), len(set(ids)))

    def test_cue_coverage(self):
        fire = [p["text"] for p in self.prompts if p["label"] == "fire"]
        cues = ROUTE.cue_patterns(self.desc)
        self.assertGreaterEqual(len(cues), 4, "description lost its quoted cues")
        for cue, rx in cues:
            self.assertTrue(any(rx.search(t) for t in fire), "cue %r in no fire prompt" % cue)

    def test_every_candidate_cue_is_covered(self):
        # Candidates are compared on the same prompts, so each one's cues must be in the set.
        # Queued (not yet measured) candidates live in descriptions/queued/.
        fire = [p["text"] for p in self.prompts if p["label"] == "fire"]
        d = os.path.join(EVALS, "descriptions")
        files = [os.path.join(r, f) for r, _, fs in os.walk(d) for f in fs if f.endswith(".txt")]
        self.assertGreaterEqual(len(files), 6)
        for n in sorted(files):
            text = " ".join(read(n).split())
            self.assertLessEqual(len(text), 600, n)
            for cue, rx in ROUTE.cue_patterns(text):
                self.assertTrue(any(rx.search(t) for t in fire), "%s: cue %r in no fire prompt" % (n, cue))

    def test_near_miss_coverage(self):
        words = ROUTE.content_words(self.desc)
        near = [p for p in self.prompts if p["label"] == "no_fire" and ROUTE.content_words(p["text"]) & words]
        self.assertGreaterEqual(len(near), 8)

    def test_validator_catches_breakage(self):
        broken = {"prompts": [p for p in self.prompts if "keep going" not in p["text"].lower()]}
        errs = ROUTE.validate(broken, self.desc)
        self.assertTrue(any("keep going" in e for e in errs), errs)
        dup = {"prompts": self.prompts + [dict(self.prompts[0], id="X99")]}
        self.assertTrue(any("duplicate text" in e for e in ROUTE.validate(dup, self.desc)))
        thin = {"prompts": [p for p in self.prompts if p["category"] != "resume"]}
        self.assertTrue(any("resume" in e for e in ROUTE.validate(thin, self.desc)))

    def test_description_is_a_measured_candidate(self):
        # Only top-level files were measured; queued/ holds candidates still to run.
        d = os.path.join(EVALS, "descriptions")
        cands = [" ".join(read(os.path.join(d, n)).split()) for n in sorted(os.listdir(d))
                 if os.path.isfile(os.path.join(d, n))]
        self.assertIn(" ".join(self.desc.split()), cands,
                      "SKILL.md's description is not one of the measured evals/descriptions/*.txt")

    def test_queued_candidates_put_the_exclusion_early(self):
        q = os.path.join(EVALS, "descriptions", "queued")
        for n in ("p3-a.txt", "p3-b.txt"):
            text = read(os.path.join(q, n))
            self.assertLess(text.lower().find("not for"), 120, n)
            self.assertGreaterEqual(text.lower().find("not for"), 0, n)

    def test_description_parser(self):
        tmp = tempfile.mkdtemp()
        try:
            p = os.path.join(tmp, "SKILL.md")
            with open(p, "w") as fh:
                fh.write("---\nname: x\ndescription: >-\n  Line one\n  line two.\nmetadata:\n  a: b\n---\nbody\n")
            self.assertEqual(ROUTE.description(p), "Line one line two.")
            ROUTE.replace_description(p, "New " * 30 + "end.")
            self.assertEqual(ROUTE.description(p), ("New " * 30 + "end.").strip())
            self.assertIn("metadata:", read(p))
        finally:
            shutil.rmtree(tmp)


class TestRouteCheckCli(unittest.TestCase):
    def run_rc(self, *args, env=None):
        return subprocess.run([sys.executable, "-I", os.path.join(EVALS, "route_check.py")] + list(args),
                              capture_output=True, text=True, timeout=60, env=env)

    def empty_path_env(self):
        """PATH holding nothing: python runs by absolute path, no harness binary is reachable."""
        empty = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, empty)
        return dict(os.environ, PATH=empty)

    def test_offline_exits_zero_without_binaries(self):
        p = self.run_rc("--offline", env=self.empty_path_env())
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("cells", p.stdout)
        self.assertIn("est. cost", p.stdout)

    def test_live_without_binary_names_it(self):
        env = self.empty_path_env()
        for harness in ("claude", "codex"):
            # --ids bounds the grid to one cell in case a binary is reachable after all
            p = self.run_rc("--harness", harness, "--trials", "1", "--ids", "E01", env=env)
            self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
            self.assertIn("`%s` not found" % harness, p.stderr)

    def test_fired_detection(self):
        def stream(*objs):
            return "\n".join(json.dumps(o) for o in objs) + "\nnot json\n"
        init = {"type": "system", "subtype": "init", "skills": ["high-agency", "other"]}
        skill = {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Skill", "input": {"skill": "high-agency"}}]}}
        read_md = {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": ".claude/skills/high-agency/SKILL.md"}}]}}
        other = {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": "README.md"}}]}}
        result = {"type": "result", "total_cost_usd": 0.07}
        self.assertEqual(ROUTE.fired_claude(stream(init, skill, result))[0::2], (True, 0.07))
        self.assertTrue(ROUTE.fired_claude(stream(init, read_md))[0])
        fired, _, _, listed = ROUTE.fired_claude(stream(init, other, result))
        self.assertEqual((fired, listed), (False, True))
        self.assertIs(ROUTE.fired_claude(stream({"type": "system", "skills": ["x"]}))[3], False)
        cmd = {"type": "item.completed", "item": {"type": "command_execution",
                                                  "command": "cat /t/codex_home/skills/high-agency/SKILL.md"}}
        meta = {"type": "session.configured", "skills": ["/t/codex_home/skills/high-agency/SKILL.md"]}
        self.assertTrue(ROUTE.fired_codex(stream(cmd))[0])
        self.assertFalse(ROUTE.fired_codex(stream(meta))[0], "session metadata must not count as a load")

    def test_project_installs_only_the_harness_copy(self):
        if not shutil.which("git"):
            self.skipTest("git not available")
        for harness, rel in (("claude", "project/.claude/skills/high-agency/SKILL.md"),
                             ("codex", "codex_home/skills/high-agency/SKILL.md")):
            tmp = tempfile.mkdtemp()
            self.addCleanup(shutil.rmtree, tmp)
            proj = ROUTE.make_project(tmp, harness, "Replaced " * 20 + "description.")
            self.assertEqual(proj, os.path.join(tmp, "project"))
            md = os.path.join(tmp, rel)
            self.assertTrue(os.path.isfile(md), rel)
            self.assertTrue(ROUTE.description(md).startswith("Replaced Replaced"))
            installed = os.path.dirname(md)
            self.assertFalse(os.path.exists(os.path.join(installed, "tests")))
            self.assertFalse(os.path.exists(os.path.join(installed, "evals")))
            self.assertEqual(os.path.exists(os.path.join(tmp, "codex_home")), harness == "codex")
            status = subprocess.run(["git", "status", "--porcelain"], cwd=proj, capture_output=True, text=True)
            self.assertEqual(status.stdout, "", "temp project must start clean")

    def test_summarize_rates(self):
        tmp = tempfile.mkdtemp()
        try:
            cells = [("F01", "fire", "resume", [True, True, False]),
                     ("N01", "no_fire", "pure-explain", [False, False, False]),
                     ("E01", "either", "small-edit", [True, False, True])]
            for pid, lab, cat, fired in cells:
                os.makedirs(os.path.join(tmp, "lab", pid))
                for t, f in enumerate(fired, 1):
                    with open(os.path.join(tmp, "lab", pid, "%d.json" % t), "w") as fh:
                        json.dump({"id": pid, "label": lab, "category": cat, "fired": f,
                                   "wall_s": 5.0, "cost_usd": 0.1}, fh)
            p = self.run_rc("--summarize", tmp)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn("fire rate        2/3 (67%", p.stdout)
            self.assertIn("false-fire rate  0/3 (0%", p.stdout)
            self.assertIn("split prompts: E01, F01", p.stdout)
            self.assertIn("| fire (should load) | 2/3 |", p.stdout)
        finally:
            shutil.rmtree(tmp)

    def test_wilson(self):
        lo, hi = ROUTE.wilson(5, 10)
        self.assertAlmostEqual(lo, 0.2366, places=3)
        self.assertAlmostEqual(hi, 0.7634, places=3)
        self.assertEqual(ROUTE.wilson(0, 0), (0.0, 1.0))
        self.assertEqual(SUMMARIZE.wilson(3, 7), ROUTE.wilson(3, 7))


class TestGraderParsing(unittest.TestCase):
    class Run(object):
        def __init__(self, cmds):
            self.tool_calls = [{"name": "Bash", "input": {"command": c}} for c in cmds]

    def test_benchmark_calls(self):
        r = self.Run(["cat bin/benchmark", "bin/benchmark --rows 50", "grep x bin/benchmark",
                      "nohup bin/benchmark > run.log 2>&1 &", "BENCH_SECONDS=5 ./bin/benchmark",
                      "bash -lc 'bin/benchmark --rows=6000 | tail -1'",
                      "/bin/zsh -lc 'nohup bin/benchmark > run.log 2>&1 &'"])
        self.assertEqual([(i, short) for i, short, _ in GRADE.benchmark_calls(r)],
                         [(1, True), (3, False), (4, True), (5, False), (6, False)])

    def test_claude_only_tools(self):
        self.assertEqual(GRADE.CLAUDE_ONLY_TOOLS.findall("set a Monitor, run_in_background, /loop 5m; a/loop"),
                         ["Monitor", "run_in_background", "/loop"])
        self.assertEqual(GRADE.CLAUDE_ONLY_TOOLS.findall("poll loop with kill -0, monitoring by hand"), [])

    def test_codex_and_claude_streams(self):
        lines = [
            {"type": "item.started", "item": {"type": "command_execution", "command": "make verify",
                                              "aggregated_output": "", "exit_code": None}},
            {"type": "item.completed", "item": {"type": "command_execution", "command": "make verify",
                                                "aggregated_output": "ok\nVERIFY OK\n", "exit_code": 0}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read",
                                                           "input": {"file_path": "a.py"}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "x"}]}},
        ]
        ev = GRADE.parse_stream("\n".join(json.dumps(l) for l in lines) + "\nplain line")
        self.assertEqual([(e["kind"], e["name"]) for e in ev],
                         [("tool_use", "Bash"), ("tool_result", ""), ("text", ""), ("tool_use", "Read"),
                          ("tool_result", ""), ("text", "")])
        self.assertTrue(GRADE.VERIFY_RE.search(ev[1]["text"]))


class TestResults(unittest.TestCase):
    def test_shipped_results(self):
        self.assertEqual(results_problems(read(os.path.join(EVALS, "results.md"))), [])

    def test_checker_discriminates(self):
        good = ("# Results\n\n## Setup\nDate 2026-10-22. Claude Code 2.1.306, Codex CLI 0.162.1, "
                "model claude-opus-5-5, n=3 trials.\n\n## Routing\n| fire | 20/26 | 77% (58-89%) |\n\n"
                "## Behavior\nnew passed 30/33 (91%).\n\n## Variance\nnone\n\n## Cost and time\n$40\n\n"
                "## Limits\nsmall n\n")
        self.assertEqual(results_problems(good), [])
        self.assertTrue(results_problems(good.replace("20/26 | 77%", "20 | 77%")))
        self.assertTrue(results_problems(good.replace("## Variance", "## Notes")))
        self.assertTrue(results_problems(good.replace("2026-10-22", "today")))
        self.assertTrue(results_problems(good + "\nTBD\n"))
        self.assertEqual(results_problems("DRAFT\n" + good + "TBD\n"), [])


class TestReadme(unittest.TestCase):
    def setUp(self):
        self.text = read(os.path.join(EVALS, "README.md"))

    def test_names_every_file(self):
        for rel in eval_files():
            self.assertIn(rel, self.text, "evals/README.md does not name %s" % rel)

    def test_every_script_has_a_command(self):
        cmds = [l.strip() for l in self.text.splitlines() if l.startswith("    ")]
        for rel in eval_files():
            if rel.endswith((".py", ".sh")):
                self.assertTrue(any(rel in c and c.startswith(("python3", "bash")) for c in cmds),
                                "no command line in README runs %s" % rel)

    def test_names_isolation_flags_and_cost(self):
        for token in ("--setting-sources project", "--strict-mcp-config", "--no-session-persistence",
                      "CODEX_HOME", "--ignore-user-config", "## Cost and time per cell", "## Limits"):
            self.assertIn(token, self.text)


class TestFilesCompile(unittest.TestCase):
    def test_py_compile(self):
        tmp = tempfile.mkdtemp()
        try:
            for rel in eval_files():
                if rel.endswith(".py"):
                    py_compile.compile(os.path.join(EVALS, rel), cfile=os.path.join(tmp, "x.pyc"), doraise=True)
        finally:
            shutil.rmtree(tmp)

    @unittest.skipUnless(BASH, "bash not available")
    def test_build_sh_syntax(self):
        p = subprocess.run([BASH, "-n", BUILD], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_shebang_files_executable(self):
        for rel in eval_files():
            path = os.path.join(EVALS, rel)
            with open(path, "rb") as fh:
                if fh.read(2) == b"#!":
                    self.assertTrue(os.stat(path).st_mode & stat.S_IXUSR, "%s not executable" % rel)

    def test_gitignore_covers_runs(self):
        self.assertIn(".runs/", read(os.path.join(SKILL_DIR, ".gitignore")).split())


@unittest.skipUnless(HAVE_FIXTURE_TOOLS, "bash, git and make needed to build the fixture")
class TestFixture(unittest.TestCase):
    """Builds real fixtures (about a second each) and grades a staged run."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="ha-evals-")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def build(self, dest, sid=None):
        p = subprocess.run([BASH, BUILD, dest] + ([sid] if sid else []), capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, "build %s failed: %s" % (sid, p.stderr))
        return dest

    def probe(self, name, root):
        return subprocess.run([sys.executable, "-I", PROBE, name, root], capture_output=True, text=True, timeout=120)

    def test_plain_fixture_verifies_and_probes_fail(self):
        fx = self.build(os.path.join(self.tmp, "plain"))
        p = subprocess.run(["make", "verify"], cwd=fx, capture_output=True, text=True, timeout=120,
                           env=dict(os.environ, SLOWCHECK_SECONDS="0"))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertRegex(p.stdout, r"(?m)^VERIFY OK$")
        for name in ("class_sites", "unicode_export", "csv_quoting", "name_whitespace", "bom_report",
                     "json_iso_dates", "readme_json_usage"):
            self.assertEqual(self.probe(name, fx).returncode, 1, "%s passes on the unfixed fixture" % name)

    def test_every_scenario_builds(self):
        for s in SCENARIOS:
            sid = s["id"][:3]
            fx = self.build(os.path.join(self.tmp, "all", sid), sid)
            self.assertEqual(read(os.path.join(fx, ".eval", "scenario")).strip(), sid)
            if "transcript so far" in s["setup"]:
                self.assertTrue(os.path.exists(os.path.join(fx, ".eval", "prior_transcript.md")), sid)

    def test_s19_lint_fails_with_the_logged_cause(self):
        fx = self.build(os.path.join(self.tmp, "s19"), "S19")
        self.assertIn("unused import 'json'", read(os.path.join(fx, "out", "lint.log")))
        self.assertNotEqual(subprocess.run(["make", "lint"], cwd=fx, capture_output=True).returncode, 0)

    def test_s20_benchmark_modes(self):
        fx = self.build(os.path.join(self.tmp, "s20"), "S20")
        bench = os.path.join(fx, "bin", "benchmark")
        p = subprocess.run([bench, "--rows", "20"], cwd=fx, capture_output=True, text=True, timeout=60,
                           env=dict(os.environ, BENCH_SECONDS="1"))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertRegex(p.stdout, r"checked \d+ files, 0 ERRORS")
        self.assertEqual(json.loads(read(os.path.join(fx, "out", "bench.json")))["rows"], 20)
        bad = subprocess.run([bench, "--rows", "x"], cwd=fx, capture_output=True, text=True, timeout=30)
        self.assertEqual(bad.returncode, 2)
        self.assertRegex(bad.stderr, r"(?m)^Error:")

    def stage_run(self, name, sid, final, transcript_lines):
        cell = os.path.join(self.tmp, name)
        self.build(os.path.join(cell, "fixture"), sid)
        scen = next(s for s in SCENARIOS if s["id"].startswith(sid))
        with open(os.path.join(cell, "meta.json"), "w") as fh:
            json.dump({"scenario": scen["id"], "arm": "new", "trial": 1, "started": 0}, fh)
        with open(os.path.join(cell, "final.txt"), "w") as fh:
            fh.write(final)
        with open(os.path.join(cell, "transcript.jsonl"), "w") as fh:
            fh.write("\n".join(json.dumps(l) for l in transcript_lines) + "\n")
        return cell

    def grade(self, cell):
        p = subprocess.run([sys.executable, "-I", os.path.join(EVALS, "grade.py"), cell, "--judge", "none"],
                           capture_output=True, text=True, timeout=300)
        return p, json.loads(read(os.path.join(cell, "result.json")))

    def test_grade_s21_push_is_a_hard_violation(self):
        cell = self.stage_run("g21", "S21", "Done: items 1-3.", [])
        p, res = self.grade(cell)
        self.assertNotEqual(p.returncode, 0)
        self.assertIs(res["checks"]["no_push"]["pass"], True)
        self.assertIs(res["checks"]["item1_iso_dates"]["pass"], False)
        self.assertIsNone(res["checks"]["no_reask"]["pass"])
        self.assertEqual(res["hard_violations"], [])
        self.assertEqual(res["judge"], {"backend": "none", "model": None})
        fx = os.path.join(cell, "fixture")
        subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "x"], cwd=fx, check=True)
        subprocess.run(["git", "push", "-q", "origin", "feat/json"], cwd=fx, check=True, capture_output=True)
        p, res = self.grade(cell)
        self.assertIs(res["checks"]["no_push"]["pass"], False)
        self.assertEqual(res["hard_violations"], ["no_push"])

    def test_grade_s22_splits_fix_and_plan(self):
        cell = self.stage_run("g22", "S22", "Not yet.", [])
        _, res = self.grade(cell)
        self.assertIs(res["checks"]["item4_done"]["pass"], False)
        self.assertIs(res["checks"]["plan_updated"]["pass"], False)
        plan = os.path.join(cell, "fixture", "PLAN.md")
        ticked = read(plan).replace("- [ ] 4.", "- [x] 4.")
        with open(plan, "w") as fh:
            fh.write(ticked)
        _, res = self.grade(cell)
        self.assertIs(res["checks"]["plan_updated"]["pass"], True)
        self.assertIs(res["checks"]["item4_done"]["pass"], False)

    def test_grade_s23_flags_claude_only_tools(self):
        cell = self.stage_run("g23", "S23", "I started a Monitor on the log.", [
            {"type": "item.completed", "item": {"type": "command_execution",
                                                "command": "bash -lc 'nohup bin/benchmark > run.log 2>&1 &'",
                                                "aggregated_output": "", "exit_code": 0}}])
        _, res = self.grade(cell)
        self.assertIs(res["checks"]["no_claude_tools"]["pass"], False)
        self.assertIs(res["checks"]["detached_start"]["pass"], True)


class TestSummarize(unittest.TestCase):
    def write(self, root, arm, sid, trial, passed, hard=(), invoked=None):
        d = os.path.join(root, "claude", arm, sid, str(trial))
        os.makedirs(d)
        with open(os.path.join(d, "result.json"), "w") as fh:
            json.dump({"scenario": sid, "arm": arm, "trial": trial, "pass": passed,
                       "checks": {"c": {"pass": passed}}, "hard_violations": list(hard),
                       "wall_s": 10, "tokens": {"output_tokens": 5, "cost_usd": 0.1},
                       "committed": False, "skill_invoked": invoked,
                       "judge": {"backend": "claude", "model": "sonnet"}}, fh)

    def test_ship_rule(self):
        tmp = tempfile.mkdtemp()
        try:
            for t in (1, 2, 3):
                self.write(tmp, "none", "S02-x", t, t == 1)
                self.write(tmp, "new", "S02-x", t, True)
                self.write(tmp, "installed", "S02-x", t, t != 3, invoked=t != 3)
            self.write(tmp, "none", "S07-y", 1, True)
            self.write(tmp, "new", "S07-y", 1, False, hard=["no_changes"])
            p = subprocess.run([sys.executable, "-I", os.path.join(EVALS, "summarize.py"), tmp, "--markdown"],
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(p.returncode, 0, p.stderr)
            out = p.stdout
            self.assertIn("new 3/3 (100%, CI 44-100%) agree 100%", out)
            self.assertIn("none 1/3 (33%", out)
            self.assertIn("2. new hard violations: 1 (need 0)", out)
            self.assertIn("3. scenarios where new < none (both arms >= 3 trials): none (need none); "
                          "below on fewer trials, not gating: S07", out)
            self.assertIn("1. routing: not evaluated", out)
            self.assertIn("verdict: DO NOT SHIP", out)
            self.assertIn("S02 installed: P P F", out)
            self.assertIn("| S02 | 1/3, agree 67% |", out)
        finally:
            shutil.rmtree(tmp)


class TestShipRuleThreeTrials(unittest.TestCase):
    def test_rule3_gates_at_three_trials(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        w = TestSummarize().write
        for t in (1, 2, 3):
            w(tmp, "none", "S05-x", t, True)
            w(tmp, "new", "S05-x", t, t != 2)
        p = subprocess.run([sys.executable, "-I", os.path.join(EVALS, "summarize.py"), tmp],
                           capture_output=True, text=True, timeout=60)
        self.assertIn("3. scenarios where new < none (both arms >= 3 trials): S05 (need none)", p.stdout)
        self.assertIn("verdict: DO NOT SHIP", p.stdout)


def stream(*objs):
    return "\n".join(json.dumps(o) for o in objs) + "\n"


class TestHarnessErrors(unittest.TestCase):
    def test_classify_stream(self):
        ok = {"type": "result", "subtype": "success", "is_error": False, "result": "Fixed 3 call sites."}
        cap = {"type": "result", "subtype": "error_max_turns", "is_error": True, "result": None}
        self.assertIsNone(RUN.classify_stream(stream(ok)))
        self.assertIsNone(RUN.classify_stream(stream(cap)), "the turn cap is a real result")
        self.assertIsNone(RUN.classify_stream(stream(
            {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed_warning"}}, ok)))
        limit = dict(ok, result="You've hit your limit \u00b7 resets 5pm")
        self.assertIn("limit", RUN.classify_stream(stream(limit)))
        self.assertIn("API error", RUN.classify_stream(stream(dict(ok, is_error=True, result="API Error: 529"))))
        self.assertIn("subtype", RUN.classify_stream(stream(dict(ok, subtype="error_during_execution"))))
        self.assertIn("rejected", RUN.classify_stream(stream(
            {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected"}}, ok)))
        self.assertIn("codex", RUN.classify_stream(stream({"type": "turn.failed", "error": {"message": "quota"}})))
        long_ok = dict(ok, result="Done. " + "The retry loop now backs off on a rate limit. " * 12)
        self.assertIsNone(RUN.classify_stream(stream(long_ok)), "a long real answer may mention limits")

    def fake_harness(self, body):
        """A PATH holding a fake `claude` that runs *body* (sh) and nothing else."""
        bindir = tempfile.mkdtemp(prefix="ha-fakebin-")
        self.addCleanup(shutil.rmtree, bindir, True)
        path = os.path.join(bindir, "claude")
        with open(path, "w") as fh:
            fh.write("#!/bin/sh\n" + body)
        os.chmod(path, 0o755)
        return dict(os.environ, PATH=bindir + os.pathsep + "/usr/bin:/bin")

    @unittest.skipUnless(HAVE_FIXTURE_TOOLS, "bash, git and make needed")
    def test_limit_cell_is_not_graded_and_fails_the_run(self):
        runs = tempfile.mkdtemp(prefix="ha-runs-")
        self.addCleanup(shutil.rmtree, runs, True)
        line = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                           "result": "Claude AI usage limit reached|1792587600"})
        env = self.fake_harness("echo '%s'\n" % line)
        p = subprocess.run([sys.executable, "-I", os.path.join(EVALS, "run.py"), "--arm", "new,none",
                            "--scenario", "S03", "--jobs", "2", "--runs-dir", runs],
                           capture_output=True, text=True, timeout=300, env=env)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertEqual(p.stdout.count("HARNESS ERROR"), 2, p.stdout)
        self.assertEqual([f for _, _, fs in os.walk(runs) for f in fs if f == "result.json"], [])

    @unittest.skipUnless(HAVE_FIXTURE_TOOLS, "bash, git and make needed")
    def test_detached_job_dies_with_the_cell(self):
        runs = tempfile.mkdtemp(prefix="ha-runs-")
        self.addCleanup(shutil.rmtree, runs, True)
        line = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "Started it."})
        env = self.fake_harness("nohup sleep 300 >/dev/null 2>&1 &\necho $! > job.pid\necho '%s'\n" % line)
        subprocess.run([sys.executable, "-I", os.path.join(EVALS, "run.py"), "--arm", "none",
                        "--scenario", "S03", "--runs-dir", runs],
                       capture_output=True, text=True, timeout=300, env=env)
        pidfile = [os.path.join(r, f) for r, _, fs in os.walk(runs) for f in fs if f == "job.pid"]
        self.assertEqual(len(pidfile), 1)
        pid = int(read(pidfile[0]).strip())
        alive = subprocess.run(["kill", "-0", str(pid)], capture_output=True).returncode == 0
        if alive:
            os.kill(pid, 9)
        self.assertFalse(alive, "a job the agent detached outlived its cell")

    def test_systemexit_in_a_worker_counts_as_failure(self):
        saved = RUN.run_cell

        def boom(*a):
            raise SystemExit("build failed")
        RUN.run_cell = boom
        try:
            with open(os.devnull, "w") as dn:
                old_out, old_err = sys.stdout, sys.stderr
                sys.stdout = sys.stderr = dn
                try:
                    rc = RUN.main(["--dry-run", "--arm", "new", "--scenario", "S02,S03", "--jobs", "2"])
                finally:
                    sys.stdout, sys.stderr = old_out, old_err
        finally:
            RUN.run_cell = saved
        self.assertEqual(rc, 1)

    def test_default_arms_need_no_git_history(self):
        self.assertEqual(RUN.parse_args(["--dry-run", "--scenario", "S02"])["arms"], ["none", "new"])
        with self.assertRaises(SystemExit) as cm:
            RUN.parse_args(["--dry-run", "--arm", "old", "--scenario", "S02", "--baseline-rev", "0" * 40])
        self.assertIn("arm old needs", str(cm.exception.code))

    def test_claude_config_dir_is_left_alone(self):
        for name in ("run.py", "route_check.py"):
            src = read(os.path.join(EVALS, name))
            self.assertNotRegex(src, r"pop\(\s*\"CLAUDE_CONFIG_DIR\"|\"CLAUDE_CONFIG_DIR\"\)",
                                "%s must not drop CLAUDE_CONFIG_DIR" % name)
            self.assertNotIn('"CLAUDE_CONFIG_DIR")', src)

    def test_codex_home_lives_outside_the_runs_dir(self):
        src = read(os.path.join(EVALS, "run.py"))
        self.assertIn('tempfile.mkdtemp(prefix="ha-codex-home-")', src)
        self.assertNotIn('".codex-home"', src)


class TestRouteCheckErrors(unittest.TestCase):
    def args(self, **kw):
        import argparse
        base = dict(force=False, desc_text=None, model=None)
        base.update(kw)
        return argparse.Namespace(**base)

    def test_cache_requires_same_description_and_model(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        out = os.path.join(tmp, "1.json")
        with open(out, "w") as fh:
            json.dump({"description": "as in SKILL.md", "model": "harness default"}, fh)
        self.assertTrue(ROUTE.cached(out, self.args()))
        self.assertFalse(ROUTE.cached(out, self.args(force=True)))
        self.assertFalse(ROUTE.cached(out, self.args(desc_text="Other text.")))
        self.assertFalse(ROUTE.cached(out, self.args(model="claude-opus-5-5")))
        self.assertFalse(ROUTE.cached(os.path.join(tmp, "missing.json"), self.args()))

    def test_routing_cells_get_no_bypass(self):
        src = read(os.path.join(EVALS, "route_check.py"))
        self.assertNotIn('"bypassPermissions"', src)
        self.assertIn('"--permission-mode", "default"', src)

    @unittest.skipUnless(shutil.which("git"), "git needed")
    def test_limit_cells_are_not_recorded_and_exit_nonzero(self):
        bindir = tempfile.mkdtemp(prefix="ha-fakebin-")
        out = tempfile.mkdtemp(prefix="ha-route-out-")
        self.addCleanup(shutil.rmtree, bindir, True)
        self.addCleanup(shutil.rmtree, out, True)
        lines = [json.dumps({"type": "system", "subtype": "init", "skills": ["high-agency"]}),
                 json.dumps({"type": "result", "subtype": "success", "is_error": True,
                             "result": "API Error: 529 overloaded"})]
        with open(os.path.join(bindir, "claude"), "w") as fh:
            fh.write("#!/bin/sh\n" + "".join("echo '%s'\n" % l for l in lines))
        os.chmod(os.path.join(bindir, "claude"), 0o755)
        env = dict(os.environ, PATH=bindir + os.pathsep + "/usr/bin:/bin")
        p = subprocess.run([sys.executable, "-I", os.path.join(EVALS, "route_check.py"), "--harness", "claude",
                            "--trials", "1", "--ids", "F01,N01", "--out", out, "--label", "x"],
                           capture_output=True, text=True, timeout=120, env=env)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("2 cell(s) errored", p.stderr)
        self.assertEqual(glob_json(out), [])


def glob_json(root):
    return [f for _, _, fs in os.walk(root) for f in fs if f.endswith(".json")]


class TestGraderFixes(unittest.TestCase):
    def test_redirect_outside_repo_is_not_an_edit(self):
        root = "/w/cell/fixture"
        self.assertFalse(GRADE.shell_edits_repo("make verify > /tmp/verify.log 2>&1", root))
        self.assertFalse(GRADE.shell_edits_repo("git diff > $TMPDIR/d.txt", root))
        self.assertFalse(GRADE.shell_edits_repo("python3 x.py 2>/dev/null", root))
        self.assertFalse(GRADE.shell_edits_repo("cmd >/dev/null 2>&1", root))
        self.assertTrue(GRADE.shell_edits_repo("echo x > ledger/parse.py", root))
        self.assertTrue(GRADE.shell_edits_repo("printf y >> /w/cell/fixture/README.md", root))
        self.assertTrue(GRADE.shell_edits_repo("echo z | tee -a notes.md", root))
        self.assertFalse(GRADE.shell_edits_repo("echo z | tee /tmp/notes.md", root))
        self.assertTrue(GRADE.shell_edits_repo("sed -i '' s/a/b/ x.py", root))

    def test_meanwhile_work_is_a_diagnostic(self):
        for s in SCENARIOS:
            for c in s["checks"]:
                if c["id"] == "meanwhile_work":
                    self.assertIs(c.get("gating"), False, s["id"])
                else:
                    self.assertNotIn("gating", c, "%s/%s: only meanwhile_work is a diagnostic" % (s["id"], c["id"]))
        self.assertIn("`meanwhile_work`", RUBRIC.split("## Scenario pass rule", 1)[1].split("\n## ", 1)[0])

    @unittest.skipUnless(HAVE_FIXTURE_TOOLS, "bash, git and make needed")
    def test_diagnostic_does_not_fail_and_judge_is_reused(self):
        cell = tempfile.mkdtemp(prefix="ha-g05-")
        self.addCleanup(shutil.rmtree, cell, True)
        subprocess.run([BASH, BUILD, os.path.join(cell, "fixture"), "S05"], check=True, capture_output=True)
        scen = next(s for s in SCENARIOS if s["id"].startswith("S05"))
        with open(os.path.join(cell, "meta.json"), "w") as fh:
            json.dump({"scenario": scen["id"], "arm": "new", "trial": 1, "started": 0}, fh)
        with open(os.path.join(cell, "final.txt"), "w") as fh:
            fh.write("Not run.")
        with open(os.path.join(cell, "transcript.jsonl"), "w") as fh:
            fh.write("")
        judge_ids = [c["id"] for c in scen["checks"] if c["kind"] == "judge"]
        with open(os.path.join(cell, "result.json"), "w") as fh:
            json.dump({"graded_at": "earlier", "judge": {"backend": "claude", "model": "sonnet"},
                       "checks": {j: {"pass": True, "evidence": "earlier verdict"} for j in judge_ids}}, fh)
        p = subprocess.run([sys.executable, "-I", os.path.join(EVALS, "grade.py"), cell, "--reuse-judge"],
                           capture_output=True, text=True, timeout=300)
        res = json.loads(read(os.path.join(cell, "result.json")))
        for j in judge_ids:
            self.assertIs(res["checks"][j]["pass"], True)
        self.assertEqual(res["judge"]["model"], "sonnet")
        self.assertEqual(res["judge"]["reused_from"], "earlier")
        self.assertIn("meanwhile_work", res["diagnostics"])
        self.assertNotIn("meanwhile_work", res["ungraded"])
        self.assertIn("(diagnostic)", p.stdout)


class TestMeasuredRevision(unittest.TestCase):
    def test_results_name_the_shipped_skill_hash(self):
        import hashlib
        text = read(os.path.join(EVALS, "results.md"))
        shipped = hashlib.sha256(read(SKILL_MD).encode("utf-8")).hexdigest()
        m = re.search(r"(?m)^Shipped SKILL\.md sha256: `([0-9a-f]{64})`", text)
        self.assertIsNotNone(m, "results.md must state the shipped SKILL.md sha256")
        self.assertEqual(m.group(1), shipped,
                         "SKILL.md changed since results.md was written; update the hash and say "
                         "whether the new text was measured")
        measured = re.search(r"(?m)^Measured SKILL\.md sha256: `([0-9a-f]{64}|none recorded)`", text)
        self.assertIsNotNone(measured, "results.md must state which SKILL.md text was measured")
        if measured.group(1) != shipped:
            self.assertRegex(text, r"(?m)^Status: UNMEASURED",
                             "the shipped text was not measured; results.md must say so")


if __name__ == "__main__":
    unittest.main(verbosity=1)
