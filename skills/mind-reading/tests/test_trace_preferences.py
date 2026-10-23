import copy
import unittest
import helpers
from mr.trace_preferences import extract_quotes, select_quotes


def case(i, family, quote):
    return {"id": str(i), "family": family, "project": "fiction", "origin": "human", "weight": 1, "partition": "train",
            "input": {"seed": "fix the export", "prior_user_turns": ["Only this task has the narrow constraint."]},
            "target": {"text": quote, "source_ref": "target-"+str(i)}, "source_refs": {"seed": "seed-"+str(i)}}


class QuoteTests(unittest.TestCase):
    def test_exact_words_scoped_with_counter_context(self):
        text = "Actually, I prefer a short export summary for this audience."
        q = extract_quotes([case(1, "a", text)])[0]
        self.assertIn(q["quote"], text)
        self.assertIn("observed contexts", q["scope"])
        self.assertEqual(q["observations"][0]["past_context"], ["Only this task has the narrow constraint."])

    def test_one_family_is_one_support_and_eval_is_refused(self):
        rows = [case(1, "a", "I prefer brief export summaries."), case(2, "a", "I prefer brief export summaries.")]
        self.assertEqual(extract_quotes(rows)[0]["support_families"], 1)
        rows.append(case(3, "b", "I prefer brief export summaries."))
        self.assertEqual(extract_quotes(rows)[0]["support_families"], 2)
        bad = copy.deepcopy(rows)
        bad[0]["partition"] = "test"
        with self.assertRaises(ValueError): extract_quotes(bad)

    def test_retrieves_evidence_without_turning_it_into_rule(self):
        q = select_quotes("export summary", {}, [case(1, "a", "I prefer brief export summaries.")])[0]
        self.assertIn("not authority", q["interpretation"])
        self.assertNotIn("rule", q)

    def test_current_family_quotes_are_excluded_too(self):
        row=case(1,"current","I prefer brief export summaries.")
        self.assertEqual(select_quotes("export summary",{"family":"current"},[row]),[])
        row["session"]="current-session"
        self.assertEqual(select_quotes("export summary",{"session":"current-session"},[row]),[])
