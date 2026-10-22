"""lint: `--skill PATH` checks the public skill for private data and broken links; `--model` checks the built model
for forbidden rules (the harness beats the model) and personal data."""
import re
from pathlib import Path

from . import grants as _grants
from . import scrub as _scrub

# Each pattern names a way a model line could tell an agent to defeat the harness. Lines that merely describe a
# preference in the fixed rewording template (REWORDING_TEMPLATE) are exempt; nothing else is.
FORBIDDEN_RULES = [
    r"(?i)bypass(ing|ed)?\s+(the\s+)?permissions?",
    r"(?i)permissions?\s+(are\s+|is\s+|get\s+|gets\s+)?bypassed",
    r"(?i)skip(ping|s)?\s+(the\s+)?permissions?",
    r"(?i)dangerously",
    r"(?i)without\s+(asking|confirm(ing|ation)?|approval|review|waiting(?!\s+to\s+be\s+(asked|told|prompted|reminded))|a\s+(check|prompt|question))",
    r"(?i)don'?t\s+ask",
    r"(?i)never\s+ask\s+(me\s+)?(for\s+)?(approval|permission|confirmation|consent|before)",
    r"(?i)never\s+prompts?\s*(again|me|for|before|first)?\b(?!\s+(what|which|about\s+(the\s+)?(seed|typo|meaning)))",
    r"(?i)never\s+(confirm|wait\s+for\s+(approval|permission|confirmation|a\s+yes))",
    r"(?i)no\s+need\s+to\s+(ask|confirm|check\s+in|wait)",
    r"(?i)auto[- ]?approv",
    r"(?i)(treat|consider|assume|as\s+if)\b.{0,40}already\s+(authori[sz]ed|approved)",
    r"(?i)already\s+(authori[sz]ed|approved)\b.{0,40}\b(permission|prompt|security|sandbox|settings?|bypass|allow[- ]?list|send|post|deploy|merge|spend|delete)",
    r"(?i)stop\s+asking",
    r"(?i)(approval|permission|authori[sz]ation)s?\s+(is|are)\s+(standing|permanent|implied|granted)",
    r"(?i)permanent(ly)?\s+(approv|authori[sz]|allow|grant)",
    r"(?i)(make|made|treat)\s+.{0,40}(approval|permission|prompt)s?\s+(as\s+)?permanent",
    r"(?i)allow[- ]?list",
    r"(?i)(fix|change|edit|update|set)\s+.{0,40}(config(uration)?|settings?)\s+.{0,20}permanent",
    r"(?i)permanent(ly)?\s+.{0,20}(config(uration)?|settings?)",
    r"(?i)(change|edit|modify|loosen|relax)\s+.{0,40}(security|permission|sandbox)\s+(settings?|rules?|config)",
    r"(?i)\b(sen[dt]|post(ed|s)?|deploy(ed|s)?|merg(e|ed|es)|push(ed|es)?|ship(ped|s)?)\b.{0,40}\bwithout\b",
    r"(?i)never\s+a\s+stopping\s+point",
]
_FORBIDDEN_RE = [re.compile(p) for p in FORBIDDEN_RULES]
REWORDING_TEMPLATE = "gets frustrated by repeated permission prompts; expects the agent to batch questions and work on unblocked items meanwhile"
FORBIDDEN_RULES_TEXT = (
    "Forbidden: any rule that tells an agent to bypass, skip or auto-approve permissions; to send, post, push, merge or "
    "deploy without asking, confirming or waiting; to stop asking or never prompt; to treat a past approval as standing, "
    "permanent or already authorised; to add anything to an allowlist; or to change security, sandbox or permission "
    "settings or configuration, permanently or not. Record such wishes only as a preference, in exactly this form: "
    "\"" + REWORDING_TEMPLATE + "\", never as a rule to act on.")
EM_DASH = chr(0x2014)
_LINK = re.compile(r"\[[^\]]*\]\(([^)\s#]+)(?:#[^)]*)?\)")
_WIN_PATH = re.compile(r"[A-Za-z]:\\Users\\")
_UNIX_HOME = re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+")
_FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n", re.S)
TEXT_EXTS = {".md", ".py", ".json", ".txt"}
DESCRIPTION_MAX = 600
NOT_FOR_WITHIN = 400


def _iter_files(root):
    for p in sorted(Path(root).rglob("*")):
        if p.is_file() and p.suffix in TEXT_EXTS and "__pycache__" not in p.parts:
            yield p


def _frontmatter(text):
    m = _FRONTMATTER.match(text)
    if not m:
        return None
    out = {}
    lines = m.group(1).splitlines()
    i = 0
    while i < len(lines):
        mm = re.match(r"^([A-Za-z_]+):\s*(.*)$", lines[i])
        i += 1
        if not mm:
            continue
        key, value = mm.group(1), mm.group(2).strip()
        if value in (">", ">-", "|", "|-"):  # YAML block scalar: indented continuation lines
            block = []
            while i < len(lines) and (lines[i].startswith((" ", "\t")) or not lines[i].strip()):
                block.append(lines[i].strip())
                i += 1
            value = (" " if value.startswith(">") else "\n").join(b for b in block if b).strip()
        out[key] = value
    return out


def _frontmatter_end(text):
    m = _FRONTMATTER.match(text)
    return text[:m.end()].count("\n") if m else 0


def load_deny(deny_file):
    """Deny words, one per line, `#` comments ignored. Returns (words, warning or None)."""
    words = []
    if not deny_file:
        return words, None
    if not Path(deny_file).exists():
        return words, "deny file %s does not exist: the private-name check did not run" % deny_file
    for line in Path(deny_file).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            words.append(line)
    if not words:
        return words, "deny file %s has no entries: the private-name check is vacuous" % deny_file
    return words, None


def lint_skill(skill_path, deny_file=None, not_for_within=NOT_FOR_WITHIN, warnings=None):
    """Return a list of 'file:line: reason' hits. Non-fatal notes (an empty deny file) go to `warnings` when given."""
    root = Path(skill_path)
    hits = []
    skill_md = root / "SKILL.md"
    if not skill_md.exists():
        return ["%s: SKILL.md missing" % root]
    text = skill_md.read_text(encoding="utf-8")
    fm = _frontmatter(text)
    if fm is None:
        hits.append("%s:1: no frontmatter" % skill_md)
    else:
        if "name" not in fm:
            hits.append("%s:1: frontmatter has no name" % skill_md)
        desc = fm.get("description", "")
        if not desc:
            hits.append("%s:1: frontmatter has no description" % skill_md)
        else:
            if len(desc) > DESCRIPTION_MAX:
                hits.append("%s:1: description is %d characters, maximum %d" % (skill_md, len(desc), DESCRIPTION_MAX))
            pos = desc.lower().find("not for")
            if pos < 0:
                hits.append("%s:1: description has no 'Not for' clause" % skill_md)
            elif pos > not_for_within:
                hits.append("%s:1: 'Not for' clause starts at character %d, must start within the first %d" % (skill_md, pos, not_for_within))
    deny, note = load_deny(deny_file)
    if note and warnings is not None:
        warnings.append(note)
    for p in _iter_files(root):
        rel = p.relative_to(root)
        in_fixtures = "tests" in rel.parts  # tests and fixtures carry pattern samples by design
        try:
            body = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        lines = body.splitlines()
        fm_end = _frontmatter_end(body) if p == skill_md else 0  # the author line in the frontmatter is public by design
        for i, line in enumerate(lines, 1):
            if EM_DASH in line:
                hits.append("%s:%d: em dash" % (p, i))
            if not in_fixtures:
                if _UNIX_HOME.search(line) or _WIN_PATH.search(line):
                    hits.append("%s:%d: absolute home path" % (p, i))
                for kind, snippet in _scrub.detect(line):
                    if kind in ("key", "email", "phone", "card", "url_credentials"):
                        hits.append("%s:%d: %s pattern (%s)" % (p, i, kind, snippet[:20]))
            if i <= fm_end:
                continue
            low = line.lower()
            for w in deny:
                if w.lower() in low:
                    hits.append("%s:%d: deny-list word %r" % (p, i, w))
        if p.suffix == ".md" and (p == skill_md or "references" in rel.parts):
            for i, line in enumerate(lines, 1):
                for m in _LINK.finditer(line):
                    target = m.group(1)
                    if re.match(r"^[a-z]+:", target) or target.startswith("$") or target.startswith("~"):
                        continue
                    if not (p.parent / target).exists():
                        hits.append("%s:%d: link target %s does not exist" % (p, i, target))
    return hits


AUTONOMY_COVERS_LINE = "- covers: " + _grants.AUTONOMY_COVERS.lower()


_QUOTED = re.compile(r'"[^"\n]*"|\u201c[^\u201d\n]*\u201d')


def forbidden_hits(text, path="model", quote_lines_exempt=False, evidence_quotes_exempt=False):
    """quote_lines_exempt: in grants.md the `- Quote:` line is the user's verbatim text (which by design contains a
    grant phrase such as "no need to ask"), and the fixed autonomy Covers line (grants.AUTONOMY_COVERS, which names
    the harness permission mode as the limit) is this module's own text; every other line is an instruction and is
    checked. evidence_quotes_exempt (profile.md): text inside double quotes is the user's verbatim evidence for a
    trait, not an instruction, so it is removed before matching; the trait wording around it is still checked.
    Elsewhere nothing is exempt but the rewording template."""
    hits = []
    quotes = [_norm_words(l.split(":", 1)[1]) for l in text.splitlines() if quote_lines_exempt and l.startswith("- Quote:")]
    for i, line in enumerate(text.splitlines(), 1):
        if quote_lines_exempt and line.startswith("- Quote:"):
            continue
        if quote_lines_exempt and _derived_from_quote(line, quotes):
            continue
        if quote_lines_exempt and re.sub(r"\s+", " ", line.strip().lower()) == AUTONOMY_COVERS_LINE:
            continue
        probe = _QUOTED.sub('""', line) if evidence_quotes_exempt else line
        for pat in _FORBIDDEN_RE:
            m = pat.search(probe)
            if m and not _is_rewording(line):
                hits.append("%s:%d: forbidden rule pattern %r" % (path, i, m.group(0)))
                break
    return hits


def _norm_words(text):
    return " ".join(re.findall(r"[a-z0-9']+", (text or "").lower().replace("\u2019", "'")))


def _derived_from_quote(line, quotes):
    """grants.md: a heading or Covers line whose words appear verbatim in one of the file's Quote lines is the user's
    own wording carried over by synthesize grants, not a rule this pipeline wrote."""
    m = re.match(r"^(## G\d+\.|- Covers:)\s*(.*)$", line)
    if not m:
        return False
    body = _norm_words(m.group(2))
    return bool(body) and any(body in q for q in quotes)


def _is_rewording(line):
    """Only the exact rewording template is exempt. "Prefers that you skip permissions" is a rule, not a preference."""
    return REWORDING_TEMPLATE in line.lower()


def lint_grants(text, path="grants.md", tokens=None):
    """Fatal problems in grants.md: structure, missing or negated grant phrase, non-typed origin, never-grantable or
    unnamed Covers, a spend grant without a cap, a message grant naming nobody, and forbidden instructions outside the
    Quote lines."""
    _, errors = _grants.parse_grants(text, path, tokens=tokens)
    return errors + forbidden_hits(text, path, quote_lines_exempt=True)


def lint_corrections(text, path="corrections.md"):
    """Forbidden instructions inside corrections.md. A correction is read first by the runtime (later lines win), so a
    `fix` or `add` whose `meant` or `wrong` carries a forbidden rule or a grant phrase is fatal; for a `drop`, `meant`
    is the model sentence to remove and is not checked (removing a forbidden line is the point)."""
    from . import refresh as _refresh
    hits = []
    entries, _ = _refresh.parse_corrections(text or "")
    for e in entries:
        fields = ["wrong"] if e.get("kind") == "drop" else ["meant", "wrong"]
        for f in fields:
            for h in forbidden_hits(e.get(f) or "", path):
                hits.append("%s:%d: %s: %s" % (path, e["line"], f, h.split(": ", 1)[1]))
                break
        if e.get("kind") != "drop" and _grants.has_grant_phrase(e.get("meant") or ""):
            hits.append("%s:%d: meant: a correction cannot grant (%r); grants are typed by hand into grants.md" % (
                path, e["line"], _grants.grant_phrase(e.get("meant"))))
    return hits


def lint_model(paths, cfg):
    """{'forbidden': [...], 'other': [...]} over light.md, profile.md (its lessons included), examples.md, grants.md and
    corrections.md."""
    out = {"forbidden": [], "other": []}
    people = _scrub.compile_people(cfg.get("people") or {})
    folded_drops = _folded_drops(paths)
    if paths.grants.exists():
        out["forbidden"] += lint_grants(paths.grants.read_text(encoding="utf-8"), tokens=list((cfg.get("people") or {}).values()))
    if paths.corrections.exists():
        out["forbidden"] += lint_corrections(paths.corrections.read_text(encoding="utf-8"))
    for name in ("light.md", "profile.md", "examples.md", "grants.md"):
        p = paths.model / name
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8")
        if name == "profile.md":
            # lessons: what went wrong in a bad lesson is a negative example, not an instruction; the rest of every
            # lesson is checked here and again for anything that reads as an authorization
            from . import lessons as _lessons
            out["forbidden"] += forbidden_hits(_lessons.lint_view(text), name, evidence_quotes_exempt=True)
            out["forbidden"] += _lessons.lesson_hits(text, name)
        elif name != "grants.md":
            out["forbidden"] += forbidden_hits(text, name)
        if name == "examples.md":
            # an upgrade pair takes a lesson's better way: it can never authorize an action either
            from . import lessons as _lessons
            out["forbidden"] += _lessons.upgrade_hits(text, name)
        counts = {}
        for kind, snippet in _scrub.detect(text, people):
            counts[kind] = counts.get(kind, 0) + 1
        for kind, n in sorted(counts.items()):
            out["other"].append("%s: %d %s hit(s)" % (name, n, kind))
        for sentence in folded_drops:
            if sentence and sentence.lower() in text.lower():
                out["other"].append("%s: dropped correction still present: %r" % (name, sentence[:60]))
    return out


def _folded_drops(paths):
    """Sentences named by `kind: drop` corrections that refresh already folded (listed in MODEL.json)."""
    from .config import read_json
    m = read_json(paths.model_json, {}) or {}
    return [d for d in m.get("folded_drops", []) if isinstance(d, str)]
