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

DELIVERY_VERBS = ("commit", "push", "merge", "PR", "deploy", "release", "message")
DELEGATED_LIMITS = ("push", "merge", "PR", "message", "spend")
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

    def test_2_resume(self):
        s = self.sec(2)
        self.assertMatch(r"no recap", s, "resume without recap")
        self.assertMatch(r"no new plan|no re-plan", s, "resume without a new plan")

    def test_3_start(self):
        s = self.sec(3)
        self.assertMatch(r"most likely.{0,40}(reading|intended)", s, "most likely reading of a gap")
        self.assertIn(".git/info/exclude", s)
        self.assertMatch(r"(never|not) committed unless asked", s, "plan file never committed unasked")

    def test_4_reach(self):
        s = self.sec(4)
        first = re.search(r"- (.*?)(?= - |$)", s).group(1)
        self.assertMatch(r"\bclass\b", first, "first bullet: the instance means the class")
        self.assertMatch(r"\bgate\b", s, "a stated number is a gate")
        self.assertMatch(r"\bnumber\b", s, "report the real number")
        self.assertMatch(r"one hypothesis|read the actual error", s, "investigate before retrying")
        self.assertMatch(r"not the go-ahead|is not approval", s, "a listed delivery step is not the go-ahead")

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

    def test_10_report(self):
        s = self.sec(10)
        self.assertMatch(r"result first|yes or no", s, "result first")
        self.assertMatch(r"(never|not) pre-filled", s, "report written from output")

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

    def test_authority_points_to_consent(self):
        auth = flat(self.text[self.text.find("## Authority"):self.text.find("## Scope")])
        self.assertIn("`references/consent.md`", auth)
        self.assertRegex(auth, r"(?i)parent owns the provenance")


if __name__ == "__main__":
    unittest.main(verbosity=1)
