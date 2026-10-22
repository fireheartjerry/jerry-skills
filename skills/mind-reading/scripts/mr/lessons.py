"""Lessons: what works for the user and what does not, from outcomes rather than stated preferences.

The `lessons` mining lens reports episodes where an approach the user chose or accepted led to a bad outcome (rework,
repeated failure, a correction, wasted time or money, a revert, frustration) or a good one (praise, reuse), with the
better alternative the evidence supports. This module turns those raw items into the profile's
`## What works and what doesn't` section, deterministically:

1. verify: every user quote must be substantive (MIN_QUOTE_CHARS characters or MIN_QUOTE_WORDS content words) and occur
   verbatim in a typed corpus message of the session the miner cites; time and session come from that message, never
   from the miner. Agent-context quotes must occur in a stored agent-context head of a session that also has a verified
   user quote, and may only support it (at most one per lesson). A lesson left without a verified user quote is dropped,
   and so is one whose better way overlaps the agent's text more than the user's, so agent context (untrusted, possibly
   injected) never becomes a lesson by itself.
2. lint: a lesson whose text authorizes anything is dropped: a grant phrase, a forbidden rule, or an outward-facing,
   destructive, spend or permission action in its better way (or, for a good outcome, in what to keep), whatever the
   wording, unless the clause negates it or gates it on the user's yes. `lint --model` checks the rendered section
   (lesson_hits) and the upgrade pairs of examples.md (upgrade_hits) the same way.
3. consolidate: similar lessons merge across lenses and chunks (normalized tokens: stems and common synonyms, compared
   with every member of a group); strength counts distinct verified sessions (moderate at two, strong at three), and
   the deterministic markers beside the quoted messages (a failed command, a tool error, an interruption, a revert)
   are outcome evidence nobody can inject: repeated ones lift a one-session bad lesson to moderate and keep it. The
   miner's own count adds at most one unquoted episode per chunk and never raises strength. The newest evidence owns the
   wording and the adoption verdict; a single episode is kept only when its cost was high or markers back it.
4. render: lessons grouped by request kind into `### W<n>.` groups with a `When:` line, so `select` can route them like
   the Unsaid-defaults groups."""
import re

from . import grants as _grants
from . import lint as _lint

HEADING = "## What works and what doesn't"
GROUP_RE = re.compile(r"^### (W\d{1,3})\. (.+?)\s*$")
STRENGTHS = ("strong", "moderate", "weak")
MERGE_SIMILARITY = 0.3
CONFLICT_SIMILARITY = 0.4
GROUP_JACCARD = 0.34
USER_QUOTES = 2
AGENT_QUOTES = 1
LINE_MAX = 600
MIN_QUOTE_CHARS = 20
MIN_QUOTE_WORDS = 3
FAILURE_MARKERS = ("command_failed", "tool_error", "interrupted", "reverted")
_WORD = re.compile(r"[a-z0-9][a-z0-9'+-]*")
_STOP = {"a", "an", "the", "and", "or", "of", "to", "for", "in", "on", "at", "by", "as", "so", "is", "are", "be", "that",
         "this", "it", "its", "with", "when", "then", "than", "not", "no", "do", "does", "any", "all", "one", "my", "me",
         "i", "you", "your", "before", "after", "first", "instead", "again", "every", "just", "only", "into", "from"}
_QUOTED = re.compile(r'"[^"\n]*"|“[^”\n]*”')

# ---------------------------------------------------------------- normalized tokens (merging, conflicts, overlap)

# words independent chunk calls use for the same thing; applied to the raw word and again to its stem
_SYNONYMS = {"prod": "production", "prd": "production", "live": "production", "deployment": "deploy", "deploys": "deploy",
             "ship": "deploy", "shipping": "deploy", "shipped": "deploy", "staging": "stage", "stg": "stage", "stag": "stage",
             "env": "environment", "envs": "environment", "repo": "repository", "repos": "repository", "pr": "pullrequest",
             "prs": "pullrequest", "verify": "check", "verifi": "check", "validat": "check", "validate": "check",
             "confirm": "check", "straight": "direct", "directly": "direct", "entire": "whole", "full": "whole",
             "single": "one", "big": "large", "huge": "large", "db": "database", "tests": "test", "spec": "test",
             "specs": "test", "rewrote": "rewrite", "wrote": "write", "written": "write", "ran": "run", "broke": "break",
             "broken": "break", "doc": "document", "docs": "document", "squash": "squash"}
_MERGE_STOP = (_STOP - {"one", "not", "no", "only"}) | {"always", "prior", "make", "sure", "each", "should", "will", "would",
                                                        "could", "can", "some", "more", "less", "there", "here", "have",
                                                        "has", "had", "was", "were", "been", "also", "via", "using", "use",
                                                        "it's", "them", "they", "their", "what", "which", "while"}
NEGATION_TOKENS = {"not", "no", "never", "without", "only", "skip", "avoid"}


def _stem(w):
    for suf in ("ing", "ed", "es", "s"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[:-len(suf)]
    return w


def _root(w):
    """A light stem for matching paraphrases: plural, -ing, -ed, -ment, -ion and a final e removed."""
    for suf, rep in (("ments", ""), ("ment", ""), ("ies", "y"), ("ied", "y"), ("ings", ""), ("ing", ""), ("ions", ""),
                     ("ion", ""), ("ed", ""), ("es", ""), ("s", "")):
        if len(w) > len(suf) + 2 and w.endswith(suf) and not w.endswith("ss"):
            w = w[:-len(suf)] + rep
            break
    if len(w) > 4 and w.endswith("e"):
        w = w[:-1]
    return w


def tokens(text):
    """Normalized content tokens: lower case, stems and common synonyms (prod/production, deploy/deployment)."""
    t = (text or "").lower().replace("pull request", "pullrequest").replace("pull-request", "pullrequest")
    out = set()
    for w in _WORD.findall(t):
        w = _SYNONYMS.get(w, w).strip("'")
        if w in _MERGE_STOP or len(w) < 2:
            continue
        r = _root(w)
        out.add(_SYNONYMS.get(r, r))
    return out


def similarity(a, b):
    sa, sb = tokens(a), tokens(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / float(len(sa | sb))


def _norm(text):
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def _content(text):
    return {w for w in _WORD.findall((text or "").lower()) if w not in _STOP and len(w) > 1}


def jaccard(a, b):
    sa, sb = _content(a), _content(b)
    if not sa and not sb:
        return 1.0 if _norm(a) == _norm(b) else 0.0
    return len(sa & sb) / float(len(sa | sb))


# ---------------------------------------------------------------- authorization

# A lesson describes an approach to the work. It never authorizes: these patterns, the forbidden rules of lint.py and
# the grant phrases of grants.py mark text that would let a lesson stand in for a grant or a yes.
AUTHORIZE_PATTERNS = [
    r"(?i)\b(?:just|go ahead and|automatically|immediately|directly|straight away|right away|on (?:your|its) own)\s+"
    r"(?:send|post|email|message|reply|ping|notify|merge|deploy|publish|release|delete|remove|drop|wipe|purge|force[- ]?push|"
    r"spend|buy|purchase|pay|subscribe|upgrade the plan|rotate|revoke)\b",
    r"(?i)\b(?:send|post|email|message|reply|merge|deploy|publish|release|delete|wipe|purge|force[- ]?push|spend|buy|"
    r"purchase|pay|subscribe)\b.{0,40}\b(?:automatically|on (?:your|its) own|unprompted|unasked|"
    r"without (?:a|the|my|his|her|their) (?:go|yes|ok|okay|sign[- ]off))\b",
    r"(?i)\b(?:change|edit|loosen|relax|grant|add|widen|disable)\b.{0,30}\b(?:permissions?|sandbox|allow[- ]?list|hooks?|"
    r"security settings?|approval settings?)\b",
    r"(?i)\b(?:lift|skip|ignore|override|bypass)(?:s|es|ed|ing)?\b.{0,20}\b(?:holds?|held|grants?|approvals?|confirmations?)\b",
    r"(?i)^(?!.*\b(?:never|don'?t|do not|avoid|no|not)\b).*\bforce[- ]?push",
]
_AUTHORIZE_RE = [re.compile(p) for p in AUTHORIZE_PATTERNS]

# Actions a lesson can never authorize, by category, whatever the qualifier. Each is checked per clause; a match is
# excused only when the clause negates it ("never", "instead of", "without") or proposes it ("draft", "suggest") before
# the action, or the sentence gates it on the user's yes ("ask first", "after my ok", "once I approve").
_LEAD = r"(?:^|[,;:(*>\-]\s*|\b(?:to|and|then|or|will|would|should|must|always|please|you|it|just|also|now|yourself|instead|first|can|could|simply|go ahead and)\s+)"
_ADV = r"(?:(?:just|always|also|then|now|directly|immediately|automatically|yourself|straight|quickly|simply)\s+)*"
_NOT_ME = r"(?!\s+(?:it\s+|them\s+|this\s+|that\s+)?(?:to\s+)?(?:me|the user|you)\b)"
_V = r"(?:s|es|ed|ing)?\b"
_DESTRUCTIVE_OBJ = (r"(?:branch(?:es)?|worktrees?|files?|folders?|director(?:y|ies)|dirs?|databases?|db|tables?|data|records|"
                    r"rows|buckets?|repos?|repositor(?:y|ies)|backups?|history|volumes?|instances?|stash(?:es)?|tags?|"
                    r"releases?|commits?|users?|accounts?|emails?|messages?|trash|snapshots?|containers?|clusters?|"
                    r"deployments?|environments?|secrets?|keys?)")
_TARGET = r"(?:thread|email|mail|issue|pr|pull request|ticket|review|comment|client|customer|channel|team|group|slack|discord|reviewer|stakeholder|vendor|recruiter|teammate|message|dm|post|tweet|list|chat)s?"
ACTION_PATTERNS = [
    ("outward", _LEAD + _ADV + r"(?:e-?mail|dm|text|ping|message|notify|notifie|tweet|forward|cc|bcc|slack)(?:s|es|ed|ing)?\b" + _NOT_ME),
    ("outward", _LEAD + _ADV + r"(?:send|sends|sent|sending)\b" + _NOT_ME),
    ("outward", _LEAD + _ADV + r"(?:post|posts|posted|posting)\b(?!-)(?!\s+(?:it\s+|them\s+)?(?:here|back|to me|in (?:the )?chat)\b)"),
    ("outward", r"\b(?:publish|announce|broadcast)(?:es|ed|ing|s)?\b"),
    ("outward", _LEAD + _ADV + r"(?:share|shares|shared|sharing)\b(?=[^,.;]{0,40}\b(?:with|to|on|publicly|externally|link)\b)(?![^,.;]{0,40}\bwith (?:me|the user)\b)"),
    ("outward", r"\b(?:reply|replies|replied|replying|respond(?:s|ed|ing)?|comment(?:s|ed|ing)?)\b[^,.;]{0,30}\b(?:to|on|in)\s+(?:the |a |an |their |that |this |every |each )?" + _TARGET + r"\b"),
    ("destructive", r"\b(?:delete[sd]?|deleting|remove[sd]?|removing|drop(?:s|ped|ping)?|erase[sd]?|erasing|destroy(?:s|ed|ing)?|prune[sd]?|pruning|clean(?:s|ed|ing)? up|clear(?:s|ed|ing)?|rm)\b[^,.;]{0,40}\b" + _DESTRUCTIVE_OBJ + r"\b"),
    ("destructive", r"\b(?:wipe[sd]?|wiping|purge[sd]?|purging|nuke[sd]?|nuking|tear(?:s|ing)? down|tore down|teardown|truncat(?:e|es|ed|ing))\b"),
    ("destructive", r"\brm\s+-[a-z]*[rf]|\bgit\s+clean\s+-[a-z]*f|\breset\s+--hard\b|\bforce[- ]?push|\bpush\w*\s+(?:-f\b|--force)"),
    ("destructive", r"\bpush(?:es|ed|ing)?\b[^,.;]{0,30}\b(?:to|into|on|onto)\s+(?:the\s+)?(?:origin[/ ])?(?:main|master|trunk|prod|production|release)\b"),
    ("destructive", r"\bmerg(?:e|es|ed|ing)\b(?!\s+(?:origin/)?(?:main|master)\s+into)[^,.;]{0,40}\b(?:main|master|trunk|prod|production|pr|prs|pull requests?)\b"),
    ("destructive", r"\b(?:deploy(?:s|ed|ing|ment)?|ship(?:s|ped|ping)?|releas(?:e|es|ed|ing)(?!\s+notes?)|roll(?:s|ed|ing)? out|promot(?:e|es|ed|ing))\b[^,.;]{0,40}\b(?:prod|production|live|customers|users|main|master)\b"),
    ("spend", r"\b(?:buy(?:s|ing)?|bought|purchas(?:e|es|ed|ing)|subscrib(?:e|es|ed|ing)|pay(?:s|ing)?|paid for|spend(?:s|ing)?|spent)\b(?!\s+(?:attention|off|more time|less time|time|some time|extra time|effort|a day|an hour|hours|minutes)\b)"),
    ("spend", r"\bupgrad(?:e|es|ed|ing)\b[^,.;]{0,30}\b(?:plan|tier|subscription|seats?|license|pro|premium|paid|enterprise)\b"),
    ("spend", r"\b(?:provision(?:s|ed|ing)?|spin(?:s|ning)? up|spun up|rent(?:s|ed|ing)?|launch(?:es|ed|ing)?|scal(?:e|es|ed|ing) up)\b[^,.;]{0,30}\b(?:gpus?|instances?|vms?|clusters?|nodes?|droplets?|ec2|servers?|machines?)\b"),
    ("permission", r"\b(?:grant|give|invite)(?:s|ed|ing)?\b[^,.;]{0,30}\b(?:access|admin|owner|collaborator|role|permissions?)\b"),
    ("permission", r"--dangerously|bypass ?permissions|skip-permissions|--yolo|full[- ]auto"),
]
_ACTION_RE = [(cat, re.compile(p, re.I)) for cat, p in ACTION_PATTERNS]
_NEGATE = re.compile(r"\b(?:never|don'?t|do not|doesn'?t|does not|didn'?t|did not|no|not|avoid\w*|stop\w*|instead of|rather than|"
                     r"without|refus\w*|draft\w*|propos\w*|suggest\w*|recommend\w*|offer(?:s|ed|ing)? to|prepar\w*|ask(?:s|ed)? "
                     r"(?:whether|if|before))\b", re.I)
_BREAK = re.compile(r"\b(?:and|then|but|so|&)\b|&", re.I)
_GATE = re.compile(
    r"\b(?:ask(?:s|ed|ing)?\s+(?:me|him|her|them|the user|first|before|for (?:a |an |the |my )?(?:go|go-ahead|yes|ok|okay|approval|sign[- ]?off))"
    r"|confirm(?:s|ed|ing)?\s+(?:first|before|with)|check(?:s|ed|ing)?\s+with\s+(?:me|him|her|them|the user)"
    r"|(?:after|once|when|until)\s+(?:a|an|my|your|his|her|their|the user'?s?|explicit)\s+(?:look|review|yes|ok|okay|go[- ]ahead|go|approval|sign[- ]?off|confirmation|consent)"
    r"|(?:after|once|when|until|unless)\s+(?:i|he|she|they|the user)\s+(?:has\s+|have\s+)?(?:approve[sd]?|confirm(?:s|ed)?|sa(?:y|ys|id)|look(?:s|ed)?|ok(?:s|ays|ayed)?|sign(?:s|ed)? off)"
    r"|only\s+(?:when|if|after)\s+(?:asked|told|approved|confirmed)"
    r"|with\s+(?:a|an|my|his|her|their|the user'?s|explicit)\s+(?:yes|ok|okay|approval|go[- ]ahead|sign[- ]?off|consent)"
    r"|for\s+(?:my|his|her|their|the user'?s)\s+(?:review|approval|ok|okay|sign[- ]?off)"
    r"|(?:requires?|needs?|wait(?:s|ed|ing)?\s+for)\s+(?:a |an |my |his |her |their |the user'?s |explicit )?(?:yes|ok|okay|approval|go[- ]ahead|sign[- ]?off|confirmation))\b",
    re.I)
_SENTENCE = re.compile(r"[.!?;\n]+(?:\s+|$)")
_CLAUSE_START = re.compile(r"[,:;(]|\bbut\b", re.I)
_LOCAL = re.compile(r"\b(?:local(?:ly|host)?|dev server|docker)\b", re.I)


def _excused(sentence, start):
    """True when the action starting at `start` is negated or proposed earlier in its clause (no `and`/`then` in
    between), or the sentence gates it on the user's yes."""
    if _GATE.search(sentence):
        return True
    cut = max([m.end() for m in _CLAUSE_START.finditer(sentence, 0, start)] or [0])
    before = sentence[cut:start]
    for m in _NEGATE.finditer(before):
        if not _BREAK.search(before[m.end():]):
            return True
    return False


def action_hit(text):
    """(category, matched text) for the first outward-facing, destructive, spend or permission action in `text` that is
    not negated or gated, else None. Quoted text is the user's evidence and is not checked."""
    probe = _QUOTED.sub('""', text or "")
    for sentence in _SENTENCE.split(probe):
        s = sentence.strip()
        if not s:
            continue
        for cat, pat in _ACTION_RE:
            for m in pat.finditer(s):
                if cat == "spend" and _LOCAL.search(s):
                    continue
                # the lead (start, comma, "and", "to") is part of the match: the action begins after it
                lead = re.match(_LEAD + _ADV, m.group(0), re.I)
                begin = m.start() + (lead.end() if lead else 0)
                if not _excused(s, begin):
                    return cat, m.group(0).strip(" ,;:(*>-")
    return None


def authorizes(text, actions=False):
    """The reason `text` reads as an authorization, or None. Quoted text is the user's evidence and is not checked.
    With `actions`, any outward-facing, destructive, spend or permission action counts too (action_hit)."""
    probe = _QUOTED.sub('""', text or "")
    for pat in _lint._FORBIDDEN_RE:
        m = pat.search(probe)
        if m and not _lint._is_rewording(probe):
            return "forbidden rule %r" % m.group(0)
    phrase = _grants.grant_phrase(probe) if _grants.has_grant_phrase(probe) else None
    if phrase:
        return "grant phrase %r" % phrase
    for pat in _AUTHORIZE_RE:
        m = pat.search(probe)
        if m:
            return "authorizes an action %r" % m.group(0)
    if actions:
        hit = action_hit(probe)
        if hit:
            return "a %s action %r" % hit
    return None


def instruction_text(lesson):
    """The parts of a lesson an agent could act on: the better alternative, the request kind and the situation, and for
    a good outcome the pattern and its detail too (they are what to keep). The pattern and outcome of a bad lesson
    describe what went wrong ("deployed without a staging check") and are not instructions."""
    keys = ["request_kind", "situation", "better_alternative"]
    if lesson.get("outcome") == "good":
        keys += ["pattern", "outcome_detail"]
    return "\n".join(str(lesson.get(k) or "") for k in keys)


def action_text(lesson):
    """The parts checked for actions whatever the qualifier: the better way, and for a good outcome what to keep."""
    keys = ["better_alternative"] + (["pattern"] if lesson.get("outcome") == "good" else [])
    return "\n".join(str(lesson.get(k) or "") for k in keys)


def lesson_authorizes(lesson):
    for line in instruction_text(lesson).splitlines():
        why = authorizes(line)
        if why:
            return why
    for line in action_text(lesson).splitlines():
        hit = action_hit(line)
        if hit:
            return "a %s action %r" % hit
    return None


# ---------------------------------------------------------------- verify

def corpus_index(rows):
    """What verification reads from the corpus, built once:
    typed: [(row, normalized text)] for typed records; typed_by_session: {session: [typed texts]}; agent: {session:
    [normalized heads]}; agent_all: every head joined; text_all: every record's text (any origin) joined; order:
    {session: [row keys in corpus order]}; rows: {key: row}."""
    typed, agent, heads, texts, order, by_key, by_session = [], {}, [], [], {}, {}, {}
    for i, r in enumerate(rows):
        key = r.get("key") or "#%d" % i
        by_key[key] = r
        s = r.get("session")
        order.setdefault(s, []).append(key)
        if r.get("text"):
            texts.append(_norm(r.get("text")))
            if (r.get("origin") or "typed") == "typed":
                typed.append((dict(r, key=key), _norm(r.get("text"))))
                by_session.setdefault(s, []).append(r.get("text"))
        ctx = r.get("agent_context")
        if isinstance(ctx, dict) and ctx.get("head"):
            h = _norm(ctx["head"]).replace(" ...", "")
            agent.setdefault(s, []).append(h)
            heads.append(h)
    return {"typed": typed, "agent": agent, "agent_all": "\n".join(heads), "text_all": "\n".join(texts),
            "order": order, "rows": by_key, "typed_by_session": by_session}


def agent_only(quote, idx):
    """True when `quote` occurs in a stored agent-context head and in no corpus message: the agent's words, never the
    user's, whatever the miner labelled them."""
    q = _norm(quote)
    return bool(q) and q in idx["agent_all"] and q not in idx["text_all"]


def substantive(quote):
    q = _norm(quote)
    return len(q) >= MIN_QUOTE_CHARS or len(_content(q)) >= MIN_QUOTE_WORDS


def same_session(row_session, cited):
    """True when the miner's cited session names the row's session: equal, or one ends with the other (a miner may cite
    a long rollout id by its tail), at least four characters long."""
    a, b = str(row_session or ""), str(cited or "").strip()
    if not a or not b:
        return False
    return a == b or (len(b) >= 4 and a.endswith(b)) or (len(a) >= 4 and b.endswith(a))


def _pick(rows, time):
    """The matching row the miner meant: the one with its time when several match, else the first."""
    if isinstance(time, str) and time:
        for r in rows:
            if isinstance(r.get("time"), str) and r["time"][:16] == time[:16]:
                return r
    return rows[0]


def marker_rows(keys, idx):
    """{row key: failure markers} for the quoted messages and the messages next to them in their session: the agent
    turn before a quoted message and the one after it (what the user's message reacted to, what followed)."""
    out = {}
    for key in keys:
        r = idx["rows"].get(key)
        if r is None:
            continue
        seq = idx["order"].get(r.get("session")) or []
        if key not in seq:
            continue
        i = seq.index(key)
        for k in seq[max(0, i - 1):i + 2]:
            ctx = idx["rows"][k].get("agent_context")
            ms = [m for m in ((ctx or {}).get("markers") or []) if m in FAILURE_MARKERS] if isinstance(ctx, dict) else []
            if ms:
                out[k] = ms
    return out


_REVERT_CUE = re.compile(r"\b(?:revert\w*|undo|undid|roll(?:ed|ing)? (?:it |that |this )?back|rollback|restore\w*)\b", re.I)
_REPEAT_CUE = re.compile(r"\b(?:again|still|keeps?|every time|another|third time|second time)\b", re.I)


def _checked_signals(lesson, user_texts, markers):
    """The miner's signals with the two that markers can check kept only when backed: `reverted` by a revert marker or
    the user's own words, `repeated_failure` by two failure markers or the user's own words."""
    flat = [m for ms in markers.values() for m in ms]
    words = " ".join(user_texts)
    out, dropped = [], []
    for s in lesson.get("signals") or []:
        if s == "reverted" and "reverted" not in flat and not _REVERT_CUE.search(words):
            dropped.append(s)
        elif s == "repeated_failure" and len(markers) < 2 and not _REPEAT_CUE.search(words):
            dropped.append(s)
        else:
            out.append(s)
    return out, dropped


def verify(lesson, idx):
    """(lesson copy with verified evidence, None) or (None, reason). User quotes must be substantive and occur in a
    typed message of the session they cite; agent quotes in a stored agent context of a session with a verified user
    quote, and only beside it."""
    users, pending_agents, agents, rows = [], [], [], []
    relabelled = short = elsewhere = 0
    for e in lesson.get("evidence") or []:
        q = _norm(e.get("quote"))
        if not q:
            continue
        role = e.get("role") or ("agent_context" if e.get("origin") == "agent_context" else "user")
        if role == "user" and e.get("origin", "typed") != "agent_context":
            hits = [r for r, t in idx["typed"] if q in t]
            if not hits:
                if q in idx["agent_all"]:
                    relabelled += 1  # the agent's words presented as the user's: never the user's voice
                continue
            if not substantive(q):
                short += 1
                continue
            same = [r for r in hits if same_session(r.get("session"), e.get("session"))]
            if not same:
                elsewhere += 1
                continue
            row = _pick(same, e.get("time"))
            users.append(dict(e, role="user", origin="typed", session=row.get("session"), time=row.get("time")))
            rows.append(row)
            continue
        pending_agents.append((q, e))
    if not users:
        if relabelled:
            return None, "its only user quote was the agent's text"
        if short:
            return None, "its user quotes are too short to anchor a lesson"
        if elsewhere:
            return None, "its user quote is not from the session it cites"
        return None, "no user quote found in a typed message"
    sessions = {u["session"] for u in users}
    for q, e in pending_agents:
        s = next((s for s in sessions if same_session(s, e.get("session"))), None)
        if len(agents) < AGENT_QUOTES and s is not None and any(q in h for h in idx["agent"].get(s, [])):
            agents.append(dict(e, role="agent_context", origin="agent_context", session=s))
    why = lesson_authorizes(lesson)
    if why:
        return None, "the lesson reads as an authorization (%s)" % why
    # the user's voice in the quoted sessions against the agent's: a better way whose words the agent said and the user
    # never typed there (beyond the request kind's own topic words) was copied from the agent context
    better = tokens(lesson.get("better_alternative"))
    user_tokens = set().union(*(tokens(x) for s in sessions for x in idx["typed_by_session"].get(s, [])))
    agent_tokens = set().union(*(tokens(h) for s in sessions for h in idx["agent"].get(s, [])))
    from_agent = (better & agent_tokens) - user_tokens - tokens(lesson.get("request_kind"))
    if len(from_agent) >= 2 and len(from_agent) > len(better & user_tokens):
        return None, "its better way comes from the agent's text, not the user's (%s)" % ", ".join(sorted(from_agent)[:5])
    markers = marker_rows([r["key"] for r in rows], idx)
    signals, unbacked = _checked_signals(lesson, [r.get("text") or "" for r in rows], markers)
    out = dict(lesson)
    out["evidence"] = users + agents
    out["signals"] = signals
    out["_markers"] = markers
    if unbacked:
        out["_unbacked_signals"] = unbacked
    return out, None


# ---------------------------------------------------------------- consolidate

def _newest(evidence):
    ts = [e.get("time") for e in evidence if e.get("role") == "user" and isinstance(e.get("time"), str)]
    return max(ts) if ts else ""


def _same_lesson(a, b):
    """Merge score of two verified lessons: normalized-token similarity of request kind plus better way, or of request
    kind plus pattern, whichever is higher."""
    kind_a, kind_b = a.get("request_kind") or "", b.get("request_kind") or ""
    return max(similarity(kind_a + " " + (a.get("better_alternative") or ""), kind_b + " " + (b.get("better_alternative") or "")),
               similarity(kind_a + " " + (a.get("pattern") or ""), kind_b + " " + (b.get("pattern") or "")))


def consolidate(chunk_items, rows, log=print, idx=None):
    """chunk_items: [(chunk id, lens, raw lesson)]. Returns (person lessons, project lessons, report).

    Each returned lesson carries `sessions` (distinct verified sessions), `episodes` (those plus at most one unquoted
    episode per chunk from the miner's count), `markers` (messages beside the quotes with a failure marker), `chunks`,
    `strength` (strong at three sessions, moderate at two or at one with two failure markers, else weak), the newest
    wording, the newest adoption verdict, the highest cost, up to USER_QUOTES user quotes (newest kept) and at most one
    agent-context quote."""
    idx = idx or corpus_index(rows)
    report = {"mined": len(chunk_items), "dropped": [], "merged": 0, "single_dropped": 0, "conflicts": 0,
              "signals_unbacked": 0}
    verified = []
    for cid, lens, raw in chunk_items:
        got, why = verify(raw, idx)
        if got is None:
            report["dropped"].append({"chunk": cid, "lens": lens, "pattern": (raw.get("pattern") or "")[:80], "reason": why})
            log("lessons: dropped %r from %s: %s" % ((raw.get("pattern") or "")[:50], cid, why))
            continue
        report["signals_unbacked"] += len(got.pop("_unbacked_signals", []))
        got["_chunk"] = cid
        verified.append(got)
    groups = []
    for it in verified:
        target = None
        for g in groups:
            if g[0].get("outcome") != it.get("outcome") or g[0].get("scope") != it.get("scope"):
                continue
            if max(_same_lesson(o, it) for o in g) >= MERGE_SIMILARITY:
                target = g
                break
        if target is None:
            groups.append([it])
        else:
            target.append(it)
            report["merged"] += 1
    merged = [_fold(g) for g in groups]
    merged = _resolve_conflicts(merged, report)
    person, project = [], []
    for m in merged:
        if m["episodes"] < 2 and m.get("cost") != "high" and m["markers"] < 2:
            report["single_dropped"] += 1
            continue
        (project if m.get("scope") == "project" else person).append(m)
    person.sort(key=lambda m: (-STRENGTHS[::-1].index(m["strength"]), -m["episodes"], m.get("outcome") != "bad", m["request_kind"]))
    report["kept"] = len(person)
    report["project"] = len(project)
    return person, project, report


def _fold(items):
    items = sorted(items, key=lambda x: _newest(x.get("evidence") or []))
    newest = items[-1]
    out = {k: newest.get(k) for k in ("request_kind", "situation", "pattern", "outcome", "outcome_detail",
                                      "better_alternative", "scope")}
    sessions, by_chunk, markers = set(), {}, {}
    for it in items:
        c = by_chunk.setdefault(it["_chunk"], {"count": 0, "sessions": set()})
        c["count"] = max(c["count"], int(it.get("count") or 1))
        c["sessions"] |= {e.get("session") for e in it.get("evidence") or [] if e.get("role") == "user"}
        markers.update(it.get("_markers") or {})
    extras = {}
    for c in by_chunk.values():
        sessions |= c["sessions"]
        # the miner's count is unverified: at most one unquoted episode per chunk, and two chunks that quote the same
        # sessions may be counting the same one, so it is counted once
        key = frozenset(c["sessions"])
        extras[key] = max(extras.get(key, 0), min(1, c["count"] - len(c["sessions"])))
    out["sessions"] = len(sessions)
    out["episodes"] = len(sessions) + sum(max(0, v) for v in extras.values())
    out["markers"] = len(markers) if out.get("outcome") == "bad" else 0
    out["chunks"] = sorted(by_chunk)
    level = 3 if len(sessions) >= 3 else 2 if len(sessions) == 2 else 1
    if level == 1 and out["markers"] >= 2:
        level = 2  # repeated deterministic failures beside the quoted messages: evidence nobody can inject
    out["strength"] = {3: "strong", 2: "moderate", 1: "weak"}[level]
    ranks = {"low": 0, "medium": 1, "high": 2}
    out["cost"] = max((it.get("cost") or "low" for it in items), key=lambda c: ranks.get(c, 0))
    out["adopted"] = "unclear"
    for it in items:  # newest verdict that says something wins
        if it.get("adopted") in ("yes", "no"):
            out["adopted"] = it["adopted"]
    signals = []
    for it in items:
        for s in it.get("signals") or []:
            if s not in signals:
                signals.append(s)
    out["signals"] = signals
    users, agents, seen = [], [], set()
    for it in reversed(items):
        for e in it.get("evidence") or []:
            key = (e.get("role"), _norm(e.get("quote")))
            if key in seen:
                continue
            seen.add(key)
            (users if e.get("role") == "user" else agents).append(e)
    users = sorted(users, key=lambda e: e.get("time") or "")[-USER_QUOTES:]
    out["evidence"] = users + agents[:AGENT_QUOTES]
    out["newest"] = _newest(out["evidence"])
    return out


def _negations(text):
    return {w for w in _WORD.findall((text or "").lower()) if w in NEGATION_TOKENS}


def _conflict(good, bad):
    """True when a good and a bad lesson judge the same approach differently: their request kind plus pattern are
    similar, with the same negations ("only", "without", "never"), and the bad lesson's better way is not simply the
    good lesson's pattern (that is agreement: one lesson seen from both sides)."""
    kg, kb = good.get("request_kind") or "", bad.get("request_kind") or ""
    same = similarity(kg + " " + (good.get("pattern") or ""), kb + " " + (bad.get("pattern") or ""))
    if same < CONFLICT_SIMILARITY or _negations(good.get("pattern")) != _negations(bad.get("pattern")):
        return False
    agree = similarity(kg + " " + (good.get("pattern") or ""), kb + " " + (bad.get("better_alternative") or ""))
    return agree < same


def _resolve_conflicts(merged, report):
    """A good lesson and a bad lesson about the same approach: the one with newer evidence wins and is marked
    `changed` with the date of that evidence."""
    keep = list(merged)
    for a in merged:
        for b in merged:
            if a is b or a not in keep or b not in keep or a.get("outcome") == b.get("outcome") or a.get("scope") != b.get("scope"):
                continue
            good, bad = (a, b) if a.get("outcome") == "good" else (b, a)
            if not _conflict(good, bad):
                continue
            older, newer = (a, b) if (a.get("newest") or "") <= (b.get("newest") or "") else (b, a)
            keep.remove(older)
            newer["changed"] = (newer.get("newest") or "")[5:10] or None
            report["conflicts"] += 1
    return keep


# ---------------------------------------------------------------- render and parse

def _mmdd(t):
    return t[5:10] if isinstance(t, str) and len(t) >= 10 else None


def _sentence(text, limit):
    t = re.sub(r"\s+", " ", text or "").strip().rstrip(".")
    if len(t) > limit:
        cut = t[:limit]
        sp = cut.rfind(" ")
        t = (cut[:sp] if sp > limit * 0.6 else cut).rstrip(" ,;:") + " ..."
    return t


def _label(lesson):
    """The first clause of the better alternative, up to 60 characters at a word boundary."""
    text = re.split(r"[,;:.]", (lesson.get("better_alternative") or lesson.get("pattern") or "").strip())[0]
    words = re.sub(r"[^\w\s'-]", " ", text).split()
    out = []
    for w in words:
        if len(" ".join(out + [w])) > 60:
            break
        out.append(w)
    while out and out[-1].lower() in _STOP:
        out.pop()
    label = " ".join(out) or "Lesson"
    return label[0].upper() + label[1:]


def render_lesson(lesson, drops=()):
    """One bullet. User quotes only: agent context never reaches the model files."""
    good = lesson.get("outcome") == "good"
    tags = "[%s]" % lesson["strength"]
    if lesson.get("adopted") == "yes":
        tags += " [adopted]"
    elif lesson.get("adopted") == "no":
        tags += " [not adopted]"
    if lesson.get("changed"):
        tags += " (changed %s)" % lesson["changed"]
    body = "- **%s.** Did: %s. Outcome: %s, %s. %s %s. %s" % (
        _label(lesson), _sentence(lesson.get("pattern"), 140), "good" if good else "bad",
        _sentence(lesson.get("outcome_detail") or ("it worked" if good else "it backfired"), 120),
        "Keep:" if good else "Better:", _sentence(lesson.get("better_alternative"), 160), tags)
    quotes = []
    for e in lesson.get("evidence") or []:
        if e.get("role") != "user":
            continue
        q = re.sub(r"\s+", " ", e.get("quote") or "").strip().replace('"', "'")
        d = _mmdd(e.get("time"))
        quotes.append('"%s"%s' % (q, (" (%s)" % d) if d else ""))
    line = body + " " + "; ".join(quotes)
    while len(line) > LINE_MAX and len(quotes) > 1:
        quotes.pop(0)
        line = body + " " + "; ".join(quotes)
    if len(line) > LINE_MAX:
        line = line[:LINE_MAX - len(' ..."')].rstrip() + ' ..."'
    if any(d and d.lower() in line.lower() for d in drops):
        return None
    return line


def _keywords(group):
    """3 to 8 `When:` keywords: the request kinds (whole, when short, and their content words), topped up from the
    better alternatives and then the patterns when fewer than three."""
    words, stems = [], set()

    def add(w):
        if w and w not in words and _stem(w) not in stems:
            words.append(w)
            stems.add(_stem(w))

    for l in group:
        rk = _norm(l.get("request_kind"))
        if rk and len(rk.split()) <= 3:
            add(rk)
        for w in _WORD.findall(rk):
            if w not in _STOP and len(w) >= 3:
                add(w)
    for field in ("better_alternative", "pattern"):
        for l in group:
            for w in _WORD.findall((l.get(field) or "").lower()):
                if len(words) >= 3:
                    break
                if w not in _STOP and len(w) >= 3:
                    add(w)
    return (words + [w for w in ("fix", "build", "do") if w not in words])[:max(3, min(8, len(words)))]


def group_lessons(lessons):
    """Lessons grouped by request kind: a lesson joins the first group whose request kinds share enough content words."""
    groups = []
    for l in lessons:
        target = None
        for g in groups:
            if max(jaccard(l.get("request_kind"), o.get("request_kind")) for o in g) >= GROUP_JACCARD:
                target = g
                break
        if target is None:
            groups.append([l])
        else:
            target.append(l)
    return groups


def render_section(lessons, max_lessons=40, drops=()):
    """The `## What works and what doesn't` section (list of lines), or [] when no lesson survives."""
    chosen = list(lessons)[:max_lessons]
    groups = group_lessons(chosen)
    lines = [HEADING]
    n = 0
    for g in groups:
        bullets = [b for b in (render_lesson(l, drops) for l in g) if b]
        if not bullets:
            continue
        n += 1
        title = _sentence(g[0].get("request_kind") or "Requests", 60)
        lines.append("### W%d. %s" % (n, title[0].upper() + title[1:]))
        lines.append("When: " + ", ".join(_keywords(g)))
        lines += bullets
        lines.append("")
    return lines if n else []


def strip_section(text):
    """profile.md text without its What-works section (whatever wrote it)."""
    out, skip = [], False
    for line in text.splitlines():
        if line.startswith("## "):
            skip = line.strip() == HEADING
        if not skip:
            out.append(line)
    return "\n".join(out).rstrip("\n") + "\n"


def section_of(text):
    lines, out, keep = text.splitlines(), [], False
    for line in lines:
        if line.startswith("## "):
            keep = line.strip() == HEADING
        if keep:
            out.append(line)
    return out


def insert_section(text, section_lines):
    """Insert the section just before `## Vocabulary` (or at the end), replacing any earlier one."""
    base = strip_section(text).splitlines()
    if not section_lines:
        return "\n".join(base) + "\n"
    at = next((i for i, l in enumerate(base) if l.strip() == "## Vocabulary"), len(base))
    block = list(section_lines)
    if block[-1].strip():
        block.append("")
    if at > 0 and base[at - 1].strip():
        block = [""] + block
    return "\n".join(base[:at] + block + base[at:]).rstrip("\n") + "\n"


def parse_groups(lines, start, end, path, errors):
    """W groups between 0-based `start` (the heading line index + 1) and `end`. Errors with line numbers."""
    out = []
    heads = [(i + 1, GROUP_RE.match(lines[i])) for i in range(start, end) if lines[i].startswith("### ")]
    for k, (ln, m) in enumerate(heads):
        gend = (heads[k + 1][0] - 1) if k + 1 < len(heads) else end
        if not m:
            errors.append("%s:%d: lesson group heading must look like `### W1. Title`" % (path, ln))
            continue
        when = None
        bullets = 0
        for i in range(ln, gend):
            line = lines[i]
            if line.startswith("When:") and when is None:
                when = [w.strip().lower() for w in line[5:].split(",") if w.strip()]
            elif line.startswith("- "):
                bullets += 1
                if not re.search(r"\b(?:Better|Keep):", line):
                    errors.append("%s:%d: lesson needs a `Better:` (or `Keep:`) clause" % (path, i + 1))
                if not re.search(r"\[(?:strong|moderate|weak)\]", line):
                    errors.append("%s:%d: lesson needs a strength tag" % (path, i + 1))
                if not _QUOTED.search(line):
                    errors.append("%s:%d: lesson needs a quote from the user" % (path, i + 1))
        if when is None:
            errors.append("%s:%d: lesson group %s has no `When:` line" % (path, ln, m.group(1)))
            continue
        if not 3 <= len(when) <= 12:
            errors.append("%s:%d: lesson group %s `When:` needs 3 to 12 keywords, has %d" % (path, ln, m.group(1), len(when)))
        out.append({"id": m.group(1), "title": m.group(2), "when": when, "lines": [ln, gend],
                    "chars": sum(len(x) + 1 for x in lines[ln - 1:gend]), "count": bullets})
    return out


_BAD_DESCRIPTION = re.compile(r"Did: .*?(?= (?:Better|Keep): )")


def instruction_view(line):
    """A lesson bullet with the description of what went wrong removed: for a bad outcome the `Did: ... Outcome: ...`
    part is a negative example, not an instruction; for a good one everything stays (it is what to keep)."""
    if "Outcome: bad" in line:
        return _BAD_DESCRIPTION.sub("Did: (what went wrong).", line, count=1)
    return line


def lint_view(profile_text):
    """profile.md text as the forbidden-rule lint should read it: lesson bullets through instruction_view."""
    out, inside = [], False
    for line in (profile_text or "").splitlines():
        if line.startswith("## "):
            inside = line.strip() == HEADING
        out.append(instruction_view(line) if inside and line.startswith("- ") else line)
    return "\n".join(out)


def lesson_hits(profile_text, path="profile.md"):
    """Fatal lint hits: a line of the What-works section that reads as an authorization."""
    hits, inside = [], False
    for i, line in enumerate((profile_text or "").splitlines(), 1):
        if line.startswith("## "):
            inside = line.strip() == HEADING
            continue
        if inside and line.startswith("- "):
            why = authorizes(instruction_view(line), actions=True)
            if why:
                hits.append("%s:%d: a lesson can never authorize an action: %s" % (path, i, why))
    return hits


def upgrade_hits(examples_text, path="examples.md"):
    """Fatal lint hits in the upgrade pairs of examples.md (Tags include `upgrade`): the full prompt and the better
    side of the `Changed:` line take a lesson's better way, so they are checked like a lesson. The literal ask on the
    left of `->` is what the user typed and is not."""
    hits, cur = [], None
    lines = (examples_text or "").splitlines()

    def flush(block):
        if block and block["upgrade"]:
            for ln, text in block["check"]:
                why = authorizes(text, actions=True)
                if why:
                    hits.append("%s:%d: an upgrade pair can never authorize an action: %s" % (path, ln, why))

    for i, line in enumerate(lines, 1):
        if re.match(r"^#{2,3} ", line):
            flush(cur)
            cur = {"upgrade": False, "check": []} if re.match(r"^### \d+\. ", line) else None
        elif cur is None:
            continue
        elif line.startswith("Tags:"):
            cur["upgrade"] = "upgrade" in [t.strip().lower() for t in line[5:].split(",")]
        elif line.startswith(">"):
            cur["check"].append((i, line.lstrip("> ")))
        elif line.startswith("Changed:"):
            cur["check"].append((i, line.split("->", 1)[1] if "->" in line else line[8:]))
    flush(cur)
    return hits


def compact_for_prompt(lessons_lines):
    """The section text the examples prompt sees (user quotes only, already rendered)."""
    return "\n".join(lessons_lines).strip()
