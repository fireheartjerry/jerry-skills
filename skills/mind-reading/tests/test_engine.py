import json
import os
import threading
import time
import unittest

import helpers
from mr import engine as _engine
from mr.config import read_json

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["answer"], "properties": {"answer": {"type": "string"}}}


def write_fake(dirpath, stage, table):
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / (stage + ".json")).write_text(json.dumps(table), encoding="utf-8")


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.paths, self.cfg = helpers.init_home(helpers.tmpdir())
        self.fake = self.paths.home / "fake"

    def units(self, n=2):
        return [_engine.Unit("u%d" % k, "prompt %d" % k, SCHEMA, self.paths.home / "out" / ("u%d.json" % k)) for k in range(n)]

    def engine(self, stage="demo", **kw):
        return _engine.Engine(self.paths, self.cfg, "fake", stage, fake_dir=self.fake, log=helpers.quiet, backoff=[0, 0], **kw)

    def test_fake_returns_canned_and_writes_meta(self):
        write_fake(self.fake, "demo", {"default": {"answer": "ok"}, "u1": {"answer": "special"}})
        res = self.engine().run(self.units())
        self.assertEqual(sorted(res["ok"]), ["u0", "u1"])
        self.assertEqual(read_json(self.paths.home / "out" / "u1.json")["answer"], "special")
        meta = read_json(self.paths.home / "out" / "u0.json")["_meta"]
        self.assertEqual((meta["unit"], meta["stage"], meta["engine"], meta["attempts"]), ("u0", "demo", "fake", 1))

    def test_fail_first_then_success_two_attempts(self):
        write_fake(self.fake, "demo", {"default": {"answer": "ok"}, "fail_first": True})
        eng = self.engine()
        res = eng.run(self.units(1))
        self.assertEqual(res["ok"], ["u0"])
        calls = _engine.read_calls(eng.run_dir)
        self.assertEqual([c["status"] for c in calls], ["error", "ok"])
        self.assertEqual([c["attempt"] for c in calls], [1, 2])

    def test_resume_skips_valid_and_force_redoes(self):
        write_fake(self.fake, "demo", {"default": {"answer": "ok"}})
        self.engine().run(self.units())
        res = self.engine().run(self.units())
        self.assertEqual(sorted(res["skipped"]), ["u0", "u1"])
        self.assertEqual(res["calls"], 0)
        res = self.engine().run(self.units(), force=True)
        self.assertEqual(len(res["ok"]), 2)
        self.assertEqual(res["calls"], 2)

    def test_invalid_output_retries_once_then_fails(self):
        write_fake(self.fake, "demo", {"default": {"wrong": 1}})
        eng = self.engine()
        res = eng.run(self.units(1))
        self.assertEqual(res["failed"], ["u0"])
        self.assertIn("validation failed twice", res["errors"]["u0"])
        self.assertEqual([c["status"] for c in _engine.read_calls(eng.run_dir)], ["invalid", "invalid"])

    def test_print_mode_writes_prompts_and_collect_validates(self):
        eng = _engine.Engine(self.paths, self.cfg, "print", "demo", log=helpers.quiet)
        res = eng.run(self.units())
        self.assertEqual(sorted(res["pending"]), ["u0", "u1"])
        self.assertTrue((eng.run_dir / "prompts" / "demo-u0.md").exists())
        self.assertTrue((eng.run_dir / "prompts" / "demo-u0.schema.json").exists())
        self.assertIn("mind_reading.py demo --run %s --engine print --collect" % eng.run_id, res["instructions"])
        self.assertIn("Read and Write as its only tools", res["instructions"])
        (eng.run_dir / "out").mkdir()
        (eng.run_dir / "out" / "demo-u0.json").write_text(json.dumps({"answer": "landed"}))
        (eng.run_dir / "out" / "demo-u1.json").write_text(json.dumps({"nope": 1}))
        eng2 = _engine.Engine(self.paths, self.cfg, "print", "demo", run_id=eng.run_id, log=helpers.quiet)
        res2 = eng2.run(self.units(), collect=True)
        self.assertEqual(res2["ok"], ["u0"])
        self.assertEqual(res2["failed"], ["u1"])
        self.assertEqual(read_json(self.paths.home / "out" / "u0.json")["answer"], "landed")

    def test_concurrency_bound(self):
        lock = threading.Lock()
        state = {"live": 0, "peak": 0}

        class Counting:
            version = "test"

            def call(self, unit, prompt, attempt):
                with lock:
                    state["live"] += 1
                    state["peak"] = max(state["peak"], state["live"])
                time.sleep(0.05)
                with lock:
                    state["live"] -= 1
                return _engine.CallResult(True, data={"answer": unit.id}, exit_code=0)
        eng = _engine.Engine(self.paths, self.cfg, "fake", "demo", concurrency=3, backend=Counting(), log=helpers.quiet, backoff=[0, 0])
        res = eng.run(self.units(6))
        self.assertEqual(len(res["ok"]), 6)
        self.assertLessEqual(state["peak"], 3)
        self.assertGreaterEqual(state["peak"], 2)

    def test_budget_stops_new_calls(self):
        write_fake(self.fake, "demo", {"default": {"answer": "ok"}})
        res = self.engine(max_calls=1, concurrency=1).run(self.units(3))
        self.assertEqual(len(res["ok"]), 1)
        self.assertEqual(len(res["stopped"]), 2)

    def test_new_run_id_does_not_collide_with_copied_mining_outputs(self):
        from unittest import mock
        with mock.patch.object(_engine._dt, "datetime") as clock:
            clock.now.return_value.strftime.return_value = "20260101-120000"
            (self.paths.mined / "20260101-120000").mkdir()
            (self.paths.runs / "20260101-120000-1").mkdir()
            self.assertEqual(_engine.new_run_id(self.paths), "20260101-120000-2")

    def test_parallel_budget_reserves_attempts_before_start(self):
        write_fake(self.fake, "demo", {"default": {"answer": "ok"}})
        eng = self.engine(max_calls=1, concurrency=4)
        original_check = eng._budget_ok
        # All workers check the ceiling before any backend result is available.
        # Synchronizing this boundary makes the old race deterministic.
        barrier = threading.Barrier(4)

        def concurrent_check():
            allowed = original_check()
            barrier.wait(timeout=10)
            return allowed

        eng._budget_ok = concurrent_check
        result = eng.run(self.units(4))
        self.assertEqual(result["calls"], 1)
        self.assertEqual(len(result["ok"]), 1)
        self.assertEqual(len(result["stopped"]), 3)
        self.assertEqual(len(_engine.read_calls(eng.run_dir)), 1)

    def test_render_strict(self):
        self.assertEqual(_engine.render("hi {name} {\"json\": 1}", {"name": "Alex"}), "hi Alex {\"json\": 1}")
        with self.assertRaises(_engine.RenderError):
            _engine.render("hi {missing}", {"name": "Alex"})

    def test_subprocess_template_omits_empty_model(self):
        b = _engine.SubprocessBackend.__new__(_engine.SubprocessBackend)
        b.kind, b.model, b.run_dir, b.timeout, b.cfg, b.isolate = "claude", "", self.paths.home / "run", 10, {}, False
        b.template = ["mind-reading-missing-binary-for-tests", "--model", "{model}", "-p", "{schema_json}"]
        unit = self.units(1)[0]
        res = b.call(unit, "x", 1)  # command assembly runs; the binary does not exist, so nothing is spent
        self.assertFalse(res.ok)
        self.assertIn("cannot run", res.error)
        self.assertEqual(res.exit_code, -2)

    def test_engine_schema_strips_meta_keys_in_rendered_command(self):
        from mr.engine import load_schema
        for name in ("mined", "requirements", "score", "index"):
            with self.subTest(name=name):
                raw = load_schema(name)
                self.assertIn("$schema", raw)  # the files stay self-describing
                clean = _engine.engine_schema(raw)
                for k in _engine.SCHEMA_META_KEYS:
                    self.assertNotIn(k, clean)
                self.assertEqual(clean["properties"], raw["properties"])
        b = _engine.SubprocessBackend.__new__(_engine.SubprocessBackend)
        b.kind, b.model, b.run_dir, b.timeout, b.cfg, b.isolate = "claude", "sonnet", self.paths.home / "run", 10, {}, False
        b.template = list(self.cfg["engine_cmd"]["claude"])
        self.assertNotIn("--bare", b.template)  # bare mode skips OAuth and would bill an API key
        unit = _engine.Unit("u", "p", load_schema("mined"), self.paths.home / "o.json")
        work = self.paths.home / "work"
        cmd, out_path, tmpdir = b.command(unit, work)
        rendered = json.loads(cmd[cmd.index("--json-schema") + 1])
        self.assertNotIn("$schema", rendered)
        self.assertNotIn("title", rendered)
        self.assertEqual(rendered["required"], load_schema("mined")["required"])
        self.assertNotIn("$schema", read_json(work / "schema.json"))

    def test_codex_runs_with_isolated_home_holding_only_auth(self):
        real = self.paths.home / "real-codex"
        real.mkdir()
        (real / "auth.json").write_text("{}", encoding="utf-8")
        (real / "AGENTS.md").write_text("always-on block that must not reach the engine\n", encoding="utf-8")
        (real / "config.toml").write_text("sandbox_mode = 'danger-full-access'\n", encoding="utf-8")
        cfg = dict(self.cfg, codex_home=str(real))
        b = _engine.SubprocessBackend("codex", ["mind-reading-missing-binary-for-tests", "-"], "", self.paths.home / "run", 10, cfg=cfg)
        env = b.environment()
        home = env["CODEX_HOME"]
        self.assertTrue(home.startswith(str(self.paths.home / "run")))
        self.assertEqual(sorted(p.name for p in (self.paths.home / "run" / "codex-home").iterdir()), ["auth.json"])
        self.assertTrue((self.paths.home / "run" / "codex-home" / "auth.json").is_symlink())
        _engine.remove_isolated_codex_home(self.paths.home / "run")
        self.assertFalse((self.paths.home / "run" / "codex-home").exists())
        env2 = b.environment()  # recreated for the next call
        self.assertTrue((self.paths.home / "run" / "codex-home" / "auth.json").exists())
        self.assertEqual(env2["CODEX_HOME"], home)
        off = _engine.SubprocessBackend("codex", ["x", "-"], "", self.paths.home / "run2", 10, cfg=dict(cfg, codex_isolate_home=False))
        self.assertIsNone(off.environment())

    def test_budget_stop_reports_resume(self):
        write_fake(self.fake, "demo", {"default": {"answer": "ok"}})
        eng = self.engine(max_calls=1, concurrency=1)
        res = eng.run(self.units(3))
        self.assertEqual(len(res["stopped"]), 2)
        self.assertIn("2 unit(s) not attempted", res["budget_note"])
        self.assertIn("mind_reading.py demo --run %s" % eng.run_id, res["budget_note"])

    def test_runs_dir_and_resume_override(self):
        write_fake(self.fake, "demo", {"default": {"answer": "ok"}})
        runs = self.paths.home / "eval" / "x" / "runs"
        eng = _engine.Engine(self.paths, self.cfg, "print", "demo", log=helpers.quiet, runs_dir=runs, resume="eval --run abc --dev", private=True)
        res = eng.run(self.units(1))
        self.assertTrue((runs / eng.run_id / "prompts" / "demo-u0.md").exists())
        self.assertFalse(list(self.paths.runs.glob("*")))
        self.assertIn("mind_reading.py eval --run abc --dev --engine print --collect", res["instructions"])
        import stat
        self.assertEqual(stat.S_IMODE(os.stat(runs).st_mode), 0o700)
        self.assertEqual(_engine.latest_run_id(self.paths, "demo", runs), eng.run_id)
        self.assertIsNone(_engine.latest_run_id(self.paths, "demo"))


if __name__ == "__main__":
    unittest.main()
