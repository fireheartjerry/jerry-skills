"""Conditional habits: scoped matching, literal phrase conditions, unless vetoes, TTL, conflicts, counterevidence,
aliases, explanation privacy, malformed input and the bounded deterministic render. All records are fictional."""
import json
import unittest

import helpers  # noqa: F401  (puts scripts/ on sys.path)
from mr import habits as H

NOW = "2026-06-01T12:00:00Z"


def pref(rid, text, **kw):
    r = {"id": rid, "kind": "preference", "text": text, "source_ref": "note:%s" % rid, "confidence": "S"}
    r.update(kw)
    return r


def ids(rows):
    return [r["id"] for r in rows]


class Scope(unittest.TestCase):
    def setUp(self):
        self.recs = [pref("g1", "keep answers short"), pref("a1", "use tabs", project="alpha"),
                     pref("b1", "use spaces", project="beta")]

    def test_project_sees_global_and_exact_project_only(self):
        res = H.select(self.recs, "format this", project="alpha")
        self.assertEqual(ids(res["habits"]), ["a1", "g1"])
        self.assertNotIn("use spaces", json.dumps(res))

    def test_no_project_means_globals_only(self):
        self.assertEqual(ids(H.select(self.recs, "x")["habits"]), ["g1"])

    def test_other_project_never_leaks_into_any_section(self):
        recs = self.recs + [pref("b2", "beta secret", project="beta", unless=["x"], key="k"),
                            {"id": "b3", "kind": "counterexample", "text": "beta cx", "source_ref": "s", "confidence": "S",
                             "project": "beta", "counter_to": ["g1"]},
                            {"id": "b4", "kind": "alias", "key": "ship", "text": "beta ship", "source_ref": "s",
                             "confidence": "S", "project": "beta", "condition": {"bogus": 1}}]
        res = H.select(recs, "x ship", project="alpha")
        dump = json.dumps(res)
        for leak in ("beta", "b2", "b3", "b4"):
            self.assertNotIn(leak, dump)

    def test_project_in_condition_block_scopes_too(self):
        recs = [pref("c1", "cond scoped", condition={"project": "alpha"})]
        self.assertEqual(ids(H.select(recs, "x", project="alpha")["habits"]), ["c1"])
        self.assertEqual(H.select(recs, "x", project="beta")["habits"], [])
        self.assertEqual(H.select(recs, "x")["habits"], [])

    def test_conflicting_project_fields_are_skipped_with_warning(self):
        recs = [pref("c1", "t", project="alpha", condition={"project": "beta"})]
        res = H.select(recs, "x", project="alpha")
        self.assertEqual(res["habits"], [])
        self.assertTrue(any("conflicting project" in w for w in res["warnings"]))

    def test_phase_condition(self):
        recs = [pref("p1", "review hard", phase="review"), pref("p2", "any phase")]
        self.assertEqual(ids(H.select(recs, "x", phase="Review")["habits"]), ["p1", "p2"])
        self.assertEqual(ids(H.select(recs, "x", phase="build")["habits"]), ["p2"])
        self.assertEqual(ids(H.select(recs, "x")["habits"]), ["p2"])

    def test_limit_orders_project_then_confidence_and_reports_omitted(self):
        recs = [pref("g1", "a"), pref("g2", "b", confidence="L"), pref("a1", "c", project="alpha", confidence="L")]
        res = H.select(recs, "x", project="alpha", limit=2)
        self.assertEqual(ids(res["habits"]), ["a1", "g1"])
        self.assertEqual(res["omitted"], 1)
        for bad in (0, -1, True, "3"):
            with self.assertRaises(H.HabitError):
                H.select(recs, "x", limit=bad)


class Phrases(unittest.TestCase):
    def apply(self, terms, seed):
        return bool(H.select([pref("t", "x", terms=terms)], seed)["habits"])

    def test_short_words_need_word_boundaries(self):
        self.assertFalse(self.apply(["go"], "we are going ago"))
        self.assertTrue(self.apply(["go"], "ok, go."))
        self.assertTrue(self.apply(["go"], "(go)"))
        self.assertTrue(self.apply(["go"], "GO"))
        self.assertFalse(self.apply(["go"], "go_lang"))

    def test_all_terms_required_and_deduped_casefolded(self):
        self.assertFalse(self.apply(["fix", "tests"], "fix it"))
        self.assertTrue(self.apply(["fix", "FIX", "tests"], "Fix the   tests"))

    def test_multiword_phrase_ignores_whitespace_runs(self):
        self.assertTrue(self.apply(["pull request"], "open a pull\n  request now"))
        self.assertFalse(self.apply(["pull request"], "pull requests"))

    def test_terms_in_condition_block_and_top_level_combine(self):
        r = pref("t", "x", terms=["a1"], condition={"terms": ["b2"]})
        self.assertEqual(H.select([r], "a1 only")["habits"], [])
        self.assertEqual(ids(H.select([r], "a1 and b2")["habits"]), ["t"])

    def test_regex_metacharacters_are_literal(self):
        self.assertFalse(self.apply([".*"], "anything"))
        self.assertTrue(self.apply(["c++"], "write c++ code"))
        self.assertTrue(self.apply(["a.b"], "see a.b here"))
        self.assertFalse(self.apply(["a.b"], "see axb here"))

    def test_bad_term_entries_skip_the_record_instead_of_widening(self):
        for terms in ([""], [3], "go", [None]):
            res = H.select([pref("t", "x", terms=terms)], "go")
            self.assertEqual(res["habits"], [], terms)
            self.assertEqual(len(res["warnings"]), 1)


class Unless(unittest.TestCase):
    def test_unless_vetoes_and_is_reported_as_exception(self):
        r = pref("u1", "prefer small diffs", unless=["rewrite", "migration"], source_ref="note:u1")
        res = H.select([r], "plan the Migration")
        self.assertEqual(res["habits"], [])
        self.assertEqual(ids(res["exceptions"]), ["u1"])
        ex = res["exceptions"][0]
        self.assertEqual(ex["vetoed_by"], ["migration"])
        self.assertIn('"migration"', ex["explanation"])
        self.assertEqual(ex["source_ref"], "note:u1")

    def test_unless_in_condition_block(self):
        r = pref("u1", "t", condition={"unless": ["hotfix"]})
        self.assertEqual(ids(H.select([r], "do a hotfix")["exceptions"]), ["u1"])

    def test_no_exception_when_another_condition_already_failed(self):
        r = pref("u1", "t", terms=["deploy"], unless=["rollback"])
        res = H.select([r], "rollback now")
        self.assertEqual((res["habits"], res["exceptions"]), ([], []))

    def test_unless_boundaries(self):
        r = pref("u1", "t", unless=["no"])
        self.assertEqual(ids(H.select([r], "know it")["habits"]), ["u1"])
        self.assertEqual(ids(H.select([r], "no, wait")["exceptions"]), ["u1"])

    def test_aliases_and_counterexamples_are_not_exceptions(self):
        a = {"id": "a", "kind": "alias", "key": "k", "text": "m", "source_ref": "s", "confidence": "S", "unless": ["k"]}
        res = H.select([a], "k")
        self.assertEqual((res["aliases"], res["exceptions"]), ([], []))


class Lifecycle(unittest.TestCase):
    def test_expiry_with_now(self):
        r = pref("e1", "t", expires_at="2026-05-01T00:00:00Z")
        self.assertEqual(H.select([r], "x", now=NOW)["habits"], [])
        self.assertEqual(ids(H.select([r], "x", now="2026-04-30T00:00:00+00:00")["habits"]), ["e1"])
        self.assertEqual(H.select([r], "x", now="2026-05-01T00:00:00Z")["habits"], [])

    def test_expiry_without_now_fails_closed(self):
        self.assertEqual(H.select([pref("e1", "t", expires_at="2099-01-01")], "x")["habits"], [])

    def test_bad_expiry_is_malformed(self):
        res = H.select([pref("e1", "t", expires_at="soon")], "x", now=NOW)
        self.assertEqual(res["habits"], [])
        self.assertIn("expires_at", res["warnings"][0])

    def test_bad_now_raises(self):
        with self.assertRaises(H.HabitError):
            H.select([], "x", now="yesterday")

    def test_inactive_and_forgotten_statuses(self):
        recs = [pref("s1", "t", status="forgotten"), pref("s2", "t", status="superseded"), pref("s3", "t", status="Active")]
        self.assertEqual(ids(H.select(recs, "x")["habits"]), ["s3"])

    def test_guess_confidence_is_not_applied(self):
        res = H.select([pref("g", "t", confidence="G"), pref("l", "t2", confidence="l")], "x")
        self.assertEqual(ids(res["habits"]), ["l"])
        self.assertTrue(any("confidence G" in w for w in res["warnings"]))

    def test_confidence_is_the_records_own_tag(self):
        h = H.select([pref("l", "t", confidence="L")], "t t")["habits"][0]
        self.assertEqual(h["confidence"], "L")
        self.assertFalse([k for k in h if "score" in k])


class Conflicts(unittest.TestCase):
    def test_same_key_different_values_conflict_and_project_preferred(self):
        recs = [pref("g1", "tabs", key="indent", value="tabs"),
                pref("a1", "spaces", key="indent", value="spaces", project="alpha")]
        res = H.select(recs, "x", project="alpha")
        self.assertEqual(len(res["conflicts"]), 1)
        c = res["conflicts"][0]
        self.assertEqual((c["key"], c["ids"], c["preferred"]), ("indent", ["a1", "g1"], ["a1"]))
        self.assertEqual(ids(res["habits"]), ["a1", "g1"])  # both stay visible
        self.assertEqual(res["habits"][0]["in_conflict"], ["g1"])

    def test_same_scope_conflict_has_no_preference(self):
        recs = [pref("g1", "a", key="K"), pref("g2", "b", key="k")]
        c = H.select(recs, "x")["conflicts"][0]
        self.assertEqual(c["preferred"], [])

    def test_same_value_or_blank_key_is_not_a_conflict(self):
        recs = [pref("g1", "Tabs ", key="i"), pref("g2", "tabs", key="i"), pref("g3", "a"), pref("g4", "b", key="  ")]
        res = H.select(recs, "x")
        self.assertEqual(res["conflicts"], [])
        self.assertEqual(ids(res["habits"]), ["g1", "g3", "g4"])  # g2 is a duplicate of g1
        self.assertEqual(res["habits"][0]["also"], ["g2"])

    def test_inapplicable_records_do_not_conflict(self):
        recs = [pref("g1", "a", key="k"), pref("g2", "b", key="k", terms=["deploy"])]
        self.assertEqual(H.select(recs, "x")["conflicts"], [])

    def test_conflict_survives_the_limit(self):
        recs = [pref("g1", "a", key="k"), pref("g2", "b", key="k")]
        res = H.select(recs, "x", limit=1)
        self.assertEqual(res["conflicts"][0]["ids"], ["g1", "g2"])

    def test_project_records_disagreeing_are_unresolved(self):
        recs = [pref("a1", "x", key="k", project="p"), pref("a2", "y", key="k", project="p"), pref("g", "z", key="k")]
        c = H.select(recs, "q", project="p")["conflicts"][0]
        self.assertEqual(c["preferred"], [])
        self.assertIn("disagree", c["resolution"])


class Counterevidence(unittest.TestCase):
    def cx(self, rid, to, **kw):
        r = {"id": rid, "kind": "counterexample", "text": "it failed once on a retry", "source_ref": "note:%s" % rid,
             "confidence": "S", "counter_to": to}
        r.update(kw)
        return r

    def test_linked_to_applicable_habit(self):
        res = H.select([pref("h1", "t"), self.cx("c1", ["h1", "zzz"])], "x")
        self.assertEqual(res["counterexamples"][0]["counter_to"], ["h1"])
        self.assertEqual(res["counterexamples"][0]["source_ref"], "note:c1")

    def test_unlinked_or_inapplicable_target_is_dropped(self):
        recs = [pref("h1", "t", terms=["deploy"]), pref("h2", "t2"), self.cx("c1", ["h1"]), self.cx("c2", ["nope"]),
                self.cx("c3", ["h2"], status="forgotten"), self.cx("c4", ["h2"], terms=["never"])]
        self.assertEqual(H.select(recs, "x")["counterexamples"], [])

    def test_counterexample_needs_counter_to(self):
        res = H.select([pref("h1", "t"), self.cx("c1", [])], "x")
        self.assertEqual(res["counterexamples"], [])
        self.assertEqual(len(res["warnings"]), 1)

    def test_other_project_counterexample_is_hidden(self):
        res = H.select([pref("h1", "t"), self.cx("c1", ["h1"], project="beta")], "x", project="alpha")
        self.assertEqual(res["counterexamples"], [])


class Aliases(unittest.TestCase):
    def alias(self, **kw):
        r = {"id": "al1", "kind": "alias", "key": "the thing", "value": "the billing export", "source_ref": "note:al1",
             "confidence": "S"}
        r.update(kw)
        return r

    def test_alias_matches_literal_phrase_and_keeps_seed(self):
        seed = "Fix the thing before friday"
        a = H.select([self.alias()], seed)["aliases"][0]
        self.assertEqual((a["phrase"], a["meaning"], a["seed"]), ("the thing", "the billing export", seed))
        self.assertIn("not executed", a["annotation"])
        self.assertEqual(a["source_ref"], "note:al1")

    def test_alias_needs_phrase_in_seed_with_boundaries(self):
        self.assertEqual(H.select([self.alias(key="go")], "going")["aliases"], [])
        self.assertEqual(H.select([self.alias()], "fix something")["aliases"], [])

    def test_alias_meaning_falls_back_to_text_and_requires_one(self):
        a = self.alias()
        del a["value"]
        a["text"] = "billing export"
        self.assertEqual(H.select([a], "the thing")["aliases"][0]["meaning"], "billing export")
        del a["text"]
        self.assertEqual(H.select([a], "the thing")["aliases"], [])

    def test_alias_respects_project_and_unless(self):
        self.assertEqual(H.select([self.alias(project="p")], "the thing")["aliases"], [])
        self.assertEqual(len(H.select([self.alias(project="p")], "the thing", project="p")["aliases"]), 1)
        self.assertEqual(H.select([self.alias(unless=["urgent"])], "urgent the thing")["aliases"], [])

    def test_alias_is_not_a_habit(self):
        res = H.select([self.alias()], "the thing")
        self.assertEqual(res["habits"], [])


class Malformed(unittest.TestCase):
    def test_garbage_records_are_skipped_not_fatal(self):
        recs = [None, 7, "x", [], {}, {"id": "a"}, {"id": "b", "kind": "preference"},
                {"id": "c", "kind": "preference", "text": "t", "source_ref": "s"},
                {"id": "d", "kind": "preference", "text": "t", "source_ref": "s", "confidence": "S", "terms": {"a": 1}},
                {"id": "e", "kind": "preference", "text": 5, "source_ref": "s", "confidence": "S"},
                {"id": "f", "kind": "preference", "text": "t", "confidence": "S"},
                {"id": "g", "kind": "preference", "text": "t", "source_ref": "s", "confidence": "S", "condition": "x"},
                pref("ok", "fine")]
        res = H.select(recs, "x")
        self.assertEqual(ids(res["habits"]), ["ok"])
        self.assertEqual(len(res["warnings"]), len(recs) - 1)

    def test_unsupported_condition_key_skips_with_warning(self):
        res = H.select([pref("u", "t", condition={"mood": "tired"}), pref("ok", "t2")], "x")
        self.assertEqual(ids(res["habits"]), ["ok"])
        self.assertIn("unsupported condition key(s): mood", res["warnings"][0])

    def test_duplicate_ids_first_wins(self):
        res = H.select([pref("d", "first"), pref("d", "second")], "x")
        self.assertEqual([h["text"] for h in res["habits"]], ["first"])
        self.assertTrue(any("duplicate id" in w for w in res["warnings"]))

    def test_api_argument_errors(self):
        with self.assertRaises(H.HabitError):
            H.select({}, "x")
        with self.assertRaises(H.HabitError):
            H.select([], 5)
        with self.assertRaises(H.HabitError):
            H.select([], "x", project=3)
        self.assertTrue(issubclass(H.HabitError, ValueError))

    def test_extra_fields_never_become_permissions_or_traits(self):
        r = pref("p", "t", permissions=["delete"], trait="impatient", grant=True)
        h = H.select([r], "x")["habits"][0]
        self.assertNotIn("delete", json.dumps(h))
        self.assertNotIn("impatient", json.dumps(h))

    def test_unknown_kind_is_context_only(self):
        res = H.select([{"id": "f1", "kind": "fact", "text": "t", "confidence": "S"}], "x")
        self.assertEqual(res["habits"], [])
        self.assertEqual(res["warnings"], [])

    def test_does_not_mutate_input_and_is_deterministic(self):
        recs = [pref("b", "t", terms=["x"]), pref("a", "u")]
        before = json.dumps(recs, sort_keys=True)
        r1 = H.select(recs, "x")
        r2 = H.select(list(reversed(recs)), "x")
        self.assertEqual(json.dumps(recs, sort_keys=True), before)
        self.assertEqual(r1, r2)


class Explain(unittest.TestCase):
    def test_applies_with_source_and_reasons(self):
        recs = [pref("h1", "short diffs", terms=["review"], phase="build", unless=["rewrite"], key="diff")]
        ex = H.explain(recs, "h1", seed="please review this", phase="build")
        self.assertTrue(ex["applies"])
        self.assertEqual(ex["outcome"], "applies")
        self.assertEqual(ex["record"]["source_ref"], "note:h1")
        self.assertEqual(ex["source_ref"], "note:h1")
        text = " ".join(ex["reasons"])
        for frag in ("global record", "phase build matches", "review", "no unless", "confidence S", "overrides"):
            self.assertIn(frag, text)

    def test_unmet_outcomes(self):
        recs = [pref("t", "x", terms=["deploy"]), pref("u", "x", unless=["stop"]), pref("p", "x", phase="qa"),
                pref("g", "x", confidence="G")]
        self.assertEqual(H.explain(recs, "t", seed="nothing")["outcome"], "terms_unmet")
        self.assertEqual(H.explain(recs, "u", seed="stop")["outcome"], "vetoed")
        self.assertEqual(H.explain(recs, "p", phase="dev")["outcome"], "phase_mismatch")
        self.assertEqual(H.explain(recs, "g")["outcome"], "low_confidence")
        for i in "tupg":
            self.assertFalse(H.explain(recs, i, seed="stop")["applies"] and i != "t")

    def test_expired_missing_forgotten(self):
        recs = [pref("e", "old rule", expires_at="2026-01-01"), pref("f", "secret text", status="forgotten")]
        ex = H.explain(recs, "e", now=NOW)
        self.assertEqual((ex["outcome"], ex["applies"]), ("expired", False))
        self.assertEqual(ex["record"]["text"], "old rule")
        fg = H.explain(recs, "f")
        self.assertEqual(fg["outcome"], "forgotten")
        self.assertNotIn("secret text", json.dumps(fg))
        self.assertNotIn("note:f", json.dumps(fg))
        self.assertEqual(H.explain(recs, "nope")["outcome"], "missing")

    def test_other_project_is_indistinguishable_from_missing(self):
        recs = [pref("b1", "beta only text", project="beta", source_ref="beta/private.md")]
        hidden = H.explain(recs, "b1", project="alpha")
        absent = H.explain(recs, "zzz", project="alpha")
        self.assertEqual(hidden["outcome"], "missing")
        self.assertEqual(hidden["reasons"], absent["reasons"])
        self.assertIsNone(hidden["record"])
        self.assertNotIn("beta", json.dumps(hidden))
        self.assertEqual(H.explain(recs, "b1")["outcome"], "missing")
        self.assertEqual(H.explain(recs, "b1", project="beta")["outcome"], "applies")

    def test_malformed_record_reveals_no_text(self):
        ex = H.explain([pref("m", "hidden words", condition={"zzz": 1})], "m")
        self.assertEqual(ex["outcome"], "malformed")
        self.assertNotIn("hidden words", json.dumps(ex))

    def test_counterexample_applies_only_with_live_target(self):
        cx = {"id": "c", "kind": "counterexample", "text": "t", "source_ref": "s", "confidence": "S", "counter_to": ["h"]}
        self.assertTrue(H.explain([pref("h", "x"), cx], "c")["applies"])
        gone = H.explain([pref("h", "x", status="forgotten"), cx], "c")
        self.assertEqual((gone["applies"], gone["outcome"]), (False, "unlinked"))

    def test_non_habit_kind_is_labelled(self):
        ex = H.explain([{"id": "f", "kind": "fact", "text": "t", "confidence": "S"}], "f")
        self.assertTrue(any("not a habit" in r for r in ex["reasons"]))

    def test_argument_errors(self):
        with self.assertRaises(H.HabitError):
            H.explain([], "")
        with self.assertRaises(H.HabitError):
            H.explain("x", "a")


class Render(unittest.TestCase):
    def result(self, n=0):
        recs = [pref("g1", "tabs", key="indent", value="tabs"), pref("a1", "spaces", key="indent", value="spaces", project="p"),
                pref("x1", "never force push", unless=["rebase"], terms=["push"]),
                {"id": "c1", "kind": "counterexample", "text": "tabs broke yaml", "source_ref": "note:c1", "confidence": "S",
                 "counter_to": ["g1"]},
                {"id": "al", "kind": "alias", "key": "the thing", "value": "billing export", "source_ref": "note:al",
                 "confidence": "L"}]
        recs += [pref("z%02d" % i, "filler preference number %d" % i) for i in range(n)]
        return H.select(recs, "push the thing with a rebase", project="p", limit=50)

    def test_sections_ids_sources_and_determinism(self):
        res = self.result()
        out = H.render(res)
        self.assertEqual(out, H.render(res))
        for title in ("## Conditional preferences", "## Exceptions", "## Conflicts", "## Counterexamples", "## Shorthand"):
            self.assertIn(title, out)
        for frag in ("[g1]", "[a1]", "[x1]", "[c1]", "[al]", "note:c1", "note:al", "prefer a1", '"tabs"', '"spaces"',
                     "not expanded", "conflicts with"):
            self.assertIn(frag, out)
        self.assertNotIn("Truncated", out)

    def test_empty_result(self):
        out = H.render(H.select([], "x"))
        self.assertEqual(out.count("none"), 5)

    def test_budget_is_respected_and_truncation_disclosed(self):
        res = self.result(40)
        full = H.render(res, 100000)
        for budget in (len(full) - 1, 1500, 900):
            out = H.render(res, budget)
            self.assertLessEqual(len(out), budget)
            self.assertIn("Truncated", out)
            self.assertRegex(out, r"omitted \d+ item\(s\)")
            self.assertIn("[g1]" if "[g1]" in out else "[a1]", out)
            for line in out.splitlines():  # every kept preference line is whole
                if line.startswith("- [z"):
                    self.assertTrue(line.endswith(")"), line)
                    self.assertIn(line, full)

    def test_conflict_is_never_dropped_for_budget(self):
        res = self.result(40)
        base = H.render(res, 100000)
        for budget in range(300, 1500, 50):
            out = H.render(res, budget)
            if out.startswith("Over budget"):
                self.assertLessEqual(len(out), budget)
                continue
            self.assertIn("key \"indent\"", out)
            self.assertIn("[g1]", out.split("## Conflicts")[1].split("## Counterexamples")[0])
            self.assertLessEqual(len(out), budget)
        long = "very long position text " * 20
        big = H.select([pref("g1", long + "one", key="k"), pref("g2", long + "two", key="k")], "x")
        compact = H.render(big, self._tight(big))
        self.assertIn("text omitted for budget", compact)
        self.assertIn("[g1]", compact)
        self.assertNotIn(long + "one", compact)
        self.assertGreater(len(base), 900)

    def _tight(self, res):
        lo = 1
        while "## Conflicts" not in H.render(res, lo):
            lo += 1
        return lo

    def test_over_budget_hint_when_conflict_cannot_fit(self):
        res = self.result()
        out = H.render(res, 120)
        self.assertTrue(out.startswith("Over budget"))
        self.assertLessEqual(len(out), 120)
        self.assertEqual(H.render(res, 1)[:1], "O")

    def test_no_invented_quotes(self):
        res = self.result(40)
        texts = [h["text"] for h in res["habits"]]
        out = H.render(res, 1200)
        for line in out.splitlines():
            if line.startswith("- [") and '"' in line:
                self.assertTrue(any('"%s"' % t in line for t in texts + ["never force push"]) or "counters" in line or "vetoed" in line
                                or "otherwise relevant" in line, line)

    def test_selection_limit_and_warnings_are_disclosed(self):
        res = H.select([pref("a", "x"), pref("b", "y"), {"id": "bad"}], "q", limit=1)
        out = H.render(res)
        self.assertIn("1 lower-priority preference(s) were cut", out)
        self.assertIn("1 record(s) skipped", out)

    def test_argument_errors(self):
        with self.assertRaises(H.HabitError):
            H.render({}, 0)
        with self.assertRaises(H.HabitError):
            H.render([], 100)

    def test_multiline_text_stays_on_one_line(self):
        out = H.render(H.select([pref("m", "line one\nline two")], "x"))
        self.assertIn('"line one line two"', out)


if __name__ == "__main__":
    unittest.main()
