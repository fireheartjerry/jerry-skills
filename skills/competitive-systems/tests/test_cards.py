"""Deterministic checks of the arithmetic the cards and worked examples state.

Runs the LPT sketch from cards-scheduling.md against brute force, and checks the numbers
quoted in worked-example.md (Examples B and D) and the binding-term rule in the LPT card.
Standard library only; no network; no timing.
"""
import itertools
import math
import os
import random
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REFS = os.path.join(os.path.dirname(HERE), "references")


def card_blocks(fname, heading):
    """Python fenced blocks inside one `## heading` card."""
    with open(os.path.join(REFS, fname), encoding="utf-8") as f:
        src = f.read()
    start = src.index("## " + heading)
    card = src[start:]
    nxt = card.find("\n## ", 3)
    card = card if nxt == -1 else card[:nxt]
    return re.findall(r"```python\n(.*?)```", card, re.S)


def brute_makespan(durations, m):
    best = math.inf
    for assign in itertools.product(range(m), repeat=len(durations)):
        loads = [0.0] * m
        for d, w in zip(durations, assign):
            loads[w] += d
        best = min(best, max(loads))
    return best


class LptSketchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ns = {}
        exec(card_blocks("cards-scheduling.md", "LPT across workers")[0], ns)
        cls.lpt = staticmethod(ns["lpt"])
        cls.lower_bound = staticmethod(ns["lower_bound"])

    def test_against_brute_force(self):
        rng = random.Random(3)
        for _ in range(200):
            m = rng.randint(2, 3)
            n = rng.randint(0, 6)
            jobs = [(float(rng.randint(1, 30)), "j%d" % i) for i in range(n)]
            plan = self.lpt(jobs, m)
            names = sorted(x for _, bucket in plan for x in bucket)
            self.assertEqual(names, sorted(name for _, name in jobs))
            makespan = max(load for load, _ in plan)
            self.assertAlmostEqual(makespan, max(sum(d for d, n2 in jobs if n2 in b) for _, b in plan)
                                   if n else 0.0)
            opt = brute_makespan([d for d, _ in jobs], m) if n else 0.0
            self.assertLessEqual(makespan, (4.0 / 3 - 1.0 / (3 * m)) * opt + 1e-9)
            self.assertGreaterEqual(makespan + 1e-9, self.lower_bound(jobs, m))

    def test_deterministic_regardless_of_input_order(self):
        rng = random.Random(5)
        jobs = [(float(rng.randint(1, 50)), "job%02d" % i) for i in range(12)]
        shuffled = list(jobs)
        rng.shuffle(shuffled)
        self.assertEqual(self.lpt(jobs, 4), self.lpt(shuffled, 4))

    def test_pinned_jobs_share_worker_zero(self):
        jobs = [(10.0, "a"), (9.0, "b"), (1.0, "c"), (1.0, "d")]
        plan = self.lpt(jobs, 2, pinned={"a", "b"})
        self.assertEqual(sorted(plan[0][1]), ["a", "b"])
        self.assertEqual(self.lower_bound(jobs, 2, pinned={"a", "b"}), 19.0)
        self.assertEqual(self.lower_bound(jobs, 2), 10.5)


class BindingTermTests(unittest.TestCase):
    """The LPT card: splitting the longest job helps only once m exceeds sum/longest."""

    def smallest_m_where_longest_binds(self, total, longest):
        m = 1
        while total / m >= longest:
            m += 1
        return m

    def test_card_example_160_minutes_14_minute_job(self):
        self.assertEqual(self.smallest_m_where_longest_binds(160.0, 14.0), 12)

    def test_example_b_numbers(self):
        total, longest, m = 9632.0, 840.0, 8
        lb = max(total / m, longest)
        self.assertEqual(lb, 1204.0)
        self.assertAlmostEqual(lb / 60, 20.07, places=2)
        self.assertEqual(self.smallest_m_where_longest_binds(total, longest), 12)
        self.assertAlmostEqual(4.0 / 3 - 1.0 / (3 * m), 1.29, places=2)
        self.assertAlmostEqual(42.5 / (lb / 60), 2.1, places=1)
        self.assertAlmostEqual(20.6 / (lb / 60), 1.03, places=2)
        before, after, iqr_before, iqr_after = 40.4, 20.6, 2.3, 0.8
        self.assertGreater(before - after, 2 * max(iqr_before, iqr_after))   # noise rule


class StopAnswerArithmeticTests(unittest.TestCase):
    """Example D and the Gate 2 flip-size rule in measurement.md."""

    def test_operation_counts(self):
        self.assertEqual(40 * 2000, 80000)
        self.assertEqual(40 * 2000 * 10, 800000)

    def test_flip_size_at_one_second(self):
        budget_ops = 1e7
        self.assertEqual(budget_ops / 2000, 5000)      # patterns over 2,000 lines
        self.assertEqual(budget_ops / 40, 250000)      # lines for 40 patterns


class ProseCrossReferenceTests(unittest.TestCase):
    def read(self, name):
        with open(os.path.join(REFS, name), encoding="utf-8") as f:
            return f.read()

    def test_examples_c_and_d_exist_and_are_plain_words(self):
        text = self.read("worked-example.md")
        for ex in ("## Example C", "## Example D"):
            self.assertIn(ex, text)
        tail = text[text.index("## Example C"):]
        self.assertNotIn("```", tail, "stop examples must not end in a report template")
        for word in ("Gate 1:", "Card:", "tier"):
            quoted = [ln for ln in tail.splitlines() if ln.startswith(">")]
            self.assertFalse(any(word in ln for ln in quoted),
                             "skill vocabulary %r leaked into a user-facing answer" % word)

    def test_lpt_card_states_binding_term_and_replay(self):
        text = self.read("cards-scheduling.md")
        self.assertIn("binds", text)
        self.assertIn("Replay the current plan", text)
        self.assertIn("m = 12", text)

    def test_measurement_covers_expensive_runs(self):
        text = self.read("measurement.md")
        self.assertIn("Expensive runs", text)
        self.assertIn("flips", text)


if __name__ == "__main__":
    unittest.main()
