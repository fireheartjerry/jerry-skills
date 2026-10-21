import copy
import json
import unittest

import helpers
from mr import schema as _schema
from mr.engine import load_schema


class SchemaValidatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = load_schema("mined")
        cls.good = json.loads((helpers.FIX / "mined" / "chunk-00.json").read_text(encoding="utf-8"))

    def test_fixtures_valid(self):
        for name in ("chunk-00.json", "chunk-01.json"):
            data = json.loads((helpers.FIX / "mined" / name).read_text(encoding="utf-8"))
            self.assertEqual(_schema.validate(data, self.schema), [])

    def test_missing_required_key(self):
        bad = copy.deepcopy(self.good)
        del bad["traits"]
        errs = _schema.validate(bad, self.schema)
        self.assertTrue(any("missing required key 'traits'" in e and e.startswith("$:") for e in errs), errs)

    def test_wrong_enum(self):
        bad = copy.deepcopy(self.good)
        bad["traits"][0]["layer"] = "vibes"
        errs = _schema.validate(bad, self.schema)
        self.assertTrue(any(e.startswith("$.traits[0].layer") and "enum" in e for e in errs), errs)

    def test_over_long_string(self):
        bad = copy.deepcopy(self.good)
        bad["traits"][0]["claim"] = "x" * 201
        errs = _schema.validate(bad, self.schema)
        self.assertTrue(any("$.traits[0].claim" in e and "longer than 200" in e for e in errs), errs)

    def test_extra_property(self):
        bad = copy.deepcopy(self.good)
        bad["traits"][0]["extra"] = 1
        errs = _schema.validate(bad, self.schema)
        self.assertTrue(any("$.traits[0]" in e and "unexpected property 'extra'" in e for e in errs), errs)

    def test_type_list_and_nullable(self):
        bad = copy.deepcopy(self.good)
        bad["traits"][0]["evidence"][0]["time"] = 5
        errs = _schema.validate(bad, self.schema)
        self.assertTrue(any("$.traits[0].evidence[0].time" in e and "string|null" in e for e in errs), errs)
        self.assertEqual(_schema.validate({"a": None}, {"type": "object", "properties": {"a": {"type": ["string", "null"]}}}), [])

    def test_check_raises_and_summary(self):
        with self.assertRaises(_schema.SchemaError):
            _schema.check({"traits": []}, self.schema)
        lines = _schema.summary(self.schema)
        self.assertTrue(any(l.startswith("traits:") for l in lines))
        self.assertTrue(any("layer" in l and "one of" in l for l in lines))

    def test_other_schemas_load_and_validate_fixtures(self):
        req = json.loads((helpers.FAKE / "eval-requirements.json").read_text())["default"]
        self.assertEqual(_schema.validate(req, load_schema("requirements")), [])
        score = json.loads((helpers.FAKE / "eval-score.json").read_text())["default"]
        self.assertEqual(_schema.validate(score, load_schema("score")), [])
        self.assertIsInstance(load_schema("index"), dict)


if __name__ == "__main__":
    unittest.main()
