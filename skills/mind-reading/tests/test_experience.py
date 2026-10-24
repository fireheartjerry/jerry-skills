"""Experience layer: repeat alert, learned reply lessons, situation hints and their packet integration."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import init_home, run_cli
from mr import experience as xp
from mr.context_tools import packet


def _case(i, seed, reply, nxt, family=None, tools=None):
    return {"id": "case-%04d" % i, "family": family or "fam-%d" % (i % 7), "session": "sess-%d" % (i % 11),
            "project": "demo", "input": {"seed": seed, "prior_user_turns": []}, "target": {"text": nxt},
            "historical_response": {"assistant_text": reply, "tools": tools or []}}


def synthetic_cases():
    """Fictional history: silent endings draw nudges, questions draw a plain go-ahead, lists change nothing."""
    cases, i = [], 0
    for k in range(40):  # silent endings: 16 of 40 followed by a nudge
        cases.append(_case(i, "refactor the parser module", "", "keep going" if k < 16 else "now add the export button"))
        i += 1
    for k in range(40):  # asking first: 16 of 40 answered with a plain go-ahead
        cases.append(_case(i, "deploy the staging build", "Built it. Want me to deploy it to staging?",
                           "yes do it" if k < 16 else "rename the settings page"))
        i += 1
    lists = "\n".join("- item %d" % n for n in range(9))
    for k in range(60):  # heavy lists: go-ahead at the base rate only, never friction beyond it
        cases.append(_case(i, "summarize the options", "Here is the summary.\n" + lists,
                           "ok" if k < 6 else "write the onboarding copy"))
        i += 1
    for k in range(100):  # plain finished replies: few reactions
        nxt = "keep going" if k < 4 else ("sure" if k < 14 else "design the pricing table")
        cases.append(_case(i, "build the invoice view", "Implemented the invoice view and the tests pass.", nxt))
        i += 1
    return cases


def write_candidate(root, cases):
    root.mkdir(parents=True, exist_ok=True)
    with open(root / "cases.jsonl", "w", encoding="utf-8") as fh:
        for case in cases:
            fh.write(json.dumps(case) + "\n")
    return root


class OutcomeTest(unittest.TestCase):
    def test_reactions_are_classified_in_priority_order(self):
        self.assertEqual(xp.outcome("fix the export page", "fix the export page please"), "friction")  # a re-send
        self.assertEqual(xp.outcome("fix it", "no, the other file"), "friction")
        self.assertEqual(xp.outcome("fix it", "it still doesn't load"), "friction")
        self.assertEqual(xp.outcome("fix it", "keep going"), "friction")
        self.assertEqual(xp.outcome("fix it", "progress update?"), "friction")
        self.assertEqual(xp.outcome("fix it", "yes do it"), "go_ahead")
        self.assertEqual(xp.outcome("fix it", "go ahead"), "go_ahead")
        self.assertEqual(xp.outcome("fix it", "did you test it on mobile"), "check")
        self.assertIsNone(xp.outcome("fix it", "now build the pricing page"))
        long_paste = "check " + "word " * 60
        self.assertIsNone(xp.outcome("fix it", long_paste))


class LessonTest(unittest.TestCase):
    def rows(self):
        out = []
        for case in synthetic_cases():
            reply = case["historical_response"]["assistant_text"]
            feats = {name: bool(f(reply, [])) for name, f in xp.FEATURES.items()}
            out.append((feats, xp.outcome(case["input"]["seed"], case["target"]["text"])))
        return out

    def test_only_significant_plausible_differences_become_lessons(self):
        lessons = {(l["feature"], l["outcome"]): l for l in xp.lessons_from(self.rows())}
        self.assertIn(("silent_end", "friction"), lessons)
        self.assertIn(("asks_or_plans", "go_ahead"), lessons)
        self.assertNotIn("heavy_format", {f for f, _ in lessons})
        silent = lessons[("silent_end", "friction")]
        self.assertEqual((silent["cases"], silent["events"]), (40, 16))
        self.assertGreater(silent["rate"], silent["otherwise"] * xp.MIN_LIFT)
        self.assertGreaterEqual(silent["z"], xp.MIN_Z)

    def test_no_lesson_without_enough_evidence(self):
        rows = self.rows()[:20]
        self.assertEqual(xp.lessons_from(rows), [])

    def test_lesson_text_carries_the_users_numbers(self):
        text = xp.lesson_text({"feature": "silent_end", "outcome": "friction", "rate": 0.4, "otherwise": 0.062,
                               "lift": 6.4, "cases": 40, "events": 16, "z": 5.0})
        self.assertIn("End every turn with a written result", text)
        self.assertIn("40%", text)
        self.assertIn("6.2%", text)
        self.assertIn("n=40", text)
        ask = xp.lesson_text({"feature": "asks_or_plans", "outcome": "go_ahead", "rate": 0.4, "otherwise": 0.1,
                              "lift": 4.0, "cases": 40, "events": 16, "z": 3.0})
        self.assertIn("plain go-ahead 40%", ask)


class SummaryCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.paths, self.cfg = init_home(self.root / "home")
        self.cand = write_candidate(self.root / "cand", synthetic_cases())
        self.cfg["candidate"] = str(self.cand)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("MIND_READING_CANDIDATE", None)

    def tearDown(self):
        self.tmp.cleanup()

    def test_summary_is_cached_and_rebuilt_when_the_candidate_changes(self):
        first = xp.load_summary(self.paths, self.cfg)
        self.assertEqual(first["replies"], 240)
        cache = Path(self.paths.derived) / xp.SUMMARY_NAME
        self.assertTrue(cache.is_file())
        self.assertEqual(oct(cache.stat().st_mode & 0o777), "0o600")
        with mock.patch.object(xp, "build_summary", side_effect=AssertionError("cache not used")):
            self.assertEqual(xp.load_summary(self.paths, self.cfg)["replies"], 240)
        write_candidate(self.cand, synthetic_cases()[:200])
        self.assertEqual(xp.load_summary(self.paths, self.cfg)["replies"], 200)

    def test_no_candidate_means_no_summary(self):
        self.cfg["candidate"] = ""
        self.assertIsNone(xp.load_summary(self.paths, self.cfg))
        self.cfg["candidate"] = str(self.root / "missing")
        self.assertIsNone(xp.load_summary(self.paths, self.cfg))

    def test_environment_overrides_config(self):
        other = write_candidate(self.root / "other", synthetic_cases()[:100])
        os.environ["MIND_READING_CANDIDATE"] = str(other)
        self.assertEqual(xp.candidate_dir(self.cfg), other)


class LiveSignalTest(unittest.TestCase):
    def test_repeat_alert_fires_on_a_restated_request(self):
        rep = xp.repeat_check("make the navbar sticky on mobile please",
                              {"recent_user_turns": ["make the navbar sticky on mobile", "looks good"]})
        self.assertEqual(rep["earlier"]["turns_back"], 2)

    def test_repeat_alert_ignores_the_seed_echo_and_short_overlap(self):
        self.assertIsNone(xp.repeat_check("make the navbar sticky on mobile",
                                          {"recent_user_turns": ["fix login", "make the navbar sticky on mobile"]}))
        self.assertIsNone(xp.repeat_check("push it", {"recent_user_turns": ["fix the bug and push it"]}))
        self.assertIsNone(xp.repeat_check("add dark mode to settings", {"recent_user_turns": ["write the README"]}))

    def test_a_repeated_nudge_asks_to_finish_not_to_change_course(self):
        rep = xp.repeat_check("keep going with the export work", {"recent_user_turns": ["keep going with the export work"
                                                                                        " now", "ok"]})
        self.assertEqual(rep["kind"], "continue")
        text = xp.render({"repeat": rep})
        self.assertIn("ask you to continue again", text)
        self.assertIn("finish line", text)
        self.assertNotIn("Change the approach", text)
        restate = xp.render({"repeat": xp.repeat_check("make the navbar sticky on mobile please",
                                                       {"recent_user_turns": ["make the navbar sticky on mobile", "ok"]})})
        self.assertIn("Change the approach", restate)

    def test_repeat_wording_fires_without_history(self):
        rep = xp.repeat_check("it's still not loading", {})
        self.assertEqual(rep["marker"], "still not")
        self.assertIsNone(rep["earlier"])
        self.assertEqual(xp.repeat_check("again, put every demo on the second monitor", {})["marker"].lower(), "again")
        self.assertIsNotNone(xp.repeat_check("like i said, use the staging database", {}))

    def test_injected_context_is_ignored(self):
        block = '<in-app-browser-context source="ambient-ui-state">Tabs: docs, dashboard, pricing page</in-app-browser-context>\n'
        self.assertIsNone(xp.repeat_check(block + "rename the settings page",
                                          {"recent_user_turns": [block + "write the onboarding copy"]}))
        files = "# Files mentioned by the user:\n\n## shot.png: /x/shot.png\n\n## My request for Codex:\nkeep going"
        self.assertEqual(xp.human_text(files), "keep going")
        self.assertEqual(xp.outcome("fix it", files), "friction")
        self.assertEqual(xp.outcome("fix it", block + "yes do it"), "go_ahead")

    def test_plain_redo_requests_are_not_repeats(self):
        for seed in ("run the tests again", "open it again and check the layout", "I asked Codex for a review",
                     "send me this again"):
            self.assertIsNone(xp.repeat_check(seed, {}), seed)

    def test_situation_hints_use_the_configured_timezone(self):
        cfg = {"timezone": "America/Toronto", "people": {"Jordan Lee": "<stakeholder>"}}
        late = xp.situation("ship it", {}, cfg, now="2026-01-15T07:30:00Z")  # 02:30 in Toronto
        self.assertTrue(any(h.startswith("late night (02:30") for h in late))
        day = xp.situation("ship it", {}, cfg, now="2026-01-15T17:30:00Z")
        self.assertFalse(any(h.startswith("late night") for h in day))

    def test_situation_hints_cover_length_pressure_emphasis_and_people(self):
        cfg = {"timezone": "UTC", "people": {"Jordan Lee": "<stakeholder>"}}
        hints = xp.situation("send the deck to Jordan Lee before the meeting, make it ALL fit on one page!!",
                             {}, cfg, now="2026-01-15T15:00:00Z")
        joined = "\n".join(hints)
        self.assertIn("time pressure", joined)
        self.assertIn("(ALL)", joined)
        self.assertIn("frustration markers", joined)
        self.assertIn("Jordan Lee (stakeholder)", joined)
        self.assertNotIn("(API", "\n".join(xp.situation("the API and CLI are down", {}, cfg, now="2026-01-15T15:00:00Z")))
        long = xp.situation(" ".join(["word"] * 90), {}, cfg, now="2026-01-15T15:00:00Z")
        self.assertIn("long dictated message", "\n".join(long))

    def test_bad_time_is_rejected(self):
        with self.assertRaises(xp.ExperienceError):
            xp.situation("hi", {}, {}, now="yesterday")


class PacketIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cand = write_candidate(self.root / "cand", synthetic_cases())
        self.paths, self.cfg = init_home(self.root / "home", people={"Jordan Lee": "<stakeholder>"})
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("MIND_READING_CANDIDATE", None)
        from mr.working_memory import remember
        remember(self.paths.home, dict(kind="goal", text="Finish the navbar fix", project="demo",
                                       source_ref="user:turn-1"))

    def tearDown(self):
        self.tmp.cleanup()

    def run_packet(self, **extra):
        data = {"seed": "make the navbar sticky on mobile please", "project": "demo", "no_light": True,
                "context": {"recent_user_turns": ["make the navbar sticky on mobile", "looks good"]},
                "now": "2026-01-15T15:00:00Z"}
        data.update(extra)
        return packet(self.paths, self.cfg, data)

    def test_off_by_default(self):
        result = self.run_packet()
        self.assertNotIn("## Experience layer", result["slice"])
        self.assertEqual(result["decision"]["experience"], {"enabled": False})

    def test_enabled_layer_adds_repeat_lessons_and_situation(self):
        self.cfg["experience"]["enabled"] = True
        self.cfg["candidate"] = str(self.cand)
        result = self.run_packet()
        self.assertEqual(result["seed"], "make the navbar sticky on mobile please")
        self.assertTrue(result["decision"]["literal_seed_preserved"])
        sl = result["slice"]
        self.assertIn("## Experience layer", sl)
        self.assertIn("### Repeat alert", sl)
        self.assertIn("### What lands with this user (learned from 240 past replies", sl)
        self.assertIn("End every turn with a written result", sl)
        info = result["decision"]["experience"]
        self.assertTrue(info["enabled"] and info["trace_store"] and info["repeat"])
        self.assertIn("silent_end", info["lessons"])
        self.assertEqual(info["chars"], len(sl) - sl.index("## Experience layer"))

    def test_packet_fields_override_config_for_ablation(self):
        self.cfg["experience"]["enabled"] = True
        self.cfg["candidate"] = str(self.cand)
        self.assertNotIn("## Experience layer", self.run_packet(experience=False)["slice"])
        terse = self.run_packet(seed="ship it", context={})
        self.assertIn("### Situation", terse["slice"])
        ablated = self.run_packet(seed="ship it", context={}, situation=False)
        self.assertNotIn("### Situation", ablated["slice"])
        self.assertEqual(ablated["decision"]["experience"]["situation"], 0)

    def test_layer_works_without_a_candidate(self):
        result = self.run_packet(experience=True)
        self.assertIn("### Repeat alert", result["slice"])
        self.assertNotIn("What lands with this user", result["slice"])
        self.assertFalse(result["decision"]["experience"]["trace_store"])

    def test_broken_candidate_never_blocks_the_packet(self):
        (self.cand / "cases.jsonl").write_text("{not json\n", encoding="utf-8")
        self.cfg["experience"]["enabled"] = True
        self.cfg["candidate"] = str(self.cand)
        result = self.run_packet()
        self.assertIn("error", result["decision"]["experience"])
        self.assertNotIn("## Experience layer", result["slice"])

    def test_odd_candidate_rows_never_block_the_packet(self):
        (self.cand / "cases.jsonl").write_text('{"input": ["not", "an", "object"]}\n', encoding="utf-8")
        self.cfg["experience"]["enabled"] = True
        self.cfg["candidate"] = str(self.cand)
        result = self.run_packet()
        self.assertIn("error", result["decision"]["experience"])
        self.assertEqual(result["seed"], "make the navbar sticky on mobile please")

    def test_field_types_are_validated(self):
        from mr.context_tools import PacketError
        for bad in ({"experience": "yes"}, {"situation": 1}, {"now": 5}, {"now": "x" * 65}):
            with self.assertRaises(PacketError):
                self.run_packet(**bad)


class AlwaysOnTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cand = write_candidate(self.root / "cand", synthetic_cases())
        self.paths, self.cfg = init_home(self.root / "home")
        self.paths.model.mkdir(parents=True, exist_ok=True)
        self.paths.light.write_text("# Light\n\n## Rules\n- Lead with the result.\n", encoding="utf-8")
        self.cfg["candidate"] = str(self.cand)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("MIND_READING_CANDIDATE", None)

    def tearDown(self):
        self.tmp.cleanup()

    def test_lessons_join_the_always_on_block_only_when_opted_in(self):
        from mr.refresh import current_block
        self.assertNotIn("Learned from reactions", current_block(self.paths, self.cfg))
        self.cfg["experience"]["enabled"] = True
        self.assertNotIn("Learned from reactions", current_block(self.paths, self.cfg))
        self.cfg["experience"]["always_on"] = True
        block = current_block(self.paths, self.cfg)
        self.assertIn("### Learned from reactions to past replies", block)
        self.assertIn("End every turn with a written result", block)
        self.assertTrue(block.startswith("<!-- BEGIN mind-reading"))
        self.assertLess(block.index("Lead with the result"), block.index("Learned from reactions"))


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cand = write_candidate(self.root / "cand", synthetic_cases())
        init_home(self.root / "home", cfg_overrides={"candidate": str(self.cand), "experience": {"enabled": True}})

    def tearDown(self):
        self.tmp.cleanup()

    def test_status_build_and_show(self):
        env = {"MIND_READING_CANDIDATE": ""}
        out = run_cli(self.root / "home", "experience", "build", "--json", env_extra=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        report = json.loads(out.stdout)
        self.assertEqual(report["replies"], 240)
        self.assertTrue(report["enabled"])
        out = run_cli(self.root / "home", "experience", "status", env_extra=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("learned from 240 replies", out.stdout)
        ctx = self.root / "ctx.json"
        ctx.write_text(json.dumps({"recent_user_turns": ["make the navbar sticky on mobile"]}), encoding="utf-8")
        out = run_cli(self.root / "home", "experience", "show", "--seed", "make the navbar sticky on mobile now",
                      "--context-file", str(ctx), "--now", "2026-01-15T15:00:00Z", env_extra=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("### Repeat alert", out.stdout)

    def test_packet_cli_includes_the_layer(self):
        out = run_cli(self.root / "home", "packet", env_extra={"MIND_READING_CANDIDATE": ""},
                      stdin=json.dumps({"seed": "ship it", "no_light": True, "now": "2026-01-15T15:00:00Z",
                                        "context": {"goal": "Release the build"}}))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("## Experience layer", out.stdout)


if __name__ == "__main__":
    unittest.main()
