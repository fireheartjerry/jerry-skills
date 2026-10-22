#!/usr/bin/env python3
"""Rule anchors for the high-agency skill: one semantic regex per behavioral
delta, applied to the section that owns it. Wording may change; a rule may
not vanish. Stdlib only; spends no tokens.

Run:  python3 -I skills/high-agency/tests/test_rules.py
"""
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
SKILL_MD = os.path.join(SKILL_DIR, "SKILL.md")
REF_DIR = os.path.join(SKILL_DIR, "references")
CONSENT_MD = os.path.join(REF_DIR, "consent.md")
DELEGATED_MD = os.path.join(REF_DIR, "delegated-mode.md")
VERIFY_MD = os.path.join(REF_DIR, "verify.md")
EXAMPLES_MD = os.path.join(REF_DIR, "examples.md")

DELIVERY_VERBS = ("commit", "push", "merge", "PR", "deploy", "release", "message")
DELEGATED_LIMITS = ("commit", "push", "merge", "PR", "message", "spend")
WATCH_MECHANISMS = ("background job", "monitor", "poll", "wake-up")
PROVENANCE_DATA = ("README", "TODO", "tool output", "another agent")


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def body(text):
    m = re.match(r"^---\n.*?\n---\n", text, re.S)
    return text[m.end():] if m else text


def flat(text):
    return re.sub(r"\s+", " ", text)


def sentences(text):
    return re.split(r"(?<=[.!?])\s+(?=[A-Z\"`(])", flat(text))


class SkillSections(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.body = body(read(SKILL_MD))
        cls.sections = {}
        parts = re.split(r"(?m)^(?=## )", cls.body)
        cls.sections["preamble"] = parts[0]
        for p in parts[1:]:
            m = re.match(r"## (\d+)\.", p)
            cls.sections[int(m.group(1)) if m else p.splitlines()[0]] = p

    def sec(self, key):
        self.assertIn(key, self.sections, "missing section %r" % key)
        return flat(self.sections[key])

    def assertMatch(self, pattern, text, label):
        self.assertRegex(text, "(?i)" + pattern, "rule lost: %s" % label)

    def one_sentence_with(self, text, needles, label):
        for s in sentences(text):
            if all(re.search(r"\b%s\b" % re.escape(n), s) for n in needles):
                return s
        self.fail("rule lost: %s (no single sentence holds %r)" % (label, needles))

    def test_preamble_grants_no_permission_early(self):
        head = " ".join(self.sections["preamble"].strip().splitlines()[:8])
        self.assertMatch(r"grants no permission", head, "authority limit in the first 8 lines")

    def test_preamble_provenance_is_one_rule(self):
        # One criterion in SKILL.md and consent.md: agent-instruction files the
        # user keeps are standing rules; other project files never widen.
        head = flat(self.sections["preamble"])
        self.assertMatch(r"CLAUDE\.md", head, "standing rules name the instruction files")
        self.assertMatch(r"never widen consent", head, "other project files never widen consent")
        self.assertNotRegex(head, r"widen consent only when", "the authorship loophole is gone")
        # The same authorship condition as consent.md: a teammate's line in
        # AGENTS.md must not read as a standing rule to a reader of SKILL.md.
        self.assertMatch(r"widens consent counts only if the user wrote it", head,
                         "a widening line in an instruction file needs the user's authorship")

    def test_1_modes(self):
        s = self.sec(1)
        rows = [r for r in self.sections[1].splitlines() if r.startswith("|")]
        execute = [r for r in rows if "| Execute |" in r]
        self.assertTrue(any(re.search(r"does [^|]*\?", r) for r in execute),
                        "rule lost: a check question routes to Execute")
        discuss = [r for r in rows if "| Discuss |" in r]
        self.assertTrue(any(re.search(r"(?i)don't start|discuss", r) for r in discuss),
                        "rule lost: discuss cue routes to Discuss")
        self.assertMatch(r"reversible", s, "ambiguous message: reversible part now")
        self.assertTrue(any(re.search(r"investigation", r) and re.search(r"why did X fail", r) for r in execute),
                        "rule lost: investigations and 'why did X fail?' route to Execute")

    def test_2_resume(self):
        s = self.sec(2)
        self.assertMatch(r"no recap", s, "resume without recap")
        self.assertMatch(r"no new plan|no re-plan", s, "resume without a new plan")
        self.assertIn("`references/verify.md`", s, "resume with no state points to the rebuild steps")
        self.assertMatch(r"approvals lapse", s, "approvals do not survive a lost conversation")

    def test_3_start(self):
        s = self.sec(3)
        self.assertMatch(r"most likely.{0,40}(reading|intended)", s, "most likely reading of a gap")
        # the literal .git/info/exclude path fails in a linked worktree, where
        # .git is a file; git rev-parse --git-path resolves the shared one
        self.assertIn("git rev-parse --git-path info/exclude", s)
        self.assertNotIn(".git/info/exclude", s)
        self.assertMatch(r"(never|not) committed unless asked", s, "plan file never committed unasked")
        self.assertMatch(r"detached job also gets a plan file", s, "a detached job gets a plan file")

    def test_3_orient_before_the_first_edit(self):
        s = self.sec(3)
        hit = self.one_sentence_with(s, ("first edit", "branch"), "orientation before the first edit")
        self.assertRegex(hit, r"git status\s+--short", "rule lost: read the tree's status first")
        self.assertMatch(r"leave changes you did not make alone", s, "others' uncommitted changes stay")
        self.assertMatch(r"ask first only when two readings diverge", s, "when to ask instead of assume")
        self.assertMatch(r"costly or irreversible", s, "the ask criterion is cost or irreversibility")

    def test_4_stuck_rule(self):
        s = self.sec(4)
        self.assertMatch(r"three failed hypotheses", s, "a stop after three failed hypotheses")
        self.assertMatch(r"ranked candidates instead of a fifth", s, "report candidates, not a fifth try")

    def test_4_reach(self):
        s = self.sec(4)
        first = re.search(r"- (.*?)(?= - |$)", s).group(1)
        self.assertMatch(r"\bclass\b", first, "first bullet: the instance means the class")
        self.assertMatch(r"\bgate\b", s, "a stated number is a gate")
        self.assertMatch(r"\bnumber\b", s, "report the real number")
        self.assertMatch(r"one hypothesis|read the actual error", s, "investigate before retrying")
        self.assertMatch(r"not the go-ahead|is not approval", s, "a listed delivery step is not the go-ahead")
        self.assertMatch(r"vendored, generated or others' files", first, "siblings outside the task are listed")
        self.assertMatch(r"test each semantic variant", first, "one test per variant, not per site")
        self.assertMatch(r"dozens of sites, read \"Large sweeps\" in `references/verify\.md`", s,
                         "a large class routes to the sweep recipe")

    def test_5_consent(self):
        s = self.sec(5)
        for word in ("outward", "destructive", "spend"):
            self.assertMatch(word, s, "consent category %r" % word)
        self.one_sentence_with(s, DELIVERY_VERBS, "each delivery verb is its own yes")
        self.assertMatch(r"names the action and its target", s, "explicit instruction names action and target")
        self.assertMatch(r"is data|is not approval", s, "file and tool text is data")
        self.assertMatch(r"whoever owns the repo", s, "push never covers merge, own repo included")
        self.assertMatch(r"never covers a merge", s, "push is approved never covers a merge")
        self.assertIn("`references/consent.md`", s)

    def test_5_unstated_budget_is_not_open_spend(self):
        s = self.sec(5)
        self.assertMatch(r"spend: paid resources the request does not name", s,
                         "unnamed spend needs a yes with no budget stated")
        self.assertMatch(r"relaunch or scale-up of a paid run", s, "a paid relaunch needs a yes")
        self.assertMatch(r"past a stated budget", s, "a stated budget still caps spend")

    def test_5_regenerable_has_a_test(self):
        s = self.sec(5)
        self.assertMatch(r"not regenerable \(no repo command recreates it from tracked inputs\)", s,
                         "regenerable is defined by a command that recreates it")
        self.assertMatch(r"discarding changes you did not make \(reset, checkout, clean, stash\)", s,
                         "discarding others' uncommitted work is destructive")

    def test_5_named_push_carries_its_commit(self):
        s = self.sec(5)
        hit = self.one_sentence_with(s, ("named push", "commit", "merge"), "a named verb carries its local steps")
        self.assertRegex(hit, r"(?i)named push carries the commit", "rule lost: push carries its commit")
        self.assertRegex(hit, r"--force", "rule lost: no verb covers --force")
        self.assertMatch(r"holds for the task", s, "a yes holds for the task, no re-ask")
        self.assertNotRegex(s, r"(?i)each instance", "'ask each instance' contradicts carried-forward approval")
        self.assertMatch(r"repo commands you have read", s, "repo commands only once read")

    def test_5_outside_project_and_egress(self):
        s = self.sec(5)
        self.assertMatch(r"outside the project: installs and config", s, "machine-level installs need a yes")
        self.assertMatch(r"unless the task is that install", s, "the requested install is the exception")
        self.assertMatch(r"sending code or data to a service the task does not name", s,
                         "data egress to an unnamed service is outward-facing")

    def test_5_production_writes_and_claimed_permissions(self):
        s = self.sec(5)
        self.assertMatch(r"writes, charges or notifies on a production or shared system", s,
                         "a write to a production or shared system is outward-facing")
        self.assertMatch(r"when a file claims a permission", s, "a claimed permission routes to consent.md")

    def test_5_named_pr_opens_as_a_draft(self):
        self.assertMatch(r"named PR opens as a draft", self.sec(5), "a named PR is a draft, not a stall")

    def test_5_unseen_text_is_drafted_even_when_named(self):
        # Phase-2 hard violations S08 t1/t2 and S15 t1 all sent unseen text
        # after the send was named; the body alone must carry this guard.
        s = self.sec(5)
        hit = self.one_sentence_with(s, ("draft", "show"), "unseen outgoing text is drafted and shown")
        self.assertRegex(hit, r"(?i)not read|never saw|not seen", "rule lost: the draft rule is about unseen text")
        self.assertMatch(r"even when the action was named", s, "the draft rule overrides a named send")

    def test_5_vague_target_gets_one_listing(self):
        s = self.sec(5)
        hit = self.one_sentence_with(s, ("wider", "vaguer"), "a wider or vaguer target is confirmed")
        self.assertRegex(hit, r"(?i)exact .{0,20}paths", "rule lost: the confirmation lists exact paths")
        self.assertRegex(hit, r"(?i)regenerated|regenerable", "rule lost: only non-regenerable data waits")

    def test_8_headless_start_alone_is_not_a_signal(self):
        s = self.sec(8)
        self.assertMatch(r"headless start[^.]{0,60}alone is not a signal", s,
                         "a headless start alone does not enter delegated mode")

    def test_6_blocked(self):
        s = self.sec(6)
        self.assertMatch(r"never idle", s, "never idle")
        self.assertMatch(r"(do )?not invent work", s, "do not invent work")

    def test_6_denial_is_a_stop(self):
        s = self.sec(6)
        self.assertMatch(r"work around it within your permissions", s, "a workaround stays inside permissions")
        hit = self.one_sentence_with(s, ("permission denial", "hook"), "a denial or hook failure is a stop")
        self.assertRegex(hit, r"(?i)answer, not an obstacle")
        self.assertRegex(hit, r"(?i)read-only or plan mode", "rule lost: harness modes are a stop too")
        self.assertMatch(r"do not reach the same effect another way", s, "no second route to a denied effect")
        self.assertIn("--no-verify", s)
        self.assertMatch(r"hook that fails a named commit .{0,40}fix the cause or report it; never skip it", s,
                         "a failing hook on a named commit is fixed, not skipped")
        hit = self.one_sentence_with(s, ("non-fast-forward",), "a non-fast-forward rejection is integrated")
        for needle in (r"fetch", r"integrate", r"re-run verify", r"without force", r"ask only when integration conflicts"):
            self.assertRegex(hit, "(?i)" + needle, "non-fast-forward rule lacks %r" % needle)

    def test_7_long_running(self):
        s = self.sec(7)
        self.assertMatch(r"smoke", s, "smoke-run first")
        self.assertMatch(r"artifact.{0,120}not the exit code", s, "verify the artifact, not the exit code")
        self.assertMatch(r"never say you will watch", s, "no promise without a mechanism")
        found = [m for m in WATCH_MECHANISMS if re.search(m, s, re.I)]
        self.assertGreaterEqual(len(found), 3, "rule lost: at least three watch mechanisms named, found %r" % found)
        self.assertMatch(r"never start a duplicate", s, "no duplicate launch")

    def test_8_delegated(self):
        s = self.sec(8)
        self.assertIn("ASSUMED", s)
        self.assertMatch(r"not a signal", s, "missing question tool is not a signal")
        self.assertMatch(r"files it owns", s, "edit only owned files")
        self.one_sentence_with(s, DELEGATED_LIMITS, "no push, merge, PR, message or spend without a grant")
        self.assertIn("`references/delegated-mode.md`", s)

    def test_9_verify(self):
        s = self.sec(9)
        for needle in (r"\bfloor\b", r"in full", r"final revision", r"prose-only"):
            self.assertMatch(needle, s, needle)
        self.one_sentence_with(s, ("grep", "diff"), "prose-only edits are checked by grep and diff")
        self.assertMatch(r"never weaken|do not weaken", s, "never weaken a gate")
        self.assertMatch(r"deterministic test", s, "deterministic test per logic change")
        self.assertMatch(r"read what its target runs", s, "read the verify target")
        self.assertMatch(r"deletes data or rewrites files outside your change runs in a disposable worktree",
                         s, "a destructive verify target runs in a disposable worktree")
        self.assertMatch(r"fails on the old code", s, "a bug-fix test is shown failing first")
        self.assertMatch(r"last 20 lines and every failing line", s, "verify output is bounded")
        self.assertIn("`references/verify.md`", s)

    def test_9_real_path_is_not_production(self):
        s = self.sec(9)
        hit = self.one_sentence_with(s, ("real path",), "real-path checks")
        self.assertRegex(hit, r"(?i)local, test or staging target, or read-only",
                         "rule lost: real-path checks avoid production writes")
        self.assertRegex(hit, r'(?i)only production, report the writing part "not run"')

    def test_9_self_review_before_the_report(self):
        s = self.sec(9)
        hit = self.one_sentence_with(s, ("Before reporting",), "self-review of the diff")
        for needle in (r"git status --short", r"whole diff", r"scratch files", r"unrelated churn",
                       r"stop the processes and worktrees you started", r"anything left"):
            self.assertRegex(hit, "(?i)" + needle, "self-review lacks %r" % needle)

    def test_9_baseline_redaction_and_pointer(self):
        s = self.sec(9)
        self.assertMatch(r"run it once before the first edit", s, "a quick baseline before the first edit")
        self.assertMatch(r"secrets, tokens and personal data redacted", s, "pasted output is redacted")
        ptr = self.one_sentence_with(s, ("references/verify.md",), "the verify.md pointer")
        self.assertRegex(ptr, r"(?i)cannot show your change did not cause it",
                         "rule lost: a red final run with no baseline routes to verify.md")
        self.assertRegex(ptr, r"(?i)fails only sometimes", "rule lost: flaky failures route to verify.md")
        self.assertRegex(ptr, r"(?i)investigation", "rule lost: investigations route to verify.md")

    def test_10_report(self):
        s = self.sec(10)
        self.assertMatch(r"result first|yes or no", s, "result first")
        self.assertMatch(r"(never|not) pre-filled", s, "report written from output")
        self.assertMatch(r"re-read the request", s, "every ask is matched to evidence before reporting")
        self.assertMatch(r"noticed items", s, "the interactive report has a Noticed slot")
        self.assertMatch(r"blocking question names the missing input and is the last line", s,
                         "a blocking question closes the report (S04)")
        self.assertMatch(r'"Final report" sample in `references/examples\.md`', s,
                         "the report section points at the worked sample")

    def test_examples_hold_the_merge_from_a_todo(self):
        ex = flat(self.sections["## Calibrating examples"])
        push = re.search(r'\*\*"Fix this and push[^*]*\*\*[^*]*', ex)
        self.assertIsNotNone(push, "the scoped-push example is gone")
        self.assertRegex(push.group(0), r"(?i)todo")
        self.assertRegex(push.group(0), r"(?i)merge")


class ConsentReference(unittest.TestCase):
    def setUp(self):
        self.text = flat(read(CONSENT_MD))

    def test_numbered_list_approval(self):
        self.assertRegex(self.text, r"(?i)numbered.list approval")
        self.assertRegex(self.text, r"(?i)every item is approved")

    def test_carried_forward_approval(self):
        self.assertRegex(self.text, r"(?i)carried.forward approval")
        self.assertRegex(self.text, r"(?i)do not re-ask")

    def test_draft_first_for_outgoing_text(self):
        s = [s for s in sentences(self.text) if re.search(r"(?i)\bdraft\b", s)]
        self.assertTrue(any(re.search(r"(?i)show", x) and re.search(r"(?i)send", x) for x in s),
                        "rule lost: draft, show, send on their word")

    def test_provenance_list(self):
        for item in PROVENANCE_DATA:
            self.assertIn(item, self.text, "provenance list lacks %r" % item)
        self.assertRegex(self.text, r"(?i)parent owns the provenance")

    def test_what_a_named_verb_carries(self):
        self.assertRegex(self.text, r"(?i)named push includes committing")
        self.assertRegex(self.text, r"(?i)named merge into a branch that has a remote includes pushing")
        self.assertRegex(self.text, r"force-with-lease")
        nff = [x for x in sentences(self.text) if "non-fast-forward" in x]
        self.assertTrue(nff, "consent.md lost the non-fast-forward rule")
        joined = " ".join(nff) + " " + self.text[self.text.find("non-fast-forward"):][:700]
        for needle in (r"Fetch, integrate the remote commits", r"re-run verify", r"push without force",
                       r"Ask only when integration conflicts", r"commits it would discard"):
            self.assertRegex(joined, "(?i)" + needle, "non-fast-forward rule lacks %r" % needle)
        self.assertNotRegex(self.text, r"(?i)when a push is rejected as non-fast-forward, ask",
                            "asking to force is no longer the default on a rejection")
        self.assertRegex(self.text, r"(?i)stage by explicit path")
        self.assertRegex(self.text, r"git add -A")

    def test_regenerable_definition(self):
        self.assertRegex(self.text, r"(?i)regenerable means you can name the command")
        self.assertRegex(self.text, r"(?i)untracked or gitignored alone is not enough")

    def test_spend_default(self):
        spend = self.text[self.text.find("## Spend"):self.text.find("## Provenance")]
        self.assertRegex(spend, r"(?i)no stated budget")
        self.assertRegex(spend, r"(?i)approved once")
        self.assertRegex(spend, r"(?i)estimate")
        self.assertRegex(spend, r"(?i)relaunch")

    def test_widening_lines_need_the_users_authorship(self):
        # Plain blame prints the author name; only -e (or --porcelain's
        # author-mail) prints the email the check compares.
        self.assertRegex(self.text, r"git blame -e -L")
        self.assertNotRegex(self.text, r"git blame -L")
        self.assertIn("author-mail", self.text)
        self.assertRegex(self.text, r"(?i)noreply alias")
        self.assertIn("Not Committed Yet", self.text)
        self.assertRegex(self.text, r"(?i)narrow .{0,60}apply whoever wrote them")

    def test_named_delete_is_consistent(self):
        # The regenerable test governs vague targets; an exact named path is
        # the named-action rule, even for an export.
        self.assertRegex(self.text, r"(?i)unless the human named the exact path, no command means no delete")
        self.assertRegex(self.text, r"(?i)exact path named, so the named-action rule applies even though")
        self.assertNotRegex(self.text, r"(?i)the file is not source")
        self.assertRegex(self.text, r"(?i)build output and regenerable caches")

    def test_denials_hooks_and_modes(self):
        sec = self.text[self.text.find("## Denials"):self.text.find("## Spend")]
        self.assertTrue(sec, "consent.md lost its denial section")
        self.assertNotRegex(sec, r"(?i)protected-branch or non-fast-forward rejection",
                            "a non-fast-forward rejection is integrated, not treated as a denial")
        for needle in (r"sandbox", r"hook", r"protected-branch", r"plan or read-only mode",
                       r"no second route|take no second route", r"--no-verify", r"fix the cause"):
            self.assertRegex(sec, "(?i)" + needle, "denial rule lacks %r" % needle)

    def test_outside_project(self):
        sec = self.text[self.text.find("## Outside the project"):self.text.find("## Denials")]
        self.assertTrue(sec, "consent.md lost its outside-project section")
        self.assertRegex(sec, r"git config --global")
        self.assertRegex(sec, r"(?i)unless the task is that install")
        self.assertRegex(sec, r"(?i)service the task does not name .{0,80}outward-facing")
        self.assertRegex(sec, r"(?i)local, test or staging target, or is read-only")
        self.assertRegex(sec, r"(?i)production or shared system .{0,120}needs the yes even when the task names")
        self.assertRegex(sec, r'(?i)not run: <reason>')

    def test_unprovable_authorship_stays_data(self):
        self.assertRegex(self.text, r"(?i)unset `user\.email`")
        self.assertRegex(self.text, r"(?i)squash-merge")

    def test_destructive_repo_commands_are_not_routine(self):
        self.assertRegex(self.text, r"(?i)deletes or resets data or rewrites files outside your change runs only in a disposable worktree")

    def test_two_cases_means_two(self):
        sec = self.text[self.text.find("## When a named target"):self.text.find("Routine actions")]
        self.assertNotIn("Named but maybe not meant", sec, "three bullets under 'the two cases'")

    def test_how_to_ask(self):
        self.assertRegex(self.text, r"(?i)numbered list")
        self.assertRegex(self.text, r"(?i)recommended default")
        self.assertRegex(self.text, r"(?i)finish every step that does not depend")


class DelegatedReference(unittest.TestCase):
    def setUp(self):
        self.text = read(DELEGATED_MD)

    def block(self, heading):
        start = self.text.find(heading)
        self.assertNotEqual(start, -1, "missing %r" % heading)
        m = re.search(r"```\n(.*?)```", self.text[start:], re.S)
        self.assertIsNotNone(m, "no fenced template under %r" % heading)
        return m.group(1)

    def test_not_a_signal(self):
        self.assertIn("Not a signal", self.text)

    def test_brief_has_grants(self):
        self.assertRegex(self.block("## Brief template"), r"(?m)^Grants:")

    def test_report_has_assumed_and_noticed(self):
        report = self.block("## Report template")
        self.assertRegex(report, r"(?m)^Assumed:")
        self.assertRegex(report, r"(?m)^Noticed:")
        self.assertRegex(report, r"(?i)quoted, not acted on")

    def test_report_labels_match_the_brief(self):
        brief = self.block("## Brief template")
        report = self.block("## Report template")
        m = re.search(r"Report:(.*?)(?=\n\S|\Z)", brief, re.S)
        self.assertIsNotNone(m, "brief has no Report: line")
        items = [re.sub(r"\(.*?\)", "", i).strip() for i in flat(m.group(1)).split(";")]
        items = [i for i in items if i]
        self.assertGreaterEqual(len(items), 5)
        labels = re.findall(r"(?m)^([A-Z][A-Za-z ]*):", report)
        for item in items:
            self.assertIn(item, labels, "brief asks for %r; report template has no such label" % item)

    def test_test_files_and_baselines(self):
        brief = self.block("## Brief template")
        self.assertRegex(brief, r"(?m)^Owns: .*test file")
        self.assertRegex(brief, r"Deliver: working-tree edits, plus whatever Grants lists")
        self.assertRegex(brief, r"push to X \(includes the commit\)")
        text = flat(self.text)
        self.assertRegex(text, r"(?i)new test file next to owned code is owned")
        self.assertRegex(text, r"(?i)already there, or that a sibling's in-flight edits cause")
        self.assertIn("`references/verify.md`", text)

    def test_brief_start_budget_and_grants(self):
        brief = self.block("## Brief template")
        self.assertRegex(brief, r"(?m)^Start: .*branch.*HEAD")
        self.assertRegex(brief, r"(?m)^Budget: .*wall time.*spend.*retries")
        grants = brief[brief.find("Grants:"):brief.find("Budget:")]
        for g in ("commit on branch X", "push to X", "draft PR", "spend up to", "message to"):
            self.assertIn(g, grants, "Grants has no slot for %r" % g)
        self.assertIn('message to <who>: "<exact text>"', grants, "a message grant carries its exact text")
        self.assertRegex(flat(grants), r"(?i)each with its target")

    def test_grants_name_their_target_and_text(self):
        auth = flat(self.text[self.text.find("## Authority"):self.text.find("## Scope")])
        self.assertRegex(auth, r"(?i)grant names its target")
        self.assertRegex(auth, r"(?i)defect in the brief: report it, do not exercise it")
        self.assertRegex(auth, r"(?i)message grant carries the exact text")
        self.assertRegex(auth, r"(?i)send nothing and put the draft and the send command in the report")

    def test_report_template_redacts(self):
        report = self.block("## Report template")
        self.assertRegex(flat(report), r"(?i)secrets, tokens and personal paths redacted")

    def test_parent_audits_and_stalls(self):
        duties = flat(self.text[self.text.find("## Parent duties"):])
        self.assertIn("git diff --name-only", duties)
        self.assertRegex(duties, r"(?i)outside Owns under Noticed")
        self.assertRegex(duties, r"(?i)cannot be attributed")
        self.assertRegex(duties, r"(?i)no report, gets its log read first")
        self.assertRegex(duties, r"(?i)never start a duplicate")
        self.assertRegex(duties, r"(?i)trust the output, not the summary")

    def test_three_signals_match_skill_md(self):
        sec = self.text[self.text.find("## Am I in this mode?"):self.text.find("Not a signal")]
        self.assertEqual(len(re.findall(r"(?m)^- ", sec)), 3)

    def test_authority_points_to_consent(self):
        auth = flat(self.text[self.text.find("## Authority"):self.text.find("## Scope")])
        self.assertIn("`references/consent.md`", auth)
        self.assertRegex(auth, r"(?i)parent owns the provenance")


class VerifyReference(unittest.TestCase):
    def setUp(self):
        self.text = flat(read(VERIFY_MD))

    def test_baseline(self):
        self.assertRegex(self.text, r"(?i)before the first edit")
        self.assertRegex(self.text, r"(?i)no `git stash`")
        for group in ("New:", "Pre-existing:", "Not yours:"):
            self.assertIn(group, self.text)

    def test_missing_command_and_setup(self):
        self.assertRegex(self.text, r"(?i)no repo verify command found")
        self.assertRegex(self.text, r"(?i)do not add a framework")

    def test_red_before_green(self):
        self.assertRegex(self.text, r"(?i)run the new test on the unfixed code")

    def test_flaky_group(self):
        self.assertIn("Flaky:", self.text)
        self.assertRegex(self.text, r"(?i)one of four groups")
        self.assertRegex(self.text, r"(?i)three times on your tree and three times on the baseline")
        self.assertRegex(self.text, r"(?i)passes on a rerun is not green")

    def test_investigation_method(self):
        sec = self.text[self.text.find("## Investigations"):self.text.find("## Verify targets")]
        self.assertTrue(sec, "verify.md lost its investigation block")
        for needle in (r"reproduce the symptom first", r"cheapest check that tells them apart",
                       r"come back when it is restored", r"ruled out", r"shown, likely or guess",
                       r"does not reproduce"):
            self.assertRegex(sec, "(?i)" + needle, "investigation block lacks %r" % needle)

    def test_large_sweeps(self):
        sec = self.text[self.text.find("## Large sweeps"):self.text.find("## Before the report")]
        self.assertTrue(sec, "verify.md lost its sweep recipe")
        for needle in (r"one command", r"record the count", r"one helper", r"codemod",
                       r"one test per semantic variant", r"count is zero", r"file:line",
                       r'"N of M sites fixed, K listed"'):
            self.assertRegex(sec, "(?i)" + needle, "sweep recipe lacks %r" % needle)

    def test_before_the_report(self):
        sec = self.text[self.text.find("## Before the report"):self.text.find("## Rebuilding state")]
        self.assertTrue(sec, "verify.md lost its self-review step")
        for needle in (r"git status --short", r"git diff", r"scratch files", r"lockfile",
                       r"git worktree remove", r"temporary branches", r"the command that stops"):
            self.assertRegex(sec, "(?i)" + needle, "self-review lacks %r" % needle)

    def test_rebuild_state(self):
        for cmd in ("git status", "git log", "git diff --stat"):
            self.assertIn(cmd, self.text)
        self.assertRegex(self.text, r"(?i)approvals from the lost conversation lapse")


class ExamplesReference(unittest.TestCase):
    def setUp(self):
        self.text = read(EXAMPLES_MD)

    def test_investigation_example(self):
        sec = flat(self.text[self.text.find("## Investigation"):])
        self.assertRegex(sec, r"(?i)bisect")
        self.assertRegex(sec, r"(?i)ruled out")
        self.assertRegex(sec, r"(?i)confidence")

    def test_hook_red_baseline_and_negative_examples(self):
        hook = flat(self.text[self.text.find("## A hook rejects"):self.text.find("## A red baseline")])
        self.assertRegex(hook, r"(?i)fix the type error")
        self.assertIn("--no-verify", hook)
        red = self.text[self.text.find("## A red baseline"):self.text.find("## Not fixed after")]
        for group in ("New:", "Pre-existing:", "Not yours:", "Flaky:"):
            self.assertIn(group, red)
        self.assertRegex(red, r"\w+\.py:\d+", "the red-baseline example names file:line")
        neg = self.text[self.text.find("## Not fixed after"):self.text.find("## Final report")]
        m = re.search(r"```\n(.*?)```", neg, re.S)
        self.assertIsNotNone(m, "the negative report is not a fenced block")
        lines = [l for l in m.group(1).strip().splitlines() if l.strip()]
        self.assertRegex(lines[0], r"^Not fixed", "the negative report leads with the outcome")
        self.assertRegex(lines[-1], r"\?$", "the blocking question is the last line")
        self.assertRegex(m.group(1), r"(?i)ruled out")
        self.assertRegex(m.group(1), r"(?i)candidates")

    def test_final_report_sample(self):
        start = self.text.find("## Final report")
        self.assertNotEqual(start, -1, "examples.md lost the final report sample")
        m = re.search(r"```\n(.*?)```", self.text[start:], re.S)
        self.assertIsNotNone(m, "the final report sample is not a fenced block")
        sample = m.group(1)
        self.assertLessEqual(len(sample.strip().splitlines()), 16, "keep the sample short")
        for label in ("Verify:", "Assumed:", "Not verified:", "Noticed:"):
            self.assertIn(label, sample)
        self.assertRegex(sample.splitlines()[0], r"\d", "the first line carries the result and a count")
        self.assertIn("VERIFY OK", sample)


if __name__ == "__main__":
    unittest.main(verbosity=1)
