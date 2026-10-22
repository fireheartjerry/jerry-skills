"""track: the correction tracker. How often does the user have to correct a mind-read in real use?

It finds every real invocation of the skill in the transcripts and classifies the user's next typed message:

- Claude Code (`<root>/<project>/<session>.jsonl`, the main thread only): a `Skill` tool_use whose skill is
  `mind-reading` (or `<plugin>:mind-reading`), a `skill_mention` attachment naming it, a `/mind-reading` slash command
  (`<command-name>`, the seed in `<command-args>`), or a typed message where the trigger rule fires (trigger.py:
  "read my mind", "mind-read this", "/mind-reading" inline).
- Codex (`<root>/**/*.jsonl`, interactive sessions only): the injected `<skill><name>mind-reading</name>` block, or a
  typed message where the trigger rule fires ("$mind-reading" included).
- The runtime's own logs: `model/decisions.jsonl` rows (one per expansion; joined to a transcript invocation by seed
  hash) and `model/corrections.md` lines the agent wrote with `correct` (joined by date and seed; an explicit
  correction always counts as one).

Every marker attaches to the triggering typed message, so one invocation seen through a phrase and a Skill call counts
once. Subagent, headless and SDK sessions, Codex exec sessions, and any transcript whose working directory lies inside
a mind-reading data home (engine runs) are skipped.

The next typed message in the same session (harness text, tool results, other slash commands and goal reminders are
skipped; an interrupt in between is noted) goes through RULES, a transparent ordered list: the first rule that matches
names the class and is stored as `rule`. RULES_VERSION changes whenever a rule changes, and every file is rescanned.
`track --engine` sends only the messages no rule matched (`U-default`) to a cheap model, in batches, under a cost cap;
its label is stored as `engine:<model>` and never overrides a rule match.

Stored (`track/invocations.jsonl`): ids, timestamps, markers, class, rule, model hash, seed hash. Never seed or message
text. A correction whose words state what was meant ("I meant the PDF export", "not the CSV, the PDF") becomes a draft
line in corrections.md marked `# pending <id> | ...`: a comment, so neither the runtime nor refresh applies it until it
is reviewed (delete the `# pending <id> | ` prefix to accept, the line to reject) or until the same correction repeats
`track.promote_after` times, when the tracker appends it as a normal line."""
import collections
import datetime as _dt
import glob
import hashlib
import json
import os
import re
from pathlib import Path

from . import agent_context as _ac
from . import filters, trigger
from .config import expand, read_json, read_jsonl, write_json, write_jsonl
from .corpus import now_iso
from .stats import rate_block

RULES_VERSION = 1
STATE_VERSION = 4  # 2: ids without the session; 3: one message is one turn; 4: time_reliable
CLASSES = ("correction", "refinement", "approval", "unrelated")
OTHER_CLASSES = ("no_reply", "unknown")
SKILL_NAME = "mind-reading"
_SKILL_RE = re.compile(r"^(?:[\w.-]+:)?mind-reading$")
_SLASH_RE = re.compile(r"<command-name>\s*/?(?:[\w.-]+:)?mind-reading\s*</command-name>")
_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.S)
_OTHER_COMMAND_RE = re.compile(r"<command-name>")
_CODEX_SKILL_RE = re.compile(r"^\s*<skill>\s*<name>\s*(?:[\w.-]+:)?mind-reading\s*</name>")
MARKERS = (b"mind-reading", b"read my mind", b"mind-read", b"mind read")
CLAUDE_USER = (b'"type":"user"', b'"type": "user"', b'"type":"attachment"', b'"type": "attachment"')
CLAUDE_ASSISTANT = (b'"type":"assistant"', b'"type": "assistant"')
CODEX_USER = (b'"role":"user"', b'"role": "user"')
CODEX_ABORT = b"turn_aborted"
CODEX_ASSISTANT = (b'"role":"assistant"', b'"role": "assistant"')
DEFERRED_MAX = 60
AGENT_HEAD_CHARS = 600

# ---------------------------------------------------------------- the classifier (versioned, ordered, transparent)

_APPROVAL_CORE = {
    "y", "ya", "yes", "yep", "yeah", "yup", "yea", "sure", "ok", "okay", "k", "kk", "good", "great", "nice", "perfect",
    "cool", "lgtm", "sgtm", "go", "proceed", "continue", "thanks", "thank", "ty", "thx", "awesome", "fine", "works",
    "correct", "exactly", "right", "bet", "approved", "agreed", "love", "ship", "sweet", "solid", "amazing",
    "excellent", "absolutely", "definitely", "do", "keep", "send", "run", "yessir", "worries", "problem", "beautiful",
    "goated", "fire", "w",
}
_APPROVAL_FILL = {
    "it", "ahead", "going", "sounds", "looks", "all", "of", "these", "them", "that", "this", "please", "pls", "plz",
    "defaults", "default", "recommended", "as", "is", "the", "with", "on", "no", "you", "so", "much", "very", "to", "me",
    "and", "just", "mate", "man", "bro", "dude", "now", "u", "ur", "a", "lot", "lol", "haha", "totally", "indeed",
    "really", "too", "good", "work", "job", "everything",
}
_WORD = re.compile(r"[a-z0-9']+")


def _approval_only(text):
    words = [w.strip("'") for w in _WORD.findall(text.lower())]
    if not words or len(words) > 12:
        return False
    if not any(w in _APPROVAL_CORE for w in words):
        return False
    return all(w in _APPROVAL_CORE or w in _APPROVAL_FILL for w in words)


_RULE_SPECS = [
    # (id, class, pattern). Evaluated in order; the first match wins. Patterns run on the lowercased message.
    ("A-held-answer", "approval", r"^\W*(\d+\s*[.):-]?\s*(yes|no|y|n|ok|okay|default|defaults|sure|skip|go)\b[\s,;.!]*)+$"),
    ("C-undo", "correction", r"^\W*(undo|revert|roll ?back|put (it|that|them) back)\b|\b(undo|revert|roll back) (that|this|it|those|these|everything|all)\b"),
    ("C-not-that", "correction", r"\bnot (that|this|what i (meant|asked|said|wanted))\b|\bthat'?s not (it|what|right|correct|the)\b|\bthats not\b|\bnot quite\b|\bnot exactly\b"),
    ("C-not-start", "correction", r"^\W*not (the|a|an|that|this|my|your|in|on|for)\b"),
    ("C-meant", "correction", r"\b(i|we) (meant|said|asked (for|you)|was asking|wanted you to|did ?n'?t (ask|say|mean|want))\b"),
    ("C-why", "correction", r"\bwhy (did|would|are|were|have|do) (you|u)\b|\bwhy'?d (you|u)\b|\bwhy (u|you) (did|do|are)\b"),
    ("C-wrong", "correction", r"\b(wrong|incorrect|misread|misunderst\w*)\b|\byou (missed|ignored|forgot|skipped|dropped)\b"),
    ("C-no", "correction", r"^\W*(no|nope|nah|nop)\b(?![\s,]*(worries|problem|prob|probs|rush|biggie|doubt)\b)"),
    ("C-wait", "correction", r"^\W*(wait|hold on|hold up|stop|halt|pause|hang on)\b"),
    ("C-actually", "correction", r"^\W*((ok|okay|oh|hmm|um|well|so)[,\s]+)?actually\b"),
    ("C-instead", "correction", r"\binstead\b|\brather than\b"),
    ("C-dont", "correction", r"^\W*(don'?t|dont|do not|never)\b(?!\s+forget)"),
    ("C-scratch", "correction", r"\b(scratch that|nvm|never ?mind|forget (that|it|this|about))\b"),
    ("C-too-much", "correction", r"\b(too (much|long|verbose|many)|overkill|over-?engineer\w*|did ?n'?t need|unnecessary|way more than)\b"),
    ("R-yes-but", "refinement", r"^\W*(ok|okay|yes|yeah|yep|sure|good|great|perfect|nice|cool)\b[\s,!.]*(but|and|also|just|plus)\b"),
    ("A-lead", "approval", r"^\W*(perfect|great|nice|good|cool|awesome|ok|okay|thanks|thx|ty|love it|lgtm|yes|yep|yeah)\b[\s,!.]*(now|next|then|so)\b"),
    ("A-only", "approval", None),  # every word is approval vocabulary (_approval_only)
    ("R-also", "refinement", r"^\W*(also|and|plus|oh and|btw|by the way|additionally|one more thing|another thing|ps|p\.s\.)\b|^\s*\+"),
    ("R-make-sure", "refinement", r"\b(make sure|don'?t forget|dont forget|remember to|be sure to|ensure)\b"),
    ("R-too", "refinement", r"\b(as well|too)\W*$"),
]
RULES = [(rid, cls, re.compile(pat) if pat else None) for rid, cls, pat in _RULE_SPECS]
DEFAULT_RULE = "U-default"
INTERRUPT_RULE = "C-interrupt"
NO_REPLY_RULE = "N-no-reply"


def classify_reply(text, interrupted=False):
    """(class, rule id) for the user's next typed message. An interrupt before a message no rule matched is a redirect."""
    low = (text or "").strip().lower()
    if not low:
        return "no_reply", NO_REPLY_RULE
    for rid, cls, rx in RULES:
        if rx is None:
            if _approval_only(low):
                return cls, rid
            continue
        if rx.search(low):
            return cls, rid
    if interrupted:
        return "correction", INTERRUPT_RULE
    return "unrelated", DEFAULT_RULE


_MEANT_SPECS = [
    ("M-meant", r"\b(?:i|we) (?:meant|mean|was asking for|asked for|wanted)\s+(?P<meant>[^.!?\n]{6,200})"),
    ("M-not-but", r"^\W*not (?P<wrong>[^,;.!?\n]{2,80})[,;]\s*(?:but\s+)?(?P<meant>[^.!?\n]{4,200})"),
    ("M-instead-of", r"\binstead of (?P<wrong>[^,;.!?\n]{2,80})[,;]?\s+(?P<meant>[^.!?\n]{4,200})"),
    ("M-instead", r"(?P<meant>\b(?:use|do|make it|go with|try)\s+[^.!?\n]{2,150}?)\s+instead\b"),
    ("M-no-do", r"^\W*(?:no|nope|nah)[,.!]*\s+(?P<meant>(?:use|do|make|it'?s|it should|just|only|keep|leave|put|run|build|"
                r"write|fix|i want|we want|go with)\b[^.!?\n]{4,200})"),
]
MEANT_RULES = [(rid, re.compile(p, re.I)) for rid, p in _MEANT_SPECS]


def extract_meant(text):
    """(meant, wrong, rule id) when a correction states what was meant in so many words, else None."""
    t = (text or "").strip()
    for rid, rx in MEANT_RULES:
        m = rx.search(t)
        if not m:
            continue
        meant = _clean_field(m.group("meant"))
        wrong = _clean_field(m.groupdict().get("wrong") or "") or "n/a"
        if len(meant) >= 8 and len(meant.split()) >= 2:
            return meant, wrong, rid
    return None


def _clean_field(s):
    s = re.sub(r"\s+", " ", (s or "").replace("|", "/").replace('"', "'")).strip(" ,;:-'")
    return s[:200]


# ---------------------------------------------------------------- hashing helpers (no text is stored)

def seed_hash(seed):
    """sha256(seed)[:8], the hash `log-decision` stores for a seed."""
    return hashlib.sha256((seed or "").encode("utf-8")).hexdigest()[:8]


def seed_key(seed):
    """A hash of the normalized first 60 characters, used to join corrections.md lines (whose seed field is the first 80
    characters with | and double quotes replaced) to invocations without keeping the text."""
    t = re.sub(r"[|\"'/`]", " ", (seed or "").lower())
    t = re.sub(r"\s+", " ", t).strip()[:60].strip()
    return hashlib.sha256(t.encode("utf-8")).hexdigest()[:10] if t else None


def inv_id(source, record_id):
    """Keyed on the record, not the session: a resumed or forked session replays earlier records (same ids) into a new
    session file, and the invocation they hold is still one invocation."""
    return hashlib.sha256(("%s\n%s" % (source, record_id)).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- scanning

def _home_markers(home):
    """Encoded forms of a data home as Claude Code names project directories after their working directory."""
    out = set()
    for h in {str(home), str(Path(home).resolve())}:
        out.add(re.sub(r"[/.]", "-", h))
    return out


_ENGINE_DIR = re.compile(r"mind-reading[\w-]*-(eval|runs)-|-eval-[0-9a-f]{8}-runs-")


def skip_claude_project(dirname, home_marks):
    """A project directory whose name encodes a working directory inside a data home (engine runs, held-out material)."""
    return any(m and m in dirname for m in home_marks) or bool(_ENGINE_DIR.search(dirname))


def _inside_home(cwd, home):
    if not cwd or not isinstance(cwd, str):
        return False
    try:
        c = str(Path(cwd).resolve())
    except OSError:
        c = cwd
    h = str(Path(home).resolve())
    return c == h or c.startswith(h + os.sep) or bool(re.search(r"mind-reading[^/]*/(eval|runs)/", c))


class _Scan:
    """Per-file state shared by both readers: the triggering message, invocations waiting for a reply, the agent's
    reply head since the trigger, interrupts."""

    def __init__(self, source, session, want_text):
        self.source, self.session, self.want_text = source, session, want_text
        self.invs = []
        self.awaiting = []
        self.last = None  # {"rid", "time", "text", "inv"}

    def human(self, rid, time, text, slash_seed=None):
        """A typed human message: the reply to whatever waits, then maybe a new invocation."""
        reply_text = slash_seed if slash_seed is not None else text
        for inv in self.awaiting:
            inv["_next"] = reply_text
            inv["next_time"] = time
        self.awaiting = []
        h = {"rid": rid, "time": time, "text": reply_text, "inv": None}
        if slash_seed is not None:
            h["inv"] = self._new(h, "slash", slash_seed)
        else:
            res = trigger.classify(text)
            if res["fire"]:
                h["inv"] = self._new(h, "phrase", res["seed"])
        self.last = h

    def marker(self, kind, time=None):
        """A Skill call, a skill mention or a Codex skill block: attach to the triggering message."""
        h = self.last
        if h is None:
            h = {"rid": "marker:%s:%s" % (kind, time), "time": time, "text": "", "inv": None}
            self.last = h
        if h["inv"] is None:
            h["inv"] = self._new(h, kind, h["text"])
        elif kind not in h["inv"]["markers"]:
            h["inv"]["markers"].append(kind)

    def interrupt(self):
        for inv in self.awaiting:
            inv["interrupted"] = True

    def agent_text(self, text):
        if not self.want_text or not text:
            return
        for inv in self.awaiting:
            if not inv.get("_agent"):
                inv["_agent"] = text[:AGENT_HEAD_CHARS]

    def _new(self, h, kind, seed):
        inv = {"id": inv_id(self.source, h["rid"]), "source": self.source, "session": self.session,
               "record_id": h["rid"], "time": h["time"], "next_time": None, "markers": [kind], "interrupted": False,
               "seed_hash": seed_hash(seed) if seed else None, "seed_key": seed_key(seed), "_seed": seed or "",
               "_next": None, "_agent": None}
        self.invs.append(inv)
        self.awaiting.append(inv)
        return inv


def _raw_texts(r):
    content = (r.get("message") or {}).get("content")
    if isinstance(content, str):
        return [content]
    return [x.get("text", "") or "" for x in content or [] if isinstance(x, dict) and x.get("type") == "text"]


def _claude_texts(r):
    """What a Claude Code user or attachment record holds for the tracker, as (kind, payload) pairs: ("mention", None)
    for a skill_mention of this skill, ("interrupt", None), ("slash", seed) for the /mind-reading command, ("text",
    typed text) after the corpus reader's filters, ("skip", None)."""
    from .extract_claude import _attachment_record, _user_record
    if r.get("type") == "attachment":
        a = r.get("attachment") or {}
        if a.get("type") == "skill_mention" and _SKILL_RE.match(str(a.get("skillName") or "")):
            return [("mention", None)]
        got = _attachment_record(r)
        return _typed_pairs(got[0] if got else [])
    if r.get("userType") not in (None, "external"):
        return [("skip", None)]
    # the slash command record is the user's own typing even when the harness marks it meta
    for t in _raw_texts(r):
        if _SLASH_RE.search(t):
            m = _ARGS_RE.search(t)
            return [("slash", (m.group(1).strip() if m else ""))]
    out = []
    tur = r.get("toolUseResult")
    if isinstance(tur, dict) and tur.get("interrupted"):
        out.append(("interrupt", None))
    content = (r.get("message") or {}).get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "tool_result":
                body = item.get("content")
                if not isinstance(body, str):
                    body = " ".join(x.get("text", "") for x in (body or []) if isinstance(x, dict))
                if _ac.REJECTED_TOOL in (body or ""):
                    out.append(("interrupt", None))
    texts = _user_record(r)
    if texts is None:
        return out or [("skip", None)]
    out += [x for x in _typed_pairs(texts) if x[0] != "skip"]
    return out or [("skip", None)]


def _typed_pairs(texts):
    out = []
    for t in texts or []:
        if not isinstance(t, str):
            continue
        if _SLASH_RE.search(t):
            m = _ARGS_RE.search(t)
            out.append(("slash", (m.group(1).strip() if m else "")))
            continue
        if _OTHER_COMMAND_RE.search(t):
            continue  # another slash command is not a reply
        if _ac.is_interrupt(t):
            out.append(("interrupt", None))
            continue
        cleaned, _ = filters.clean(t)
        if filters.keep(cleaned):
            out.append(("text", cleaned))
    return out or [("skip", None)]


def _claude_assistant(r, scan):
    if r.get("isSidechain"):
        return
    content = (r.get("message") or {}).get("content")
    if isinstance(content, str):
        scan.agent_text(content)
        return
    for item in content or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "tool_use" and item.get("name") == "Skill":
            sk = str((item.get("input") or {}).get("skill") or "")
            if _SKILL_RE.match(sk.strip()):
                scan.marker("skill_tool", r.get("timestamp"))
        elif item.get("type") == "text":
            scan.agent_text(item.get("text") or "")


def _claude_user(r, scan, ordinal):
    """One record is one human turn: its text parts are joined, never a message and its own reply."""
    if r.get("isSidechain"):
        return
    time = r.get("timestamp")
    rid = r.get("uuid") or "%s:%d" % (time, ordinal)
    texts, markers = [], []
    for kind, payload in _claude_texts(r):
        if kind == "mention":
            markers.append("skill_mention")
        elif kind == "interrupt":
            scan.interrupt()
        elif kind == "slash":
            scan.human(rid, time, payload, slash_seed=payload)
        elif kind == "text":
            texts.append(payload)
    if texts:
        scan.human(rid, time, "\n\n".join(texts))
    for m in markers:
        scan.marker(m, time)


def scan_claude_file(path, want_text=False):
    """Invocations in one Claude Code session file (empty when the file has no marker or is an SDK run)."""
    session = Path(path).stem
    scan = _Scan("claude-code", session, want_text)
    deferred = collections.deque(maxlen=DEFERRED_MAX)
    with open(path, "rb") as fh:
        for ordinal, line in enumerate(fh, 1):
            low = line.lower()
            marked = any(m in low for m in MARKERS)
            is_user = any(m in line for m in CLAUDE_USER)
            is_asst = not is_user and any(m in line for m in CLAUDE_ASSISTANT)
            if not marked:
                if is_user:
                    if scan.awaiting:
                        _parse_claude(line, scan, ordinal)
                    elif b'"tool_use_id"' not in line:
                        deferred.append((ordinal, line))
                elif is_asst and scan.awaiting and want_text:
                    _parse_claude(line, scan, ordinal)
                continue
            r = _loads(line)
            if r is None:
                continue
            if _sdk(r):
                return []
            if r.get("type") == "assistant" and _has_skill_call(r) and deferred:
                _drain(deferred, scan, _parse_claude)
            elif r.get("type") == "attachment" and deferred:
                _drain(deferred, scan, _parse_claude)
            elif r.get("type") == "user" and deferred:
                _drain(deferred, scan, _parse_claude)
            _dispatch_claude(r, scan, ordinal)
    return scan.invs


def _has_skill_call(r):
    for item in (r.get("message") or {}).get("content") or []:
        if isinstance(item, dict) and item.get("type") == "tool_use" and item.get("name") == "Skill":
            return True
    return False


def _sdk(r):
    ep = r.get("entrypoint") or ""
    return isinstance(ep, str) and ep.startswith("sdk")


def _drain(deferred, scan, parse):
    """Find the newest typed message among the deferred lines (they hold no marker, so none can start an invocation,
    and nothing was waiting, so none is a reply) and make it the trigger candidate."""
    items = list(deferred)
    deferred.clear()
    for ordinal, line in reversed(items):
        probe = _Scan(scan.source, scan.session, False)
        parse(line, probe, ordinal)
        if probe.last is not None:
            scan.last = {"rid": probe.last["rid"], "time": probe.last["time"], "text": probe.last["text"], "inv": None}
            return


def _parse_claude(line, scan, ordinal):
    r = _loads(line)
    if r is not None and not _sdk(r):
        _dispatch_claude(r, scan, ordinal)


def _dispatch_claude(r, scan, ordinal):
    t = r.get("type")
    if t == "assistant":
        _claude_assistant(r, scan)
    elif t in ("user", "attachment"):
        _claude_user(r, scan, ordinal)


def _loads(line):
    try:
        r = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return r if isinstance(r, dict) else None


def _codex_line(r, scan, ordinal):
    from .extract_codex import GOAL_OPEN, QUESTION_OPEN, _unwrap_question
    t = r.get("type")
    p = r.get("payload") or {}
    if t == "event_msg":
        if p.get("type") == "turn_aborted":
            scan.interrupt()
        return
    if t != "response_item" or p.get("type") != "message":
        return
    role = p.get("role")
    if role == "assistant":
        scan.agent_text("\n".join(c.get("text", "") or "" for c in (p.get("content") or [])
                                  if isinstance(c, dict) and c.get("type") in ("output_text", "text")))
        return
    if role != "user":
        return
    time = r.get("timestamp")
    texts, markers, first = [], [], None
    for item in p.get("content") or []:
        if not isinstance(item, dict) or item.get("type") != "input_text":
            continue
        text = item.get("text", "") or ""
        if first is None:
            first = text
        if _CODEX_SKILL_RE.match(text):
            markers.append("codex_skill")
            continue
        if _ac.is_interrupt(text) or text.lstrip().startswith("<turn_aborted>"):
            scan.interrupt()
            continue
        if GOAL_OPEN.match(text):
            continue
        if text.lstrip().startswith(QUESTION_OPEN):
            got = _unwrap_question(text)
            if got:
                texts.append(got[0])
            continue
        cleaned, _ = filters.clean(text)
        if filters.keep(cleaned):
            texts.append(cleaned)
    # one message is one human turn: its input_text items are joined, never a message and its own reply
    if texts:
        rid = p.get("id") or "%s:%s" % (time, hashlib.sha256((first or "").encode("utf-8")).hexdigest()[:12])
        scan.human(rid, time, "\n\n".join(texts))
    for m in markers:
        scan.marker(m, time)


def _parse_codex(line, scan, ordinal):
    r = _loads(line)
    if r is not None:
        _codex_line(r, scan, ordinal)


def scan_codex_file(path, home=None, want_text=False):
    """Invocations in one Codex rollout (empty for exec and subagent sessions and engine runs inside a data home)."""
    from .extract_codex import _skip_file
    with open(path, "rb") as fh:
        head = _loads(fh.readline())
        if head is None:
            return []
        meta = head.get("payload") if isinstance(head.get("payload"), dict) else {}
        if _skip_file(meta) or (home and _inside_home(meta.get("cwd"), home)):
            return []
        scan = _Scan("codex", Path(path).stem, want_text)
        deferred = collections.deque(maxlen=DEFERRED_MAX)
        for ordinal, line in enumerate(fh, 2):
            low = line.lower()
            marked = any(m in low for m in MARKERS)
            if not marked:
                if any(m in line for m in CODEX_USER) or CODEX_ABORT in line:
                    if scan.awaiting:
                        _parse_codex(line, scan, ordinal)
                    else:
                        deferred.append((ordinal, line))
                elif want_text and scan.awaiting and any(m in line for m in CODEX_ASSISTANT):
                    _parse_codex(line, scan, ordinal)
                continue
            r = _loads(line)
            if r is None:
                continue
            if deferred and (r.get("payload") or {}).get("role") == "user":
                _drain(deferred, scan, _parse_codex)
            _codex_line(r, scan, ordinal)
    return scan.invs


# ---------------------------------------------------------------- sources

def sources(cfg, home, claude_dirs=None, codex_dirs=None):
    """(claude roots, codex roots): explicit dirs win; otherwise config.sources, track.*_dirs and, unless
    track.include_default_dirs is false, the standard local roots. Duplicates (by resolved path) are dropped."""
    tcfg = cfg.get("track") or {}

    def uniq(items):
        seen, out = set(), []
        for d in items:
            if not d:
                continue
            p = expand(d)
            try:
                key = str(p.resolve())
            except OSError:
                key = str(p)
            if key not in seen:
                seen.add(key)
                out.append(str(p))
        return out
    if claude_dirs or codex_dirs:
        return uniq(claude_dirs or []), uniq(codex_dirs or [])
    cl = [cfg["sources"].get("claude_projects_dir")] + list(tcfg.get("claude_dirs") or [])
    cx = [cfg["sources"].get("codex_sessions_dir")] + list(tcfg.get("codex_dirs") or [])
    if tcfg.get("include_default_dirs", True):
        cl.append("~/.claude/projects")
        cx.append("~/.codex/sessions")
    return uniq(cl), uniq(cx)


def _claude_files(root, home_marks):
    out = []
    for path in sorted(glob.glob(os.path.join(str(root), "*", "*.jsonl"))):
        if skip_claude_project(Path(path).parent.name, home_marks):
            continue
        out.append(path)
    return out


def _codex_files(root):
    return sorted(glob.glob(os.path.join(str(root), "**", "*.jsonl"), recursive=True))


def _stat(path):
    try:
        st = os.stat(path)
        return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}
    except OSError:
        return None


# ---------------------------------------------------------------- model attribution, weeks

def load_history(paths):
    rows = [r for r in read_jsonl(paths.history) if r.get("time") and r.get("model_hash")]
    return sorted(rows, key=lambda r: r["time"])


def model_at(time_iso, history, manifest):
    """(model_hash, how) live at a time: the newest history entry at or before it, else the current manifest when it
    was built before that time, else unknown."""
    t = _norm_time(time_iso)
    if t:
        best = None
        for r in history:
            if _norm_time(r["time"]) <= t:
                best = r
        if best:
            return best["model_hash"], "history"
        built = _norm_time((manifest or {}).get("built"))
        if manifest and manifest.get("model_hash") and built and built <= t and not history:
            return manifest["model_hash"], "manifest"
    return "unknown", "unknown"


def _norm_time(s):
    """An ISO time as a sortable UTC string (YYYY-MM-DDTHH:MM:SS), or None."""
    if not s or not isinstance(s, str):
        return None
    t = s.strip()
    try:
        if len(t) == 10:
            d = _dt.datetime.strptime(t, "%Y-%m-%d").replace(tzinfo=_dt.timezone.utc)
        else:
            d = _dt.datetime.fromisoformat(t.replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=_dt.timezone.utc)
    except ValueError:
        return None
    return d.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _tz(cfg):
    name = (cfg or {}).get("timezone") or "UTC"
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:  # unknown zone or no tz database: weeks fall back to UTC
        return _dt.timezone.utc


def week_of(time_iso, tz):
    t = _norm_time(time_iso)
    if not t:
        return "unknown"
    d = _dt.datetime.strptime(t, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=_dt.timezone.utc).astimezone(tz)
    y, w, _ = d.isocalendar()
    return "%d-W%02d" % (y, w)


def _gap_minutes(a, b):
    ta, tb = _norm_time(a), _norm_time(b)
    if not ta or not tb:
        return None
    da = _dt.datetime.strptime(ta, "%Y-%m-%dT%H:%M:%S")
    db = _dt.datetime.strptime(tb, "%Y-%m-%dT%H:%M:%S")
    return round((db - da).total_seconds() / 60.0, 1)


# ---------------------------------------------------------------- the run

def _finalize(inv):
    """Classify one scanned invocation. `class_scan`/`rule_scan` hold the transcript verdict (rules, then the engine
    for U-default); `class`/`rule` are the final verdict after the runtime logs are joined."""
    cls, rule = classify_reply(inv.get("_next") or "", inv.get("interrupted"))
    inv["class_scan"], inv["rule_scan"], inv["rules_version"] = cls, rule, RULES_VERSION
    inv["class"], inv["rule"] = cls, rule
    inv["gap_min"] = _gap_minutes(inv.get("time"), inv.get("next_time"))
    # a reply stamped under two seconds after its trigger is an import that re-stamped the session, not typing: the
    # class stands (the order of messages is kept), the time does not place the invocation in a week or under a model
    gap_s = _gap_seconds(inv.get("time"), inv.get("next_time"))
    inv["time_reliable"] = not (gap_s is not None and 0 <= gap_s < 2)
    inv["markers"] = sorted(set(inv["markers"]))
    return inv


def _gap_seconds(a, b):
    def parse(s):
        try:
            d = _dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=_dt.timezone.utc)
        except ValueError:
            return None
    da, db = (parse(a) if a else None), (parse(b) if b else None)
    return (db - da).total_seconds() if da and db else None


PUBLIC_KEYS = ("id", "source", "session", "record_id", "time", "time_reliable", "next_time", "gap_min", "markers", "interrupted",
               "seed_hash", "seed_key", "class_scan", "rule_scan", "base_rule", "class", "rule", "rules_version",
               "decision", "logged_correction", "model_hash", "model_hash_from")


def public(inv):
    return {k: inv.get(k) for k in PUBLIC_KEYS if k in inv}


def run_track(paths, cfg, claude_dirs=None, codex_dirs=None, full=False, engine=None, engine_model=None,
              max_cost_usd=None, max_calls=None, drafts=None, fake_dir=None, log=print, backoff=None):
    """Scan, classify, join the runtime logs, draft corrections, write records and the summary. Returns the summary."""
    paths.track.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(paths.track, 0o700)
    except OSError:
        pass
    state = read_json(paths.track_state, {}) or {}
    if state.get("rules_version") != RULES_VERSION:
        full = True
    old_rows = {r["id"]: r for r in read_jsonl(paths.track_records) if r.get("source") in ("claude-code", "codex")}
    if state.get("version") != STATE_VERSION:
        full, old_rows = True, {}  # ids changed scheme: rebuild from the transcripts
        state = {k: v for k, v in state.items() if k in ("drafts", "drafted", "draft_lines", "promoted_lines")}
    files_state = {} if full else dict(state.get("files") or {})
    engine_labels = dict(state.get("engine") or {})
    want_text = bool(engine)  # the agent's reply head is read only for the engine classifier; nothing textual is written
    claude_roots, codex_roots = sources(cfg, paths.home, claude_dirs, codex_dirs)
    home_marks = _home_markers(paths.home)
    candidates = []
    for root in claude_roots:
        if Path(root).exists():
            candidates += [("claude-code", f) for f in _claude_files(root, home_marks)]
    for root in codex_roots:
        if Path(root).exists():
            candidates += [("codex", f) for f in _codex_files(root)]
    rows, fresh, scanned, skipped = {}, [], 0, 0
    seen_files = set()
    for source, f in candidates:
        if f in seen_files:
            continue
        seen_files.add(f)
        st = _stat(f)
        if st is None:
            continue
        prev = files_state.get(f)
        unlabeled = engine and prev and any(old_rows.get(i, {}).get("rule_scan") == DEFAULT_RULE and i not in engine_labels
                                            for i in prev.get("rows", []))
        if prev and prev.get("size") == st["size"] and prev.get("mtime_ns") == st["mtime_ns"] and not unlabeled:
            for i in prev.get("rows", []):
                if i in old_rows:
                    rows[i] = old_rows[i]
            skipped += 1
            continue
        try:
            invs = scan_claude_file(f, want_text) if source == "claude-code" else scan_codex_file(f, paths.home, want_text)
        except OSError:
            continue
        scanned += 1
        for inv in invs:
            _finalize(inv)
            prev_inv = rows.get(inv["id"])
            if prev_inv is not None and prev_inv.get("_next") is not None and inv.get("_next") is None:
                continue  # another copy of this session saw the reply; keep that one
            if prev_inv is not None and prev_inv in fresh:
                fresh.remove(prev_inv)
            rows[inv["id"]] = inv
            fresh.append(inv)
        files_state[f] = dict(st, rows=[inv["id"] for inv in invs])
    # rows from transcripts that are gone (Claude Code prunes old sessions) are kept: the history is the metric
    for i, r in old_rows.items():
        rows.setdefault(i, r)
    if engine:
        _engine_labels(paths, cfg, engine, [inv for inv in fresh if inv["rule_scan"] == DEFAULT_RULE and inv.get("_next")
                                            and inv["id"] not in engine_labels], engine_labels, engine_model,
                       max_cost_usd, max_calls, fake_dir, log, backoff)
    for i, r in rows.items():
        r.setdefault("class_scan", r.get("class"))
        r.setdefault("rule_scan", r.get("rule"))
        lab = engine_labels.get(i)
        if lab and r["rule_scan"] == DEFAULT_RULE and lab.get("rules_version") == RULES_VERSION:
            r["base_rule"] = DEFAULT_RULE
            r["class_scan"], r["rule_scan"] = lab["class"], "engine:%s" % (lab.get("model") or "?")
    draft_info = {"drafted": 0, "refused": 0}
    do_drafts = (cfg.get("track") or {}).get("drafts", True) if drafts is None else drafts
    if do_drafts:
        draft_info = _draft_corrections(paths, cfg, state, [inv for inv in fresh if inv["class_scan"] == "correction"], log)
    review = _review_drafts(paths, cfg, state, log)
    transcript_rows = sorted(rows.values(), key=lambda r: (r.get("time") or "", r["id"]))
    all_rows = _join_runtime(paths, cfg, transcript_rows, state)
    write_jsonl(paths.track_records, [public(r) if r.get("source") in ("claude-code", "codex") else r for r in all_rows])
    state.update({"version": STATE_VERSION, "rules_version": RULES_VERSION, "files": files_state, "engine": engine_labels,
                  "last_run": now_iso()})
    write_json(paths.track_state, state)
    summary = summarize(paths, cfg, all_rows)
    summary.update({"files_scanned": scanned, "files_unchanged": skipped, "roots": {"claude": claude_roots, "codex": codex_roots},
                    "drafts": dict(draft_info, **review)})
    write_json(paths.track_summary, summary)
    for p in (paths.track_records, paths.track_state, paths.track_summary):
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
    log("track: %d invocation(s) (%d classified); %s; scanned %d file(s), %d unchanged" % (
        summary["invocations"], summary["classified"], format_counts(summary["counts"]), scanned, skipped))
    return summary


def format_counts(counts):
    order = list(CLASSES) + list(OTHER_CLASSES)
    return ", ".join("%s %d" % (k, counts.get(k, 0)) for k in order if counts.get(k)) or "no classes"


# ---------------------------------------------------------------- runtime logs: decisions and logged corrections

def _join_runtime(paths, cfg, rows, state):
    """Attach decisions.jsonl rows and corrections.md lines to transcript invocations; the rest become rows of their own.
    Sets model_hash on every row."""
    from .refresh import parse_corrections
    history = load_history(paths)
    manifest = read_json(paths.model_json, {}) or {}
    decisions = read_jsonl(paths.decisions)
    text = paths.corrections.read_text(encoding="utf-8") if paths.corrections.exists() else ""
    entries, _ = parse_corrections(text)
    tracker_lines = set((state.get("draft_lines") or {}).keys()) | set(state.get("promoted_lines") or [])
    logged = [e for e in entries if e["hash"] not in tracker_lines]
    by_hash = collections.defaultdict(list)
    for r in rows:
        for k in ("decision", "logged_correction", "model_hash", "model_hash_from"):
            r.pop(k, None)
        r["class"], r["rule"] = r.get("class_scan", r.get("class")), r.get("rule_scan", r.get("rule"))
        if r.get("seed_hash"):
            by_hash[r["seed_hash"]].append(r)
    extra = []
    for k, d in enumerate(decisions):
        sh = d.get("seed_hash")
        date = d.get("date")
        match = next((r for r in by_hash.get(sh, []) if not r.get("decision") and _same_day(r.get("time"), date, 1)), None)
        if match:
            match["decision"] = True
            if d.get("model_hash"):
                match["model_hash"], match["model_hash_from"] = d["model_hash"], "decision"
            continue
        extra.append({"id": "dec-" + hashlib.sha256(("%s\n%s\n%d" % (sh, date, k)).encode()).hexdigest()[:12],
                      "source": "decisions.jsonl", "time": date, "markers": ["decision"], "class": "unknown",
                      "rule": "D-decision-only", "rules_version": RULES_VERSION, "seed_hash": sh, "decision": True,
                      "model_hash": d.get("model_hash"), "model_hash_from": "decision" if d.get("model_hash") else None})
    by_key = collections.defaultdict(list)
    for r in rows:
        if r.get("seed_key"):
            by_key[r["seed_key"]].append(r)
    for e in logged:
        key = seed_key(e.get("seed"))
        match = next((r for r in by_key.get(key, []) if _same_day(r.get("time"), e["date"], 1, after_only=True)), None)
        if match is None:
            sh = seed_hash(e.get("seed"))
            match = next((x for x in extra if x["source"] == "decisions.jsonl" and x.get("seed_hash") == sh
                          and x.get("class") == "unknown" and _same_day(x.get("time"), e["date"], 1)), None)
        if match is not None:
            match["logged_correction"] = True
            if match.get("class") != "correction":
                match["class"], match["rule"] = "correction", "L-logged-correct"
            continue
        extra.append({"id": "cor-" + e["hash"][:12], "source": "corrections.md", "time": e["date"], "markers": ["correct"],
                      "class": "correction", "rule": "L-logged-correct", "rules_version": RULES_VERSION,
                      "logged_correction": True})
    out = rows + extra
    for r in out:
        if not r.get("model_hash"):
            r["model_hash"], r["model_hash_from"] = model_at(r.get("time"), history, manifest)
    return sorted(out, key=lambda r: (_norm_time(r.get("time")) or "", r["id"]))


def _same_day(time_iso, date, slack_days=0, after_only=False):
    t = _norm_time(time_iso)
    d = _norm_time(date)
    if not t or not d:
        return False
    a = _dt.datetime.strptime(t[:10], "%Y-%m-%d").date()
    b = _dt.datetime.strptime(d[:10], "%Y-%m-%d").date()
    delta = (b - a).days
    if after_only:
        return 0 <= delta <= slack_days
    return abs(delta) <= slack_days


# ---------------------------------------------------------------- the summary

def _rates(rows):
    classified = [r for r in rows if r.get("class") in CLASSES]
    n = len(classified)
    return {"invocations": len(rows), "classified": n,
            "correction": rate_block(sum(1 for r in classified if r["class"] == "correction"), n),
            "refinement": rate_block(sum(1 for r in classified if r["class"] == "refinement"), n)}


def summarize(paths, cfg, rows):
    tz = _tz(cfg)
    counts = collections.Counter(r.get("class") for r in rows)
    rules = collections.Counter(r.get("rule") for r in rows)
    by_week, by_model = collections.defaultdict(list), collections.defaultdict(list)
    for r in rows:
        by_week[week_of(r.get("time"), tz)].append(r)
        by_model[r.get("model_hash") or "unknown"].append(r)
    manifest = read_json(paths.model_json, {}) or {}
    current = manifest.get("model_hash")
    overall = _rates(rows)
    out = {"generated": now_iso(), "rules_version": RULES_VERSION, "invocations": overall["invocations"],
           "classified": overall["classified"], "counts": dict(counts), "rules": dict(rules),
           "correction_rate": overall["correction"], "refinement_rate": overall["refinement"],
           "by_week": {w: _rates(v) for w, v in sorted(by_week.items())},
           "by_model": {m: _rates(v) for m, v in sorted(by_model.items())},
           "current_model": current, "current": _rates(by_model.get(current, [])) if current else None,
           "sources": dict(collections.Counter(r.get("source") for r in rows)),
           "logged_corrections": sum(1 for r in rows if r.get("logged_correction")),
           "unreliable_times": sum(1 for r in rows if r.get("time_reliable") is False),
           "decisions_joined": sum(1 for r in rows if r.get("decision"))}
    return out


def regression(summary, live_hash, parent_hash, min_n=10):
    """None when no regression can be shown; else a dict describing it. Regressed means the live model's correction rate
    lies above its parent's 95% interval, with at least min_n classified invocations under each."""
    bm = (summary or {}).get("by_model") or {}
    live, parent = bm.get(live_hash), bm.get(parent_hash)
    if not live or not parent:
        return None
    lc, pc = live["correction"], parent["correction"]
    if lc["n"] < min_n or pc["n"] < min_n or lc["rate"] is None or pc["hi"] is None:
        return None
    if lc["rate"] > pc["hi"]:
        return {"live": live_hash, "parent": parent_hash, "live_rate": lc, "parent_rate": pc}
    return None


# ---------------------------------------------------------------- correction drafts

PENDING_PREFIX = "# pending "


def draft_line(draft_id, date, seed, wrong, meant):
    body = "%s | seed: \"%s\" | wrong: %s | meant: %s | scope: global | kind: fix" % (
        date, (seed or "(none)").replace("\n", " ").replace("|", "/").replace('"', "'").strip()[:80] or "(none)",
        (wrong or "n/a"), meant)
    return "%s%s | %s" % (PENDING_PREFIX, draft_id, body), body


def _line_hash(line):
    from .refresh import line_hash
    return line_hash(line)


def _draft_corrections(paths, cfg, state, invs, log):
    """Append a pending draft for each new correction that states what was meant. Returns counts."""
    from .grants import has_grant_phrase
    from .lint import forbidden_hits
    from .refresh import parse_corrections
    drafted = state.setdefault("drafted", {})
    drafts = state.setdefault("drafts", {})
    draft_lines = state.setdefault("draft_lines", {})
    lines, n, refused, new_ids = [], 0, 0, []
    for inv in invs:
        if inv["id"] in drafted:
            continue
        got = extract_meant(inv.get("_next") or "")
        if not got:
            continue
        meant, wrong, rid = got
        if forbidden_hits(meant, "meant") or forbidden_hits(wrong, "wrong") or has_grant_phrase(meant):
            drafted[inv["id"]] = None  # never drafted: a correction can never grant or lift a hold
            refused += 1
            continue
        did = "d" + hashlib.sha256(inv["id"].encode()).hexdigest()[:7]
        date = (_norm_time(inv.get("next_time") or inv.get("time")) or now_iso())[:10]
        line, body = draft_line(did, date, inv.get("_seed"), wrong, meant)
        if len(parse_corrections(body)[0]) != 1:
            drafted[inv["id"]] = None
            refused += 1
            continue
        lines.append(line)
        drafted[inv["id"]] = did
        drafts[did] = {"inv": inv["id"], "date": date, "body_hash": _line_hash(body), "rule": rid, "status": "pending"}
        draft_lines[_line_hash(body)] = inv["id"]
        new_ids.append(did)
        n += 1
    if lines:
        if not _append(paths.corrections, lines, log):
            for did in new_ids:
                d = drafts.pop(did, None)
                if d:
                    draft_lines.pop(d["body_hash"], None)
                    drafted.pop(d["inv"], None)
            return {"drafted": 0, "refused": refused}
        log("track: %d correction draft(s) appended to %s as `# pending` lines (not applied until reviewed)" % (n, paths.corrections))
    return {"drafted": n, "refused": refused}


def _append(path, lines, log):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        old = path.read_text(encoding="utf-8") if path.exists() else ""
        with open(path, "a", encoding="utf-8") as fh:
            if old and not old.endswith("\n"):
                fh.write("\n")
            for line in lines:
                fh.write(line.rstrip("\n") + "\n")
        return True
    except OSError as exc:
        log("track: could not write %s (%s); drafts skipped" % (path, exc.strerror or exc))
        return False


def _tokens(text):
    stop = {"the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "it", "is", "be", "with", "that", "this", "my", "your"}
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 2 and w not in stop}


def _similar(a, b, threshold=0.6):
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    return len(ta & tb) / float(len(ta | tb)) >= threshold


def _review_drafts(paths, cfg, state, log):
    """Statuses from corrections.md (pending, approved by deleting the prefix, rejected by deleting the line), then
    promotion: a pending draft whose correction repeats track.promote_after times is appended as a normal line."""
    drafts = state.setdefault("drafts", {})
    if not drafts:
        return {"pending": 0, "approved": 0, "rejected": 0, "promoted": 0}
    text = paths.corrections.read_text(encoding="utf-8") if paths.corrections.exists() else ""
    pending_ids, normal_hashes = set(), set()
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith(PENDING_PREFIX):
            pending_ids.add(line[len(PENDING_PREFIX):].split(" ", 1)[0])
        elif line and not line.startswith("#"):
            normal_hashes.add(_line_hash(line))
    for did, d in drafts.items():
        if d["status"] == "promoted":
            continue
        if did in pending_ids:
            d["status"] = "pending"
        elif d["body_hash"] in normal_hashes:
            d["status"] = "approved"
        else:
            d["status"] = "rejected"
    need = max(2, int((cfg.get("track") or {}).get("promote_after", 3) or 3))
    from .refresh import parse_corrections
    meant_of = {}  # the text lives in corrections.md only; state keeps ids and hashes
    for did in pending_ids:
        body = _body_of(text, did)
        got = parse_corrections(body)[0] if body else []
        if got:
            meant_of[did] = got[0]["meant"]
    pending = [(did, d) for did, d in sorted(drafts.items(), key=lambda kv: kv[1]["date"])
               if d["status"] == "pending" and did in meant_of]
    groups = []
    for did, d in pending:
        for g in groups:
            if _similar(meant_of[did], meant_of[g[0][0]]):
                g.append((did, d))
                break
        else:
            groups.append([(did, d)])
    promoted = 0
    promoted_lines = state.setdefault("promoted_lines", [])
    for g in groups:
        if len(g) < need:
            continue
        newest_id, newest = g[-1]
        body = _body_of(text, newest_id)
        if not body:
            continue
        today = now_iso()[:10]
        line = re.sub(r"^\d{4}-\d{2}-\d{2}", today, body)
        if _append(paths.corrections, [line], log):
            promoted_lines.append(_line_hash(line))
            for did, d in g:
                d["status"] = "promoted"
            promoted += 1
    if promoted:
        log("track: promoted %d correction(s) seen %d+ times to normal corrections.md lines" % (promoted, need))
    counts = collections.Counter(d["status"] for d in drafts.values())
    return {"pending": counts.get("pending", 0), "approved": counts.get("approved", 0), "rejected": counts.get("rejected", 0),
            "promoted": counts.get("promoted", 0), "promoted_now": promoted}


def _body_of(text, draft_id):
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith(PENDING_PREFIX + draft_id + " | "):
            return line.split(" | ", 1)[1]
    return None


# ---------------------------------------------------------------- the optional engine classifier

TRACK_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["items"],
                "properties": {"items": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False, "required": ["id", "class"],
                    "properties": {"id": {"type": "string"}, "class": {"type": "string", "enum": list(CLASSES)}}}}}}


def _engine_labels(paths, cfg, kind, invs, labels, model, max_cost_usd, max_calls, fake_dir, log, backoff):
    if not invs:
        log("track: engine classifier: nothing to label (every message matched a rule or is labelled already)")
        return
    from . import budget as _budget
    from . import engine as _engine
    from .mine import privacy_line
    tcfg = cfg.get("track") or {}
    model = model or ((tcfg.get("engine_model") or {}).get(kind) or None)
    cap = max_cost_usd if max_cost_usd is not None else tcfg.get("engine_max_cost_usd", 0.25)
    calls = max_calls if max_calls is not None else tcfg.get("engine_max_calls", 10)
    size = max(1, int(tcfg.get("engine_batch", 20)))
    eng = _engine.Engine(paths, cfg, kind, "track", model=model, concurrency=1, max_calls=calls, max_cost_usd=cap,
                         fake_dir=fake_dir, log=log, backoff=backoff,
                         cost_estimate=_budget.estimator(cfg, model, "track", paths.runs))
    tpl = _engine.load_prompt("track-classify")
    units = []
    for k in range(0, len(invs), size):
        batch = invs[k:k + size]
        items = [{"id": inv["id"], "seed": (inv.get("_seed") or "")[:300], "agent_reply_head": (inv.get("_agent") or "")[:500],
                  "next_message": (inv.get("_next") or "")[:800]} for inv in batch]
        ids = sorted(i["id"] for i in items)
        mapping = {"name": cfg["name"], "privacy": privacy_line(cfg), "items_json": json.dumps(items, ensure_ascii=False, indent=0)}

        def check(data, ids=ids):
            got = sorted(it["id"] for it in data.get("items", []))
            return None if got == ids else "items ids %s do not match %s exactly" % (got, ids)
        units.append(_engine.Unit("batch-%03d" % (k // size), _engine.render(tpl, mapping), TRACK_SCHEMA,
                                  eng.run_dir / ("track-%03d.json" % (k // size)), check=check))
    res = eng.run(units, force=True)
    got = 0
    for u in units:
        data = read_json(u.out_path) if u.out_path.exists() else None
        for it in (data or {}).get("items", []):
            labels[it["id"]] = {"class": it["class"], "model": model or kind, "rules_version": RULES_VERSION}
            got += 1
    log("track: engine classifier labelled %d message(s) in %d call(s), $%.4f%s" % (
        got, res["calls"], res["cost_usd"], ("; " + res["budget_note"]) if res.get("budget_note") else ""))


def format_status_line(summary):
    """One status line (no text): invocations, correction and refinement rates with intervals, current model."""
    from .stats import format_rate
    if not summary:
        return "tracker: not run yet (track)"
    cur = summary.get("current") or {}
    d = summary.get("drafts") or {}
    line = "tracker (rules v%s, %s): %d invocation(s), %d classified; correction rate %s; refinement rate %s" % (
        summary.get("rules_version"), (summary.get("generated") or "")[:16], summary.get("invocations", 0),
        summary.get("classified", 0), format_rate(summary.get("correction_rate")), format_rate(summary.get("refinement_rate")))
    if summary.get("current_model"):
        line += "; current model %s: corrections %s" % (summary["current_model"][:8], format_rate(cur.get("correction")))
    if d.get("pending"):
        line += "; %d correction draft(s) pending review in corrections.md" % d["pending"]
    return line
