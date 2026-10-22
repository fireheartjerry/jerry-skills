import json
import re
import unittest
from pathlib import Path

import helpers
from mr import prefer
from mr.config import read_json, write_json

DATA_RE = re.compile(r'<script type="application/json" id="data">(.*?)</script>', re.S)


def page_data(html):
    return json.loads(DATA_RE.search(html).group(1))


def seeds_dir(root, n=6, extra=None):
    d = Path(root) / "seeds"
    d.mkdir(parents=True)
    for k in range(n):
        write_json(d / ("d%02d.json" % k), {"id": "d%02d" % k, "seed": "tidy the pricing page, round %d" % k,
                                             "context": ["earlier one", "earlier two", "earlier three", "earlier four"],
                                             "time": "2026-03-0%dT10:00:00Z" % (k + 1)})
    for name, text in (extra or {}).items():
        (d / name).write_text(text, encoding="utf-8")
    return d


def expansion_dir(root, name, ids, word):
    d = Path(root) / name
    d.mkdir(parents=True)
    for i in ids:
        (d / (i + ".md")).write_text("Reading: %s reply for %s, with enough words to read." % (word, i), encoding="utf-8")
    return d


class ArmAndSeedTests(unittest.TestCase):
    def test_parse_arm(self):
        tmp = helpers.tmpdir()
        paths, _ = helpers.init_home(tmp / "home")
        self.assertEqual(prefer.parse_arm("none")["kind"], "none")
        self.assertEqual(prefer.parse_arm(str(paths.home))["kind"], "home")
        arm = prefer.parse_arm("%s+%s" % (helpers.SKILL, paths.home))
        self.assertEqual((arm["kind"], arm["skill"]), ("skill_home", str(helpers.SKILL.resolve())))
        (tmp / "exp").mkdir()
        self.assertEqual(prefer.parse_arm("dir:%s" % (tmp / "exp"))["kind"], "dir")
        for bad in ("", "dir:%s" % (tmp / "missing"), str(tmp / "nothing-here")):
            with self.assertRaises(prefer.PreferError):
                prefer.parse_arm(bad)

    def test_load_seeds(self):
        tmp = helpers.tmpdir()
        d = seeds_dir(tmp, 2, extra={"plain.txt": "rename the config loader\n", "note.md": "  ", "bad.json": "[1, 2]"})
        seeds = prefer.load_seeds(d)
        self.assertEqual([s["id"] for s in seeds], ["d00", "d01", "plain"])
        self.assertEqual(seeds[2]["seed"], "rename the config loader")
        self.assertEqual(len(prefer.load_seeds(d, 1)), 1)
        write_json(d / "dup.json", {"id": "d00", "seed": "again"})
        with self.assertRaises(prefer.PreferError):
            prefer.load_seeds(d)


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = helpers.tmpdir()
        self.paths, self.cfg = helpers.init_home(self.tmp / "home")
        hostile = "ship it </script><script>alert(1)</script> <!-- now"
        self.seeds = seeds_dir(self.tmp, 6, extra={"d99.json": json.dumps({"id": "d99", "seed": hostile})})
        ids = ["d%02d" % k for k in range(6)] + ["d99"]
        self.a = expansion_dir(self.tmp, "arm-alpha", ids, "first")
        self.b = expansion_dir(self.tmp, "arm-beta", ids[1:], "second")  # d00 has no B expansion

    def build(self, out, rng_seed=7):
        return prefer.run_build(self.paths, self.cfg, "dir:%s" % self.a, "dir:%s" % self.b, self.seeds, out=out,
                                rng_seed=rng_seed, log=helpers.quiet)

    def test_page_is_self_contained_blind_and_the_key_is_separate(self):
        res = self.build(self.tmp / "out1")
        html = Path(res["page"]).read_text(encoding="utf-8")
        self.assertEqual(res["missing"], ["d00"])
        self.assertEqual(res["pairs"], 6)
        self.assertNotRegex(html, r"https?://")
        self.assertNotIn("<script src", html)
        self.assertNotIn("<link", html)
        self.assertIn("default-src 'none'", html)
        for leak in ("arm-alpha", "arm-beta", '"id": "d', "seed_id"):
            self.assertNotIn(leak, html)
        data = page_data(html)
        self.assertEqual(set(data), {"build_id", "pairs"})
        self.assertTrue(all(set(p) == {"id", "context", "seed", "A", "B"} for p in data["pairs"]))
        self.assertEqual(data["build_id"], res["build_id"])
        self.assertIn("ship it </script><script>alert(1)</script> <!-- now", [p["seed"] for p in data["pairs"]])
        self.assertEqual(html.count("</script>"), 2)  # the data script and the app script: the seed closed nothing
        self.assertEqual(html.count("<!--"), 0)
        self.assertTrue(all(len(p["context"]) == 3 for p in data["pairs"] if p["context"]))
        key = read_json(res["key"])
        self.assertEqual(set(key["pairs"]), {p["id"] for p in data["pairs"]})
        self.assertEqual({v["A"] for v in key["pairs"].values()}, {"a", "b"})  # sides vary
        for p in data["pairs"]:
            k = key["pairs"][p["id"]]
            self.assertIn({"a": "first", "b": "second"}[k["A"]], p["A"])
            self.assertIn({"a": "first", "b": "second"}[k["B"]], p["B"])

    def test_randomization_is_reproducible_from_the_seed(self):
        k1 = read_json(self.build(self.tmp / "o1", 7)["key"])["pairs"]
        k2 = read_json(self.build(self.tmp / "o2", 7)["key"])["pairs"]
        k3 = read_json(self.build(self.tmp / "o3", 8)["key"])["pairs"]
        self.assertEqual(k1, k2)
        self.assertNotEqual(k1, k3)

    def test_generated_arms_with_cache_cap_and_contamination(self):
        home_paths, home_cfg = helpers.build_home(through="synthesize")
        train = [m["text"] for r in helpers.read_jsonl(home_paths.sessions) if r["split"] == "train" for m in r["messages"]
                 if len(m["text"]) > 30]
        seeds = seeds_dir(self.tmp / "gen", 3, extra={"seen.json": json.dumps({"id": "seen", "seed": train[0]})})
        fake = helpers.tmpdir()
        write_json(fake / "prefer.json", {"default": {"reply": "Reading: a generated reply that is long enough to pass."}})
        res = prefer.run_build(self.paths, self.cfg, str(home_paths.home), "none", seeds, out=self.tmp / "gen-out", rng_seed=3,
                               kind="fake", fake_dir=fake, max_calls=6, log=helpers.quiet)
        self.assertEqual(res["pairs"], 2)  # 4 seeds x 2 arms = 8 calls; the cap allows 6: arm a takes 4, arm b gets 2
        self.assertEqual(res["contaminated"], {"a": ["seen"], "b": []})
        key = read_json(res["key"])
        self.assertEqual(key["arms"]["a"]["model_hash"], read_json(home_paths.model_json)["model_hash"])
        self.assertTrue(key["arms"]["a"]["skill_version"])
        cached = sorted(p.name for p in (self.tmp / "gen-out" / "expansions" / "a").glob("*.json"))
        self.assertEqual(len(cached), 4)
        self.assertEqual(len(list((self.tmp / "gen-out" / "expansions" / "b").glob("*.json"))), 2)
        res2 = prefer.run_build(self.paths, self.cfg, str(home_paths.home), "none", seeds, out=self.tmp / "gen-out", rng_seed=3,
                                kind="fake", fake_dir=fake, max_calls=6, log=helpers.quiet)
        self.assertEqual(res2["pairs"], 4)
        self.assertEqual((res2["spend"]["a"]["calls"], res2["spend"]["b"]["calls"]), (0, 2))  # cached: never spent twice


class ScoreTests(unittest.TestCase):
    def key(self):
        return {"build_id": "b1", "arms": {"a": {"label": "new"}, "b": {"label": "old"}},
                "pairs": {"p01": {"A": "a", "B": "b"}, "p02": {"A": "b", "B": "a"}, "p03": {"A": "a", "B": "b"},
                          "p04": {"A": "b", "B": "a"}, "p05": {"A": "a", "B": "b"}}}

    def test_unblinding_and_sign_test(self):
        answers = {"p01": {"pick": "A", "wrong": ["B"]}, "p02": {"pick": "B"}, "p03": {"pick": "both"},
                   "p04": {"pick": "A", "wrong": ["A"]}, "zz": {"pick": "A"}}
        res = prefer.score(self.key(), answers)
        self.assertEqual(res["wins"], {"a": 2, "b": 1})
        self.assertEqual((res["both"], res["neither"], res["answered"], res["unanswered"], res["unknown"]), (1, 0, 4, 1, 1))
        self.assertEqual(res["wrong"], {"a": 0, "b": 2})
        self.assertEqual(res["p_sign"], 1.0)
        self.assertEqual(res["share"]["a"]["k"], 2)
        self.assertEqual(res["at_least_as_good"]["a"]["k"], 3)

    def test_run_score_records_the_primary_metric_once_per_build(self):
        from mr.evaluate import ledger_has
        tmp = helpers.tmpdir()
        paths, _ = helpers.init_home(tmp / "home")
        write_json(tmp / "key.json", dict(self.key(), arms={"a": {"label": "new", "model_hash": "aaaa"}, "b": {"label": "old"}}))
        write_json(tmp / "answers.json", {"build_id": "b1", "answers": {p: {"pick": "A" if p != "p02" else "B"} for p in self.key()["pairs"]}})
        res = prefer.run_score(paths, tmp / "answers.json", log=helpers.quiet)
        self.assertEqual(res["wins"], {"a": 4, "b": 1})
        prefer.run_score(paths, tmp / "answers.json", log=helpers.quiet)
        ledger = read_json(paths.ledger)
        self.assertEqual(len(ledger), 1)
        self.assertEqual((ledger[0]["kind"], ledger[0]["primary"], ledger[0]["arms"]["a"]["model_hash"]), ("prefer", True, "aaaa"))
        self.assertFalse(ledger_has(paths, "aaaa", None))  # a preference score is never an eval look
        self.assertIn("human preference (primary, ", prefer.format_status_line(prefer.latest_prefer(ledger)))
        st = helpers.run_cli(paths.home, "status")
        self.assertIn("new 4, old 1, both 0, neither 0 of 5", st.stdout)
        write_json(tmp / "other.json", {"build_id": "zzz", "answers": {}})
        with self.assertRaises(prefer.PreferError):
            prefer.run_score(paths, tmp / "other.json", key_path=tmp / "key.json", log=helpers.quiet)

    def test_reads_the_earlier_one_off_format(self):
        tmp = helpers.tmpdir()
        write_json(tmp / "k.json", {"x1": {"A": "new", "B": "old"}, "x2": {"A": "old", "B": "new"}})
        write_json(tmp / "a.json", {"x1": {"pick": "B", "wrong": [], "note": ""}, "x2": {"pick": "both", "wrong": [], "note": ""}})
        res = prefer.run_score(None, tmp / "a.json", key_path=tmp / "k.json", ledger=False, log=helpers.quiet)
        self.assertEqual((res["wins"], res["both"]), ({"new": 0, "old": 1}, 1))


class PreferCliTests(unittest.TestCase):
    def test_build_then_score(self):
        tmp = helpers.tmpdir()
        paths, _ = helpers.init_home(tmp / "home")
        seeds = seeds_dir(tmp, 3)
        ids = ["d00", "d01", "d02"]
        a, b = expansion_dir(tmp, "x", ids, "one"), expansion_dir(tmp, "y", ids, "two")
        r = helpers.run_cli(paths.home, "--json", "prefer", "build", "--a", "dir:%s" % a, "--b", "dir:%s" % b, "--seeds", seeds,
                            "--out", tmp / "out", "--rng-seed", "1")
        self.assertEqual(r.returncode, 0, r.stderr)
        built = json.loads(r.stdout)
        data = page_data(Path(built["page"]).read_text(encoding="utf-8"))
        write_json(tmp / "out" / "answers.json", {"build_id": data["build_id"], "answers": {p["id"]: {"pick": "A"} for p in data["pairs"]}})
        s = helpers.run_cli(paths.home, "--json", "prefer", "score", tmp / "out" / "answers.json")
        self.assertEqual(s.returncode, 0, s.stderr)
        res = json.loads(s.stdout)
        self.assertEqual(res["answered"], 3)
        self.assertEqual(sum(res["wins"].values()), 3)


if __name__ == "__main__":
    unittest.main()
