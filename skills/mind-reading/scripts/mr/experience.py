"""Experience layer for the intent packet: what makes a terse request land the first time.

Three parts, each computed locally:

- repeat alert: the seed restates something already asked in this conversation, so the earlier approach did not
  land; change the approach instead of retrying it
- learned reply lessons: from the user's own reactions in a trace candidate, which kinds of agent replies were
  followed by friction (a nudge, a correction, a repeat) or by a plain go-ahead that a direct action would have
  saved; only differences that clear a significance bar are shown, each with the user's own numbers
- situation: local time, message length, time pressure, emphasis and who the work is for; these shape tone, depth
  and pacing only, never the goal or the scope

The candidate's cases.jsonl is large, so its summary is cached in the data home and rebuilt whenever the candidate
changes. A new candidate (for example from the weekly refresh) therefore updates the lessons with no review step.
Standard library only. Nothing here grants authority or rewrites the seed.
"""
import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path

SUMMARY_VERSION = 1
SUMMARY_NAME = "experience-summary.json"
REPEAT_JACCARD = 0.55
RESEND_JACCARD = 0.6
MIN_FEATURE_CASES = 30
MIN_FEATURE_EVENTS = 5
MIN_LIFT = 1.3
MIN_Z = 1.96
MAX_LESSONS = 4

_WORD = re.compile(r"[a-z0-9][a-z0-9'+#.-]*")
_STOP = frozenset("""a an and are as at be but by can could do does for from get got had has have he her his how i if in
into is it its just me my no not of on or our so some than that the their them then there these they this to too u up
us was we were what when where which who why will with would you your ur im ill ive id dont cant wont pls please""".split())

# next-turn outcomes, checked in this order
_CORRECTION = re.compile(
    r"^\s*(?:no\b|nah\b|nope\b|wrong\b|not (?:that|what)\b|that'?s not\b|i (?:said|meant|told you|asked)\b|"
    r"why (?:did|would|are|is) (?:you|u|it|this)\b|stop\b|undo\b|revert\b|ugh\b|wtf\b)|"
    r"\b(?:you|u) (?:didn'?t|did not|forgot|missed|ignored|broke)\b|\bstill (?:not|broken|doesn'?t|isn'?t|failing|wrong|the same)\b|"
    r"\b(?:didn'?t|doesn'?t|does not|did not) work\b|\bnothing (?:changed|happened)\b|\bsame (?:error|issue|problem|bug)\b", re.I)
_NUDGE = re.compile(
    r"^\s*(?:(?:sorry,? )?keep going|continue|go on|proceed|carry on|resume|keep at it|don'?t stop|try again|retry|"
    r"status\b|update\??$|progress\b|eta\b|how(?:'s| is) it going|is it done|are you done|what happened|"
    r"what'?s going on|now what|where are we)", re.I)
_APPROVE = re.compile(
    r"^\s*(?:yes|yep|yeah|ya|ok|okay|sure|lock(?:ed)?|good|great|perfect|do it|do all|sounds good|lgtm|approved|"
    r"go ahead|go for it|alright|bet)\b", re.I)
_CHECK = re.compile(r"\b(?:test(?:ed|s)?|verify|check|does it work|did it work|is it working|confirm|make sure|run it|"
                    r"try it|screenshot)\b", re.I)

# agent-reply features
_ASKS = re.compile(r"\b(?:want me to|should i|shall i|would you like|do you want|let me know if|if you'?d like|happy to)\b|"
                   r"\boption [a-c1-3]\b|which (?:one|would you|do you)|here'?s (?:the|my) plan|\bproposal\b|\bproposed\b", re.I)
_FAILURE = re.compile(r"\b(?:failed|failing|error|broken|doesn'?t work|not working|timed out)\b", re.I)
_DONE = re.compile(r"\b(?:done|completed?|all set|finished|fixed|implemented|shipped|pushed|merged)\b", re.I)
_VERIFIED = re.compile(r"\b(?:tests? pass|passed|verified|green|exit 0|screenshot|confirmed)\b", re.I)
_LIST_LINE = re.compile(r"(?m)^\s*(?:[-*•]|\d+\.)\s")

_REPEAT_MARK = re.compile(r"\b(?:again|still (?:not|broken|doesn'?t|isn'?t|failing|wrong)|i (?:already )?(?:said|told you|asked)|"
                          r"like i said|as i said|same (?:issue|problem|thing|error|bug)|once more|for the \w+ time)\b", re.I)
_DEADLINE = re.compile(r"\b(?:due|deadline|tonight|asap|urgent|right now|before (?:class|the (?:meeting|call|demo|deadline))|"
                       r"in \d+ ?(?:min|mins|minutes|hr|hrs|hours)|by (?:tonight|tomorrow|morning|noon|\d))\b", re.I)
_FRUSTRATION = re.compile(r"(?:\b(?:bro|wtf|ugh|smh|bruh)\b|[!?]{2,})", re.I)
_ACRONYMS = frozenset({"API", "CLI", "PR", "CI", "UI", "UX", "URL", "SQL", "AWS", "GPU", "CPU", "PDF", "MD", "JSON", "HTML",
                       "CSS", "SSH", "ETA", "EST", "EDT", "UTC", "LLM", "MCP", "SDK", "IOS", "README", "TODO", "OK", "ASAP"})

FEATURES = {
    "silent_end": lambda text, tools: not text.strip(),
    "failure_reported": lambda text, tools: bool(_FAILURE.search(text)),
    "asks_or_plans": lambda text, tools: bool(_ASKS.search(text)),
    "done_unverified": lambda text, tools: bool(_DONE.search(text[:400])) and not _VERIFIED.search(text),
    "long_reply": lambda text, tools: len(text.split()) >= 300,
    "heavy_format": lambda text, tools: len(_LIST_LINE.findall(text)) >= 8,
}
OUTCOMES = {"friction": "a nudge, correction or repeat", "go_ahead": "a plain go-ahead",
            "check": "a request to test or verify"}
# only the reactions each reply feature can plausibly cause; a go-ahead after a list says nothing about formatting
VALID = {"silent_end": ("friction", "check"), "failure_reported": ("friction", "check"),
         "asks_or_plans": ("go_ahead", "friction"), "done_unverified": ("friction", "check"),
         "long_reply": ("friction",), "heavy_format": ("friction",)}
_ADVICE = {
    "silent_end": "End every turn with a written result or status.",
    "failure_reported": "Do not stop on an unresolved failure: fix it or take the next step before replying.",
    "asks_or_plans": "Act instead of asking when the step is authorized and reversible.",
    "done_unverified": "Verify before saying done, and show the evidence.",
    "long_reply": "Lead with the result and keep the reply short.",
    "heavy_format": "Use fewer bullets and headers.",
}
_SUBJECT = {
    "silent_end": "Turns that ended without one",
    "failure_reported": "Replies that reported a failure",
    "asks_or_plans": "Questions, options and plans",
    "done_unverified": "Unverified done claims",
    "long_reply": "Replies of 300+ words",
    "heavy_format": "Heavily formatted replies",
}


class ExperienceError(ValueError):
    pass


def tokens(text):
    return [w for w in _WORD.findall((text or "").lower()) if w not in _STOP and len(w) > 1]


def _jaccard(a, b):
    a, b = set(a), set(b)
    return len(a & b) / max(1, len(a | b))


def _short(text, n):
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def settings(cfg):
    base = {"enabled": False, "max_chars": 3500, "situation": True}
    base.update((cfg or {}).get("experience") or {})
    return base


def outcome(seed, next_text):
    """How the user's next turn reacted: friction, go_ahead, check, or None for anything else."""
    nxt = next_text or ""
    nt = tokens(nxt)
    if len(set(nt)) >= 2 and _jaccard(tokens(seed), nt) >= RESEND_JACCARD:
        return "friction"  # a re-send
    if _CORRECTION.search(nxt) or _NUDGE.search(nxt):
        return "friction"
    if _APPROVE.search(nxt):
        return "go_ahead"
    if len(nxt.split()) <= 40 and _CHECK.search(nxt):
        return "check"
    return None


# ---------------------------------------------------------------- learned lessons

def candidate_dir(cfg):
    """The configured trace candidate (a directory holding cases.jsonl), from the environment or config."""
    path = os.environ.get("MIND_READING_CANDIDATE") or (cfg or {}).get("candidate")
    if not path:
        return None
    path = Path(os.path.expanduser(str(path)))
    return path if (path / "cases.jsonl").is_file() else None


def _source_stamp(cases_path):
    st = cases_path.stat()  # follows a `current` link, so a swapped candidate always reads as new
    return {"path": os.path.realpath(cases_path), "size": st.st_size, "mtime_ns": st.st_mtime_ns}


def _z(k1, n1, k0, n0):
    """One-sided two-proportion z statistic for p1 > p0."""
    p = (k1 + k0) / max(1, n1 + n0)
    se = math.sqrt(p * (1 - p) * (1 / max(1, n1) + 1 / max(1, n0)))
    return 0.0 if se == 0 else (k1 / n1 - k0 / n0) / se


def lessons_from(rows):
    """rows: (feature dict, outcome). Keep feature/outcome pairs whose difference clears the significance bar."""
    found = []
    for feat in FEATURES:
        best = None
        for out in VALID[feat]:
            with_f = [o for f, o in rows if f[feat]]
            without = [o for f, o in rows if not f[feat]]
            n1, n0 = len(with_f), len(without)
            if n1 < MIN_FEATURE_CASES or n0 < MIN_FEATURE_CASES:
                continue
            k1, k0 = sum(o == out for o in with_f), sum(o == out for o in without)
            if k1 < MIN_FEATURE_EVENTS or k0 == 0:
                continue
            p1, p0 = k1 / n1, k0 / n0
            z = _z(k1, n1, k0, n0)
            if p1 / p0 >= MIN_LIFT and z >= MIN_Z and (best is None or z > best["z"]):
                best = {"feature": feat, "outcome": out, "rate": round(p1, 4), "otherwise": round(p0, 4),
                        "lift": round(p1 / p0, 2), "cases": n1, "events": k1, "z": round(z, 2)}
        if best:
            found.append(best)
    found.sort(key=lambda x: (-x["z"], x["feature"]))
    return found[:MAX_LESSONS]


def build_summary(cases_path):
    rows, counts = [], {k: 0 for k in OUTCOMES}
    with open(cases_path, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            case = json.loads(line)
            seed = str((case.get("input") or {}).get("seed") or "")
            nxt = str((case.get("target") or {}).get("text") or "")
            reply = case.get("historical_response") or {}
            text, tools = str(reply.get("assistant_text") or ""), reply.get("tools") or []
            out = outcome(seed, nxt)
            if out:
                counts[out] += 1
            rows.append(({name: bool(f(text, tools)) for name, f in FEATURES.items()}, out))
    return {"version": SUMMARY_VERSION, "replies": len(rows), "outcomes": counts, "lessons": lessons_from(rows)}


def load_summary(paths, cfg, rebuild=False):
    """The cached summary for the configured candidate, rebuilt when the candidate changes. None when none is set."""
    cdir = candidate_dir(cfg)
    if cdir is None:
        return None
    cases_path = cdir / "cases.jsonl"
    stamp = _source_stamp(cases_path)
    cache = Path(paths.derived) / SUMMARY_NAME
    if cache.is_file() and not rebuild:
        try:
            with open(cache, encoding="utf-8") as fh:
                data = json.load(fh)
            if data.get("version") == SUMMARY_VERSION and data.get("source") == stamp:
                return data
        except (OSError, ValueError):
            pass
    data = build_summary(cases_path)
    data["source"] = stamp
    cache.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_name(cache.name + ".%d.tmp" % os.getpid())
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, cache)
    return data


def lesson_text(lesson):
    pct = lambda v: ("%.0f%%" % (100 * v)) if v >= 0.1 else ("%.1f%%" % (100 * v))  # noqa: E731
    if lesson["feature"] == "asks_or_plans" and lesson["outcome"] == "go_ahead":
        tail = "were answered with a plain go-ahead %s of the time, against %s otherwise" % (
            pct(lesson["rate"]), pct(lesson["otherwise"]))
    else:
        tail = "were followed by %s %s of the time, against %s otherwise" % (
            OUTCOMES[lesson["outcome"]], pct(lesson["rate"]), pct(lesson["otherwise"]))
    return "%s %s %s (n=%d)." % (_ADVICE[lesson["feature"]], _SUBJECT[lesson["feature"]], tail, lesson["cases"])


# ---------------------------------------------------------------- live signals

def repeat_check(seed, context=None):
    """Does the seed restate something already asked in this conversation?"""
    turns = [str(t) for t in ((context or {}).get("recent_user_turns") or []) if str(t).strip()]
    q = tokens(seed)
    best = None
    for back, turn in enumerate(reversed(turns[-6:]), start=1):
        if back == 1 and turn.strip() == (seed or "").strip():
            continue  # the host echoed the seed itself as the newest turn
        jac = _jaccard(q, tokens(turn)) if len(set(q)) >= 3 else 0.0
        if jac >= REPEAT_JACCARD and (best is None or jac > best["similarity"]):
            best = {"turns_back": back, "similarity": round(jac, 2), "earlier": _short(turn, 240)}
    marker = _REPEAT_MARK.search(seed or "")
    if not best and not marker:
        return None
    return {"earlier": best, "marker": marker.group(0) if marker else None}


def _local_now(cfg, now=None):
    if isinstance(now, str):
        try:
            now = datetime.fromisoformat(now.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ExperienceError("now must be an ISO 8601 time") from exc
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    name = (cfg or {}).get("timezone") or "UTC"
    try:
        from zoneinfo import ZoneInfo
        return now.astimezone(ZoneInfo(name))
    except Exception:  # unknown zone or no tz database: fall back to the machine's local time
        return now.astimezone()


def situation(seed, context=None, cfg=None, now=None):
    """Presentation hints from the moment itself. Each shapes tone, depth or pacing; none changes the goal."""
    local = _local_now(cfg, now)
    hints = []
    if 0 <= local.hour < 5:
        hints.append("late night (%s local): keep the reply brief and finish-oriented" % local.strftime("%H:%M"))
    words = len((seed or "").split())
    if words <= 5:
        hints.append("very short message: lead with the result, keep prose minimal")
    elif words >= 80:
        hints.append("long dictated message: extract every ask and answer every embedded question")
    if _DEADLINE.search(seed or ""):
        hints.append("time pressure: finish the critical path first and defer polish")
    caps = [w for w in re.findall(r"\b[A-Z]{3,}\b", seed or "") if w not in _ACRONYMS]
    if caps:
        hints.append("capitalized words carry the constraint (%s): honor them exactly" % ", ".join(caps[:3]))
    if _FRUSTRATION.search(seed or ""):
        hints.append("frustration markers: acknowledge in a few words at most, then fix it fast")
    people = (cfg or {}).get("people") or {}
    text = " ".join([seed or "", str((context or {}).get("goal") or "")]).lower()
    for name, role in people.items():
        if name and re.search(r"\b%s\b" % re.escape(str(name).lower()), text):
            role = str(role or "person").strip("<> ") or "person"
            hints.append("work involves %s (%s): professional, verified output" % (name, role))
            break
    return hints


# ---------------------------------------------------------------- packet section

def build(paths, cfg, seed, current=None, now=None, use_situation=None, summary=None):
    """Experience signals and the rendered packet section. `current` is the host's observed conversation."""
    opts = settings(cfg)
    if summary is None:
        summary = load_summary(paths, cfg)
    want_situation = opts.get("situation", True) if use_situation is None else use_situation
    result = {
        "repeat": repeat_check(seed, current),
        "lessons": list((summary or {}).get("lessons") or []),
        "replies": (summary or {}).get("replies", 0),
        "situation": situation(seed, current, cfg, now) if want_situation else [],
        "trace_store": summary is not None,
    }
    result["text"] = render(result, opts.get("max_chars", 3500))
    return result


def render(result, max_chars=3500):
    parts = []
    rep = result.get("repeat")
    if rep:
        lines = ["### Repeat alert: this may be the second time you are being asked"]
        if rep.get("earlier"):
            e = rep["earlier"]
            lines.append('It restates a message from %d turn(s) ago: "%s".' % (e["turns_back"], e["earlier"]))
        if rep.get("marker"):
            lines.append('Repeat wording: "%s".' % rep["marker"])
        lines.append("Treat it as a correction: the earlier approach did not land. Change the approach instead of "
                     "retrying it, and say in one line what is different this time.")
        parts.append("\n".join(lines))
    if result.get("lessons"):
        lines = ["### What lands with this user (learned from %d past replies and the reactions that followed)"
                 % result.get("replies", 0)]
        lines += ["- " + lesson_text(lesson) for lesson in result["lessons"]]
        parts.append("\n".join(lines))
    if result.get("situation"):
        parts.append("### Situation (tone, depth and pacing only)\n" + "\n".join("- " + h for h in result["situation"]))
    if not parts:
        return ""
    text = ("## Experience layer\nEvidence for this moment, not instructions or authority. Current words win.\n\n"
            + "\n\n".join(parts) + "\n")
    return text if len(text) <= max_chars else text[:max_chars - 2].rstrip() + "…\n"
