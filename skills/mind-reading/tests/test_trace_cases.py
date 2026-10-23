"""Trace cases: preceding-only examples, family grouping, and the frozen chronological family split. Fictional data."""
import copy
import datetime as dt
import json
import time
import unittest

import helpers  # noqa: F401  (puts the skill scripts on sys.path)
from mr import trace_cases as tc

BASE = dt.datetime(2026, 1, 1, 10, 0, 0, tzinfo=dt.timezone.utc)
SECRET = "ZEBRA-FUTURE-7731"


def stamp(day, minute=0):
    return (BASE + dt.timedelta(days=day, minutes=minute)).strftime("%Y-%m-%dT%H:%M:%SZ")


def ev(session, ordinal, role, actor, text, when="auto", source="claude", machine="m1", project="proj-a", **kw):
    if when == "auto":
        when = stamp(0, ordinal)
    row = {
        "id": "%s:%s:%s:%s" % (source, session, ordinal, machine), "source": source, "machine": machine,
        "session": session, "project": project, "time": when, "ordinal": ordinal, "role": role, "actor": actor,
        "text": text, "source_ref": "%s/%s#%d" % (source, session, ordinal), "parent_session": None,
        "human_signal": None, "content_kinds": ["text"], "action": None,
    }
    row.update(kw)
    return row


def convo(session, texts, day=0, spacing=5, **kw):
    """Alternating human turn and agent reply; every human turn is at least two words."""
    out = []
    for i, text in enumerate(texts):
        when = stamp(day, i * spacing)
        out.append(ev(session, 2 * i, "user", "human", text, when=when, **kw))
        out.append(ev(session, 2 * i + 1, "assistant", "agent", "reply %s %d" % (session, i), when=when, **kw))
    return out


def long_text(tag):
    return ("please carry on with the %s work exactly as we discussed earlier and keep the same careful approach "
            "for every remaining step" % tag)


class ExamplesTest(unittest.TestCase):
    def test_same_machine_physical_copy_is_not_an_extra_turn(self):
        events = convo("same", ["keep going", "keep going", "finish it"])
        copied = [dict(e, source_ref="another-file:"+e["source_ref"]) for e in events]
        self.assertEqual(len(tc.build_examples(events+copied)), 2)
        self.assertEqual(tc.build_examples(events+copied)[0]["target"]["text"], "keep going")

    def test_short_fork_prefix_groups_by_original_evidence(self):
        a = convo("original", ["keep going", "finish it"])
        b = convo("fork", ["keep going", "some new task"], day=2)
        a[0]["evidence_key"] = b[0]["evidence_key"] = "original-human-record"
        mapping = tc.families(a+b)
        self.assertEqual(mapping["claude:original"], mapping["claude:fork"])

    def test_export_ordinal_drift_does_not_duplicate_a_human_turn(self):
        events = convo("same", ["keep going", "finish it"])
        for e in events:
            if e["role"] == "user": e["evidence_key"] = "human-original-"+str(e["ordinal"])
        copied = [dict(e, id="copy:"+e["id"], ordinal=e["ordinal"]+1) for e in events if e["role"] == "user"]
        rows = tc.build_examples(events+copied)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["target"]["text"], "finish it")
    def test_future_secret_only_in_target_and_reaction(self):
        evs = [
            ev("s", 0, "user", "human", "start the report"),
            ev("s", 1, "assistant", "agent", "AGENT-AFTER-SEED drafting now"),
            ev("s", 2, "assistant", "agent", "tool ran", action={"kind": "tool", "name": "Bash", "call_id": "c1",
                                                              "success": True, "summary": "AFTER-TOOL-SUMMARY"}),
            ev("s", 3, "user", "human", "ok then %s please" % SECRET),
        ]
        (ex,) = tc.build_examples(evs)
        self.assertIn(SECRET, ex["target"]["text"])
        blob = json.dumps(ex["input"])
        self.assertNotIn(SECRET, blob)
        self.assertNotIn("AGENT-AFTER-SEED", blob)
        self.assertNotIn("AFTER-TOOL-SUMMARY", blob)
        meta = json.dumps({k: v for k, v in ex.items() if k not in ("target", "reaction_input")})
        self.assertNotIn(SECRET, meta)
        react = json.dumps(ex["reaction_input"])
        self.assertNotIn(SECRET, react)
        self.assertIn("AGENT-AFTER-SEED", react)
        self.assertIn("AFTER-TOOL-SUMMARY", react)
        self.assertEqual(ex["reaction_input"]["task"], "post_action_reaction")
        self.assertEqual(ex["input"]["task"], "pre_action_prediction")
        self.assertEqual(ex["input"]["seed"], "start the report")

    def test_prior_context_is_bounded_and_preceding_only(self):
        evs = []
        for i in range(11):
            evs.append(ev("s", 2 * i, "user", "human", "turn number %d here %s" % (i, "x" * 2500 if i == 8 else "")))
            evs.append(ev("s", 2 * i + 1, "assistant", "agent", "A" * 3000 + "end%d" % i,
                          action={"kind": "tool", "name": "T", "call_id": "c%d" % i, "success": True, "summary": "s%d" % i}))
        evs.append(ev("s", 99, "assistant", "agent", "HIDDEN", content_kinds=["thinking"]))
        evs.append(ev("s", 100, "user", "human", "final seed words"))
        exs = tc.build_examples(evs, max_seed_words=60)
        last = [e for e in exs if e["input"]["seed"].startswith("turn number 10")][0]
        self.assertEqual(len(last["input"]["prior_user_turns"]), 3)
        self.assertTrue(all(len(t) <= 2000 for t in last["input"]["prior_user_turns"]))
        self.assertLessEqual(len(last["input"]["prior_agent_text"]), 2000)
        self.assertEqual(len(last["input"]["prior_tools"]), 8)
        self.assertEqual(last["input"]["prior_tools"][-1]["call_id"], "c9")
        self.assertNotIn("HIDDEN", last["reaction_input"]["post_action"]["assistant_text"])
        self.assertIn("end10", last["reaction_input"]["post_action"]["assistant_text"])

    def test_two_machines_same_logical_session_dedupe(self):
        one = convo("s", ["alpha beta", "gamma delta", "epsilon zeta"], machine="m1")
        two = convo("s", ["alpha beta", "gamma delta", "epsilon zeta"], machine="m2")
        single = tc.build_examples(one)
        both = tc.build_examples(one + two)
        self.assertEqual(len(single), 2)
        self.assertEqual(len(both), 2)
        self.assertEqual([e["id"] for e in single], [e["id"] for e in both])
        self.assertEqual(len(tc.families(one + two)), 1)

    def test_legitimate_repeated_short_seed_stays(self):
        evs = convo("s", ["keep going", "keep going", "keep going", "done now"])
        exs = tc.build_examples(evs)
        self.assertEqual([e["input"]["seed"] for e in exs], ["keep going"] * 3)
        self.assertEqual(len({e["id"] for e in exs}), 3)
        # the same repeats on a second machine are one logical copy, not extra events
        evs2 = evs + convo("s", ["keep going", "keep going", "keep going", "done now"], machine="m2")
        self.assertEqual(len(tc.build_examples(evs2)), 3)

    def test_seed_word_bounds_and_generic_short_prompts(self):
        evs = convo("s", ["go", "keep going", " ".join(["w"] * 61), " ".join(["w"] * 60), "last one here"])
        seeds = [e["input"]["seed"] for e in tc.build_examples(evs)]
        self.assertEqual(seeds, ["keep going", " ".join(["w"] * 60)])
        self.assertEqual([e["input"]["seed"] for e in tc.build_examples(evs, max_seed_words=2)], ["keep going"])

    def test_non_human_turns_are_not_targets(self):
        evs = [
            ev("s", 0, "user", "human", "first request here"),
            ev("s", 1, "user", "injected", "system reminder injected"),
            ev("s", 2, "user", "agent", "subagent prompt text"),
            ev("s", 3, "meta", "unknown", "meta record words"),
            ev("s", 4, "user", "unknown", "maybe the person said this"),
            ev("s", 5, "user", "human", "real second request"),
        ]
        (ex,) = tc.build_examples(evs)
        self.assertEqual(ex["target"]["text"], "real second request")
        self.assertEqual(ex["audit"]["unknown_user_turns_between"], 1)
        self.assertEqual(ex["origin"], "human")
        self.assertEqual(ex["weight"], 1)

    def test_unknown_actor_is_opt_in_and_down_weighted(self):
        evs = [
            ev("s", 0, "user", "human", "first request here"),
            ev("s", 1, "user", "unknown", "maybe the person said this"),
            ev("s", 2, "user", "human", "third request now"),
        ]
        default = tc.build_examples(evs)
        self.assertEqual([e["target"]["text"] for e in default], ["third request now"])
        self.assertEqual(default[0]["weight"], 1)
        opted = tc.build_examples(evs, allow_unknown=True)
        self.assertEqual(len(opted), 2)
        self.assertTrue(all(e["origin"] == "unknown" and 0 < e["weight"] <= 0.25 for e in opted))

    def test_invalid_input_raises(self):
        with self.assertRaises(tc.CaseError):
            tc.build_examples([{"source": "x", "session": "s", "role": "user", "actor": "sdk"}])
        with self.assertRaises(tc.CaseError):
            tc.build_examples("nope")
        with self.assertRaises(tc.CaseError):
            tc.build_examples([], max_seed_words=1)

    def test_clocks_none_are_audited_not_guessed(self):
        evs = [
            ev("s", 0, "user", "human", "first request here", when=None),
            ev("s", 1, "user", "human", "second request here", when="not a date"),
            ev("s", 2, "user", "human", "third request here", when="2026-01-01T10:00:00"),   # naive: ambiguous
            ev("s", 3, "user", "human", "fourth request here", when="2026-01-01T12:00:00+02:00"),
        ]
        a, b, c = tc.build_examples(evs)
        self.assertIsNone(a["seed_time"])
        self.assertEqual((a["audit"]["seed_clock"], a["audit"]["target_clock"]), ("missing", "invalid"))
        self.assertEqual(b["audit"]["seed_clock"], "invalid")
        self.assertEqual(c["audit"]["seed_clock"], "invalid")
        self.assertEqual(c["target_time"], "2026-01-01T10:00:00Z")
        self.assertEqual(c["audit"]["clock_order"], "unknown")
        self.assertIsNone(a["input"]["time"])
        spl = tc.freeze_split([a, b, c])
        self.assertEqual(spl["excluded_ids"], sorted(e["id"] for e in (a, b, c)))
        self.assertEqual(spl["status"], "insufficient")

    def test_source_refs_fidelity(self):
        evs = convo("s", ["first request here", "second request here", "third request here"], source="codex", machine="mx")
        exs = tc.build_examples(evs)
        self.assertEqual(exs[1]["source_refs"], {"seed": "codex/s#2", "target": "codex/s#4",
                                                 "prior_user_turns": ["codex/s#0"]})
        self.assertEqual(exs[1]["target"]["source_ref"], "codex/s#4")
        self.assertEqual((exs[1]["source"], exs[1]["machine"], exs[1]["project"]), ("codex", "mx", "proj-a"))
        self.assertEqual(exs[0]["session"], "codex:s")

    def test_deterministic_and_input_not_mutated(self):
        evs = convo("a", ["one two", "three four", "five six"]) + convo("b", ["seven eight", "nine ten"], day=1)
        snap = copy.deepcopy(evs)
        first = tc.build_examples(evs)
        self.assertEqual(first, tc.build_examples(list(reversed(evs))))
        self.assertEqual(first, tc.build_examples(evs))
        self.assertEqual(evs, snap)
        self.assertEqual(json.dumps(tc.freeze_split(first), sort_keys=True), json.dumps(tc.freeze_split(first), sort_keys=True))


class FamiliesTest(unittest.TestCase):
    def test_parent_and_fork_group(self):
        base = convo("root", ["one two", "three four"])
        fork = [dict(e, parent_session="root") for e in convo("fork", ["five six", "seven eight"], day=1)]
        fork2 = [dict(e, parent_session="fork") for e in convo("fork2", ["nine ten", "eleven twelve"], day=2)]
        other = convo("lone", ["alpha beta", "gamma delta"])
        fam = tc.families(base + fork + fork2 + other)
        self.assertEqual(len({fam["claude:root"], fam["claude:fork"], fam["claude:fork2"]}), 1)
        self.assertNotEqual(fam["claude:lone"], fam["claude:root"])
        # parent from another source is not guessed
        cross = [dict(e, parent_session="root") for e in convo("x", ["a b", "c d"], source="codex")]
        self.assertNotEqual(tc.families(base + cross)["codex:x"], tc.families(base + cross)["claude:root"])

    def test_family_id_deterministic_from_sorted_members(self):
        a = convo("a", ["one two"]) + [dict(e, parent_session="a") for e in convo("b", ["three four"])]
        self.assertEqual(tc.families(a), tc.families(list(reversed(a))))
        self.assertTrue(tc.families(a)["claude:a"].startswith("fam_"))

    def test_long_mirror_groups_but_weak_overlap_does_not(self):
        longs = [long_text("task%d" % i) for i in range(5)]
        orig = convo("orig", longs)
        mirror = convo("copy", longs[:4], day=3, project="proj-b")
        one_shared = convo("weak1", [longs[0], long_text("other1"), long_text("other2")], day=4)
        two_of_ten = convo("weak2", [longs[0], longs[1]] + [long_text("w%d" % i) for i in range(8)], day=5)
        short_same = convo("sh1", ["keep going", "keep going", "keep going"]) + convo("sh2", ["keep going"] * 3, day=1)
        fam = tc.families(orig + mirror + one_shared + two_of_ten + short_same)
        self.assertEqual(fam["claude:orig"], fam["claude:copy"])
        self.assertEqual(len({fam[k] for k in ("claude:weak1", "claude:weak2", "claude:orig", "claude:sh1", "claude:sh2")}), 5)

    def test_identical_trajectory_merges_with_human_only(self):
        texts = [long_text("same"), "keep going"]
        a, b = convo("a", texts), convo("b", texts, day=2)
        self.assertEqual(len(set(tc.families(a + b).values())), 1)
        # agent text differences do not matter, human-only content does
        c = convo("c", [long_text("same"), "keep going here"], day=3)
        self.assertEqual(len(set(tc.families(a + c).values())), 2)

    def test_templates_do_not_build_a_mega_family(self):
        t1, t2 = long_text("template-one"), long_text("template-two")
        evs = []
        for i in range(40):
            evs += convo("s%d" % i, [t1, t2, long_text("unique%d" % i)], day=i, project="proj-%d" % i)
        fam = tc.families(evs)
        self.assertEqual(len(set(fam.values())), 40)

    def test_near_linear_not_quadratic(self):
        t1, t2 = long_text("template-one"), long_text("template-two")

        def corpus(n):
            evs = []
            for i in range(n):
                evs += [ev("s%d" % i, 0, "user", "human", t1), ev("s%d" % i, 1, "user", "human", t2),
                        ev("s%d" % i, 2, "user", "human", long_text("unique%d" % i))]
            return evs

        def best(evs):
            runs = []
            for _ in range(2):
                t0 = time.perf_counter()
                fam = tc.families(evs)
                runs.append(time.perf_counter() - t0)
            return min(runs), fam

        small, big = corpus(1500), corpus(12000)
        t_small, _ = best(small)
        t_big, fam = best(big)
        self.assertEqual(len(set(fam.values())), 12000)
        self.assertLess(t_big / max(t_small, 1e-6), 28.0, "8x input took %.1fx time" % (t_big / max(t_small, 1e-6)))


def synthetic(days, turns=3, **kw):
    evs = []
    for i, day in enumerate(days):
        sid = "d%02d" % i
        texts = ["session %s step %d of the work" % (sid, k) for k in range(turns)]
        evs += convo(sid, texts, day=day, **kw)
    return evs


class SplitTest(unittest.TestCase):
    def test_family_fences_ignore_long_conversation_row_count(self):
        examples = tc.build_examples(synthetic(range(20)))
        baseline = tc.freeze_split(examples, boundary_unit="families")
        first = examples[0]
        expanded = examples + [dict(first, id="extra%d" % i) for i in range(100)]
        result = tc.freeze_split(expanded, boundary_unit="families")
        self.assertEqual(baseline["cutoffs"], result["cutoffs"])
        self.assertTrue(result["test_ids"])

    def test_backwards_pair_clock_excludes_whole_family(self):
        examples = tc.build_examples(synthetic(range(20)))
        examples[0]["target_time"] = "2020-01-01T00:00:00Z"
        split = tc.freeze_split(examples)
        self.assertEqual(split["excluded_families"][examples[0]["family"]], "bad_clock")
    def setUp(self):
        evs = synthetic(range(20))
        # one family stretching across every boundary
        evs += [ev("wide", 0, "user", "human", "wide one two", when=stamp(10)),
                ev("wide", 1, "user", "human", "wide three four", when=stamp(11)),
                ev("wide", 2, "user", "human", "wide five six", when=stamp(19))]
        # one family with no usable clock
        evs += [ev("noclock", 0, "user", "human", "no clock a b", when=None),
                ev("noclock", 1, "user", "human", "no clock c d", when=None)]
        self.exs = tc.build_examples(evs)
        self.split = tc.freeze_split(self.exs)
        self.by_id = {e["id"]: e for e in self.exs}

    def span(self, part):
        fams = {}
        for i in self.split[part + "_ids"]:
            e = self.by_id[i]
            fams.setdefault(e["family"], []).extend([e["seed_time"], e["target_time"]])
        return [(min(v), max(v)) for v in fams.values()]

    def test_shape_and_ids_only(self):
        s = self.split
        for key in ("version", "train_ids", "validation_ids", "test_ids", "excluded_ids", "family_partition",
                    "cutoffs", "counts", "protocol"):
            self.assertIn(key, s)
        self.assertEqual(s["status"], "ok")
        blob = json.dumps(s)
        for e in self.exs:
            self.assertNotIn(e["target"]["text"], blob)
        total = sum(len(s[k]) for k in ("train_ids", "validation_ids", "test_ids", "excluded_ids"))
        self.assertEqual(total, len(self.exs))
        self.assertEqual(s["counts"]["examples"], len(self.exs))

    def test_whole_family_chronological_order(self):
        s = self.split
        for part in ("train", "validation", "test"):
            self.assertTrue(s[part + "_ids"], part)
        self.assertLess(max(b for _, b in self.span("train")), min(a for a, _ in self.span("validation")))
        self.assertLess(max(b for _, b in self.span("validation")), min(a for a, _ in self.span("test")))
        homes = {}
        for part in ("train", "validation", "test", "excluded"):
            for i in s[part + "_ids"]:
                homes.setdefault(self.by_id[i]["family"], set()).add(part)
        self.assertTrue(all(len(v) == 1 for v in homes.values()))
        self.assertLessEqual(s["cutoffs"]["train_end"], s["cutoffs"]["validation_end"])

    def test_straddlers_and_bad_clocks_are_excluded(self):
        excluded = {self.by_id[i]["session"] for i in self.split["excluded_ids"]}
        self.assertIn("claude:wide", excluded)
        self.assertIn("claude:noclock", excluded)
        reasons = self.split["counts"]["excluded_by_reason"]
        self.assertEqual(reasons["bad_clock"], 1)
        self.assertGreaterEqual(reasons["straddles_boundary"], 2)

    def test_deterministic(self):
        again = tc.freeze_split(list(reversed(self.exs)))
        self.assertEqual(json.dumps(self.split, sort_keys=True), json.dumps(again, sort_keys=True))

    def test_audit_passes_on_frozen_split(self):
        res = tc.audit_split(self.exs, self.split)
        self.assertTrue(res["ok"], res["issues"])
        self.assertEqual(res["counts"]["train"], len(self.split["train_ids"]))

    def test_thin_data_is_insufficient_not_padded(self):
        one = tc.build_examples(synthetic([0]))
        spl = tc.freeze_split(one)
        self.assertEqual(spl["status"], "insufficient")
        self.assertEqual(spl["test_ids"], [])
        self.assertIn("no_test_examples", spl["counts"]["insufficient_reasons"])
        self.assertEqual(tc.freeze_split([])["status"], "insufficient")
        # everyone overlapping in time: nothing can be held out
        overlap = tc.build_examples(synthetic([0, 0, 0, 0]))
        self.assertEqual(tc.freeze_split(overlap)["status"], "insufficient")

    def test_bad_fractions(self):
        for args in ((0, 0.1), (0.9, 0.2), (1.0, 0), (0.5, -0.1), ("a", 0.1)):
            with self.assertRaises(tc.CaseError):
                tc.freeze_split(self.exs, *args)

    def test_validation_fraction_zero(self):
        spl = tc.freeze_split(self.exs, 0.7, 0)
        self.assertEqual(spl["validation_ids"], [])
        self.assertTrue(spl["test_ids"] and spl["train_ids"])
        self.assertTrue(tc.audit_split(self.exs, spl)["ok"])

    def test_shared_reference_across_families_is_pushed_out_of_later_partition(self):
        exs = copy.deepcopy(self.exs)
        by_id = {e["id"]: e for e in exs}
        train_ex = by_id[self.split["train_ids"][0]]
        test_ex = by_id[self.split["test_ids"][0]]
        test_ex["source_refs"]["seed"] = train_ex["source_refs"]["seed"]
        spl = tc.freeze_split(exs)
        self.assertIn(test_ex["id"], spl["excluded_ids"])
        self.assertIn(train_ex["id"], spl["train_ids"])
        self.assertEqual(spl["counts"]["excluded_by_reason"].get("cross_partition_leak", 0) > 0, True)
        self.assertTrue(tc.audit_split(exs, spl)["ok"])
        # the old split no longer passes against the tampered examples
        res = tc.audit_split(exs, self.split)
        self.assertFalse(res["ok"])
        self.assertTrue(any("reference leak" in i for i in res["issues"]))

    def test_shared_long_target_text_across_partitions_is_rejected(self):
        exs = copy.deepcopy(self.exs)
        by_id = {e["id"]: e for e in exs}
        shared = long_text("shared-target")
        by_id[self.split["train_ids"][0]]["target"]["text"] = shared
        by_id[self.split["test_ids"][0]]["target"]["text"] = shared
        res = tc.audit_split(exs, self.split)
        self.assertFalse(res["ok"])
        self.assertTrue(any("exact-text leak" in i for i in res["issues"]))
        spl = tc.freeze_split(exs)
        self.assertTrue(tc.audit_split(exs, spl)["ok"])
        self.assertIn(self.split["test_ids"][0], spl["excluded_ids"])

    def test_short_repeated_text_is_not_a_leak(self):
        exs = copy.deepcopy(self.exs)
        by_id = {e["id"]: e for e in exs}
        for part in ("train", "test"):
            by_id[self.split[part + "_ids"][0]]["target"]["text"] = "keep going"
        self.assertTrue(tc.audit_split(exs, self.split)["ok"])

    def test_audit_catches_family_temporal_and_coverage_errors(self):
        bad = copy.deepcopy(self.split)
        moved = bad["test_ids"].pop(0)
        bad["train_ids"].append(moved)
        res = tc.audit_split(self.exs, bad)
        self.assertFalse(res["ok"])
        self.assertTrue(any("family split" in i for i in res["issues"]))
        self.assertTrue(any("temporal overlap" in i for i in res["issues"]))

        dropped = copy.deepcopy(self.split)
        dropped["test_ids"].pop()
        self.assertTrue(any("missing from split" in i for i in tc.audit_split(self.exs, dropped)["issues"]))

        dup = copy.deepcopy(self.split)
        dup["test_ids"].append(dup["train_ids"][0])
        self.assertTrue(any("two partitions" in i for i in tc.audit_split(self.exs, dup)["issues"]))

        unknown = copy.deepcopy(self.split)
        unknown["excluded_ids"].append("ex_nope")
        self.assertTrue(any("unknown example id" in i for i in tc.audit_split(self.exs, unknown)["issues"]))

    def test_audit_flags_bad_clock_inside_a_partition(self):
        bad = copy.deepcopy(self.split)
        noclock = [i for i in bad["excluded_ids"] if self.by_id[i]["session"] == "claude:noclock"]
        bad["excluded_ids"] = [i for i in bad["excluded_ids"] if i not in noclock]
        bad["train_ids"] += noclock
        res = tc.audit_split(self.exs, bad)
        self.assertFalse(res["ok"])
        self.assertTrue(any("bad clock" in i for i in res["issues"]))

    @staticmethod
    def family_of(exs, ex_id):
        return [e["family"] for e in exs if e["id"] == ex_id][0]

    def test_mirror_families_cannot_straddle(self):
        longs = [long_text("m%d" % i) for i in range(4)]
        evs = synthetic(range(12)) + convo("orig", longs, day=0) + convo("mirror", longs, day=15)
        exs = tc.build_examples(evs)
        fams = {e["session"]: e["family"] for e in exs}
        self.assertEqual(fams["claude:orig"], fams["claude:mirror"])
        spl = tc.freeze_split(exs)
        self.assertIn(spl["family_partition"][fams["claude:orig"]], ("train", "validation", "test", "excluded"))
        homes = set()
        for part in ("train", "validation", "test", "excluded"):
            for i in spl[part + "_ids"]:
                if self.family_of(exs, i) == fams["claude:orig"]:
                    homes.add(part)
        self.assertEqual(len(homes), 1)
        self.assertTrue(tc.audit_split(exs, spl)["ok"])


if __name__ == "__main__":
    unittest.main()
