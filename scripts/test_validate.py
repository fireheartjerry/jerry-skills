#!/usr/bin/env python3
"""Unit tests for scripts/validate.py, run on synthetic repositories in temp dirs.

Run: python3 -m unittest discover -s scripts -p 'test_*.py' -v
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import validate  # noqa: E402


def j(*parts):
    """Join parts. Secret- and PII-shaped test values are assembled at runtime so
    this file never contains a literal the validator itself would flag."""
    return "".join(parts)


FAKE_ANTHROPIC = j("sk-ant-", "api03-", "A" * 40)
FAKE_OPENAI = j("sk-", "B" * 32)
FAKE_GHP = j("ghp_", "C" * 36)
FAKE_AKIA = j("AKIA", "D" * 16)
FAKE_SLACK = j("xoxb-", "123456789012-", "E" * 24)
FAKE_GOOGLE = j("AIza", "F" * 35)
FAKE_PEM = j("-----BEGIN ", "RSA PRIVATE KEY-----")
FAKE_EMAIL = j("jane.doe@", "gmail.com")
FAKE_HOME_MAC = j("/Users/", "janedoe", "/Code/project")
FAKE_HOME_LINUX = j("/home/", "janedoe", "/project")
FAKE_NAME = j("Zorblat", " Quenvix")

PLUGIN = {"name": "demo-skills", "version": "1.2.3", "description": "demo", "skills": "./skills/"}
MARKET = {
    "name": "demo-skills",
    "metadata": {"version": "1.2.3"},
    "plugins": [{"name": "demo-skills", "source": "./", "version": "1.2.3"}],
}

GOOD_DESCRIPTION = "Does a good thing with files. Use when the user asks for the good thing."
# skill_md() emits 5 lines before the body: ---, name, description, ---, blank.
BODY_OFFSET = 5


def write(path, content, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if isinstance(content, bytes):
        with open(path, "wb") as fh:
            fh.write(content)
    else:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
    if mode is not None:
        os.chmod(path, mode)


def skill_md(name="good-skill", description=GOOD_DESCRIPTION, body=None, extra_front="", quote=True):
    desc = json.dumps(description) if quote else description
    if body is None:
        body = (
            "# Good skill\n\nRead [the guide](references/guide.md) first, then run `scripts/run.sh`.\n"
            "The `scripts/` directory holds helpers.\n"
        )
    return "---\nname: %s\ndescription: %s\n%s---\n\n%s" % (name, desc, extra_front, body)


class FixtureCase(unittest.TestCase):
    """A minimal valid repository: one skill, one reference, one script, manifests."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="validate-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "repo")
        self.skill = os.path.join(self.root, "skills", "good-skill")
        write(os.path.join(self.root, ".claude-plugin", "plugin.json"), json.dumps(PLUGIN))
        write(os.path.join(self.root, ".claude-plugin", "marketplace.json"), json.dumps(MARKET))
        write(os.path.join(self.root, "README.md"), "# demo\n\nContact: t@example.com\n")
        write(os.path.join(self.skill, "SKILL.md"), skill_md())
        write(os.path.join(self.skill, "references", "guide.md"), "# Guide\n\nSee `../scripts/run.sh` and `scripts/run.sh`.\n")
        write(os.path.join(self.skill, "scripts", "run.sh"), "#!/usr/bin/env bash\necho ok\n", 0o755)

    def run_validate(self, **kw):
        kw.setdefault("use_git", False)
        return validate.validate_repo(self.root, **kw)

    def codes(self, report, level=None):
        return sorted(f.code for f in report.findings if level is None or f.level == level)

    def find(self, report, code):
        return [f for f in report.findings if f.code == code]

    def skill_path(self, *parts):
        return os.path.join(self.skill, *parts)


class CleanRepoTests(FixtureCase):
    def test_clean_fixture_has_no_findings(self):
        report = self.run_validate()
        self.assertEqual(report.findings, [], [f.format() for f in report.findings])
        self.assertEqual(report.skills, ["good-skill"])
        self.assertEqual(report.file_source, "walk")
        self.assertGreaterEqual(report.files_scanned, 5)

    def test_non_directories_and_hidden_entries_in_skills_are_ignored(self):
        write(os.path.join(self.root, "skills", "README.md"), "# skills\n")
        write(os.path.join(self.root, "skills", ".hidden", "SKILL.md"), "garbage")
        report = self.run_validate()
        self.assertEqual(report.skills, ["good-skill"])
        self.assertEqual(self.codes(report), [])


class FrontmatterTests(FixtureCase):
    def test_missing_frontmatter(self):
        write(self.skill_path("SKILL.md"), "# No frontmatter\n")
        self.assertIn("frontmatter-missing", self.codes(self.run_validate(), "ERROR"))

    def test_unterminated_frontmatter(self):
        write(self.skill_path("SKILL.md"), "---\nname: good-skill\ndescription: x\n# body\n")
        self.assertIn("frontmatter-unterminated", self.codes(self.run_validate(), "ERROR"))

    def test_invalid_frontmatter_line(self):
        write(self.skill_path("SKILL.md"), "---\nname: good-skill\nthis is not yaml\n---\n")
        report = self.run_validate()
        self.assertIn("frontmatter-invalid", self.codes(report, "ERROR"))
        self.assertIn("line 2", self.find(report, "frontmatter-invalid")[0].message)

    def test_name_mismatch(self):
        write(self.skill_path("SKILL.md"), skill_md(name="other-name"))
        report = self.run_validate()
        self.assertIn("name-mismatch", self.codes(report, "ERROR"))
        self.assertNotIn("name-format", self.codes(report))

    def test_name_format(self):
        bad = os.path.join(self.root, "skills", "Bad_Name")
        write(os.path.join(bad, "SKILL.md"), skill_md(name="Bad_Name", body="# x\n"))
        report = self.run_validate()
        self.assertIn("name-format", self.codes(report, "ERROR"))
        self.assertNotIn("name-mismatch", self.codes(report))

    def test_name_missing(self):
        write(self.skill_path("SKILL.md"), "---\ndescription: x\n---\n# x\n")
        self.assertIn("name-missing", self.codes(self.run_validate(), "ERROR"))

    def test_description_missing_and_empty(self):
        write(self.skill_path("SKILL.md"), "---\nname: good-skill\n---\n# x\n")
        self.assertIn("description-missing", self.codes(self.run_validate(), "ERROR"))
        write(self.skill_path("SKILL.md"), "---\nname: good-skill\ndescription: \"\"\n---\n# x\n")
        self.assertIn("description-missing", self.codes(self.run_validate(), "ERROR"))

    def test_description_length_thresholds(self):
        write(self.skill_path("SKILL.md"), skill_md(description="x" * 600, body="# x\n"))
        self.assertEqual(self.codes(self.run_validate()), [])
        write(self.skill_path("SKILL.md"), skill_md(description="x" * 601, body="# x\n"))
        report = self.run_validate()
        self.assertEqual(self.codes(report), ["description-long"])
        self.assertEqual(report.findings[0].level, "WARN")
        write(self.skill_path("SKILL.md"), skill_md(description="x" * 1024, body="# x\n"))
        self.assertEqual(self.codes(self.run_validate()), ["description-long"])
        write(self.skill_path("SKILL.md"), skill_md(description="x" * 1025, body="# x\n"))
        report = self.run_validate()
        self.assertEqual(self.codes(report), ["description-too-long"])
        self.assertEqual(report.findings[0].level, "ERROR")
        self.assertIn("1025", report.findings[0].message)

    def test_folded_description_is_measured_after_folding(self):
        folded = ">\n" + "".join("  %s\n" % ("w" * 70) for _ in range(10))
        write(self.skill_path("SKILL.md"), "---\nname: good-skill\ndescription: %s---\n# x\n" % folded)
        report = self.run_validate()
        self.assertEqual(self.codes(report), ["description-long"])
        self.assertIn("709 characters", report.findings[0].message)

    def test_unknown_key_warns_and_metadata_is_known(self):
        extra = "metadata:\n  author: someone\n  version: 1.0.0\nmystery: 1\n"
        write(self.skill_path("SKILL.md"), skill_md(extra_front=extra))
        report = self.run_validate()
        self.assertEqual(self.codes(report), ["unknown-key"])
        self.assertEqual(report.findings[0].level, "WARN")
        self.assertIn("mystery", report.findings[0].message)

    def test_skill_md_missing_and_wrong_case(self):
        os.remove(self.skill_path("SKILL.md"))
        self.assertIn("skill-md-missing", self.codes(self.run_validate(), "ERROR"))
        write(self.skill_path("skill.md"), skill_md())
        if os.path.exists(self.skill_path("SKILL.md")):
            self.skipTest("case-insensitive filesystem")
        self.assertIn("skill-md-case", self.codes(self.run_validate(), "ERROR"))

    def test_long_skill_md_warns(self):
        body = "# x\n" + "line\n" * 500
        write(self.skill_path("SKILL.md"), skill_md(body=body))
        report = self.run_validate()
        self.assertEqual(self.codes(report), ["skill-too-long"])
        self.assertEqual(report.findings[0].level, "WARN")


class YamlParserTests(unittest.TestCase):
    def parse(self, text):
        return validate.parse_frontmatter(text)

    def test_quoted_scalars(self):
        fm = self.parse('---\nname: a\ndescription: "He said \\"hi\\": ok"\nlicense: \'it\'\'s\'\n---\n')
        self.assertEqual(fm["description"], 'He said "hi": ok')
        self.assertEqual(fm["license"], "it's")

    def test_folded_and_literal_blocks(self):
        text = "---\nname: a\ndescription: >\n  one\n  two\n\n  three\nnotes: |\n  l1\n  l2\n---\n"
        fm = self.parse(text)
        self.assertEqual(fm["description"], "one two\nthree")
        self.assertEqual(fm["notes"], "l1\nl2")

    def test_folded_with_chomping_indicator(self):
        fm = self.parse("---\ndescription: >-\n  a\n  b\n---\n")
        self.assertEqual(fm["description"], "a b")

    def test_nested_mapping_and_list(self):
        text = "---\nname: a\nmetadata:\n  author: me\n  version: 1.0.0\nallowed-tools:\n  - Read\n  - Bash\n---\n"
        fm = self.parse(text)
        self.assertEqual(fm["metadata"], {"author": "me", "version": "1.0.0"})
        self.assertEqual(fm["allowed-tools"], ["Read", "Bash"])

    def test_multiline_plain_scalar(self):
        fm = self.parse("---\ndescription: first part\n  second part\n  third\n---\n")
        self.assertEqual(fm["description"], "first part second part third")

    def test_plain_scalar_trailing_comment_and_blank_lines(self):
        fm = self.parse("---\n\nname: a # the name\n\ndescription: b\n---\n")
        self.assertEqual(fm, {"name": "a", "description": "b"})

    def test_crlf_and_bom(self):
        fm = self.parse("﻿---\r\nname: a\r\ndescription: b\r\n---\r\nbody")
        self.assertEqual(fm, {"name": "a", "description": "b"})

    def test_errors(self):
        with self.assertRaises(validate.FrontmatterError):
            self.parse("no frontmatter")
        with self.assertRaises(validate.FrontmatterError):
            self.parse("---\nname: a\n")
        with self.assertRaises(validate.FrontmatterError):
            self.parse("---\nname: a\n  stray: indent\n---\n")


class ReferenceTests(FixtureCase):
    def test_missing_link_target_is_error_with_line(self):
        body = "# x\n\nintro\n\nSee [missing](references/nope.md).\n"
        write(self.skill_path("SKILL.md"), skill_md(body=body))
        report = self.run_validate()
        found = self.find(report, "missing-path")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].level, "ERROR")
        self.assertEqual(found[0].line, BODY_OFFSET + 5)
        self.assertIn("references/nope.md", found[0].message)
        self.assertEqual(found[0].path, "skills/good-skill/SKILL.md")

    def test_missing_inline_code_path_is_error(self):
        write(self.skill_path("SKILL.md"), skill_md(body="# x\n\nRun `python3 scripts/none.py --flag`.\n"))
        found = self.find(self.run_validate(), "missing-path")
        self.assertEqual([(f.level, "scripts/none.py" in f.message) for f in found], [("ERROR", True)])

    def test_inline_code_path_with_line_suffix(self):
        write(self.skill_path("SKILL.md"), skill_md(body="# x\n\nSee `scripts/run.sh:12` and `scripts/none.sh:3`.\n"))
        found = self.find(self.run_validate(), "missing-path")
        self.assertEqual(len(found), 1)
        self.assertIn("scripts/none.sh", found[0].message)

    def test_prose_and_fenced_paths_are_warnings(self):
        body = "# x\n\nThen follow references/later.md for details.\n\n```bash\npython3 scripts/ghost.py\n```\n"
        write(self.skill_path("SKILL.md"), skill_md(body=body))
        found = self.find(self.run_validate(), "missing-path")
        self.assertEqual(sorted((f.level, f.line) for f in found), [("WARN", BODY_OFFSET + 3), ("WARN", BODY_OFFSET + 6)])

    def test_prose_path_inside_frontmatter_description(self):
        write(self.skill_path("SKILL.md"), skill_md(description="Follow references/build.md when asked.", body="# x\n"))
        found = self.find(self.run_validate(), "missing-path")
        self.assertEqual([(f.level, f.line) for f in found], [("WARN", 3)])

    def test_existing_paths_in_all_forms_pass(self):
        body = ("# x\n\n[a](references/guide.md) [b](./references/guide.md#section) `scripts/run.sh` "
                "`./scripts/run.sh` `scripts/` `references/` references/guide.md\n\n```\nbash scripts/run.sh\n```\n")
        write(self.skill_path("SKILL.md"), skill_md(body=body))
        self.assertEqual(self.find(self.run_validate(), "missing-path"), [])

    def test_directory_token_requires_directory(self):
        write(self.skill_path("SKILL.md"), skill_md(body="# x\n\n`assets/` and `scripts/run.sh/`\n"))
        found = self.find(self.run_validate(), "missing-path")
        self.assertEqual(sorted(f.message.split(": ")[-1] for f in found), ["assets/", "scripts/run.sh/"])

    def test_placeholders_urls_and_anchors_are_ignored(self):
        body = ("# x\n\n`references/<topic>.md` `scripts/*.py` `$SKILL/scripts/x.py` `~/.claude/skills/x` "
                "[doc](https://example.com/references/x.md) [here](#section) [ph](references/${name}.md) "
                "[abs](/etc/hosts) `python3 <skill-dir>/scripts/bench.py` `skills/<name>/SKILL.md`\n")
        write(self.skill_path("SKILL.md"), skill_md(body=body))
        self.assertEqual(self.find(self.run_validate(), "missing-path"), [])

    def test_references_md_files_are_checked(self):
        write(self.skill_path("references", "guide.md"), "# g\n\n[x](../scripts/run.sh) `scripts/run.sh` [bad](references/none.md)\n")
        write(self.skill_path("references", "deep", "more.md"), "`../none.txt`\n")
        found = self.find(self.run_validate(), "missing-path")
        self.assertEqual(sorted((f.path, f.message.split(": ")[-1]) for f in found), [
            ("skills/good-skill/references/deep/more.md", "../none.txt"),
            ("skills/good-skill/references/guide.md", "references/none.md"),
        ])

    def test_duplicate_mentions_reported_once(self):
        write(self.skill_path("SKILL.md"), skill_md(body="# x\n\n`scripts/none.py`\n\n`scripts/none.py` again\n"))
        self.assertEqual(len(self.find(self.run_validate(), "missing-path")), 1)


class ContentScanTests(FixtureCase):
    def assert_secret(self, value, label):
        write(os.path.join(self.root, "notes.md"), "token: %s\n" % value)
        found = self.find(self.run_validate(), "secret")
        self.assertEqual(len(found), 1, "expected one finding for %s" % label)
        self.assertIn(label, found[0].message)
        self.assertNotIn(value, found[0].message)
        self.assertEqual(found[0].path, "notes.md")
        self.assertEqual(found[0].line, 1)

    def test_secret_shapes(self):
        self.assert_secret(FAKE_ANTHROPIC, "Anthropic")
        self.assert_secret(FAKE_OPENAI, "OpenAI")
        self.assert_secret(FAKE_GHP, "GitHub token")
        self.assert_secret(FAKE_AKIA, "AWS")
        self.assert_secret(FAKE_SLACK, "Slack")
        self.assert_secret(FAKE_GOOGLE, "Google")
        self.assert_secret(FAKE_PEM, "private key")

    def test_short_lived_opaque_and_oauth_credentials(self):
        self.assert_secret("AQ." + "x" * 40, "opaque short-lived")
        self.assert_secret("ya29." + "y" * 40, "Google OAuth")

    def test_pattern_sources_do_not_trip_the_scan(self):
        write(os.path.join(self.root, "scrub.py"),
              'P = [r"AKIA[0-9A-Z]{16}", r"sk-ant-[A-Za-z0-9_-]{16,}", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"]\n'
              "# words: task-management-system sk-ant-key xoxb-token ghp_ prefix\n")
        self.assertEqual(self.find(self.run_validate(), "secret"), [])

    def test_email_allowlist(self):
        write(os.path.join(self.root, "notes.md"), "mail %s and t@example.com and noreply@anthropic.com\n" % FAKE_EMAIL)
        found = self.find(self.run_validate(), "email")
        self.assertEqual(len(found), 1)
        self.assertNotIn(FAKE_EMAIL, found[0].message)
        self.assertIn("gmail.com", found[0].message)
        self.assertEqual(self.find(self.run_validate(email_allowlist=[FAKE_EMAIL]), "email"), [])
        self.assertEqual(self.find(self.run_validate(email_allowlist=["@" + FAKE_EMAIL.split("@")[1]]), "email"), [])

    def test_reserved_tlds_and_format_placeholders_are_not_emails(self):
        write(os.path.join(self.root, "notes.md"),
              "%s %s %s %s\n" % (j("sam@", "host.test"), j("sam@", "svc.example"), j("sam@", "a.b.invalid"), j("%s@", "host.test")))
        self.assertEqual(self.find(self.run_validate(), "email"), [])
        write(os.path.join(self.root, "notes.md"), j("sam@", "host.testing") + "\n")
        self.assertEqual(len(self.find(self.run_validate(), "email")), 1)

    def test_email_regex_ignores_package_specs(self):
        write(os.path.join(self.root, "notes.md"), "npm i react@18.3.1 @anthropic-ai/sdk@0.30.0 user@host\n")
        self.assertEqual(self.find(self.run_validate(), "email"), [])

    def test_home_paths(self):
        write(os.path.join(self.root, "notes.md"), "%s\n%s\n" % (FAKE_HOME_MAC, FAKE_HOME_LINUX))
        found = self.find(self.run_validate(), "home-path")
        self.assertEqual([f.line for f in found], [1, 2])
        for f in found:
            self.assertNotIn("janedoe", f.message)

    def test_home_allowlist_for_synthetic_persona(self):
        write(os.path.join(self.root, "fixtures", "t.jsonl"), "%s\n%s\n%s\n" % (FAKE_HOME_LINUX, FAKE_HOME_LINUX, FAKE_HOME_MAC))
        found = self.find(self.run_validate(), "home-path")
        self.assertEqual(len(found), 2)
        linux = [f for f in found if "/home/" in f.message][0]
        self.assertIn("[2 occurrences in this file, first shown]", linux.message)
        self.assertEqual(linux.line, 1)
        self.assertEqual(self.find(self.run_validate(home_allowlist=["JaneDoe"]), "home-path"), [])

    def test_home_path_placeholders_allowed(self):
        write(os.path.join(self.root, "notes.md"),
              "/Users/<name>/x /home/$USER/x /Users/you/x /home/runner/work /Users/${USER}/x ~/Code/x /Users/Shared/x\n")
        self.assertEqual(self.find(self.run_validate(), "home-path"), [])

    def test_denylist(self):
        write(os.path.join(self.root, "notes.md"), "Written by %s in 2026.\nlowercase %s too\n" % (FAKE_NAME, FAKE_NAME.lower()))
        self.assertEqual(self.find(self.run_validate(), "denylist"), [])
        deny = os.path.join(self.tmp, "deny.txt")
        write(deny, "# names\n\n%s\n" % FAKE_NAME)
        report = self.run_validate(denylist_path=deny)
        found = self.find(report, "denylist")
        # repeats within one file are aggregated into a single finding with a count
        self.assertEqual([(f.line, f.path) for f in found], [(1, "notes.md")])
        self.assertIn("[2 occurrences in this file, first shown]", found[0].message)
        self.assertNotIn(FAKE_NAME, found[0].message)
        self.assertEqual(report.denylist_entries, 1)

    def test_denylist_default_location_and_example_file_exempt(self):
        write(os.path.join(self.root, validate.DENYLIST_FILE), "%s\n" % FAKE_NAME)
        write(os.path.join(self.root, validate.DENYLIST_EXAMPLE), "%s\n" % FAKE_NAME)
        report = self.run_validate()
        self.assertEqual(report.denylist_entries, 1)
        self.assertEqual(self.find(report, "denylist"), [])
        write(os.path.join(self.root, "README.md"), "%s\n" % FAKE_NAME)
        self.assertEqual([f.path for f in self.find(self.run_validate(), "denylist")], ["README.md"])

    def test_denylist_whole_word(self):
        deny = os.path.join(self.tmp, "deny.txt")
        write(deny, "Ann\n")
        write(os.path.join(self.root, "notes.md"), "Annual planning by Anna and Ann.\n")
        found = self.find(self.run_validate(denylist_path=deny), "denylist")
        self.assertEqual(len(found), 1)

    def test_binary_files_are_skipped(self):
        write(os.path.join(self.root, "blob.bin"), b"\x00\x01" + FAKE_GHP.encode() + b"\x00")
        self.assertEqual(self.find(self.run_validate(), "secret"), [])


class ExecBitTests(FixtureCase):
    def test_shebang_without_exec_bit(self):
        write(self.skill_path("tests", "test_x.py"), "#!/usr/bin/env python3\n", 0o644)
        found = self.find(self.run_validate(), "not-executable")
        self.assertEqual([(f.level, f.path) for f in found], [("ERROR", "skills/good-skill/tests/test_x.py")])

    def test_exec_bit_without_shebang_warns_for_non_python(self):
        write(self.skill_path("references", "guide.md"), "# Guide\n", 0o755)
        write(self.skill_path("scripts", "mod.py"), "x = 1\n", 0o755)
        report = self.run_validate()
        self.assertEqual([(f.level, f.path) for f in self.find(report, "exec-no-shebang")],
                         [("WARN", "skills/good-skill/references/guide.md")])


class ManifestTests(FixtureCase):
    def plugin_path(self):
        return os.path.join(self.root, ".claude-plugin", "plugin.json")

    def market_path(self):
        return os.path.join(self.root, ".claude-plugin", "marketplace.json")

    def test_missing_manifest(self):
        os.remove(self.market_path())
        found = self.find(self.run_validate(), "manifest-missing")
        self.assertEqual([f.path for f in found], [validate.MARKETPLACE_MANIFEST])

    def test_invalid_json(self):
        write(self.plugin_path(), "{not json")
        self.assertEqual([f.path for f in self.find(self.run_validate(), "manifest-parse")], [validate.PLUGIN_MANIFEST])

    def test_version_mismatch_in_plugins_and_metadata(self):
        market = json.loads(json.dumps(MARKET))
        market["plugins"][0]["version"] = "1.2.4"
        write(self.market_path(), json.dumps(market))
        found = self.find(self.run_validate(), "manifest-version-mismatch")
        self.assertEqual(len(found), 1)
        self.assertIn("1.2.4", found[0].message)
        market["plugins"][0]["version"] = "1.2.3"
        market["metadata"]["version"] = "9.9.9"
        write(self.market_path(), json.dumps(market))
        found = self.find(self.run_validate(), "manifest-version-mismatch")
        self.assertEqual(len(found), 1)
        self.assertIn("metadata.version", found[0].message)

    def test_plugin_entry_missing(self):
        market = json.loads(json.dumps(MARKET))
        market["plugins"][0]["name"] = "other"
        write(self.market_path(), json.dumps(market))
        self.assertIn("manifest-plugin-missing", self.codes(self.run_validate(), "ERROR"))

    def test_bad_version_and_paths(self):
        plugin = dict(PLUGIN, version="1.2", skills="./nope/")
        write(self.plugin_path(), json.dumps(plugin))
        market = json.loads(json.dumps(MARKET))
        market["plugins"][0]["version"] = "1.2"
        market["metadata"]["version"] = "1.2"
        market["plugins"][0]["source"] = "./missing"
        write(self.market_path(), json.dumps(market))
        codes = self.codes(self.run_validate(), "ERROR")
        self.assertIn("manifest-field", codes)
        self.assertEqual(codes.count("manifest-path"), 2)
        self.assertNotIn("manifest-version-mismatch", codes)


@unittest.skipUnless(shutil.which("git"), "git not installed")
class GitFileListTests(FixtureCase):
    def git(self, *args):
        subprocess.run(["git", "-C", self.root] + list(args), check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def test_ignored_files_are_not_scanned(self):
        self.git("init", "-q")
        write(os.path.join(self.root, ".gitignore"), "ignored.txt\nsecret-dir/\n")
        write(os.path.join(self.root, "ignored.txt"), FAKE_GHP + "\n")
        write(os.path.join(self.root, "secret-dir", "k.txt"), FAKE_AKIA + "\n")
        write(os.path.join(self.root, "untracked.md"), "fine\n")
        report = self.run_validate(use_git=True)
        self.assertEqual(report.file_source, "git")
        self.assertEqual(self.find(report, "secret"), [])
        files, _ = validate.list_repo_files(self.root, use_git=True)
        self.assertIn("untracked.md", files)
        self.assertNotIn("ignored.txt", files)
        self.assertNotIn(".gitignore", [f for f in files if f.startswith(".git/")])

    def test_untracked_files_are_scanned(self):
        self.git("init", "-q")
        write(os.path.join(self.root, "new.md"), FAKE_GHP + "\n")
        self.assertEqual([f.path for f in self.find(self.run_validate(use_git=True), "secret")], ["new.md"])

    def test_walk_fallback_outside_git(self):
        files, source = validate.list_repo_files(self.root, use_git=True)
        self.assertEqual(source, "walk")
        self.assertIn("README.md", files)


class CliTests(FixtureCase):
    def cli(self, *args):
        proc = subprocess.run([sys.executable, "-I", os.path.join(HERE, "validate.py"), "--root", self.root, "--no-git"] + list(args),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        return proc.returncode, proc.stdout, proc.stderr

    def test_clean_exit_zero(self):
        code, out, err = self.cli()
        self.assertEqual(code, 0, out + err)
        self.assertIn("OK: 0 errors, 0 warnings", out)
        self.assertIn("skills: 1 (good-skill)", out)
        self.assertIn("denylist: none", out)

    def test_errors_exit_one_and_are_listed(self):
        write(self.skill_path("SKILL.md"), skill_md(name="wrong", body="# x\n\n[m](references/none.md)\n"))
        code, out, _ = self.cli()
        self.assertEqual(code, 1)
        self.assertIn("FAIL: 2 errors, 0 warnings", out)
        self.assertIn("ERROR name-mismatch", out)
        self.assertIn("skills/good-skill/SKILL.md:%d" % (BODY_OFFSET + 3), out)

    def test_strict_turns_warnings_into_failure(self):
        write(self.skill_path("SKILL.md"), skill_md(description="x" * 700, body="# x\n"))
        self.assertEqual(self.cli()[0], 0)
        code, out, _ = self.cli("--strict")
        self.assertEqual(code, 1)
        self.assertIn("FAIL: 0 errors, 1 warning", out)

    def test_json_output(self):
        write(os.path.join(self.root, "notes.md"), FAKE_GHP + "\n")
        code, out, _ = self.cli("--json")
        self.assertEqual(code, 1)
        data = json.loads(out)
        self.assertFalse(data["ok"])
        self.assertEqual(data["errors"], 1)
        self.assertEqual(data["findings"][0]["code"], "secret")

    def test_allow_email_flag_and_denylist_flag(self):
        write(os.path.join(self.root, "notes.md"), "%s %s\n" % (FAKE_EMAIL, FAKE_NAME))
        deny = os.path.join(self.tmp, "deny.txt")
        write(deny, FAKE_NAME + "\n")
        code, out, _ = self.cli("--denylist", deny)
        self.assertEqual(code, 1)
        self.assertIn("ERROR email", out)
        self.assertIn("ERROR denylist", out)
        self.assertIn("denylist: 1 entries", out)
        code, out, _ = self.cli("--denylist", deny, "--allow-email", FAKE_EMAIL)
        self.assertNotIn("ERROR email", out)

    def test_allow_home_flag(self):
        write(os.path.join(self.root, "notes.md"), FAKE_HOME_LINUX + "\n")
        code, out, _ = self.cli()
        self.assertEqual(code, 1)
        self.assertIn("ERROR home-path", out)
        code, out, _ = self.cli("--allow-home", "janedoe")
        self.assertEqual(code, 0, out)
        self.assertNotIn("home-path", out)

    def test_bad_root_exits_two(self):
        proc = subprocess.run([sys.executable, "-I", os.path.join(HERE, "validate.py"), "--root", os.path.join(self.tmp, "nope")],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(proc.returncode, 2)


if __name__ == "__main__":
    unittest.main()
