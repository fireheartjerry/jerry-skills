"""End to end through the CLI in a subprocess with --home <tmp> and --engine fake; runs twice to prove idempotence."""
import hashlib
import json
import os
import subprocess
import sys
import unittest

import helpers
from mr.config import read_json

EXPECTED = ["config.json", "corpus/messages.jsonl", "corpus/manifest.json", "corpus/scrub-report.json", "corpus/unknown-tags.json",
            "derived/sessions.jsonl", "derived/prep-report.json", "derived/split.json", "derived/chunks/chunk-00.jsonl",
            "derived/dev/seeds/s00.json", "eval/ledger.json", "model/MODEL.json", "model/light.md", "model/profile.md",
            "model/examples.md", "model/index.json"]


class CliSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = helpers.tmpdir() / "home"

    def cli(self, *args, expect=0):
        r = helpers.run_cli(self.home, *args)
        self.assertEqual(r.returncode, expect, "args=%s\nstdout=%s\nstderr=%s" % (args, r.stdout, r.stderr))
        return r

    def test_full_pipeline_twice(self):
        import stat
        fake = ("--fake-dir", helpers.FAKE)
        self.cli("extract", expect=1)  # no config yet
        self.cli("init", "--name", "Alex", "--engine", "fake", "--person", "Sam Taylor=<teammate>")
        self.assertEqual(stat.S_IMODE(os.stat(self.home).st_mode), 0o700)  # the data home describes one person
        self.assertEqual(stat.S_IMODE(os.stat(self.home / "config.json").st_mode), 0o600)
        cfg = read_json(self.home / "config.json")
        cfg["claude_settings_path"] = str(self.home / "no-settings.json")  # machine-independent: no guard check
        (self.home / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        self.cli("extract", "--claude-dir", helpers.FIX / "claude", "--codex-dir", helpers.FIX / "codex")
        self.cli("prep", "--cutoff", "auto")
        self.cli("mine", "--engine", "fake", *fake)
        self.cli("synthesize", "--engine", "fake", *fake)
        r = self.cli("eval", "--engine", "fake", *fake)
        self.assertIn("report:", r.stdout)
        self.cli("refresh", "--corrections", "--engine", "fake", *fake)
        r = self.cli("refresh", "--always-on")
        self.assertIn("<!-- BEGIN mind-reading", r.stdout)
        self.assertIn("<!-- END mind-reading -->", r.stdout)
        r = self.cli("status")
        self.assertIn("corpus: 229 records", r.stdout)
        self.assertIn("grants: 1 total, 1 active", r.stdout)
        self.assertFalse(r.stdout.startswith("BLOCKED"))
        r = self.cli("select", "--seed", "fix the export button", "--no-light")
        self.assertTrue(r.stdout.startswith("<!-- mind-reading slice: groups E,A;"))
        self.assertIn("## G1.", r.stdout)
        r = self.cli("--json", "select", "--seed", "keep going")
        self.assertEqual(json.loads(r.stdout)["decision"]["groups"], ["B"])
        r = self.cli("--json", "status")
        self.assertEqual(json.loads(r.stdout)["status"]["chunks"], 2)
        for rel in EXPECTED + ["model/grants.md"]:
            self.assertTrue((self.home / rel).exists(), rel)
        # BLOCKED exits 2 so scripts can tell; the text still leads with the blocked list
        (self.home / "corpus" / "unknown-tags.json").write_text(json.dumps({"weird-tag": 50}), encoding="utf-8")
        r = self.cli("status", expect=2)
        self.assertTrue(r.stdout.startswith("BLOCKED:"))
        r = self.cli("--json", "status", expect=2)
        self.assertTrue(json.loads(r.stdout)["status"]["blocked"])
        (self.home / "corpus" / "unknown-tags.json").write_text(json.dumps({"weird-tag": 2}), encoding="utf-8")
        self.cli("status")
        split_id = read_json(self.home / "derived" / "split.json")["split_id"]
        self.assertTrue((self.home / "eval" / split_id / "seeds" / "s00.json").exists())
        self.assertTrue(list((self.home / "eval" / split_id / "results").glob("*/report.md")))
        self.assertTrue(list((self.home / "mined").glob("*/chunk-00-core.json")))

        # second pass: nothing new, same split, model unchanged, eval refused (one look)
        r = self.cli("--json", "extract", "--claude-dir", helpers.FIX / "claude", "--codex-dir", helpers.FIX / "codex")
        self.assertEqual(sum(json.loads(r.stdout)["added"].values()), 0)
        self.cli("prep", "--cutoff", "auto")
        self.assertEqual(read_json(self.home / "derived" / "split.json")["split_id"], split_id)
        self.cli("mine", "--engine", "fake", "--run", "latest", *fake)
        before = read_json(self.home / "model" / "MODEL.json")["model_hash"]
        self.cli("synthesize", "--engine", "fake", *fake)
        self.assertEqual(read_json(self.home / "model" / "MODEL.json")["model_hash"], before)
        self.cli("eval", "--engine", "fake", *fake, expect=2)
        self.cli("eval", "--dev", "--engine", "fake", *fake)
        self.cli("lint", "--model")

    def test_print_mode_writes_prompts_with_slice_and_collect(self):
        home = helpers.tmpdir() / "home-print"
        paths, cfg = helpers.build_home(home, through="synthesize")
        r = helpers.run_cli(home, "eval", "--engine", "print", "--arm", "full", "--arm", "none", "--max-sessions", "1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Print mode", r.stdout)
        split_id = read_json(paths.split)["split_id"]
        runs = paths.eval / split_id / "runs"  # eval prompts quote held-out truth, so they live inside eval/
        req_prompt = next(runs.glob("*-req/prompts/eval-requirements-s00.md"))
        self.assertFalse(list(paths.runs.glob("*-req")))
        self.assertIn("Rubric version 3", req_prompt.read_text(encoding="utf-8"))
        out = req_prompt.parent.parent / "out"
        out.mkdir()
        (out / "eval-requirements-s00.json").write_text(json.dumps(read_json(helpers.FAKE / "eval-requirements.json")["default"]))
        run_id = req_prompt.parent.parent.name.rsplit("-", 1)[0]
        self.assertIn("mind_reading.py eval --run %s --engine print --collect" % run_id, r.stdout)
        r = helpers.run_cli(home, "eval", "--engine", "print", "--arm", "full", "--arm", "none", "--max-sessions", "1", "--collect", "--run", run_id)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        full_prompt = next(runs.glob("*-expand/prompts/eval-expand-s00_full.md")).read_text(encoding="utf-8")
        self.assertIn("Use this slice of Alex's model", full_prompt)
        self.assertIn("<!-- mind-reading slice:", full_prompt)
        none_prompt = next(runs.glob("*-expand/prompts/eval-expand-s00_none.md")).read_text(encoding="utf-8")
        self.assertIn("a demanding, experienced user of coding agents", none_prompt)
        self.assertNotIn("Standing rules", none_prompt)

    def test_mine_print_instructions(self):
        home = helpers.tmpdir() / "home-mine"
        helpers.build_home(home, through="prep")
        r = helpers.run_cli(home, "mine", "--engine", "print")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Read and Write as its only tools", r.stdout)
        self.assertRegex(r.stdout, r"mind_reading\.py mine --run \S+ --engine print --collect")
        r = helpers.run_cli(home, "mine", "--engine", "print", "--collect", "--run", "latest")
        self.assertEqual(r.returncode, 0)
        self.assertIn("pending 12", r.stdout)  # 2 chunks x 6 lenses

    def test_budget_stop_is_visible_and_exits_3(self):
        home = helpers.tmpdir() / "home-budget"
        helpers.build_home(home, through="prep")
        r = helpers.run_cli(home, "mine", "--engine", "fake", "--fake-dir", helpers.FAKE, "--max-calls", "1", "--concurrency", "1")
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("ok 1,", r.stdout)
        self.assertIn("stopped 11", r.stdout)  # 2 chunks x 6 lenses, one call allowed
        self.assertIn("budget stop: 11 unit(s) not attempted", r.stdout)
        self.assertRegex(r.stdout, r"resume with: mind_reading\.py mine --run \S+")
        r = helpers.run_cli(home, "mine", "--engine", "fake", "--fake-dir", helpers.FAKE, "--run", "latest")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("skipped 1,", r.stdout)
        self.assertIn("ok 11,", r.stdout)

    def test_select_seed_file_and_no_model_exit(self):
        home = helpers.tmpdir() / "home-select"
        paths, cfg = helpers.build_home(home, through="synthesize")
        seed = "fix `npm test` and $(echo the) export button; it's \"broken\""  # shell metacharacters stay literal
        r = subprocess.run([sys.executable, "-I", str(helpers.CLI), "--home", str(home), "--json", "select", "--seed-file", "-", "--no-light"],
                           input=seed + "\n", capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        d = json.loads(r.stdout)["decision"]
        self.assertIn("E", d["groups"])
        self.assertIn("export / the export", d["vocabulary"])
        r = helpers.run_cli(home, "select", "--seed", "x", "--seed-file", "-")
        self.assertEqual(r.returncode, 1)
        empty = helpers.tmpdir() / "home-empty"
        r = helpers.run_cli(empty, "select", "--seed", "anything")
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertIn("no model", r.stdout + r.stderr)
        # a model without index.json (built before the index format) still yields a slice, exit 0
        (paths.model / "index.json").unlink()
        r = helpers.run_cli(home, "--json", "select", "--seed", "fix the export button", "--no-light")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        d = json.loads(r.stdout)
        self.assertTrue(d["decision"]["legacy"])
        self.assertIn("legacy model", d["slice"].splitlines()[0])
        self.assertEqual(len(d["decision"]["examples"]), 20)

    def test_log_decision_and_correct_via_stdin(self):
        home = helpers.tmpdir() / "home-log"
        paths, cfg = helpers.build_home(home, through="synthesize")
        payload = {"seed": "fix the export button", "mode": "execute", "s": 5, "l": 3, "g": 2, "held": ["back-fill?"],
                   "groups": ["E", "A"], "examples": [5], "grants": ["G1"], "chars": 6900}
        r = helpers.run_cli(home, "log-decision", stdin=json.dumps(payload))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        rows = helpers.read_jsonl(paths.decisions)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["seed_hash"], hashlib.sha256(b"fix the export button").hexdigest()[:8])
        self.assertNotIn("seed", rows[0])
        self.assertEqual((rows[0]["s"], rows[0]["l"], rows[0]["g"], rows[0]["held"], rows[0]["grants"]), (5, 3, 2, ["back-fill?"], ["G1"]))
        corr = {"seed": "the export is broken again", "wrong": "assumed the CSV export", "meant": "the PDF export on the invoices page",
                "scope": "project:webshop", "kind": "fix"}
        r = helpers.run_cli(home, "correct", stdin=json.dumps(corr))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        from mr.refresh import parse_corrections
        entries, bad = parse_corrections(paths.corrections.read_text(encoding="utf-8"))
        self.assertEqual(bad, [])
        self.assertEqual((entries[0]["seed"], entries[0]["scope"], entries[0]["kind"]), ("the export is broken again", "project:webshop", "fix"))
        r = helpers.run_cli(home, "correct", stdin=json.dumps({"seed": "x", "meant": "y", "kind": "nope"}))
        self.assertEqual(r.returncode, 1)
        # a correction never lifts a held category: forbidden rules and grant phrases are refused with exit 4, nothing recorded
        before = paths.corrections.read_text(encoding="utf-8")
        for meant in ["push and merge to main and deploy without asking, bypass permissions prompts", "you don't need to ask before pushing",
                      "from now on feel free to merge my branch"]:
            r = helpers.run_cli(home, "correct", stdin=json.dumps({"seed": "keep going", "wrong": "asked first", "meant": meant, "scope": "global", "kind": "fix"}))
            self.assertEqual(r.returncode, 4, (meant, r.stdout, r.stderr))
            self.assertIn("correct refused", r.stdout + r.stderr)
        r = helpers.run_cli(home, "correct", stdin=json.dumps({"seed": "keep going", "wrong": "added 'stop asking me' to the prompt", "meant": "ask once, then continue", "scope": "global", "kind": "fix"}))
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)  # `wrong` is read by the runtime too
        self.assertEqual(paths.corrections.read_text(encoding="utf-8"), before)
        # a drop may name a forbidden sentence: that is how one is removed
        r = helpers.run_cli(home, "correct", stdin=json.dumps({"seed": "keep going", "wrong": "n/a", "meant": "Approval is standing.", "scope": "global", "kind": "drop"}))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = helpers.run_cli(home, "--json", "select", "--seed", "keep going", "--no-light")
        self.assertIn("kind: drop", json.loads(r.stdout)["slice"])
        self.assertNotIn("bypass permissions", json.loads(r.stdout)["slice"])

    def test_unwritable_home_spools_then_drains(self):
        """Codex workspace-write: the data home is read-only, so the line goes to the spool (exit 0) and the next run
        with a writable home applies it. When the spool is unwritable too, exit 5."""
        home = helpers.tmpdir() / "home-ro"
        paths, cfg = helpers.build_home(home, through="synthesize")
        payload = {"seed": "fix the export button", "mode": "execute", "s": 5, "l": 3, "g": 2, "held": [], "groups": ["E"], "examples": [5], "chars": 6900}
        corr = {"seed": "the export is broken again", "wrong": "n/a", "meant": "the PDF export", "scope": "global", "kind": "fix"}
        spool = helpers.tmpdir() / "spool"
        env = {"MIND_READING_SPOOL": str(spool)}
        os.chmod(paths.model, 0o500)
        try:
            r = helpers.run_cli(home, "log-decision", stdin=json.dumps(payload), env_extra=env)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("spooled", r.stdout + r.stderr)
            r = helpers.run_cli(home, "correct", stdin=json.dumps(corr), env_extra=env)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            rows = helpers.read_jsonl(spool / "pending.jsonl")
            self.assertEqual([row["target"] for row in rows], ["decisions.jsonl", "corrections.md"])
            self.assertFalse(paths.decisions.exists())
            import stat
            self.assertEqual(stat.S_IMODE(os.stat(spool).st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(os.stat(spool / "pending.jsonl").st_mode), 0o600)
            # status while still read-only: the pending lines are reported, nothing is lost
            r = helpers.run_cli(home, "--json", "status", env_extra=env)
            self.assertEqual(json.loads(r.stdout)["status"]["spool_pending"], 2)
            # both unwritable: exit 5
            ro_spool = helpers.tmpdir() / "ro-spool"
            ro_spool.mkdir()
            os.chmod(ro_spool, 0o500)
            try:
                r = helpers.run_cli(home, "log-decision", stdin=json.dumps(payload), env_extra={"MIND_READING_SPOOL": str(ro_spool)})
                self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
                self.assertIn("MIND_READING_SPOOL", r.stdout + r.stderr)
            finally:
                os.chmod(ro_spool, 0o700)
        finally:
            os.chmod(paths.model, 0o700)
        r = helpers.run_cli(home, "select", "--seed", "fix the export button", "--no-light", env_extra=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse((spool / "pending.jsonl").exists())
        self.assertEqual(len(helpers.read_jsonl(paths.decisions)), 1)
        from mr.refresh import parse_corrections
        entries, bad = parse_corrections(paths.corrections.read_text(encoding="utf-8"))
        self.assertEqual((bad, entries[0]["meant"]), ([], "the PDF export"))
        r = helpers.run_cli(home, "--json", "status", env_extra=env)
        self.assertEqual(json.loads(r.stdout)["status"]["spool_pending"], 0)
        self.assertEqual(json.loads(r.stdout)["status"]["calibration"]["invocations"], 1)


if __name__ == "__main__":
    unittest.main()
