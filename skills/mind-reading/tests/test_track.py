import json
import os
import unittest
from pathlib import Path

import helpers
import synth_transcripts as T
from mr import track
from mr.config import read_json, write_json
from mr.refresh import parse_corrections

CLASSIFY_CASES = [
    ("no, I meant the PDF export", "correction", "C-meant"),
    ("nope", "correction", "C-no"),
    ("wait", "correction", "C-wait"),
    ("actually make it red", "correction", "C-actually"),
    ("why did you push to main?", "correction", "C-why"),
    ("not the CSV, the PDF", "correction", "C-not-start"),
    ("that's not what I asked", "correction", "C-not-that"),
    ("undo that", "correction", "C-undo"),
    ("revert it please", "correction", "C-undo"),
    ("use postgres instead", "correction", "C-instead"),
    ("don't touch the tests", "correction", "C-dont"),
    ("nvm", "correction", "C-scratch"),
    ("way too long", "correction", "C-too-much"),
    ("you missed the migration", "correction", "C-wrong"),
    ("no worries", "approval", "A-only"),
    ("yes", "approval", "A-only"),
    ("keep going", "approval", "A-only"),
    ("lgtm, ship it", "approval", "A-only"),
    ("1 yes 2 no", "approval", "A-held-answer"),
    ("perfect, now do the settings page", "approval", "A-lead"),
    ("ok but skip the docs", "refinement", "R-yes-but"),
    ("also add a changelog entry", "refinement", "R-also"),
    ("make sure the tests pass", "refinement", "R-make-sure"),
    ("don't forget the docs", "refinement", "R-make-sure"),
    ("and the invoices page too", "refinement", "R-also"),
    ("update the readme as well", "refinement", "R-too"),
    ("fix the flaky login test", "unrelated", "U-default"),
    ("", "no_reply", "N-no-reply"),
]


class ClassifierTests(unittest.TestCase):
    def test_rule_table(self):
        for text, cls, rule in CLASSIFY_CASES:
            with self.subTest(text=text):
                self.assertEqual(track.classify_reply(text), (cls, rule))

    def test_every_rule_is_exercised_and_ids_are_unique(self):
        ids = [r[0] for r in track.RULES]
        self.assertEqual(len(ids), len(set(ids)))
        exercised = {rule for _, _, rule in CLASSIFY_CASES}
        self.assertEqual(set(ids) - exercised, set())

    def test_interrupt_turns_an_unmatched_reply_into_a_redirect_only(self):
        self.assertEqual(track.classify_reply("fix the flaky login test", interrupted=True), ("correction", "C-interrupt"))
        self.assertEqual(track.classify_reply("yes", interrupted=True), ("approval", "A-only"))
        self.assertEqual(track.classify_reply("also the docs", interrupted=True), ("refinement", "R-also"))

    def test_extract_meant(self):
        self.assertEqual(track.extract_meant("no, I meant the PDF export on the invoices page"),
                         ("the PDF export on the invoices page", "n/a", "M-meant"))
        self.assertEqual(track.extract_meant("not the CSV, the PDF one"), ("the PDF one", "the CSV", "M-not-but"))
        self.assertEqual(track.extract_meant("use postgres instead"), ("use postgres", "n/a", "M-instead"))
        self.assertEqual(track.extract_meant("instead of mocking, hit the staging api"),
                         ("hit the staging api", "mocking", "M-instead-of"))
        self.assertEqual(track.extract_meant("no, use the staging database"), ("use the staging database", "n/a", "M-no-do"))
        for vague in ("wait", "no", "why did you do that", "nope not that"):
            self.assertIsNone(track.extract_meant(vague))

    def test_week_in_the_configured_timezone(self):
        self.assertEqual(track.week_of("2026-03-02T03:00:00Z", track._tz({"timezone": "UTC"})), "2026-W10")
        self.assertEqual(track.week_of("2026-03-02T03:00:00Z", track._tz({"timezone": "America/Los_Angeles"})), "2026-W09")
        self.assertEqual(track.week_of(None, track._tz({})), "unknown")


def claude_session(seed_line, reply, session="s1", t0="2026-03-02T10:00", skill=True):
    lines = [T.claude_listing(t0 + ":00Z"), T.claude_user(session + "-u1", t0 + ":01Z", seed_line, session=session)]
    if skill:
        lines.append(T.claude_skill(t0 + ":02Z"))
        lines.append(T.claude_tool_result(session + "-tr", t0 + ":03Z", "t1", "Launching skill: mind-reading"))
    lines.append(T.claude_text(t0 + ":04Z", "Reading: something"))
    if reply is not None:
        lines.append(T.claude_user(session + "-u2", t0 + ":30Z", reply, session=session))
    return lines


def finalize(invs):
    return [track._finalize(i) for i in invs]


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.root = helpers.tmpdir()

    def scan(self, lines, session="s1"):
        return finalize(track.scan_claude_file(T.write_claude(self.root / "claude", "-home-user-Code-webshop", session, lines), True))

    def test_phrase_and_skill_call_are_one_invocation(self):
        invs = self.scan(claude_session("read my mind: the export is broken again", "no, I meant the PDF export"))
        self.assertEqual(len(invs), 1)
        inv = invs[0]
        self.assertEqual((inv["markers"], inv["class"], inv["rule"]), (["phrase", "skill_tool"], "correction", "C-meant"))
        self.assertEqual(inv["seed_hash"], track.seed_hash("the export is broken again"))
        self.assertEqual(inv["gap_min"], 0.5)

    def test_skill_call_after_a_plain_message_and_many_tool_results(self):
        lines = [T.claude_user("u1", "2026-03-02T10:00:00Z", "clean up the repo")]
        for k in range(80):
            lines.append(T.claude_assistant("2026-03-02T10:00:01Z", [{"type": "tool_use", "id": "b%d" % k, "name": "Bash", "input": {"command": "ls"}}]))
            lines.append(T.claude_tool_result("r%d" % k, "2026-03-02T10:00:02Z", "b%d" % k, "file.txt"))
        lines += [T.claude_skill("2026-03-02T10:01:00Z", skill="tools:mind-reading"), T.claude_user("u2", "2026-03-02T10:02:00Z", "yes go")]
        invs = self.scan(lines)
        self.assertEqual(len(invs), 1)
        self.assertEqual((invs[0]["markers"], invs[0]["class"]), (["skill_tool"], "approval"))
        self.assertEqual(invs[0]["seed_hash"], track.seed_hash("clean up the repo"))

    def test_slash_mention_and_what_never_counts(self):
        lines = [
            T.claude_slash("u1", "2026-03-02T10:00:00Z", "deploy it"),
            T.claude_user("u1m", "2026-03-02T10:00:01Z", "Base directory for this skill: /x/mind-reading", isMeta=True),
            T.claude_user("u2", "2026-03-02T10:01:00Z", "also add a changelog entry"),
            T.claude_user("u3", "2026-03-02T10:05:00Z", "she typed 'read my mind: ship it' in the doc"),
            T.claude_user("u4", "2026-03-02T10:06:00Z", "fix the mind-reading tags, they are noisy"),
            T.claude_skill("2026-03-02T10:06:30Z", skill="other-skill"),
            T.claude_user("u5", "2026-03-02T10:07:00Z", "read my mind: tidy the readme", isSidechain=True),
            T.claude_user("u6", "2026-03-02T10:08:00Z", "make the footer sticky"),
            T.claude_mention("2026-03-02T10:08:01Z"),
            T.claude_user("u7", "2026-03-02T10:09:00Z", "<command-name>/clear</command-name>"),
            T.claude_user("u8", "2026-03-02T10:10:00Z", "wait, only on mobile"),
        ]
        invs = self.scan(lines)
        self.assertEqual([(i["markers"], i["class"]) for i in invs], [(["slash"], "refinement"), (["skill_mention"], "correction")])

    def test_sdk_sessions_are_skipped(self):
        lines = claude_session("read my mind: ship it", "yes")
        lines = [l.replace('"entrypoint": "cli"', '"entrypoint": "sdk-cli"') for l in lines]
        self.assertEqual(self.scan(lines), [])

    def test_interrupt_then_new_instruction_is_a_redirect(self):
        lines = claude_session("read my mind: refactor the cart", None)
        lines += [T.claude_user("ui", "2026-03-02T10:00:20Z", "[Request interrupted by user]"),
                  T.claude_user("u3", "2026-03-02T10:00:40Z", "the checkout module only")]
        inv = self.scan(lines)[0]
        self.assertTrue(inv["interrupted"])
        self.assertEqual((inv["class"], inv["rule"]), ("correction", "C-interrupt"))

    def test_no_reply(self):
        inv = self.scan(claude_session("read my mind: ship it", None))[0]
        self.assertEqual((inv["class"], inv["rule"]), ("no_reply", "N-no-reply"))

    def test_codex(self):
        root = self.root / "codex"
        p = T.write_codex(root, "rollout-a", [
            T.codex_meta(), T.codex_user("2026-03-03T09:00:00Z", "$mind-reading clean up the flaky tests", "m1"),
            T.codex_skill("2026-03-03T09:00:01Z"), T.codex_assistant("2026-03-03T09:00:05Z", "Reading: ..."),
            T.codex_abort("2026-03-03T09:01:00Z"), T.codex_user("2026-03-03T09:01:05Z", "the integration ones only", "m2"),
            T.codex_user("2026-03-03T09:05:00Z", "rename the config loader", "m3"), T.codex_skill("2026-03-03T09:05:01Z"),
            T.codex_user("2026-03-03T09:06:00Z", '<codex_internal_context source="goal">keep going</codex_internal_context>', "g1"),
            T.codex_user("2026-03-03T09:07:00Z", "<send_user_message_question_reply>[{\"question\": \"which?\", \"answer\": \"the second one\"}]</send_user_message_question_reply>", "q1"),
        ])
        invs = finalize(track.scan_codex_file(p, None, True))
        self.assertEqual([(i["markers"], i["class"], i["rule"]) for i in invs],
                         [(["codex_skill", "phrase"], "correction", "C-interrupt"), (["codex_skill"], "unrelated", "U-default")])
        exec_p = T.write_codex(root, "rollout-exec", [T.codex_meta(source="exec"), T.codex_user("2026-03-03T09:00:00Z", "$mind-reading x y", "m1")])
        self.assertEqual(track.scan_codex_file(exec_p, None, True), [])
        home = self.root / "home"
        engine_p = T.write_codex(root, "rollout-engine", [T.codex_meta(cwd=str(home / "runs" / "r1" / "work")),
                                                          T.codex_user("2026-03-03T09:00:00Z", "$mind-reading x y", "m1")])
        self.assertEqual(track.scan_codex_file(engine_p, home, True), [])

    def test_parts_of_one_message_are_one_turn(self):
        cx = T.write_codex(self.root / "codex", "rollout-multi", [
            T.codex_meta(),
            json.dumps({"type": "response_item", "timestamp": "2026-03-03T09:00:00Z", "payload": {
                "type": "message", "role": "user", "id": "m1", "content": [
                    {"type": "input_text", "text": "$mind-reading clean up the flaky tests"},
                    {"type": "input_text", "text": "make sure the ci stays green"}]}}),
            T.codex_user("2026-03-03T09:03:00Z", "yes go", "m2")])
        invs = finalize(track.scan_codex_file(cx, None, True))
        self.assertEqual([(i["class"], i["gap_min"], i["time_reliable"]) for i in invs], [("approval", 3.0, True)])
        stamped = T.write_codex(self.root / "codex", "rollout-import", [
            T.codex_meta(), T.codex_user("2026-03-03T09:00:00.100Z", "$mind-reading clean up the flaky tests", "m1"),
            T.codex_user("2026-03-03T09:00:00.120Z", "yes go", "m2")])
        imp = finalize(track.scan_codex_file(stamped, None, True))
        self.assertEqual([(i["class"], i["time_reliable"]) for i in imp], [("approval", False)])
        self.assertEqual(invs[0]["seed_hash"], track.seed_hash("clean up the flaky tests\n\nmake sure the ci stays green"))
        two_parts = json.dumps({"type": "user", "uuid": "u1", "timestamp": "2026-03-02T10:00:00Z", "userType": "external",
                                "message": {"role": "user", "content": [{"type": "text", "text": "read my mind: tidy the readme"},
                                                                        {"type": "text", "text": "also the changelog"}]}})
        invs = self.scan([two_parts, T.claude_user("u2", "2026-03-02T10:02:00Z", "wait")])
        self.assertEqual([(i["class"], i["rule"]) for i in invs], [("correction", "C-wait")])

    def test_engine_run_project_dirs_are_skipped(self):
        marks = track._home_markers("/home/user/.local/share/mind-reading")
        self.assertTrue(track.skip_claude_project("-home-user--local-share-mind-reading-runs-1-work-cwd", marks))
        self.assertTrue(track.skip_claude_project("-Users-x--local-share-mind-reading-v3-eval-303589d2-runs-1-cwd", set()))
        self.assertFalse(track.skip_claude_project("-home-user-Code-webshop", marks))


class RunTrackTests(unittest.TestCase):
    SECRET_TEXTS = ("the export is broken again", "the PDF export on the invoices page", "tidy the release notes",
                    "rename the config loader", "ship the onboarding flow")

    def setUp(self):
        tmp = helpers.tmpdir()
        self.claude, self.codex = tmp / "claude", tmp / "codex"
        self.paths, self.cfg = helpers.init_home(tmp / "home", cfg_overrides={
            "sources": {"claude_projects_dir": str(self.claude), "codex_sessions_dir": str(self.codex)}, "timezone": "UTC"})
        T.write_claude(self.claude, "-home-user-Code-webshop", "s1", claude_session(
            "read my mind: the export is broken again", "no, I meant the PDF export on the invoices page", "s1", "2026-03-02T10:00"))
        T.write_claude(self.claude, "-home-user-Code-webshop", "s2", claude_session(
            "read my mind: tidy the release notes", "yes go", "s2", "2026-03-10T10:00"))
        T.write_codex(self.codex, "rollout-c1", [T.codex_meta(), T.codex_user("2026-03-10T11:00:00Z", "$mind-reading rename the config loader", "m1"),
                                                  T.codex_user("2026-03-10T11:02:00Z", "also update the docs", "m2")])
        # an engine run inside the data home: never read
        T.write_claude(self.claude, track.re.sub(r"[/.]", "-", str(self.paths.home)) + "-runs-1-work-cwd", "e1",
                       claude_session("read my mind: ship the onboarding flow", "yes", "e1"))

    def run_track(self, **kw):
        return track.run_track(self.paths, self.cfg, log=helpers.quiet, **kw)

    def test_end_to_end_counts_rates_and_no_text_on_disk(self):
        s = self.run_track()
        self.assertEqual((s["invocations"], s["classified"]), (3, 3))
        self.assertEqual(s["counts"], {"correction": 1, "approval": 1, "refinement": 1})
        self.assertEqual(s["correction_rate"]["k"], 1)
        self.assertAlmostEqual(s["correction_rate"]["rate"], 0.3333, places=4)
        self.assertEqual(sorted(s["by_week"]), ["2026-W10", "2026-W11"])
        for p in (self.paths.track_records, self.paths.track_summary, self.paths.track_state):
            body = p.read_text(encoding="utf-8")
            for t in self.SECRET_TEXTS:
                self.assertNotIn(t, body, "%s leaks text" % p.name)
            self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)
        rows = helpers.read_jsonl(self.paths.track_records)
        self.assertEqual({r["rule"] for r in rows}, {"C-meant", "A-only", "R-also"})

    def test_incremental_rescan_and_rows_outlive_their_transcripts(self):
        self.run_track()
        s2 = self.run_track()
        self.assertEqual((s2["files_scanned"], s2["files_unchanged"]), (0, 3))
        self.assertEqual(s2["invocations"], 3)
        p = self.claude / "-home-user-Code-webshop" / "s2.jsonl"
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(T.claude_user("s2-u9", "2026-03-10T12:00:00Z", "read my mind: polish the pricing page", session="s2") + "\n")
            fh.write(T.claude_user("s2-u10", "2026-03-10T12:05:00Z", "nope", session="s2") + "\n")
        s3 = self.run_track()
        self.assertEqual((s3["files_scanned"], s3["invocations"], s3["counts"]["correction"]), (1, 4, 2))
        p.unlink()
        s4 = self.run_track()
        self.assertEqual(s4["invocations"], 4)
        state = read_json(self.paths.track_state)
        state["rules_version"] = 0
        write_json(self.paths.track_state, state)
        s5 = self.run_track()
        self.assertEqual(s5["files_scanned"], 2)  # a rules change rescans every file that still exists

    def test_a_resumed_session_copy_counts_once_and_keeps_the_reply(self):
        lines = claude_session("read my mind: the export is broken again", None, "s1", "2026-03-02T10:00")
        T.write_claude(self.claude, "-home-user-Code-webshop", "s1", lines)  # the original ended without a reply
        resumed = lines + [T.claude_user("s1-u2", "2026-03-02T10:00:30Z", "no, I meant the PDF export on the invoices page", session="s1r")]
        T.write_claude(self.claude, "-home-user-Code-webshop-worktree", "s1r", resumed)
        s = self.run_track()
        self.assertEqual(s["invocations"], 3)
        rows = {r["id"]: r for r in helpers.read_jsonl(self.paths.track_records)}
        self.assertEqual(rows[track.inv_id("claude-code", "s1-u1")]["rule"], "C-meant")

    def test_decisions_corrections_and_model_attribution(self):
        self.paths.model.mkdir(parents=True, exist_ok=True)
        with open(self.paths.decisions, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"date": "2026-03-10", "seed_hash": track.seed_hash("tidy the release notes"), "mode": "execute",
                                 "model_hash": "bbbbbbbbbbbbbbbb"}) + "\n")
            fh.write(json.dumps({"date": "2026-03-11", "seed_hash": "deadbeef", "mode": "execute"}) + "\n")
        self.paths.corrections.write_text(
            "2026-03-10 | seed: \"tidy the release notes\" | wrong: assumed all notes | meant: only this release | scope: global | kind: fix\n"
            "2026-03-12 | seed: \"something from another machine\" | wrong: n/a | meant: the other one | scope: global | kind: fix\n",
            encoding="utf-8")
        self.paths.history.write_text(json.dumps({"time": "2026-03-01T00:00:00Z", "event": "build", "model_hash": "aaaaaaaaaaaaaaaa"}) + "\n" +
                                      json.dumps({"time": "2026-03-09T00:00:00Z", "event": "auto", "model_hash": "cccccccccccccccc"}) + "\n",
                                      encoding="utf-8")
        s = self.run_track()
        self.assertEqual(s["invocations"], 5)  # 3 transcript + 1 unmatched decision + 1 unmatched correction
        self.assertEqual(s["classified"], 4)
        self.assertEqual(s["counts"]["correction"], 3)
        self.assertEqual((s["logged_corrections"], s["decisions_joined"]), (2, 2))
        rows = {r["id"]: r for r in helpers.read_jsonl(self.paths.track_records)}
        tidy = rows[track.inv_id("claude-code", "s2-u1")]
        self.assertEqual((tidy["class"], tidy["rule"], tidy["rule_scan"]), ("correction", "L-logged-correct", "A-only"))
        self.assertEqual((tidy["model_hash"], tidy["model_hash_from"]), ("bbbbbbbbbbbbbbbb", "decision"))
        self.assertEqual(rows[track.inv_id("claude-code", "s1-u1")]["model_hash"], "aaaaaaaaaaaaaaaa")
        self.assertEqual(rows[track.inv_id("codex", "m1")]["model_hash"], "cccccccccccccccc")
        self.assertEqual(set(s["by_model"]), {"aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb", "cccccccccccccccc"})
        # the logged line goes away: the invocation returns to its transcript verdict
        self.paths.corrections.write_text("", encoding="utf-8")
        s2 = self.run_track()
        self.assertEqual(s2["counts"]["correction"], 1)
        self.assertEqual(s2["counts"]["approval"], 1)

    def test_drafts_review_and_promotion(self):
        s = self.run_track()
        self.assertEqual(s["drafts"]["drafted"], 1)
        text = self.paths.corrections.read_text(encoding="utf-8")
        self.assertTrue(text.startswith(track.PENDING_PREFIX))
        self.assertEqual(parse_corrections(text), ([], []))  # a pending draft is a comment: never applied, never unparseable
        self.run_track()
        self.assertEqual(self.paths.corrections.read_text(encoding="utf-8"), text)  # not drafted twice
        # approve by deleting the prefix: counted once, as the invocation it came from
        body = text.strip().split(" | ", 1)[1]
        self.paths.corrections.write_text(body + "\n", encoding="utf-8")
        s3 = self.run_track()
        self.assertEqual((s3["drafts"]["approved"], s3["counts"]["correction"], s3["logged_corrections"]), (1, 1, 0))
        # reject by deleting the line
        self.paths.corrections.write_text("", encoding="utf-8")
        self.assertEqual(self.run_track()["drafts"]["rejected"], 1)

    def test_same_correction_three_times_is_promoted_once(self):
        for k in (3, 4, 5):
            T.write_claude(self.claude, "-home-user-Code-other", "p%d" % k, claude_session(
                "read my mind: export run %d" % k, "no, I meant the PDF export on the invoices page", "p%d" % k, "2026-03-1%dT10:00" % k))
        s = self.run_track()
        self.assertEqual(s["drafts"]["promoted_now"], 1)
        entries, _ = parse_corrections(self.paths.corrections.read_text(encoding="utf-8"))
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["meant"], "the PDF export on the invoices page")
        self.assertEqual(s["counts"]["correction"], 4)  # the promoted line is the tracker's, not a fifth correction
        self.assertEqual(self.run_track()["drafts"]["promoted_now"], 0)

    def test_a_correction_that_reads_as_a_permission_is_never_drafted(self):
        T.write_claude(self.claude, "-home-user-Code-webshop", "s9", claude_session(
            "read my mind: release it", "no, I meant push without asking me", "s9", "2026-03-04T10:00"))
        s = self.run_track()
        self.assertEqual(s["drafts"]["refused"], 1)
        self.assertNotIn("without asking", self.paths.corrections.read_text(encoding="utf-8"))
        self.assertEqual(track.run_track(self.paths, self.cfg, drafts=False, full=True, log=helpers.quiet)["drafts"]["drafted"], 0)

    def test_engine_classifier_labels_only_unmatched_messages_under_its_cap(self):
        lines = []
        for k in (1, 2):
            lines += claude_session("read my mind: task %d" % k, "the pricing page needs a toggle %d" % k, "e%d" % k, "2026-03-0%dT10:00" % (k + 4))
            T.write_claude(self.claude, "-home-user-Code-webshop", "e%d" % k, lines[-7:])
        fake = helpers.tmpdir()
        ids = [track.inv_id("claude-code", "e%d-u1" % k) for k in (1, 2)]
        write_json(fake / "track.json", {"batch-000": {"items": [{"id": ids[0], "class": "refinement"}]},
                                          "batch-001": {"items": [{"id": ids[1], "class": "correction"}]}})
        self.cfg["track"]["engine_batch"] = 1
        s = self.run_track(engine="fake", engine_model="haiku", max_calls=1, fake_dir=fake)
        rows = {r["id"]: r for r in helpers.read_jsonl(self.paths.track_records)}
        self.assertEqual((rows[ids[0]]["class"], rows[ids[0]]["rule"], rows[ids[0]]["base_rule"]), ("refinement", "engine:haiku", "U-default"))
        self.assertEqual((rows[ids[1]]["class"], rows[ids[1]]["rule"]), ("unrelated", "U-default"))  # the cap stopped its batch
        self.assertEqual(s["counts"]["correction"], 1)
        write_json(fake / "track.json", {"batch-000": {"items": [{"id": ids[1], "class": "correction"}]}})
        s2 = self.run_track(engine="fake", engine_model="haiku", max_calls=5, fake_dir=fake)
        self.assertEqual(s2["counts"]["correction"], 2)
        self.assertEqual(s2["rules"].get("engine:haiku"), 2)

    def test_regression_rule(self):
        def summ(live_k, live_n, par_k, par_n):
            from mr.stats import rate_block
            return {"by_model": {"L": {"correction": rate_block(live_k, live_n)}, "P": {"correction": rate_block(par_k, par_n)}}}
        self.assertIsNotNone(track.regression(summ(8, 10, 1, 10), "L", "P"))
        self.assertIsNone(track.regression(summ(3, 10, 2, 10), "L", "P"))
        self.assertIsNone(track.regression(summ(8, 9, 1, 10), "L", "P"))  # too few under the live model
        self.assertIsNone(track.regression(summ(8, 10, 1, 10), "L", None))


class CliTests(unittest.TestCase):
    def test_track_cli_and_status_line_print_no_text(self):
        tmp = helpers.tmpdir()
        claude = tmp / "claude"
        paths, _ = helpers.init_home(tmp / "home")
        T.write_claude(claude, "-home-user-Code-webshop", "s1", claude_session(
            "read my mind: the export is broken again", "no, I meant the PDF export", "s1", "2026-03-02T10:00"))
        r = helpers.run_cli(paths.home, "track", "--claude-dir", claude, "--codex-dir", tmp / "none", "--no-drafts")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("1 invocation(s), 1 classified; correction rate 100% (1/1", r.stdout)
        for t in ("export is broken", "PDF export"):
            self.assertNotIn(t, r.stdout + r.stderr)
        st = helpers.run_cli(paths.home, "status")
        self.assertIn("tracker (rules v%d" % track.RULES_VERSION, st.stdout)
        self.assertIn("human preference (primary): none recorded yet", st.stdout)

    def test_log_decision_records_the_live_model_hash(self):
        paths, _ = helpers.build_home(through="synthesize")
        r = helpers.run_cli(paths.home, "log-decision", stdin=json.dumps({"seed": "ship it", "mode": "execute"}))
        self.assertEqual(r.returncode, 0, r.stderr)
        row = helpers.read_jsonl(paths.decisions)[-1]
        self.assertEqual(row["model_hash"], read_json(paths.model_json)["model_hash"])
        self.assertEqual(row["seed_hash"], track.seed_hash("ship it"))


class SliceTests(unittest.TestCase):
    def test_pending_drafts_and_comments_never_reach_the_slice(self):
        from mr.select import select_slice
        paths, cfg = helpers.build_home(through="synthesize")
        line, body = track.draft_line("d1234567", "2026-03-02", "the export", "n/a", "the PDF export on the invoices page")
        paths.corrections.write_text("# a note to self\n" + line + "\n2026-03-03 | seed: \"x\" | wrong: n/a | meant: keep the "
                                     "footer sticky | scope: global | kind: fix\n", encoding="utf-8")
        text, _ = select_slice(paths.model, "fix the export", cfg["select"])
        self.assertIn("keep the footer sticky", text)
        self.assertNotIn("invoices page", text)
        self.assertNotIn("note to self", text)


if __name__ == "__main__":
    unittest.main()
