"""Standing grants: phrases and negation, sentence-level verification against the corpus, Covers containment,
recipient and spend rules, deterministic synthesis, merge with hand edits, tombstones for revoked and deleted grants."""
import unittest

import helpers
from mr import grants as _grants
from mr.config import read_json, write_json
from mr.corpus import load_corpus
from mr.synthesize import build_grants, run_synthesize

GRANT_TEXT = "btw you can push the working branch whenever its green, no need to ask me"


def block(n, quote, covers, title="Push the working branch", scope="own projects", never="main; shared repos", status="active",
          date="2026-01-03", origin="typed", session="cs01", source=None):
    src = ("- Source: %s\n" % source) if source else ""
    return ("## G%d. %s\n- Quote: \"%s\" (%s, %s, %s)\n%s- Scope: %s\n- Covers: %s\n- Never covers: %s\n- Status: %s\n"
            % (n, title, quote, date, origin, session, src, scope, covers, never, status))


def row(text, session="s1", record_id="r1", source="claude-code", origin="typed", time="2026-02-01T10:00:00Z"):
    return {"text": text, "session": session, "record_id": record_id, "source": source, "origin": origin, "time": time,
            "key": "%s-%s" % (session, record_id)}


class GrantPhraseTests(unittest.TestCase):
    def test_explicit_phrases_detected(self):
        yes = ["you don't need to ask before pushing", "no need to ask me", "push without asking me", "you have my permission to deploy",
               "from now on just push when green", "I authorize you to merge my branch", "it's always ok to restart the worker",
               "u legit have perms bro", "you can push the working branch whenever its green",
               # weak openers count only next to a standing marker
               "from now on feel free to delete scratch files", "you can always push to my branch, any time",
               "in general you're allowed to restart the worker"]
        for s in yes:
            with self.subTest(s=s):
                self.assertTrue(_grants.has_grant_phrase(s), s)

    def test_preferences_and_one_off_approvals_are_not_grants(self):
        no = ["ok", "yes do it", "push it when green", "ugh another prompt", "I hate being asked twice", "looks good, ship it",
              "please push", "merge when you can", "go ahead", "approved",
              # the weak openers alone: pleasantries and task-local approvals, not standing authorizations
              "feel free to run that", "feel free to ask questions", "feel free to use workflows", "you can always ask me if unsure",
              "you are allowed to disagree", "you're allowed to fully delete that one", "whenever you want lol", "I'm not sure you can always push"]
        for s in no:
            with self.subTest(s=s):
                self.assertFalse(_grants.has_grant_phrase(s), s)

    def test_negated_phrases_are_prohibitions(self):
        for s in ["never push to main without asking me, ever", "do not deploy without asking", "please don't push without asking",
                  "not without asking me first", "you can't merge without asking", "stop pushing without asking me"]:
            with self.subTest(s=s):
                self.assertTrue(_grants.has_grant_phrase(s), s)  # the phrase is there
                self.assertIsNotNone(_grants.negated_grant(s), s)  # and a negator comes first in the sentence
        for s in ["no, you don't need to ask me before pushing", "no worries, you can push whenever its green", GRANT_TEXT,
                  "never force-push. you don't need to ask before pushing the working branch"]:
            with self.subTest(s=s):
                self.assertIsNone(_grants.negated_grant(s), s)  # an answer particle, a pleasantry, or an earlier sentence

    def test_grant_sentence_widens_a_truncated_quote_and_rejects_a_cut_negator(self):
        corpus_text = "never push to main without asking me, ever"
        sentence, phrase, reason = _grants.grant_sentence(corpus_text, "push to main without asking me")
        self.assertIsNone(sentence)
        self.assertEqual(phrase, "without asking me")
        self.assertIn("negated", reason)
        text = "ok fix that first. " + GRANT_TEXT + ". then report"
        sentence, phrase, reason = _grants.grant_sentence(text, "push the working branch whenever its green")
        self.assertEqual((sentence, reason), (GRANT_TEXT, None))
        self.assertEqual(phrase, "whenever its green")
        self.assertEqual(_grants.grant_sentence(text, "not in the text")[2], "quote not found verbatim in a typed corpus message")
        self.assertEqual(_grants.grant_sentence("push it when green", "push it when green")[2], "no explicit grant phrase")

    def test_never_grantable_recipient_and_spend_rules(self):
        self.assertIsNotNone(_grants.never_grantable("bypass permissions when blocked"))
        self.assertIsNotNone(_grants.never_grantable("change the sandbox settings"))
        self.assertIsNotNone(_grants.never_grantable("rotate the api keys"))
        self.assertIsNotNone(_grants.never_grantable("do anything you need"))
        self.assertIsNotNone(_grants.never_grantable("add webhooks to the repo"))
        self.assertIsNotNone(_grants.never_grantable("set up mail forwarding rules"))
        self.assertIsNotNone(_grants.never_grantable("edit ~/.codex/config.toml and CLAUDE.md"))
        self.assertIsNotNone(_grants.never_grantable("change the configuration so it stops prompting"))
        self.assertIsNone(_grants.never_grantable("push the working branch after the verify command passes"))
        self.assertIsNone(_grants.never_grantable("spend up to the agreed ceiling on owned machines"))
        self.assertTrue(_grants.names_a_recipient("reply to <teammate> about build failures"))
        self.assertTrue(_grants.names_a_recipient("post in #deploys"))
        self.assertTrue(_grants.names_a_recipient("dm @oncall"))
        self.assertFalse(_grants.names_a_recipient("send status messages"))
        self.assertFalse(_grants.names_a_recipient("send messages to <anyone>"))  # only role tokens and channels name someone
        self.assertFalse(_grants.names_a_recipient("send messages to <the team>"))
        self.assertTrue(_grants.names_a_recipient("send messages to <lead>", tokens=["<lead>"]))  # a configured people token
        for c in ["open PRs on the shared repo", "ping the team when done", "notify support", "contact the vendor", "invite reviewers",
                  "open an issue on their tracker", "send status messages"]:
            with self.subTest(c=c):
                self.assertTrue(_grants.covers_messages(c), c)
        self.assertFalse(_grants.covers_messages("fix issues in the export"))
        self.assertTrue(_grants.spend_without_cap("pay for the api"))
        self.assertTrue(_grants.spend_without_cap("spend on cloud machines"))
        self.assertFalse(_grants.spend_without_cap("spend up to the agreed ceiling on owned machines"))
        self.assertFalse(_grants.spend_without_cap("spend at most $20 a day on the eval"))
        self.assertFalse(_grants.spend_without_cap("push the working branch"))

    def test_covers_must_be_named_by_the_quote(self):
        self.assertTrue(_grants.covers_named_by_quote("push the working branch once its green", GRANT_TEXT))
        self.assertFalse(_grants.covers_named_by_quote("push the working branch once the tests are green", GRANT_TEXT))  # "tests"
        self.assertFalse(_grants.covers_named_by_quote("force-push the working branch", "you can always ask me if unsure"))
        self.assertFalse(_grants.covers_named_by_quote("edit settings.json permissions and send messages to the team", GRANT_TEXT))
        self.assertTrue(_grants.covers_named_by_quote("delete merged branches", "you don't need to ask before deleting old branches", strict=False))
        self.assertFalse(_grants.covers_named_by_quote("delete merged branches", "you don't need to ask before deleting old branches"))  # "merged"
        self.assertFalse(_grants.covers_named_by_quote("force-push the working branch", "you can always ask me if unsure", strict=False))
        self.assertFalse(_grants.covers_named_by_quote("", GRANT_TEXT))

    def test_covers_derived_from_the_quote_keeps_the_action_and_its_condition(self):
        self.assertEqual(_grants.derive_covers(GRANT_TEXT), "push the working branch whenever its green")
        self.assertEqual(_grants.derive_covers("push it when green, no need to ask me first"), "push it when green")
        self.assertEqual(_grants.derive_covers("from now on feel free to restart the worker"), "restart the worker")
        self.assertEqual(_grants.derive_covers("you have my permission to deploy the staging host whenever its green"),
                         "deploy the staging host whenever its green")
        self.assertIsNone(_grants.derive_covers("u legit have perms bro"))  # frustration, no class of action
        self.assertIsNone(_grants.derive_covers("from now on you can always ask me if unsure"))  # asking is not an action grant


# Owner-style autonomy grants: second person, shorthand kept. Each must become one active `Class: autonomy` grant.
AUTONOMY_YES = ["you have max permissions", "ur in bypass perms", "you have full perms, just do it",
                "do it in bypass permissions mode", "you're pre-authorized for all of this",
                "you're in bypass permissions mode so dont ask me"]
# Mentions, descriptions, config requests, conditions and instructions about another process: never a grant.
AUTONOMY_NO = ["the bypass permissions setting is off so ask me", "turn on bypass permissions in settings.json for me",
               "bypassing permissions is bad", "bypass permissions doesn't let you push to main",
               "run claude with --dangerously-skip-permissions", "set it so you have bypass permissions",
               "if you have full perms push it", "would you work in bypass permissions mode?",
               "you don't have bypass permissions", "the deploy was pre-authorized by ops"]


class AutonomyTests(unittest.TestCase):
    def pipeline(self, text):
        """verify -> merge -> lint -> runtime: (kept, dropped, grants.md text, lint errors, runtime ids)."""
        from mr import lint as _lint
        from mr import select as _select
        rows = [row(text, record_id="r1")]
        kept, dropped = _grants.verify_quotes([{"quote": text, "origin": "typed", "session": "s1", "scope": "whatever the engine said",
                                                "covers": "general autonomy within the harness permission mode"}], rows)
        md, _, _ = _grants.merge_grants("", kept, "Alex")
        errs = _lint.lint_grants(md)
        _, ids = _select.runtime_grants(md)
        return kept, dropped, md, errs, ids

    def test_owner_style_quotes_become_one_active_autonomy_grant(self):
        for text in AUTONOMY_YES:
            with self.subTest(text=text):
                self.assertTrue(_grants.has_grant_phrase(text), text)
                self.assertEqual(_grants.grant_class(text), "autonomy")
                kept, dropped, md, errs, ids = self.pipeline(text)
                self.assertEqual((len(kept), dropped), (1, []))
                self.assertEqual(errs, [])
                self.assertEqual(ids, ["G1"])
                g = _grants.parse_grants(md)[0][0]
                self.assertEqual((g["cls"], g["covers"], g["scope"]), ("autonomy", _grants.AUTONOMY_COVERS, _grants.AUTONOMY_SCOPE))
                self.assertEqual(g["never"], _grants.AUTONOMY_NEVER)
                self.assertIn("- Class: autonomy", md)

    def test_mentions_requests_and_descriptions_never_become_grants(self):
        for text in AUTONOMY_NO:
            with self.subTest(text=text):
                self.assertIsNone(_grants.autonomy_phrase(text), text)
                kept, dropped, md, errs, ids = self.pipeline(text)
                self.assertEqual(kept, [])
                self.assertEqual(len(dropped), 1)
                self.assertEqual(ids, [])

    def test_autonomy_versus_action_class(self):
        self.assertEqual(_grants.grant_class("you have my permission to deploy the staging host"), "action")
        self.assertEqual(_grants.grant_class(GRANT_TEXT), "action")
        self.assertEqual(_grants.grant_class("you have full permissions to push"), "action")  # "to <verb>" names one action
        self.assertEqual(_grants.grant_class("you have max perms, no need to ask"), "autonomy")
        self.assertEqual(_grants.grant_class("you're in yolo mode"), "autonomy")
        self.assertTrue(_grants.has_grant_phrase("you're in yolo mode"))  # yolo mode is a grant phrase in the same frame

    def test_negated_and_hypothetical_autonomy_rejected(self):
        for text in ["never assume you have full permissions without asking", "don't act like you're in bypass perms",
                     "what if you had max permissions?", "do you think you have full perms"]:
            with self.subTest(text=text):
                kept, dropped, _, _, ids = self.pipeline(text)
                self.assertEqual((kept, ids), ([], []))

    def test_exception_clause_is_kept_in_never_covers(self):
        for text, clause in [("you have full permissions, except don't push to main", "except don't push to main"),
                             ("you have full permissions but never message anyone", "but never message anyone")]:
            with self.subTest(text=text):
                self.assertEqual(_grants.autonomy_exception(text), clause)
                kept, dropped, md, errs, ids = self.pipeline(text)
                self.assertEqual((errs, ids), ([], ["G1"]))
                g = _grants.parse_grants(md)[0][0]
                self.assertTrue(g["never"].startswith('the quote\'s own limit: "%s"; ' % clause), g["never"])
                self.assertTrue(g["never"].endswith(_grants.AUTONOMY_NEVER))
        # an exception that would read as a forbidden rule is dropped, never rendered
        kept, dropped, _, _, _ = self.pipeline("you have full permissions but don't push without asking")
        self.assertEqual(kept, [])
        self.assertIn("forbidden rule", dropped[0][1])

    def test_autonomy_rule_text_bounds_the_grant(self):
        never = _grants.AUTONOMY_NEVER
        for word in ("outward-facing", "destructive", "force-pushes", "deploys", "spend", "permission, sandbox", "literal current message"):
            self.assertIn(word, never)
        self.assertIn("reversible, own-repo", _grants.AUTONOMY_COVERS)
        self.assertIn("reversible, own-repo", _grants.INTRO)
        from mr import lint as _lint
        self.assertEqual(_lint.forbidden_hits(_grants.header("Alex")), [])  # the header itself is clean
        self.assertEqual(_lint.forbidden_hits("- Never covers: " + never), [])

    def test_legacy_autonomy_text_is_flagged_and_migrated(self):
        kept, _, md, _, _ = self.pipeline("you have max permissions")
        old = md.replace(_grants.AUTONOMY_COVERS, _grants.LEGACY_AUTONOMY_COVERS).replace(_grants.AUTONOMY_NEVER, _grants.LEGACY_AUTONOMY_NEVER)
        errs = _grants.parse_grants(old)[1]
        self.assertTrue(any("older, unbounded text" in e for e in errs), errs)
        new, changed = _grants.migrate_autonomy(old)
        self.assertEqual((new, changed), (md, 2))
        narrowed = old.replace("- Never covers: ", "- Never covers: main; ")
        self.assertIn("- Never covers: main; " + _grants.AUTONOMY_NEVER, _grants.migrate_autonomy(narrowed)[0])  # hand edit kept

    def test_sentence_split_keeps_dotted_words(self):
        text = "use settings.json and v5.1 as the base, you have max permissions. then report"
        sentence, _, reason = _grants.grant_sentence(text, "you have max permissions")
        self.assertIsNone(reason)
        self.assertEqual(sentence, "use settings.json and v5.1 as the base, you have max permissions")
        self.assertEqual(_grants.grant_sentence("ok. ur in bypass perms. go", "ur in bypass perms")[0], "ur in bypass perms")


class ParseTests(unittest.TestCase):
    def test_parse_good_file(self):
        text = _grants.header("Alex") + "\n" + block(1, GRANT_TEXT, "push the working branch once its green", source="claude-code:cs01:abc")
        gs, errors = _grants.parse_grants(text)
        self.assertEqual(errors, [])
        self.assertEqual(len(gs), 1)
        g = gs[0]
        self.assertEqual((g["id"], g["date"], g["origin"], g["session"], g["active"], g["errors"]), ("G1", "2026-01-03", "typed", "cs01", True, []))
        self.assertEqual(g["covers"], "push the working branch once its green")
        self.assertEqual(g["source"], "claude-code:cs01:abc")
        self.assertEqual(_grants.valid_active_grants(text), gs)
        self.assertEqual(_grants.active_grants(text), gs)
        self.assertIn("revoked or deleted entry is never re-added", text)

    def test_source_line_is_optional(self):
        gs, errors = _grants.parse_grants(_grants.header("Alex") + "\n" + block(1, GRANT_TEXT, "push the working branch"))
        self.assertEqual(errors, [])
        self.assertEqual(gs[0]["source"], "")

    def test_each_rule_reports_a_line(self):
        cases = {
            "no explicit grant phrase": block(1, "push it when green", "push the working branch"),
            "negates its grant phrase": block(1, "never push to main without asking me", "push to main"),
            "must come from typed text": block(1, GRANT_TEXT, "push the working branch", origin="drafted"),
            "cannot be granted": block(1, GRANT_TEXT, "bypass permissions when blocked"),
            "blanket": block(1, GRANT_TEXT, "do anything you want"),
            "names an action the quote does not": block(1, "you can always ask me if unsure, from now on", "force-push the working branch"),
            "spend grant must state a cap": block(1, "you have my permission to pay for the api", "pay for the api"),
            "name the person": block(1, "you can always send status messages to the team, from now on", "send status messages to the team"),
            "Status must be": block(1, GRANT_TEXT, "push the working branch", status="maybe"),
            "has no `- Scope:`": block(1, GRANT_TEXT, "push the working branch").replace("- Scope: own projects\n", ""),
            "Quote must be": block(1, GRANT_TEXT, "push the working branch").replace("(2026-01-03, typed, cs01)", "(sometime)"),
            "duplicate grant id": block(1, GRANT_TEXT, "push the working branch") + block(1, GRANT_TEXT + " ok", "push again"),
            "grant heading must look like": "## not a grant\n- Quote: x\n",
        }
        for expect, body in cases.items():
            with self.subTest(expect=expect):
                gs, errors = _grants.parse_grants(_grants.header("Alex") + "\n" + body)
                self.assertTrue(any(expect in e for e in errors), (expect, errors))
                self.assertTrue(all(":" in e.split(" ", 1)[0] for e in errors), errors)  # every error names a line
                self.assertEqual(_grants.valid_active_grants(_grants.header("Alex") + "\n" + body), [])  # an invalid grant never applies
                for g in gs:
                    self.assertTrue(g["errors"], g)  # and carries its own errors

    def test_message_grant_that_names_the_recipient_passes(self):
        text = _grants.header("Alex") + "\n" + block(
            1, "you can always reply to <teammate> about build failures without asking me", "reply to <teammate> about build failures",
            title="Reply to the teammate about builds")
        gs, errors = _grants.parse_grants(text)
        self.assertEqual(errors, [])
        self.assertEqual(len(gs), 1)
        text = _grants.header("Alex") + "\n" + block(1, "from now on feel free to post deploy notes in #deploys", "post deploy notes in #deploys")
        self.assertEqual(_grants.parse_grants(text)[1], [])

    def test_revoked_is_valid_but_inactive(self):
        text = _grants.header("Alex") + "\n" + block(1, GRANT_TEXT, "push the working branch", status="revoked 2026-03-01")
        gs, errors = _grants.parse_grants(text)
        self.assertEqual(errors, [])
        self.assertFalse(gs[0]["active"])
        self.assertEqual(_grants.valid_active_grants(text), [])


class VerifyTests(unittest.TestCase):
    def test_negated_and_truncated_quotes_are_dropped(self):
        rows = [row("never push to main without asking me, ever", record_id="r1"),
                row("do not deploy without asking. push the working branch whenever its green tho, no need to ask", record_id="r2")]
        kept, dropped = _grants.verify_quotes([
            {"quote": "push to main without asking me", "origin": "typed", "session": "s1", "covers": "push to main"},
            {"quote": "deploy without asking", "origin": "typed", "session": "s1", "covers": "deploy"},
            {"quote": "push the working branch whenever its green", "origin": "typed", "session": "s1", "covers": "push the working branch"}], rows)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["quote"], "push the working branch whenever its green tho, no need to ask")  # widened to its sentence
        self.assertEqual(kept[0]["source"], "claude-code:s1:r2")
        reasons = {c["quote"]: why for c, why in dropped}
        self.assertIn("negated", reasons["push to main without asking me"])
        self.assertIn("negated", reasons["deploy without asking"])

    def test_covers_outside_the_quote_is_derived_or_dropped(self):
        rows = [row("you can always ask me if unsure, from now on", record_id="r1"), row(GRANT_TEXT, record_id="r2"),
                row("u legit have perms bro", record_id="r3")]
        kept, dropped = _grants.verify_quotes([
            {"quote": "you can always ask me if unsure, from now on", "origin": "typed", "session": "s1", "covers": "force-push the working branch"},
            {"quote": GRANT_TEXT, "origin": "typed", "session": "s1", "covers": "push the working branch once the tests are green"},
            {"quote": "u legit have perms bro", "origin": "typed", "session": "s1", "covers": "run commands without prompts"}], rows)
        self.assertEqual([c["covers"] for c in kept], ["push the working branch whenever its green"])  # derived from the quote
        self.assertTrue(kept[0]["covers_derived"])
        self.assertEqual(len(dropped), 2)
        self.assertTrue(all("names actions the quote does not" in why for _, why in dropped), dropped)

    def test_spend_without_cap_and_unnamed_recipient_are_dropped(self):
        rows = [row("you have my permission to pay for the api", record_id="r1"),
                row("from now on feel free to send status messages to the team", record_id="r2"),
                row("from now on feel free to send status messages to <teammate>", record_id="r3")]
        kept, dropped = _grants.verify_quotes([
            {"quote": "you have my permission to pay for the api", "origin": "typed", "session": "s1", "covers": "pay for the api"},
            {"quote": "from now on feel free to send status messages to the team", "origin": "typed", "session": "s1", "covers": "send status messages to the team"},
            {"quote": "from now on feel free to send status messages to <teammate>", "origin": "typed", "session": "s1", "covers": "send status messages to <teammate>"}], rows)
        self.assertEqual([c["source"] for c in kept], ["claude-code:s1:r3"])
        self.assertEqual(sorted(why for _, why in dropped), ["a spend grant must state a cap", "covers messages without naming the person or channel"])

    def test_message_grant_naming_a_recipient_keeps_messages_out_of_never_covers(self):
        text = "from now on feel free to post deploy notes in #general"
        kept, _ = _grants.verify_quotes([{"quote": text, "origin": "typed", "session": "s1", "covers": "post deploy notes in #general"}],
                                        [row(text, record_id="r1")])
        md, _, _ = _grants.merge_grants("", kept, "Alex")
        g = _grants.parse_grants(md)[0][0]
        self.assertNotIn("messages or posts to other people", g["never"])
        self.assertIn("permission, sandbox or security changes", g["never"])
        kept, _ = _grants.verify_quotes([{"quote": GRANT_TEXT, "origin": "typed", "session": "s1", "covers": "push the working branch"}],
                                        [row(GRANT_TEXT, record_id="r2")])
        self.assertIn("messages or posts to other people", _grants.parse_grants(_grants.merge_grants("", kept, "Alex")[0])[0][0]["never"])

    def test_same_session_record_preferred_for_the_source(self):
        rows = [row(GRANT_TEXT, session="other", record_id="r9"), row(GRANT_TEXT, session="cs01", record_id="r1")]
        kept, _ = _grants.verify_quotes([{"quote": GRANT_TEXT, "origin": "typed", "session": "cs01", "covers": "push the working branch"}], rows)
        self.assertEqual(kept[0]["source"], "claude-code:cs01:r1")


class MergeAndTombstoneTests(unittest.TestCase):
    def cand(self, quote=GRANT_TEXT, source="claude-code:cs01:r1", covers="push the working branch", session="cs01", time="2026-01-03T10:25:00Z"):
        return {"quote": quote, "time": time, "session": session, "origin": "typed", "scope": "own projects", "covers": covers, "source": source}

    def test_merge_appends_new_sources_with_next_id(self):
        existing = _grants.header("Alex") + "\n" + block(3, GRANT_TEXT, "push the working branch", source="claude-code:cs01:r1")
        text, added, records = _grants.merge_grants(existing, [
            self.cand(),  # same source: not new
            self.cand(quote="you can push the working branch whenever its green", source="claude-code:cs01:r1"),  # a re-mined paraphrase of the same record
            self.cand(quote="you can always restart the worker, no need to ask", source="codex:cs08:r4", covers="restart the background worker",
                      session="cs08", time="2026-02-01T10:00:00Z")], "Alex")
        self.assertEqual(added, 1)
        self.assertEqual(records, [{"id": "G4", "source": "codex:cs08:r4", "quote": "you can always restart the worker, no need to ask"}])
        gs, errors = _grants.parse_grants(text)
        self.assertEqual(errors, [])
        self.assertEqual([g["id"] for g in gs], ["G3", "G4"])
        self.assertEqual(gs[1]["title"], "Restart the background worker")
        self.assertEqual(gs[1]["source"], "codex:cs08:r4")

    def test_tombstones_block_re_adding_by_source_or_quote(self):
        tomb = [{"source": "claude-code:cs01:r1", "quote": "", "reason": "deleted"}, {"source": "", "quote": "you can always restart the worker, no need to ask", "reason": "revoked"}]
        text, added, _ = _grants.merge_grants("", [
            self.cand(quote="a different slice of the same message, no need to ask me", source="claude-code:cs01:r1"),
            self.cand(quote="You can always restart the worker,  no need to ask", source="codex:cs08:r4"),
            self.cand(quote="from now on just push when green", source="codex:cs09:r1", covers="push when green")], "Alex", tombstones=tomb)
        self.assertEqual(added, 1)
        self.assertEqual([g["quote"] for g in _grants.parse_grants(text)[0]], ["from now on just push when green"])

    def test_revoke_from_changes_needs_a_verified_revoking_message(self):
        text = _grants.header("Alex") + "\n" + block(1, GRANT_TEXT, "push the working branch")
        rows = [row(GRANT_TEXT, record_id="r1"), row("actually never push without asking me again", record_id="r2")]
        changes = [{"topic": "pushing", "earlier": GRANT_TEXT, "later": "actually never push without asking me again",
                    "earlier_time": "2026-01-03T10:25:00Z", "later_time": "2026-02-09T09:00:00Z", "session": "s1"}]
        out, revoked = _grants.revoke_from_changes(text, changes, rows, "2026-03-01")
        self.assertEqual([r[0] for r in revoked], ["G1"])
        self.assertIn("- Status: revoked 2026-02-09", out)
        self.assertEqual(_grants.valid_active_grants(out), [])
        # the later message is not in the corpus: nothing changes
        out2, revoked2 = _grants.revoke_from_changes(text, [dict(changes[0], later="stop pushing on your own")], rows, "2026-03-01")
        self.assertEqual((out2, revoked2), (text, []))
        # a later message without a revocation marker is a change of taste, not a revocation
        rows2 = rows + [row("push to the release branch instead", record_id="r3")]
        out3, revoked3 = _grants.revoke_from_changes(text, [dict(changes[0], later="push to the release branch instead")], rows2, "2026-03-01")
        self.assertEqual(revoked3, [])
        self.assertEqual(out3, text)


class SynthesizeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.paths, cls.cfg = helpers.build_home(through="synthesize")

    def consolidated(self):
        return read_json(self.paths.mined / read_json(self.paths.model_json)["mined_run"] / "consolidated.json")

    def test_only_verified_typed_quotes_survive(self):
        cands = self.consolidated()["grant_candidates"]
        self.assertEqual(len(cands), 10)  # 5 per chunk, 2 chunks
        kept, dropped = _grants.verify_quotes(cands, load_corpus(self.paths))
        self.assertEqual([c["quote"] for c in kept], [GRANT_TEXT] * 4)  # the short quote was widened to its sentence
        self.assertEqual(len({c["source"] for c in kept}), 1)  # all four name the same corpus record
        self.assertTrue(all(c["source"].startswith("claude-code:cs01:") for c in kept))
        self.assertEqual(kept[0]["covers"], "push the working branch once its green")
        self.assertEqual(kept[1]["covers"], "push the working branch")
        reasons = {c["quote"][:20]: why for c, why in dropped}
        self.assertIn("not found verbatim", reasons["you don't need to as"])
        self.assertIn("not typed", reasons["Do not touch the cat"])
        self.assertIn("no explicit grant phrase", reasons["push it when green"])
        # an engine Covers the quote does not name ("send") is replaced by the quote's own action, never kept
        kept2, dropped2 = _grants.verify_quotes([{"quote": GRANT_TEXT, "origin": "typed", "covers": "send status messages"}], load_corpus(self.paths))
        self.assertEqual(dropped2, [])
        self.assertEqual((kept2[0]["covers"], kept2[0]["covers_derived"]), ("push the working branch whenever its green", True))

    def test_synthesize_wrote_grants_md_deterministically(self):
        text = self.paths.grants.read_text(encoding="utf-8")
        gs, errors = _grants.parse_grants(text)
        self.assertEqual(errors, [])
        self.assertEqual([g["id"] for g in gs], ["G1"])
        self.assertEqual(gs[0]["quote"], GRANT_TEXT)
        self.assertEqual(gs[0]["date"], "2026-01-03")
        self.assertTrue(gs[0]["source"].startswith("claude-code:cs01:"))
        self.assertIn("Alex's own projects", gs[0]["scope"])
        self.assertTrue(text.startswith("# Alex: standing grants"))
        self.assertIn("nothing here is inferred", text)
        idx = read_json(self.paths.index)
        self.assertEqual(idx["grants"], {"path": "grants.md", "count": 1, "active": 1})
        m = read_json(self.paths.model_json)
        self.assertIn("grants.md", m["files"])
        self.assertEqual([a["id"] for a in m["grants_added"]], ["G1"])
        self.assertEqual(m["grant_tombstones"], [])

    def test_hand_edits_survive_a_rerun_and_force_rebuilds(self):
        original = self.paths.grants.read_text(encoding="utf-8")
        manifest = self.paths.model_json.read_text(encoding="utf-8")
        edited = original.replace("- Status: active", "- Status: revoked 2026-04-01").replace(
            "- Never covers: anything the quote does not name", "- Never covers: the release branch; anything the quote does not name")
        self.paths.grants.write_text(edited, encoding="utf-8")
        try:
            s = build_grants(self.paths, self.cfg, self.consolidated(), log=helpers.quiet)
            self.assertEqual((s["added"], s["total"], s["active"], s["tombstones"]), (0, 1, 0, 1))
            after = self.paths.grants.read_text(encoding="utf-8")
            self.assertEqual(after, edited)  # the user's revocation and narrowing stay
            tomb = read_json(self.paths.model_json)["grant_tombstones"]
            self.assertEqual((tomb[0]["id"], tomb[0]["reason"]), ("G1", "revoked"))
            self.assertTrue(tomb[0]["source"].startswith("claude-code:cs01:"))
            s = build_grants(self.paths, self.cfg, self.consolidated(), force=True, log=helpers.quiet)
            self.assertEqual((s["added"], s["total"], s["tombstones"]), (1, 1, 0))  # --force starts over, tombstones included
            self.assertIn("- Status: active", self.paths.grants.read_text(encoding="utf-8"))
        finally:
            self.paths.grants.write_text(original, encoding="utf-8")
            self.paths.model_json.write_text(manifest, encoding="utf-8")

    def test_deleted_grant_never_comes_back(self):
        original = self.paths.grants.read_text(encoding="utf-8")
        manifest = self.paths.model_json.read_text(encoding="utf-8")
        self.paths.grants.write_text(_grants.header("Alex"), encoding="utf-8")  # the user deleted G1 by hand
        try:
            logs = []
            s = build_grants(self.paths, self.cfg, self.consolidated(), log=logs.append)
            self.assertEqual((s["added"], s["total"], s["tombstones"]), (0, 0, 1))
            self.assertEqual(_grants.parse_grants(self.paths.grants.read_text(encoding="utf-8"))[0], [])
            self.assertTrue(any("deleted by hand" in l for l in logs), logs)
            m = read_json(self.paths.model_json)
            self.assertEqual(m["grant_tombstones"][0]["reason"], "deleted")
            self.assertEqual(m["grants_added"], [])
            s = build_grants(self.paths, self.cfg, self.consolidated(), log=helpers.quiet)  # and again on the next refresh
            self.assertEqual((s["added"], s["total"]), (0, 0))
            # a revoked grant stays revoked when the same record is mined again under a different quote substring
            self.paths.grants.write_text(original.replace("- Status: active", "- Status: revoked 2026-04-02"), encoding="utf-8")
            cons = dict(self.consolidated())
            cons["grant_candidates"] = [{"quote": "you can push the working branch whenever its green", "time": "2026-01-03T10:25:00Z", "session": "cs01",
                                         "origin": "typed", "scope": "own projects", "covers": "push the working branch"}]
            s = build_grants(self.paths, self.cfg, cons, log=helpers.quiet)
            self.assertEqual((s["added"], s["total"], s["active"]), (0, 1, 0))
        finally:
            self.paths.grants.write_text(original, encoding="utf-8")
            self.paths.model_json.write_text(manifest, encoding="utf-8")

    def test_preference_change_revokes_a_grant(self):
        original = self.paths.grants.read_text(encoding="utf-8")
        manifest = self.paths.model_json.read_text(encoding="utf-8")
        from mr.config import write_jsonl
        rows = load_corpus(self.paths)
        revoking = dict(rows[0], text="actually never push without asking me again", record_id="rev-1", key="rev-1", session="cs01", origin="typed",
                        time="2026-04-03T10:00:00Z")
        write_jsonl(self.paths.messages, rows + [revoking])
        prepared = self.paths.sessions.read_text()
        sessions = helpers.read_jsonl(self.paths.sessions)
        next(s for s in sessions if s["session"] == "cs01")["messages"].append(dict(revoking, typed_text=revoking["text"]))
        write_jsonl(self.paths.sessions, sessions)
        try:
            cons = dict(self.consolidated())
            cons["preference_changes"] = [{"topic": "pushing", "earlier": GRANT_TEXT, "later": "actually never push without asking me again",
                                           "earlier_time": "2026-01-03T10:25:00Z", "later_time": "2026-04-03T10:00:00Z", "session": "cs01"}]
            logs = []
            s = build_grants(self.paths, self.cfg, cons, log=logs.append)
            self.assertEqual((s["revoked"], s["active"], s["total"]), (["G1"], 0, 1))
            self.assertIn("- Status: revoked 2026-04-03", self.paths.grants.read_text(encoding="utf-8"))
            self.assertTrue(any("revoked by a later typed message" in l for l in logs), logs)
            self.assertEqual(read_json(self.paths.model_json)["grant_tombstones"][0]["reason"], "revoked")
        finally:
            write_jsonl(self.paths.messages, rows)
            self.paths.sessions.write_text(prepared)
            self.paths.grants.write_text(original, encoding="utf-8")
            self.paths.model_json.write_text(manifest, encoding="utf-8")

    def test_manifest_carries_grant_state_and_folded_drops_across_rebuilds(self):
        from mr.synthesize import write_manifest
        m = read_json(self.paths.model_json)
        m["folded_drops"] = ["Some dropped sentence."]
        m["grant_tombstones"] = [{"id": "G9", "source": "x:y:z", "quote": "q", "reason": "deleted", "date": "2026-01-01"}]
        write_json(self.paths.model_json, m)
        try:
            out = write_manifest(self.paths, self.cfg)
            self.assertEqual(out["folded_drops"], ["Some dropped sentence."])
            self.assertEqual(out["grant_tombstones"][0]["id"], "G9")
            self.assertEqual([a["id"] for a in out["grants_added"]], ["G1"])
        finally:
            m.pop("folded_drops", None)
            m["grant_tombstones"] = []
            write_json(self.paths.model_json, m)
            write_manifest(self.paths, self.cfg)

    def test_grants_substep_runs_without_an_engine(self):
        paths, cfg = helpers.build_home(through="mine")
        s = run_synthesize(paths, cfg, "fake", substeps=["consolidate", "grants"], fake_dir=helpers.FAKE, log=helpers.quiet)
        self.assertEqual(s["substeps"]["grants"], "3 candidate(s), 2 verified, 1 added, 1 in grants.md (1 valid and active)")
        self.assertEqual(s["warnings"], [])
        self.assertTrue(paths.grants.exists())
        self.assertFalse(paths.profile.exists())  # no engine step ran
        m = read_json(paths.model_json)
        self.assertNotIn("model_hash", m)  # grant bookkeeping only; status treats this as "no model yet"
        self.assertEqual([a["id"] for a in m["grants_added"]], ["G1"])
        from mr.status import gather
        self.assertIsNone(gather(paths, cfg)["model"])


if __name__ == "__main__":
    unittest.main()
