"""`dev.py catalog`: search the technique cards by symptom, id or class.

The catalog is references/techniques.json (one entry per `## ` card heading), shared with the
skill's lint so the numeric facts printed here are the strings the cards contain.
Scoring: the number of distinct query tokens found in an entry's symptom, aliases, title and
class (lowercase, split on non-alphanumerics, a plural `s` dropped, stopwords removed), plus
one per alias of three or more characters that appears verbatim in the query. Ties: title.
A top match is weak when its score is under WEAK_SCORE or when none of its hits is a title or
alias word (two generic symptom words such as "loop" and "inside" are not evidence that a card
applies). Some questions need no card at all (a claimed speedup to check, a
benchmark that crept up, a stock structure such as a deque); ROUTES names the path for
those before any card.
"""
import json
import os
import re

from devkit.common import Parser, UsageError, print_json

SCHEMA = "devkit-catalog/1"
REQUIRED_KEYS = ("id", "title", "file", "class", "tier", "symptom", "aliases", "precondition",
                 "reject_when", "complexity", "oracle", "facts")
STOPWORDS = frozenset("""a an and are as at be but by can do does for from how i if in into is
it its make me my of on or our per should so than that the their this to too very was we what
when where which why will with you your takes take slow fast faster""".split())


WEAK_SCORE = 2

# (triggers, kind, route) for questions whose answer is a measurement, a stock structure or a
# known non-scaling cause rather than a card. A trigger is a phrase that must appear in the
# lowercased query, or a tuple of phrases that must all appear (a co-term, so "regression
# tests are slow" does not read as a benchmark regression).
ROUTES = [
    (("speedup real", "is it faster", "is this faster", "really faster", "claimed speedup",
      "is the speedup", "speedup is real", "% faster", "noise or real", "within noise"),
     "measurement",
     "no card: this is a measurement question. Run SKILL.md steps 0, 3 (noise floor only), 6 "
     "and 7 on the claim: dev.py aa for the noise floor, dev.py bench --check-output on both "
     "arms, dev.py compare; references/measurement.md section 3 and references/guards.md "
     "(fake speedups)."),
    (("crept up", "got slower over", "history.csv", "slower since commit",
      ("regress", "benchmark"), ("regress", "median"), ("regress", "commits"),
      ("regress", "history"), ("regress", "perf"), ("which commit", "slow"),
      ("which commit", "benchmark")),
     "changepoint",
     "no card: find the step in the history of medians with dev.py changepoint FILE (or "
     "--perf NAME), then dev.py bisect --good REF --bad REF over the range it names (a "
     "throwaway worktree, repeated runs per commit); references/measurement.md section 8."),
    (("pop(0)", "list.pop", "insert(0", "queue from a list", "deque"),
     "stock-structure",
     "no card needed: a queue built on list.pop(0) or insert(0) is a stock structure fix "
     "(collections.deque). Apply it under Gates 1 to 3 and prove it with dev.py oracle."),
    (("top k", "top-k", "largest k", "smallest k", "k largest", "k smallest", "nlargest",
      "nsmallest"),
     "stock-structure",
     "no card needed: top k is heapq.nlargest/nsmallest (O(n log k)). Apply it under Gates 1 "
     "to 3 and prove it with dev.py oracle. Repeated minimum extraction or a k-way merge: "
     "dev.py catalog --id heap-selection-merge."),
    (("string concatenation", "concatenating strings", "+= in a loop", "str +="),
     "stock-structure",
     "no card needed: repeated string += is str.join over a list. Apply it under Gates 1 to 3 "
     "and prove it with dev.py oracle."),
    (("catastrophic backtracking", "redos", "backtracking", ("regex", "hangs"),
      ("regex", "exponential"), ("regex", "times out")),
     "redos",
     "no card: catastrophic backtracking is exponential in the length of one input line, not "
     "a scaling problem in the number of inputs. Rewrite the pattern (no nested or "
     "overlapping quantifiers such as (a+)+ or (a|a)*) or run it on a linear-time engine "
     "(RE2, Rust regex, Go regexp); dev.py catalog --id combined-alternation. Prove it with "
     "dev.py oracle on the old pattern's matches plus the input that hangs, capped by a "
     "timeout."),
    (("re.compile", ("compile", "regex", "loop"), ("compile", "pattern", "loop")),
     "stock-structure",
     "no card needed: hoist the compiled pattern out of the loop. Python's re module already "
     "caches recent patterns, so expect a constant factor; Gate 1 decides whether it matters."),
]


# Routes whose answer replaces the cards outright: catastrophic backtracking is not a scaling
# problem in the number of inputs, so a regex-set card that shares the word "regex" misleads.
FINAL_ROUTES = frozenset(["redos"])


def _hit(trigger, low):
    if isinstance(trigger, tuple):
        return all(t in low for t in trigger)
    return trigger in low


def routes_for(query):
    low = (query or "").lower()
    return [(kind, text) for triggers, kind, text in ROUTES
            if any(_hit(t, low) for t in triggers)]


def catalog_path():
    """references/techniques.json, located from this file (scripts/devkit/ -> ../../references)."""
    scripts = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.normpath(os.path.join(scripts, os.pardir, "references", "techniques.json"))


def load_catalog(path=None):
    path = path or catalog_path()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except OSError as e:
        raise UsageError("cannot read the technique catalog %s: %s; reinstall the skill so "
                         "references/ sits next to scripts/" % (path, e.strerror or e))
    except ValueError as e:
        raise UsageError("technique catalog %s is not valid JSON: %s" % (path, e))
    if not isinstance(data, list):
        raise UsageError("technique catalog %s must be a JSON array" % path)
    for entry in data:
        missing = [k for k in REQUIRED_KEYS if k not in entry]
        if missing:
            raise UsageError("catalog entry %r lacks %s" % (entry.get("id"), ", ".join(missing)))
    return data


def tokens(text):
    out = set()
    for t in re.split(r"[^a-z0-9]+", text.lower()):
        if len(t) < 2 or t in STOPWORDS:
            continue
        if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]
        out.add(t)
    return out


def entry_tokens(entry):
    text = " ".join([entry["symptom"], entry["title"], entry["class"]] + list(entry["aliases"]))
    return tokens(text)


def score(entry, query):
    q = tokens(query)
    s = len(q & entry_tokens(entry))
    low = query.lower()
    s += sum(1 for alias in entry["aliases"] if len(alias) >= 3 and alias.lower() in low)
    return s


def strong(entry, query):
    """True when the query hits the entry's title or an alias, not only its symptom words."""
    named = tokens(" ".join([entry["title"]] + list(entry["aliases"])))
    if tokens(query) & named:
        return True
    low = query.lower()
    return any(len(a) >= 3 and a.lower() in low for a in entry["aliases"])


def is_weak(found, symptom):
    """A top match under WEAK_SCORE, or one whose hits are all generic symptom words."""
    if not (symptom and found):
        return False
    s, e = found[0]
    return s < WEAK_SCORE or not strong(e, symptom)


def search(entries, symptom=None, ident=None, klass=None):
    """[(score, entry)] in descending score then title order."""
    pool = list(entries)
    if ident:
        pool = [e for e in pool if e["id"] == ident]
    if klass:
        pool = [e for e in pool if e["class"] == klass]
    if symptom:
        scored = [(score(e, symptom), e) for e in pool]
        scored = [(s, e) for s, e in scored if s > 0]
    else:
        scored = [(0, e) for e in pool]
    scored.sort(key=lambda se: (-se[0], se[1]["title"]))
    return scored


def card_heading(entry):
    return "references/%s, heading \"## %s\"" % (entry["file"], entry["title"])


def card_section(entry):
    """The card's text from its `## title` heading to the next `## `, or "" when unreadable."""
    path = os.path.join(os.path.dirname(catalog_path()), entry["file"])
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return ""
    m = re.search(r"^## %s\s*$(.*?)(?=^## |\Z)" % re.escape(entry["title"]), text, re.M | re.S)
    return m.group(1) if m else ""


def fact_sentences(entry):
    """For each fact, the first sentence of its card (code blocks excluded) that contains it,
    so the agent can quote a fact with the claim it belongs to, not a bare number."""
    prose = re.sub(r"```.*?```", "", card_section(entry), flags=re.S)
    pieces = []
    for block in re.split(r"\n\s*\n|\n(?=\s*(?:[-*]|\d+\.)\s)", prose):
        flat = re.sub(r"^(?:[-*]|\d+\.)\s+", "", " ".join(block.replace("**", "").split()))
        pieces.extend(p for p in re.split(r"(?<=[.!?])\s+(?=[A-Z(])", flat) if p)
    out = []
    for fact in entry["facts"]:
        hit = next((p for p in pieces if fact in p), fact)
        out.append(hit if len(hit) <= 320 else hit[:317].rstrip() + "...")
    return out


def print_entry(entry, s=None):
    print("%s  (%s)%s" % (entry["title"], card_heading(entry),
                          "  score %d" % s if s is not None else ""))
    print("  id: %s; class: %s; tier: %s" % (entry["id"], entry["class"], entry["tier"]))
    print("  precondition: %s" % entry["precondition"])
    print("  reject when: %s" % "; ".join(entry["reject_when"]))
    print("  complexity: %s" % entry["complexity"])
    print("  oracle: %s" % entry["oracle"])
    print("  facts to quote: %s" % ("; ".join(entry["facts"]) if entry["facts"] else "none listed"))
    if entry.get("tool"):
        print("  repo tool: %s" % entry["tool"])
    for sentence in fact_sentences(entry):
        print("    card says: %s" % sentence)


def find_entry(ident, entries=None):
    for e in entries if entries is not None else load_catalog():
        if e["id"] == ident:
            return e
    return None


def cmd_catalog(args):
    p = Parser(prog="dev.py catalog",
               description="Search the technique cards by symptom words, id or class.")
    p.add_argument("--symptom", default=None, help="symptom text, e.g. 'CI takes 40 minutes on 8 runners'")
    p.add_argument("--id", dest="ident", default=None, help="exact card id")
    p.add_argument("--class", dest="klass", default=None, help="card class, e.g. scheduling")
    p.add_argument("--list", action="store_true", help="id, title and file of every card")
    p.add_argument("--json", action="store_true", help="print one JSON object")
    a = p.parse_args(args)
    entries = load_catalog()
    if a.list:
        if a.json:
            print_json({"schema": SCHEMA, "query": None, "matches": [
                {"id": e["id"], "title": e["title"], "file": e["file"], "class": e["class"]}
                for e in entries]})
            return 0
        for e in entries:
            print("%-30s %-48s %s" % (e["id"], e["title"], e["file"]))
        return 0
    if not (a.symptom or a.ident or a.klass):
        raise UsageError("give --symptom TEXT, --id ID, --class CLASS or --list")
    found = search(entries, a.symptom, a.ident, a.klass)
    routes = routes_for(a.symptom) if a.symptom else []
    weak = is_weak(found, a.symptom)
    if not found and not routes:
        raise UsageError("no card matches; try --list, or treat it as no card: if the fix is a "
                         "stock structure apply it under Gates 1 to 3 with dev.py oracle")
    if a.json:
        matches = []
        for s, e in found:
            m = dict(e)
            m["score"] = s
            m["fact_sentences"] = fact_sentences(e)
            matches.append(m)
        print_json({"schema": SCHEMA, "query": {"symptom": a.symptom, "id": a.ident,
                                                "class": a.klass}, "weak": weak,
                    "routes": [{"kind": k, "text": t} for k, t in routes],
                    "matches": matches})
        return 0
    for _, text in routes:
        print(text)
    final = any(k in FINAL_ROUTES for k, _ in routes)
    if routes and (final or not found or weak or found[0][0] < WEAK_SCORE + 1):
        return 0                    # the route answers it; weak card matches would mislead
    if routes and found:
        print()
    if weak:
        top = found[0][0]
        why = ("shares %d word with the symptom" % top if top < WEAK_SCORE else
               "shares %d generic symptom words and no title or alias word" % top)
        print("weak match: the best card %s, which is not evidence it applies; read its "
              "precondition and reject-when below before opening the card, and treat a "
              "failed check as no card" % why)
        print()
    for i, (s, e) in enumerate(found):
        if i:
            print()
        print_entry(e, s if a.symptom else None)
    return 0
