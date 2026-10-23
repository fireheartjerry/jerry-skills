"""Conditional preference reasoning over notebook records. Pure: no I/O, no clock, no engine calls.

Records are plain dicts (kinds: preference, alias, counterexample; other kinds such as facts are accepted but never
become habits). Malformed or unsupported records are skipped with a warning instead of widening a condition.
Conditions are literal casefolded phrases bounded by word edges, never regular expressions. A record can only
shape suggestions: nothing here infers traits, grants permission or executes an alias expansion. Confidence is the
record's own S/L/G tag; a G (guess) record is never applied."""
from datetime import datetime, timezone

CONDITION_KEYS = ("project", "phase", "terms", "unless")
FORGOTTEN = ("forgotten", "deleted", "removed", "retracted")
HABIT_KINDS = ("preference", "alias", "counterexample")
CONFIDENCE_ORDER = {"S": 0, "L": 1, "G": 2}
MISSING = "no record with this id applies in the current scope"
PRECEDENCE = "the current request overrides any record; a record is a default, not an instruction"


class HabitError(ValueError):
    pass


class _Bad(ValueError):
    pass


def _one_line(s):
    return " ".join(str(s).split())


def _fold(s):
    return " ".join(s.casefold().split())


def _wordch(c):
    return bool(c) and (c.isalnum() or c == "_")


def _has_phrase(hay, phrase):
    """True when `phrase` occurs in `hay` (both already folded) with no word character touching either edge."""
    if not phrase:
        return False
    n, start = len(phrase), 0
    while True:
        i = hay.find(phrase, start)
        if i < 0:
            return False
        before = hay[i - 1] if i else ""
        after = hay[i + n] if i + n < len(hay) else ""
        if not _wordch(before) and not _wordch(after):
            return True
        start = i + 1


def _when(value, what):
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            dt = datetime.fromtimestamp(value, timezone.utc)
        except (OverflowError, OSError, ValueError):
            raise _Bad("%s is not a valid time" % what)
    elif isinstance(value, str) and value.strip():
        s = value.strip()
        if s[-1] in "zZ":
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            raise _Bad("%s is not an ISO time" % what)
    else:
        raise _Bad("%s is not a valid time" % what)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _opt_str(rec, name):
    v = rec.get(name)
    if v is None:
        return None
    if not isinstance(v, str):
        raise _Bad("%s must be a string" % name)
    return v.strip() or None


def _phrase_list(values, name):
    out, seen = [], set()
    for v in values:
        if not isinstance(v, str) or not v.strip():
            raise _Bad("%s entries must be non-empty strings" % name)
        f = _fold(v)
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def _listish(v, name):
    if v is None:
        return []
    if not isinstance(v, list):
        raise _Bad("%s must be a list" % name)
    return v


def _single(rec, cond, name):
    vals = []
    for v in (rec.get(name), cond.get(name)):
        if v is None:
            continue
        if not isinstance(v, str):
            raise _Bad("%s must be a string" % name)
        if v.strip():
            vals.append(v.strip())
    if len(set(vals)) > 1:
        raise _Bad("conflicting %s" % name)
    return vals[0] if vals else None


def _norm(rec, active_project):
    """("skip",) for another project, ("error", msg, visible_id) for a bad record, ("ok", item) otherwise."""
    if not isinstance(rec, dict):
        return ("error", "not an object", None)
    cond = rec.get("condition")
    if cond is None:
        cond = {}
    if not isinstance(cond, dict):
        return ("error", "condition must be an object", None)
    try:
        project = _single(rec, cond, "project")
    except _Bad as e:
        return ("error", str(e), None)
    if project is not None and project != active_project:
        return ("skip",)
    rid = rec.get("id").strip() if isinstance(rec.get("id"), str) and rec.get("id").strip() else None
    try:
        if rid is None:
            raise _Bad("id must be a non-empty string")
        bad = sorted(str(k) for k in cond if k not in CONDITION_KEYS)
        if bad:
            raise _Bad("unsupported condition key(s): %s" % ", ".join(bad))
        kind = rec.get("kind")
        if not isinstance(kind, str) or not kind.strip():
            raise _Bad("kind must be a non-empty string")
        kind = kind.strip().casefold()
        text, key, value = _opt_str(rec, "text"), _opt_str(rec, "key"), _opt_str(rec, "value")
        source_ref = _opt_str(rec, "source_ref")
        phase = _single(rec, cond, "phase")
        terms = _phrase_list(_listish(rec.get("terms"), "terms") + _listish(cond.get("terms"), "terms"), "terms")
        unless = _phrase_list(_listish(rec.get("unless"), "unless") + _listish(cond.get("unless"), "unless"), "unless")
        counter_to = _listish(rec.get("counter_to"), "counter_to")
        if any(not isinstance(c, str) or not c.strip() for c in counter_to):
            raise _Bad("counter_to entries must be non-empty strings")
        counter_to = sorted(set(c.strip() for c in counter_to))
        status = _opt_str(rec, "status") or "active"
        conf = rec.get("confidence")
        conf = conf.strip().upper() if isinstance(conf, str) else conf
        if conf not in CONFIDENCE_ORDER:
            raise _Bad("confidence must be S, L or G")
        expires = rec.get("expires_at")
        expires = None if expires is None else _when(expires, "expires_at")
        if kind in HABIT_KINDS:
            if not (text or value):
                raise _Bad("%s needs text or value" % kind)
            if not source_ref:
                raise _Bad("%s needs a source_ref" % kind)
        if kind == "alias" and not key:
            raise _Bad("alias needs a key phrase")
        if kind == "counterexample" and not counter_to:
            raise _Bad("counterexample needs counter_to")
    except _Bad as e:
        return ("error", str(e), rid)
    shown = text or value
    return ("ok", {
        "id": rid, "kind": kind, "text": shown, "value": value, "key": key, "source_ref": source_ref,
        "project": project, "phase": phase, "terms": terms, "unless": unless, "counter_to": counter_to,
        "status": status, "confidence": conf, "expires_at": rec.get("expires_at") if isinstance(rec.get("expires_at"), str) else None,
        "_expires": expires, "_key": _fold(key) if key else "", "_pos": _fold(value or text or ""),
        "also": [],
    })


class _Ctx(object):
    def __init__(self, seed, project, phase, now):
        if not isinstance(seed, str):
            raise HabitError("seed must be a string")
        for name, v in (("project", project), ("phase", phase)):
            if v is not None and not isinstance(v, str):
                raise HabitError("%s must be a string or None" % name)
        self.seed = seed
        self.seed_n = _fold(seed)
        self.project = (project or "").strip() or None
        self.phase = (phase or "").strip() or None
        self.phase_n = self.phase.casefold() if self.phase else None
        try:
            self.now = None if now is None else _when(now, "now")
        except _Bad as e:
            raise HabitError(str(e))


def _prepare(records, ctx):
    if not isinstance(records, list):
        raise HabitError("records must be a list")
    items, warnings, seen = [], [], set()
    for idx, rec in enumerate(records):
        res = _norm(rec, ctx.project)
        if res[0] == "skip":
            continue
        if res[0] == "error":
            warnings.append("record %d%s skipped: %s" % (idx, " (%s)" % res[2] if res[2] else "", res[1]))
            continue
        it = res[1]
        if it["id"] in seen:
            warnings.append("record %d (%s) skipped: duplicate id" % (idx, it["id"]))
            continue
        seen.add(it["id"])
        items.append(it)
    items.sort(key=lambda i: i["id"])
    kept, sigs = [], {}
    for it in items:
        sig = (it["kind"], it["project"], it["phase"], it["_key"], _fold(it["text"] or ""), it["_pos"],
               tuple(it["terms"]), tuple(it["unless"]), it["status"].casefold(), it["confidence"],
               it["expires_at"], tuple(it["counter_to"]))
        if sig in sigs:
            sigs[sig]["also"].append(it["id"])
        else:
            sigs[sig] = it
            kept.append(it)
    return kept, warnings


def _evaluate(it, ctx):
    """(outcome, reasons). Checks run cheapest-first; `vetoed` is reached only when everything else holds."""
    status = it["status"].casefold()
    if status in FORGOTTEN:
        return "forgotten", ["status is %s; the record is not used" % status]
    if status != "active":
        return "inactive", ["status is %s, not active" % status]
    if it["_expires"] is not None:
        if ctx.now is None:
            return "expiry_unchecked", ["expires_at is set but no current time was given; not applied"]
        if ctx.now >= it["_expires"]:
            return "expired", ["expired at %s" % it["expires_at"]]
    if it["confidence"] == "G":
        return "low_confidence", ["confidence G is a guess; it is not applied"]
    reasons = ["global record" if it["project"] is None else "project %s matches" % it["project"]]
    if it["phase"]:
        if ctx.phase_n is None or ctx.phase_n != it["phase"].casefold():
            return "phase_mismatch", ["requires phase %s" % it["phase"]]
        reasons.append("phase %s matches" % it["phase"])
    if it["kind"] == "alias" and not _has_phrase(ctx.seed_n, it["_key"]):
        return "terms_unmet", ["alias phrase %s is not in the request" % it["key"]]
    missing = [t for t in it["terms"] if not _has_phrase(ctx.seed_n, t)]
    if missing:
        return "terms_unmet", ["missing required phrase(s): %s" % ", ".join(missing)]
    if it["terms"]:
        reasons.append("all required phrases present: %s" % ", ".join(it["terms"]))
    vetoes = [u for u in it["unless"] if _has_phrase(ctx.seed_n, u)]
    if vetoes:
        return "vetoed", ["unless phrase present: %s" % ", ".join(vetoes)], vetoes
    if it["unless"]:
        reasons.append("no unless phrase present")
    return "applies", reasons


def _eval(it, ctx):
    r = _evaluate(it, ctx)
    return r[0], r[1], (r[2] if len(r) > 2 else [])


def _habit_row(it, reasons):
    return {"id": it["id"], "text": it["text"], "key": it["key"], "value": it["value"],
            "source_ref": it["source_ref"], "project": it["project"],
            "scope": "project" if it["project"] else "global", "phase": it["phase"],
            "confidence": it["confidence"], "terms": list(it["terms"]), "unless": list(it["unless"]),
            "also": list(it["also"]), "in_conflict": [], "reasons": reasons}


def _find_conflicts(habits):
    groups = {}
    for h in habits:
        if h["key"]:
            groups.setdefault(_fold(h["key"]), []).append(h)
    out = []
    for k in sorted(groups):
        g = sorted(groups[k], key=lambda h: h["id"])
        pos = {}
        for h in g:
            pos.setdefault(_fold(h["value"] or h["text"]), []).append(h)
        if len(pos) < 2:
            continue
        proj = [h for h in g if h["project"]]
        glob = [h for h in g if not h["project"]]
        preferred, why = [], "no scope precedence applies"
        if proj and glob:
            if len(set(_fold(h["value"] or h["text"]) for h in proj)) == 1:
                preferred = [h["id"] for h in proj]
                why = "explicit project record outranks global; both stay visible"
            else:
                why = "project records disagree with each other"
        ids = [h["id"] for h in g]
        for h in g:
            h["in_conflict"] = [i for i in ids if i != h["id"]]
        out.append({"key": g[0]["key"], "ids": ids, "preferred": preferred, "resolution": why,
                    "records": [{"id": h["id"], "text": h["text"], "value": h["value"], "project": h["project"],
                                 "source_ref": h["source_ref"], "confidence": h["confidence"]} for h in g]})
    return out


def select(records, seed, project=None, phase=None, now=None, limit=8):
    ctx = _Ctx(seed, project, phase, now)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise HabitError("limit must be a positive integer")
    items, warnings = _prepare(records, ctx)
    habits, exceptions, cands, aliases = [], [], [], []
    for it in items:
        outcome, reasons, vetoes = _eval(it, ctx)
        if outcome == "low_confidence" and it["kind"] in HABIT_KINDS:
            warnings.append("record %s not applied: confidence G" % it["id"])
        if it["kind"] == "preference":
            if outcome == "applies":
                habits.append(_habit_row(it, reasons))
            elif outcome == "vetoed":
                exceptions.append({
                    "id": it["id"], "text": it["text"], "source_ref": it["source_ref"], "project": it["project"],
                    "confidence": it["confidence"], "vetoed_by": vetoes,
                    "explanation": "otherwise relevant, but the request contains unless phrase %s" % ", ".join(
                        '"%s"' % v for v in vetoes)})
        elif outcome == "applies" and it["kind"] == "alias":
            aliases.append({
                "id": it["id"], "phrase": it["key"], "meaning": it["value"] or it["text"],
                "source_ref": it["source_ref"], "project": it["project"], "confidence": it["confidence"],
                "seed": ctx.seed,
                "annotation": "the request says %s; record %s says it means %s; the request text is unchanged "
                              "and the meaning is not executed" % (it["key"], it["id"], it["value"] or it["text"])})
        elif outcome == "applies" and it["kind"] == "counterexample":
            cands.append(it)
    conflicts = _find_conflicts(habits)
    habits.sort(key=lambda h: (h["project"] is None, CONFIDENCE_ORDER[h["confidence"]], h["id"]))
    omitted = max(0, len(habits) - limit)
    habits = habits[:limit]
    shown = set(h["id"] for h in habits)
    counters = []
    for it in cands:
        linked = [c for c in it["counter_to"] if c in shown]
        if linked:
            counters.append({"id": it["id"], "text": it["text"], "source_ref": it["source_ref"],
                             "project": it["project"], "confidence": it["confidence"], "counter_to": linked})
    exceptions.sort(key=lambda e: e["id"])
    counters.sort(key=lambda c: c["id"])
    aliases.sort(key=lambda a: (a["phrase"].casefold(), a["id"]))
    return {"habits": habits, "exceptions": exceptions, "conflicts": conflicts, "counterexamples": counters,
            "aliases": aliases, "omitted": omitted, "warnings": sorted(warnings), "precedence": PRECEDENCE}


def _public(it):
    return {"id": it["id"], "kind": it["kind"], "text": it["text"], "key": it["key"], "value": it["value"],
            "source_ref": it["source_ref"], "project": it["project"], "phase": it["phase"],
            "confidence": it["confidence"], "status": it["status"], "expires_at": it["expires_at"],
            "terms": list(it["terms"]), "unless": list(it["unless"]), "counter_to": list(it["counter_to"])}


def explain(records, record_id, seed="", project=None, phase=None, now=None):
    ctx = _Ctx(seed, project, phase, now)
    if not isinstance(record_id, str) or not record_id.strip():
        raise HabitError("record_id must be a non-empty string")
    if not isinstance(records, list):
        raise HabitError("records must be a list")
    rid = record_id.strip()
    missing = {"id": rid, "outcome": "missing", "applies": False, "record": None, "reasons": [MISSING]}
    for rec in records:
        if not (isinstance(rec, dict) and isinstance(rec.get("id"), str) and rec["id"].strip() == rid):
            continue
        res = _norm(rec, ctx.project)
        if res[0] == "skip":
            continue  # another project's record is indistinguishable from an absent one
        if res[0] == "error":
            return {"id": rid, "outcome": "malformed", "applies": False, "record": {"id": rid},
                    "reasons": ["record is malformed: %s" % res[1]]}
        it = res[1]
        outcome, reasons, _ = _eval(it, ctx)
        if outcome == "forgotten":
            return {"id": rid, "outcome": "forgotten", "applies": False,
                    "record": {"id": rid, "status": it["status"]}, "reasons": reasons}
        applies = outcome == "applies"
        if applies and it["kind"] == "counterexample":
            items, _w = _prepare(records, ctx)
            live = set(i["id"] for i in items if i["kind"] == "preference" and _eval(i, ctx)[0] == "applies")
            linked = [c for c in it["counter_to"] if c in live]
            if linked:
                reasons.append("counters applicable habit(s): %s" % ", ".join(linked))
            else:
                applies, outcome = False, "unlinked"
                reasons = ["none of the habits it counters applies here: %s" % ", ".join(it["counter_to"])]
        if applies:
            reasons = reasons + ["confidence %s is the record's own tag" % it["confidence"]]
            if it["kind"] not in HABIT_KINDS:
                reasons.append("kind %s is context, not a habit" % it["kind"])
            if ctx.seed.strip():
                reasons.append(PRECEDENCE)
        return {"id": rid, "outcome": outcome, "applies": applies, "record": _public(it),
                "source_ref": it["source_ref"], "reasons": reasons}
    return missing


def _where(project):
    return "project: %s" % project if project else "global"


def _habit_line(h):
    extra = ""
    if h.get("in_conflict"):
        extra += "; conflicts with %s" % ", ".join(h["in_conflict"])
    if h.get("also"):
        extra += "; duplicates %s" % ", ".join(h["also"])
    return '- [%s] "%s" (%s; confidence %s; source: %s%s)' % (
        h.get("id"), _one_line(h.get("text")), _where(h.get("project")), h.get("confidence"), h.get("source_ref"), extra)


def _exception_line(e):
    return '- [%s] "%s" (%s; source: %s) %s' % (
        e.get("id"), _one_line(e.get("text")), _where(e.get("project")), e.get("source_ref"), _one_line(e.get("explanation")))


def _conflict_line(c, full):
    head = '- key "%s": %s' % (_one_line(c.get("key")), " vs ".join("[%s]" % r["id"] for r in c.get("records", [])))
    parts = []
    for r in c.get("records", []):
        parts.append('[%s] %s (%s; source: %s)' % (
            r["id"], '"%s"' % _one_line(r.get("value") or r.get("text")) if full else "text omitted for budget",
            _where(r.get("project")), r.get("source_ref")))
    pref = "prefer %s" % ", ".join(c["preferred"]) if c.get("preferred") else "no preference"
    return "%s: %s; %s (%s)" % (head, "; ".join(parts), pref, _one_line(c.get("resolution")))


def _counter_line(c):
    return '- [%s] "%s" counters %s (%s; source: %s)' % (
        c.get("id"), _one_line(c.get("text")), ", ".join(c.get("counter_to", [])), _where(c.get("project")), c.get("source_ref"))


def _alias_line(a):
    return '- "%s" may mean "%s" [%s] (source: %s); the original request is kept, not expanded' % (
        _one_line(a.get("phrase")), _one_line(a.get("meaning")), a.get("id"), a.get("source_ref"))


SECTIONS = (("Conditional preferences", "habits", _habit_line), ("Exceptions", "exceptions", _exception_line),
            ("Conflicts", "conflicts", None), ("Counterexamples", "counterexamples", _counter_line),
            ("Shorthand", "aliases", _alias_line))


def render(result, budget_chars=6000):
    """Markdown within budget_chars. Items are kept whole or dropped and counted; conflicts are never dropped.
    If even the compact conflicts do not fit, a one-line over-budget hint is returned instead."""
    if isinstance(budget_chars, bool) or not isinstance(budget_chars, int) or budget_chars < 1:
        raise HabitError("budget_chars must be a positive integer")
    if not isinstance(result, dict):
        raise HabitError("result must be a dict")
    rows = {k: [r for r in (result.get(k) or []) if isinstance(r, dict)] for _t, k, _f in SECTIONS}
    conflicts = rows["conflicts"]
    notes = []
    if result.get("omitted"):
        notes.append("%d lower-priority preference(s) were cut by the selection limit" % result["omitted"])
    if result.get("warnings"):
        notes.append("%d record(s) skipped; see warnings" % len(result["warnings"]))
    skeleton = ["Records are defaults; the current request overrides every one of them."]
    for title, key, _f in SECTIONS:
        skeleton.append("## %s" % title)
        if not rows[key]:
            skeleton.append("none")
    compact = [_conflict_line(c, False) for c in conflicts]
    fixed = "\n".join(skeleton + compact + ["_%s._" % n for n in notes])
    spare = sum(len(rows[k]) for _t, k, _f in SECTIONS if k != "conflicts")
    full_foot = "_Truncated to fit budget_chars=%d: omitted %d item(s)._" % (budget_chars, spare)
    need = len(fixed) + (1 + len(full_foot) if spare else 0)
    if need > budget_chars:
        hint = "Over budget: %d conflict(s) need at least %d chars; raise budget_chars. ids: %s" % (
            len(conflicts), need, ", ".join(i for c in conflicts for i in c.get("ids", [])))
        short = "Over budget: conflicts need at least %d chars." % need
        return (hint if len(hint) <= budget_chars else short)[:budget_chars]
    used = need + (0 if spare else 1 + len(full_foot))
    chosen = {k: [] for _t, k, _f in SECTIONS}
    for i, c in enumerate(conflicts):
        delta = len(_conflict_line(c, True)) - len(compact[i])
        if used + delta <= budget_chars:
            used += delta
            chosen["conflicts"].append(_conflict_line(c, True))
        else:
            chosen["conflicts"].append(compact[i])
    dropped = {}
    for title, key, fmt in SECTIONS:
        if key == "conflicts":
            continue
        for r in rows[key]:
            line = fmt(r)
            if used + len(line) + 1 <= budget_chars:
                used += len(line) + 1
                chosen[key].append(line)
            else:
                dropped[key] = dropped.get(key, 0) + 1
    out = [skeleton[0]]
    for title, key, _f in SECTIONS:
        out.append("## %s" % title)
        out.extend(chosen[key] or (["none"] if not rows[key] else []))
    out.extend("_%s._" % n for n in notes)
    if dropped:
        out.append("_Truncated to fit budget_chars=%d: omitted %d item(s)._" % (budget_chars, sum(dropped.values())))
    text = "\n".join(out)
    return text
