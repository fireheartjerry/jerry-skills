"""Pure-function tests for the local reranker helpers. No model is loaded and nothing touches the network."""
import os
import sys
import tempfile
import unittest

from helpers import SCRIPTS  # noqa: F401  (puts scripts/ on sys.path)

from mr import trace_rerank as tr


class FakeScorer:
    def __init__(self, table=None, bad=None):
        self.table, self.bad, self.calls = table or {}, bad, []

    def scores(self, query, documents):
        self.calls.append((query, list(documents)))
        if self.bad is not None:
            return self.bad
        return [self.table.get(d, 0.5) for d in documents]


class FormatTests(unittest.TestCase):
    def test_prompt_is_exact(self):
        text = tr.format_pair("q", "d")
        self.assertTrue(text.startswith("<|im_start|>system\nJudge whether the Document meets the requirements"))
        self.assertIn("<Instruct>: " + tr.INSTRUCT + "\n<Query>: q\n<Document>: d", text)
        self.assertTrue(text.endswith("<|im_start|>assistant\n<think>\n\n</think>\n\n"))

    def test_text_is_not_altered(self):
        text = tr.format_pair("  qé\n", "\td ☃ ")
        self.assertIn("<Query>:   qé\n\n<Document>: \td ☃ <|im_end|>", text)

    def test_offline_kwargs(self):
        self.assertIs(tr.tokenizer_kwargs()["local_files_only"], True)
        self.assertIs(tr.model_kwargs("float32")["local_files_only"], True)
        self.assertEqual(tr.tokenizer_kwargs()["padding_side"], "left")

    def test_missing_directory_is_an_error_before_any_import(self):
        before = {k: os.environ.get(k) for k in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")}
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                tr.LocalReranker(os.path.join(tmp, "absent"))
        self.assertEqual(before, {k: os.environ.get(k) for k in before})

    def test_max_length_validated(self):
        with self.assertRaises(ValueError):
            tr.LocalReranker(".", max_length=8)


class RerankPairsTests(unittest.TestCase):
    DOCS = {i: "doc%d" % i for i in range(20)}

    def run_rerank(self, pairs, scorer, **kw):
        return tr.rerank_pairs(pairs, self.DOCS.__getitem__, "query", scorer, **kw)

    def test_reranks_only_top_k_by_base_score_and_keeps_tail_below(self):
        pairs = [(i, 1.0 - i / 100) for i in range(20)]
        scorer = FakeScorer({"doc3": 0.9, "doc0": 0.2})
        out = self.run_rerank(pairs, scorer)
        self.assertEqual(len(out), 20)
        self.assertEqual(scorer.calls[0][1], ["doc%d" % i for i in range(12)])
        self.assertEqual(scorer.calls[0][0], "query")
        head = out[:12]
        self.assertEqual([i for i, _ in head], list(range(12)))
        self.assertEqual(dict(head)[3], 0.9)
        floor = min(s for _, s in head)
        tail = out[12:]
        self.assertEqual([i for i, _ in tail], list(range(12, 20)))
        self.assertTrue(all(s <= floor for _, s in tail))
        self.assertEqual([s for _, s in tail], sorted((s for _, s in tail), reverse=True))

    def test_deterministic_ties_and_input_order_independence(self):
        a = [(2, 0.5), (1, 0.5), (0, 0.5)]
        b = list(reversed(a))
        out_a = self.run_rerank(a, FakeScorer(), top_k=2)
        out_b = self.run_rerank(b, FakeScorer(), top_k=2)
        self.assertEqual(out_a, out_b)
        self.assertEqual([i for i, _ in out_a[:2]], [0, 1])

    def test_skipped_pairs_do_not_use_slots(self):
        pairs = [(0, 0.9), (1, 0.8), (2, 0.7)]
        scorer = FakeScorer()
        out = self.run_rerank(pairs, scorer, top_k=2, skip=lambda i: i == 0)
        self.assertEqual(scorer.calls[0][1], ["doc1", "doc2"])
        self.assertEqual(sorted(i for i, _ in out), [0, 1, 2])
        self.assertEqual(out[2][0], 0)

    def test_empty_and_all_skipped_never_call_the_scorer(self):
        scorer = FakeScorer()
        self.assertEqual(self.run_rerank([], scorer), [])
        out = self.run_rerank([(0, 0.4)], scorer, skip=lambda i: True)
        self.assertEqual(out, [(0, 0.4)])
        self.assertEqual(scorer.calls, [])

    def test_bad_scores_are_rejected(self):
        for bad in ([0.5], [float("nan"), 0.5], [1.5, 0.5], [True, 0.5]):
            with self.assertRaises(ValueError):
                self.run_rerank([(0, 0.9), (1, 0.8)], FakeScorer(bad=bad))
        with self.assertRaises(ValueError):
            self.run_rerank([(0, 0.9)], FakeScorer(), top_k=0)


if __name__ == "__main__":
    unittest.main()
