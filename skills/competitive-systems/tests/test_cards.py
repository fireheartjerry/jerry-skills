"""Deterministic checks of the arithmetic and cross-references the documentation states.

The numbers quoted in worked-example.md (Examples A, B and D), the binding-term rule in the
LPT card, the Gate 2 flip sizes, and prose cross-references between SKILL.md and the
references. The card sketches themselves are executed in test_sketches.py. Standard library
only; no network; no timing.
"""
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REFS = os.path.join(os.path.dirname(HERE), "references")


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
        replay, plan = 2538.0, 1208.0                 # dev.py bound makespan output
        self.assertAlmostEqual(replay / 60, 42.3, places=1)
        self.assertAlmostEqual(replay / lb, 2.11, places=2)
        self.assertAlmostEqual(plan / lb - 1, 0.003, places=3)  # "0.3% above LB"
        self.assertAlmostEqual(replay / plan, 2.10, places=2)
        before, after = 2424.0, 1236.0                # compare medians: history, reruns
        self.assertAlmostEqual(before / 60, 40.4, places=1)
        self.assertAlmostEqual(after / 60, 20.6, places=1)
        self.assertAlmostEqual(before / lb, 2.01, places=2)
        self.assertAlmostEqual(after / lb, 1.03, places=2)
        self.assertAlmostEqual(before / after, 1.96, places=2)


class ExampleAArithmeticTests(unittest.TestCase):
    """Example A: the budget model against the measured times, Gate 2 and Gate 3."""

    def test_budget_matches_measurement_and_report(self):
        ops = lambda n: 0.0085 * n * n
        self.assertAlmostEqual(ops(80000) / 1e7, 5.44, places=2)     # measured 5.08 s
        self.assertAlmostEqual(ops(1.2e6) / 1e7, 1224.0, places=0)   # "1,224 s", ~19 min
        self.assertAlmostEqual(ops(1.2e7) / 1e7 / 3600, 34.0, places=0)
        self.assertAlmostEqual(5.08 * 15 ** 1.91 / 60, 15.0, delta=0.2)
        cap = 1 / (1 - 0.98)
        self.assertAlmostEqual(cap, 50.0)
        self.assertLess(28.78, cap)                                  # within the Amdahl cap
        self.assertAlmostEqual(1.37 / 0.0476, 28.78, delta=0.1)


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
        self.assertIn("Hand rule", text)
        self.assertNotIn("## 2. Gate 1", text, "gate text lives once, in SKILL.md")
        with open(os.path.join(os.path.dirname(REFS), "SKILL.md"), encoding="utf-8") as f:
            skill = f.read()
        self.assertIn("flip size", skill)

    def test_guards_state_the_correctness_rule_and_tests(self):
        text = self.read("guards.md")
        self.assertIn("the oracle runs before the timing", text)
        self.assertNotRegex(text, r"\d+ to\s+\d+%", "uncited statistic")
        self.assertIn("mutate the input in place", text)
        self.assertIn("swap the run order", text)
        self.assertEqual(text.count("\n1. "), 2)       # oracle stack and checklist both numbered
        for n in range(1, 10):
            self.assertIn("\n%d. " % n, text)

    def test_example_b_oracle_line_matches_the_instance(self):
        text = self.read("worked-example.md")
        self.assertIn("brute force m^n for n <= 8, m <= 3 on 200 random cases, seed 3: pass "
                      "(checks the algorithm); the m = 8 plan itself is checked against LB (0.3% "
                      "above) and the (4/3 - 1/24) = 1.29 bound.", text)


if __name__ == "__main__":
    unittest.main()
