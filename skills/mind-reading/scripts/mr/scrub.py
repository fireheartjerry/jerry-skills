"""Secret and PII scrub. Runs on every kept text before it enters the corpus. Idempotent.

Replacement tokens are [REDACTED:<kind>]. Kinds: key, secret, email, phone, card, url_credentials, home_path, person.
`detect` reports hits without rewriting (used by lint)."""
import re

SCRUB_VERSION = 2  # 2: Stripe, Hugging Face, GitLab, npm, Google OAuth, Authorization: Bearer, prefixed secret labels

_KEY_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"[sr]k_(?:live|test)_[A-Za-z0-9]{16,}"),  # Stripe secret and restricted keys
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"glpat-[A-Za-z0-9_-]{20,}"),  # GitLab
    re.compile(r"hf_[A-Za-z0-9]{20,}"),  # Hugging Face
    re.compile(r"npm_[A-Za-z0-9]{20,}"),
    re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"AIza[0-9A-Za-z_-]{35}"),
    re.compile(r"ya29\.[A-Za-z0-9_-]{20,}"),  # Google OAuth access tokens
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[^\n]*(?:\n[^\n]*)*"),
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
]
# Labelled secrets: the label may carry underscore-joined prefixes and suffixes (aws_secret_access_key, GITHUB_TOKEN,
# DB_PASSWORD, api_key_v2). A bare plural ("tokens: 150000") is a count, not a secret, and is left alone.
_SECRET = re.compile(
    r"(?i)((?:[A-Za-z0-9-]+_)*(?:api[_-]?key|apikey|secret|token|password|passwd|pwd|bearer|access[_-]?key|private[_-]?key)"
    r"(?:_[A-Za-z0-9-]+)*\s*[:=]\s*[\"']?)([^\s\"'&]{6,})")
_BEARER = re.compile(r"(?i)(\bbearer\s+)([A-Za-z0-9._~+/=-]{16,})")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"\+?\d[\d\s().-]{8,}\d")
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_URL_USERINFO = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)([^/\s:@]+):([^/\s@]+)@")
_URL_PARAM = re.compile(
    r"(?i)([?&](?:token|key|sig|signature|access_token|x-amz-signature|x-amz-credential|apikey|api_key)=)([^&\s\"'<>]+)")
_HOME_SLASH = re.compile(r"/(?:Users|home)/[^/\s\"'`]+/")
_HOME_BARE = re.compile(r"/(?:Users|home)/[^/\s\"'`:)]+(?=[\s\"'`:)\]]|$)")
_WIN_HOME = re.compile(r"[A-Za-z]:\\Users\\[^\\\s\"']+\\?")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_CLOCK = re.compile(r"\d{1,2}:\d{2}")
_REDACTED = "[REDACTED:%s]"


def luhn_ok(digits):
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _looks_like_time(candidate):
    digits = re.sub(r"\D", "", candidate)
    if _ISO_DATE.search(candidate) or _CLOCK.search(candidate):
        return True
    if candidate.strip("+").isdigit() and len(digits) in (10, 13):
        return True  # epoch seconds / milliseconds
    if re.fullmatch(r"\d{8}", digits) and digits[:2] in ("19", "20"):
        return True  # yyyymmdd
    if re.fullmatch(r"(?:19|20)\d{6}[-_T. ]?\d{4,9}", candidate.strip("+")):
        return True  # yyyymmdd-hhmmss style run ids and stamps
    return False


def _embedded_in_token(m):
    """True when the match continues into letters or hex-like identifier text (uuids, hashes, paths)."""
    s = m.string
    before = s[m.start() - 1] if m.start() > 0 else ""
    after = s[m.end()] if m.end() < len(s) else ""
    if before.isalnum() or before in "-_/":
        return True
    if after.isalnum() or (after in "-_" and m.end() + 1 < len(s) and s[m.end() + 1].isalnum()):
        return True
    return False


def _phone_sub(m):
    cand = m.group(0)
    digits = re.sub(r"\D", "", cand)
    if not 10 <= len(digits) <= 15 or _looks_like_time(cand) or _embedded_in_token(m):
        return cand
    return _REDACTED % "phone"


def _card_sub(m):
    cand = m.group(0)
    digits = re.sub(r"\D", "", cand)
    if not 13 <= len(digits) <= 19 or not luhn_ok(digits):
        return cand
    if cand.isdigit() and len(cand) == 13:
        return cand  # epoch milliseconds
    return _REDACTED % "card"


def _secret_sub(m):
    if m.group(2).startswith("[REDACTED:"):
        return m.group(0)
    return m.group(1) + _REDACTED % "secret"


def _bearer_sub(m):
    if m.group(2).startswith("[REDACTED:"):
        return m.group(0)
    return m.group(1) + _REDACTED % "secret"


def _count_sub(pattern, text, repl, counts, kind):
    def _inner(m):
        out = repl(m) if callable(repl) else repl
        if out != m.group(0):
            counts[kind] = counts.get(kind, 0) + 1
        return out
    return pattern.sub(_inner, text)


def compile_people(people):
    """config.people {"Full Name": "<role>"} -> [(regex, token)] for the full name and its first word."""
    out = []
    for name, token in (people or {}).items():
        name = name.strip()
        if not name:
            continue
        parts = name.split()
        variants = [name]
        if len(parts) > 1 and len(parts[0]) >= 3:
            variants.append(parts[0])
        for v in variants:
            out.append((re.compile(r"(?i)\b%s\b" % re.escape(v)), token))
    return out


def scrub(text, people=None, counts=None):
    """Return (scrubbed_text, counts). `counts` accumulates per kind when passed."""
    counts = counts if counts is not None else {}
    if not text:
        return text, counts
    for pat in _KEY_PATTERNS:
        text = _count_sub(pat, text, _REDACTED % "key", counts, "key")
    text = _count_sub(_URL_USERINFO, text, lambda m: m.group(1) + _REDACTED % "url_credentials" + "@",
                      counts, "url_credentials")
    text = _count_sub(_URL_PARAM, text, lambda m: m.group(1) + _REDACTED % "url_credentials", counts,
                      "url_credentials")
    text = _count_sub(_SECRET, text, _secret_sub, counts, "secret")
    text = _count_sub(_BEARER, text, _bearer_sub, counts, "secret")
    text = _count_sub(_EMAIL, text, _REDACTED % "email", counts, "email")
    text = _count_sub(_CARD, text, _card_sub, counts, "card")
    text = _count_sub(_PHONE, text, _phone_sub, counts, "phone")
    text = _count_sub(_HOME_SLASH, text, "~/", counts, "home_path")
    text = _count_sub(_HOME_BARE, text, "~", counts, "home_path")
    text = _count_sub(_WIN_HOME, text, "~\\", counts, "home_path")
    people_re = people if isinstance(people, list) else compile_people(people)
    for pat, token in people_re:
        text = _count_sub(pat, text, token, counts, "person:" + token)
    return text, counts


def detect(text, people=None):
    """List of (kind, snippet) hits a scrub would change. Home paths included."""
    hits = []
    if not text:
        return hits
    for pat in _KEY_PATTERNS:
        for m in pat.finditer(text):
            hits.append(("key", m.group(0)[:40]))
    for m in _SECRET.finditer(text):
        if not m.group(2).startswith("[REDACTED:"):
            hits.append(("secret", m.group(0)[:40]))
    for m in _BEARER.finditer(text):
        if not m.group(2).startswith("[REDACTED:"):
            hits.append(("secret", m.group(0)[:40]))
    for m in _URL_USERINFO.finditer(text):
        hits.append(("url_credentials", m.group(0)[:40]))
    for m in _URL_PARAM.finditer(text):
        hits.append(("url_credentials", m.group(0)[:40]))
    for m in _EMAIL.finditer(text):
        hits.append(("email", m.group(0)[:40]))
    for m in _CARD.finditer(text):
        if _card_sub(m) != m.group(0):
            hits.append(("card", m.group(0)[:40]))
    for m in _PHONE.finditer(text):
        if _phone_sub(m) != m.group(0):
            hits.append(("phone", m.group(0)[:40]))
    for pat in (_HOME_SLASH, _HOME_BARE, _WIN_HOME):
        for m in pat.finditer(text):
            hits.append(("home_path", m.group(0)[:60]))
    people_re = people if isinstance(people, list) else compile_people(people)
    for pat, token in people_re:
        for m in pat.finditer(text):
            hits.append(("person", m.group(0)))
    return hits
