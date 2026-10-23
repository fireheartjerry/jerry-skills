"""Scrub pattern tests. Inputs are built from fragments at runtime so no credential-shaped literal sits in the repo."""
import unittest

import helpers  # noqa: F401
from mr import scrub

R = "[REDACTED:%s]"


def key_cases():
    return [
        ("AKIA" + "A" * 16, "key"),
        ("sk-" + "a" * 20, "key"),
        ("sk-ant-" + "a" * 20, "key"),
        ("ghp_" + "b" * 24, "key"),
        ("github_pat_" + "c" * 24, "key"),
        ("xoxb-" + "1" * 12, "key"),
        ("AIza" + "d" * 35, "key"),
        ("-----BEGIN " + "PRIVATE KEY-----\nAAAA\nBBBB\n-----END " + "PRIVATE KEY-----", "key"),  # assembled: no header literal in the repo
        ("eyJ" + "a" * 12 + "." + "b" * 12 + "." + "c" * 12, "key"),
        ("sk_live_" + "e" * 24, "key"),
        ("rk_live_" + "f" * 24, "key"),
        ("sk_test_" + "g" * 24, "key"),
        ("hf_" + "h" * 30, "key"),
        ("glpat-" + "i" * 20, "key"),
        ("npm_" + "j" * 36, "key"),
        ("ya29." + "k" * 40, "key"),
        ("AQ." + "m" * 40, "key"),
    ]


class ScrubTests(unittest.TestCase):
    def test_key_patterns(self):
        for value, kind in key_cases():
            with self.subTest(value=value[:12]):
                out, counts = scrub.scrub("before %s after" % value)
                self.assertEqual(out, "before %s after" % (R % kind))
                self.assertEqual(counts.get(kind), 1)

    def test_secret_label_kept_value_redacted(self):
        out, counts = scrub.scrub("set password: " + "x" * 10 + " and API_KEY=" + "y" * 12)
        self.assertEqual(out, "set password: %s and API_KEY=%s" % (R % "secret", R % "secret"))
        self.assertEqual(counts["secret"], 2)

    def test_prefixed_and_suffixed_secret_labels(self):
        cases = ["aws_secret_access_key = " + "w" * 40, "AWS_SECRET_ACCESS_KEY=" + "w" * 40, "GITHUB_TOKEN: " + "t" * 30,
                 "DB_PASSWORD=" + "p" * 12, "api_key_v2 = " + "v" * 20, "openai-api-key: " + "o" * 20, "private_key=" + "q" * 20]
        for c in cases:
            with self.subTest(c=c):
                out, counts = scrub.scrub("env " + c + " end")
                self.assertTrue(out.endswith("%s end" % (R % "secret")), out)
                self.assertEqual(counts.get("secret"), 1)
        for keep in ("tokens: 150000", "max_tokens=4096", "the token budget is 12000", "tokens used: 13,241", "token=abc"):
            with self.subTest(keep=keep):
                out, counts = scrub.scrub(keep)
                self.assertEqual(out, keep)  # counts are not secrets; a 3-character value is below the floor

    def test_authorization_bearer(self):
        out, counts = scrub.scrub("curl -H 'Authorization: Bearer " + "b" * 32 + "' https://api.example.test/v1")
        self.assertIn("Authorization: Bearer %s'" % (R % "secret"), out)
        self.assertEqual(counts.get("secret"), 1)
        out, counts = scrub.scrub("bearer token refresh flow")  # prose: a short word after bearer is not a token
        self.assertEqual(out, "bearer token refresh flow")
        self.assertEqual(scrub.SCRUB_VERSION, 3)

    def test_email_phone_card(self):
        out, counts = scrub.scrub("mail someone@example.com or call +1 416 555 0199, card 4111 1111 1111 1111")
        self.assertEqual(out, "mail %s or call %s, card %s" % (R % "email", R % "phone", R % "card"))
        self.assertEqual((counts["email"], counts["phone"], counts["card"]), (1, 1, 1))

    def test_timestamps_and_dates_survive_phone_rule(self):
        text = "deployed at 2026-01-20T10:20:30 and epoch 1700000000 then 1700000000123 on 20260120 at 10:20"
        out, counts = scrub.scrub(text)
        self.assertEqual(out, text)
        self.assertEqual(counts, {})

    def test_identifier_fragments_survive_phone_rule(self):
        text = "see scratch-workspaces/04561947-40d8-4093-b056-4407f7dca54a/x and commit a1b2c3-1234567890-deadbeef"
        out, counts = scrub.scrub(text)
        self.assertEqual(out, text)
        self.assertEqual(counts, {})
        out, counts = scrub.scrub("call 298-7497575 today")
        self.assertEqual(out, "call %s today" % (R % "phone"))

    def test_luhn_invalid_digit_runs_kept(self):
        self.assertTrue(scrub.luhn_ok("4111111111111111"))
        self.assertFalse(scrub.luhn_ok("4111111111111112"))
        out, _ = scrub.scrub("ref 4111 1111 1111 1112 is an order id")
        self.assertIn("4111 1111 1111 1112", out)

    def test_url_credentials(self):
        out, counts = scrub.scrub("https://user:secretpw@host.test/path?token=abcdef&keep=1&sig=zzz")
        self.assertEqual(out, "https://%s@host.test/path?token=%s&keep=1&sig=%s" % (R % "url_credentials", R % "url_credentials", R % "url_credentials"))
        self.assertEqual(counts["url_credentials"], 3)

    def test_home_paths(self):
        unix_users = "/" + "Users" + "/user/Code/x.py"
        out, counts = scrub.scrub("see %s and /home/user/Code/y.py and /home/user" % unix_users)
        self.assertEqual(out, "see ~/Code/x.py and ~/Code/y.py and ~")
        self.assertEqual(counts["home_path"], 3)
        win = "C:" + "\\" + "Users" + "\\alex\\x.txt"
        out, _ = scrub.scrub("file " + win)
        self.assertEqual(out, "file ~\\x.txt")

    def test_people_word_boundaries(self):
        people = {"Sam Taylor": "<teammate>", "Jordan Lee": "<stakeholder>"}
        out, counts = scrub.scrub("ask Sam Taylor; sam agreed; Samantha did not; Jordan Lee too", people)
        self.assertEqual(out, "ask <teammate>; <teammate> agreed; Samantha did not; <stakeholder> too")
        self.assertEqual(counts["person:<teammate>"], 2)
        self.assertEqual(counts["person:<stakeholder>"], 1)

    def test_idempotent(self):
        text = "key %s mail someone@example.com password: %s" % ("AKIA" + "B" * 16, "z" * 9)
        once, c1 = scrub.scrub(text)
        twice, c2 = scrub.scrub(once)
        self.assertEqual(once, twice)
        self.assertEqual(c2, {})
        self.assertEqual(sum(c1.values()), 3)

    def test_detect_reports_without_rewriting(self):
        hits = scrub.detect("mail someone@example.com and /home/user/x", {"Sam Taylor": "<teammate>"})
        kinds = {k for k, _ in hits}
        self.assertEqual(kinds, {"email", "home_path"})


if __name__ == "__main__":
    unittest.main()
