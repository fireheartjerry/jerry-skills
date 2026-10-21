"""references/techniques.json and `dev.py catalog`: schema, agreement with the cards, search.

The catalog's titles must equal the cards' `## ` headings file by file, and every `facts`
string must appear verbatim in its own card, so the numbers `dev.py catalog` prints are the
numbers the cards state. Standard library only; no network.
"""
import json
import os
import subprocess
import sys
import unittest

from test_sketches import CARD_FILES, REFS, card_sections

HERE = os.path.dirname(os.path.abspath(__file__))
DEV = os.path.join(os.path.dirname(HERE), "scripts", "dev.py")
REQUIRED = ("id", "title", "file", "class", "tier", "symptom", "aliases", "precondition",
            "reject_when", "complexity", "oracle", "facts")


def run_dev(*args):
    return subprocess.run([sys.executable, "-I", DEV] + list(args), stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True)


def catalog():
    with open(os.path.join(REFS, "techniques.json"), encoding="utf-8") as f:
        return json.load(f)


class CatalogDataTests(unittest.TestCase):
    def test_required_keys_and_types(self):
        for e in catalog():
            for k in REQUIRED:
                self.assertIn(k, e, "%s lacks %s" % (e.get("id"), k))
            for k in ("aliases", "reject_when", "facts"):
                self.assertIsInstance(e[k], list, "%s.%s" % (e["id"], k))
            self.assertIn(e["tier"], ("free", "cheap", "expensive"), e["id"])
            self.assertTrue(e["reject_when"], "%s has no reject_when" % e["id"])
            self.assertRegex(e["id"], r"^[a-z0-9]+(-[a-z0-9]+)*$")

    def test_ids_unique(self):
        ids = [e["id"] for e in catalog()]
        self.assertEqual(len(ids), len(set(ids)))

    def test_titles_equal_headings_per_file(self):
        entries = catalog()
        for fname in CARD_FILES:
            headings = sorted(h for h, _ in card_sections(fname))
            titles = sorted(e["title"] for e in entries if e["file"] == fname)
            self.assertEqual(titles, headings, fname)
        self.assertEqual(sorted(set(e["file"] for e in entries)), CARD_FILES)

    def test_facts_appear_verbatim_in_their_card(self):
        for e in catalog():
            text = dict(card_sections(e["file"]))[e["title"]]
            for fact in e["facts"]:
                self.assertIn(fact, text, "%s: fact %r is not in its card" % (e["id"], fact))

    def test_required_facts_present(self):
        facts = dict((e["id"], e["facts"]) for e in catalog())
        self.assertIn("(4/3 - 1/(3m))", facts["lpt-workers"])
        self.assertIn("m >= 2", facts["lpt-workers"])
        self.assertIn("18", facts["coverage-time-budget"])
        self.assertIn("(1 - 1/e)/2", facts["coverage-time-budget"])
        self.assertIn("k >= 50", facts["aho-corasick"])
        self.assertIn("V <= about 20,000", facts["bitset-closure"])
        self.assertIn("n <= about 3,000", facts["blocking-near-duplicates"])
        self.assertIn("about 200 chunks", facts["bm25-chunks"])
        self.assertIn("under about 5 minutes", facts["impact-test-selection"])
        self.assertIn("11/9 OPT + 6/9", facts["fewest-workers-deadline"])
        self.assertIn("about 10 queries", facts["index-once"])
        self.assertIn("(nightly or on main)", facts["impact-test-selection"])
        self.assertIn("O(V+E) per query (BFS on the reversed graph)",
                      facts["reverse-graph-impact"])


class CatalogCliTests(unittest.TestCase):
    def search(self, text):
        p = run_dev("catalog", "--symptom", text, "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def test_nested_loop_duplicates_ranks_hash_dedup_first(self):
        data = self.search("nested loop duplicates")
        self.assertEqual(data["matches"][0]["id"], "hash-key-dedup")

    def test_ci_runners_ranks_lpt_first_with_its_bound(self):
        data = self.search("CI 40 minutes runners")
        self.assertEqual(data["matches"][0]["id"], "lpt-workers")
        self.assertIn("(4/3 - 1/(3m))", data["matches"][0]["facts"])
        p = run_dev("catalog", "--symptom", "CI 40 minutes runners")
        first = p.stdout.split("\n\n")[0]
        self.assertTrue(first.startswith("LPT across workers"), first)
        self.assertIn("facts to quote: (4/3 - 1/(3m)); m >= 2", first)
        self.assertIn("card says: Precondition test: m >= 2;", first)
        self.assertIn("cards-scheduling.md", first)

    def test_every_fact_comes_with_the_card_sentence_that_states_it(self):
        import sys as _sys
        scripts = os.path.join(os.path.dirname(HERE), "scripts")
        if scripts not in _sys.path:
            _sys.path.insert(0, scripts)
        from devkit.catalog import fact_sentences
        for e in catalog():
            for fact, sentence in zip(e["facts"], fact_sentences(e)):
                self.assertIn(fact, sentence, e["id"])
                self.assertNotEqual(fact, sentence, "%s: no prose sentence holds %r"
                                    % (e["id"], fact))
        data = self.search("exact best set of optional suites in a time budget")
        m = [x for x in data["matches"] if x["id"] == "coverage-time-budget"][0]
        self.assertIn("n <= 18 optional candidates", m["fact_sentences"][0])

    def test_more_symptoms_route_to_the_expected_card(self):
        for text, want in (("40 patterns over 2000 log lines", "aho-corasick"),
                           ("which tests must run for this PR", "impact-test-selection"),
                           ("context packer over the token budget", "pack-token-budget"),
                           ("what depends on this service, blast radius", "reverse-graph-impact"),
                           ("this is O(n^2)", "hash-key-dedup")):
            self.assertEqual(self.search(text)["matches"][0]["id"], want, text)

    def test_no_match_exits_2_and_points_at_list(self):
        p = run_dev("catalog", "--symptom", "zzzz")
        self.assertEqual(p.returncode, 2)
        self.assertIn("no card matches; try --list", p.stderr)
        self.assertEqual(p.stdout, "")
        self.assertNotIn("Traceback", p.stderr)

    def test_list_prints_every_id(self):
        p = run_dev("catalog", "--list")
        self.assertEqual(p.returncode, 0, p.stderr)
        for e in catalog():
            self.assertIn(e["id"], p.stdout)
        self.assertEqual(len(p.stdout.strip().splitlines()), len(catalog()))

    def test_json_schema(self):
        data = self.search("dedup")
        self.assertEqual(data["schema"], "devkit-catalog/1")
        self.assertEqual(data["query"]["symptom"], "dedup")
        for m in data["matches"]:
            for k in REQUIRED + ("score",):
                self.assertIn(k, m)
        scores = [m["score"] for m in data["matches"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        p = run_dev("catalog", "--list", "--json")
        self.assertEqual(json.loads(p.stdout)["schema"], "devkit-catalog/1")

    def test_id_and_class_filters(self):
        p = run_dev("catalog", "--id", "lpt-workers", "--json")
        self.assertEqual([m["id"] for m in json.loads(p.stdout)["matches"]], ["lpt-workers"])
        p = run_dev("catalog", "--class", "scheduling", "--json")
        ids = [m["id"] for m in json.loads(p.stdout)["matches"]]
        self.assertEqual(len(ids), len([e for e in catalog() if e["class"] == "scheduling"]))
        self.assertEqual(run_dev("catalog", "--id", "no-such-card").returncode, 2)
        p = run_dev("catalog")
        self.assertEqual(p.returncode, 2)
        self.assertIn("--list", p.stderr)


if __name__ == "__main__":
    unittest.main()
