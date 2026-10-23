"""Strictly preceding-only next-human-turn examples and conservative, family-level chronological splits.

An example is a human seed turn plus bounded context that existed before it, and the next eligible human turn as the
target. The before-action input ends AT the seed; assistant text and tools after the seed live only in a separate
reaction_input. Nothing here labels a person: no persona, no time-of-day or delay features, no invented success.
A forecast score built on these cases is not an endorsement score.

Splits are decided from family start/end clocks and example counts only, never from targets. Whole families stay in one
partition, straddlers are excluded, and nothing is padded or relaxed when the data is too thin.
"""
from collections import defaultdict, deque
import datetime as dt
import hashlib
import re


class CaseError(ValueError):
    pass


SPLIT_VERSION = "trace-cases-split-1"
ROLES = ("user", "assistant", "tool", "meta")
ACTORS = ("human", "agent", "unknown", "injected")
HIDDEN_KINDS = ("thinking", "reasoning", "redacted_thinking", "encrypted_reasoning")
LONG_CHARS = 80            # an "informative" exact message
LONG_MIN_WORDS = 5
MIRROR_MIN_SHARED = 2
MIRROR_CONTAINMENT = 0.8
MAX_DOC_FREQ = 8           # a long message in more sessions than this is a template, never evidence of a copy
MAX_PRIOR_USERS = 3
MAX_PRIOR_CHARS = 2000
MAX_PRIOR_AGENT_CHARS = 2000
MAX_PRIOR_TOOLS = 8
MAX_SUMMARY_CHARS = 500
MAX_REACTION_TEXT = 4000
MAX_REACTION_TOOLS = 16
UNKNOWN_WEIGHT = 0.25
MIN_SEED_WORDS = 2

_UTC = dt.timezone.utc
_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d+))?)?(Z|[+-]\d{2}:?\d{2})$")


def _parse_time(value):
    """Aware UTC datetime, or None for anything missing, naive or unparseable."""
    if isinstance(value, dt.datetime):
        return value.astimezone(_UTC) if value.tzinfo else None
    if not isinstance(value, str):
        return None
    m = _ISO.match(value.strip())
    if not m:
        return None
    y, mo, d, h, mi, s, frac, tz = m.groups()
    try:
        micro = int((frac + "000000")[:6]) if frac else 0
        if tz == "Z":
            offset = dt.timedelta(0)
        else:
            sign = -1 if tz[0] == "-" else 1
            digits = tz[1:].replace(":", "")
            offset = sign * dt.timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
            if abs(offset) >= dt.timedelta(days=1):
                return None
        local = dt.datetime(int(y), int(mo), int(d), int(h), int(mi), int(s or 0), micro, tzinfo=_UTC)
        return local - offset
    except (ValueError, OverflowError):
        return None


def _iso(value):
    if value is None:
        return None
    fmt = "%Y-%m-%dT%H:%M:%S.%fZ" if value.microsecond else "%Y-%m-%dT%H:%M:%SZ"
    return value.astimezone(_UTC).strftime(fmt)


def _clock_state(raw, parsed):
    if parsed is not None:
        return "ok"
    return "missing" if raw is None else "invalid"


def _sha(*parts):
    h = hashlib.sha1()
    for part in parts:
        h.update(str(part).encode("utf-8", "surrogatepass"))
        h.update(b"\x00")
    return h.hexdigest()


def _text(ev):
    value = ev.get("text")
    return value if isinstance(value, str) else ""


def _is_long(text):
    return len(text) >= LONG_CHARS and len(text.split()) >= LONG_MIN_WORDS


def _session_key(ev):
    return "%s:%s" % (ev["source"], ev["session"])


# ---------------------------------------------------------------- normalisation

def _sessions(events):
    """session_key -> ordered, cross-machine-deduplicated event list. Validates the input."""
    if not isinstance(events, (list, tuple)):
        raise CaseError("events must be a list of dicts")
    by_session = defaultdict(list)
    for idx, ev in enumerate(events):
        if not isinstance(ev, dict):
            raise CaseError("event %d is not a dict" % idx)
        for field in ("source", "session"):
            if not isinstance(ev.get(field), str) or not ev[field]:
                raise CaseError("event %d needs a non-empty string %s" % (idx, field))
        if ev.get("role") not in ROLES:
            raise CaseError("event %d has invalid role %r" % (idx, ev.get("role")))
        if ev.get("actor") not in ACTORS:
            raise CaseError("event %d has invalid actor %r" % (idx, ev.get("actor")))
        ordinal = ev.get("ordinal")
        if ordinal is not None and (isinstance(ordinal, bool) or not isinstance(ordinal, int)):
            raise CaseError("event %d has a non-integer ordinal" % idx)
        by_session[_session_key(ev)].append((idx, ev))
    return {key: _order(key, _dedupe(items)) for key, items in by_session.items()}


def _event_key(ev):
    action = ev.get("action") if isinstance(ev.get("action"), dict) else {}
    ordinal = ev.get("ordinal")
    time = None if ordinal is not None else ev.get("time")
    return (ordinal, ev["role"], ev["actor"], _text(ev), action.get("call_id"), action.get("kind"), time)


def _dedupe(items):
    """One logical session copied to several machines: keep the copy count of the fullest machine, never more.

    Equal events on one machine (a repeated 'keep going') are distinct events and all stay."""
    unique, seen_ids, seen_human_evidence = [], set(), set()
    for item in items:
        ident = item[1].get("id")
        evidence = item[1].get("evidence_key") if item[1]["role"] == "user" and item[1]["actor"] == "human" else None
        if ident and ident in seen_ids:
            continue
        if evidence and evidence in seen_human_evidence:
            continue
        if ident:
            seen_ids.add(ident)
        if evidence:
            seen_human_evidence.add(evidence)
        unique.append(item)
    groups = defaultdict(lambda: defaultdict(list))
    for item in unique:
        groups[_event_key(item[1])][str(item[1].get("machine"))].append(item)
    kept = []
    for per_machine in groups.values():
        best = min(per_machine, key=lambda m: (-len(per_machine[m]), m))
        kept.extend(per_machine[best])
    return kept


def _order(key, items):
    has = [isinstance(ev.get("ordinal"), int) for _, ev in items]
    if any(has) and not all(has):
        raise CaseError("session %s mixes events with and without ordinals" % key)
    if all(has):
        items = sorted(items, key=lambda it: (it[1]["ordinal"], it[0]))
    else:
        items = sorted(items, key=lambda it: it[0])
    return [ev for _, ev in items]


# --------------------------------------------------------------------- families

class _Union:
    def __init__(self):
        self.parent = {}

    def add(self, x):
        self.parent.setdefault(x, x)

    def find(self, x):
        self.add(x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def families(events):
    """session_key -> family_id. Merges explicit parents, byte-identical human trajectories and long-message mirrors."""
    return _families(_sessions(events))


def _families(sessions):
    uf = _Union()
    for key in sessions:
        uf.add(key)
    for key, evs in sessions.items():
        for ev in evs:
            parent = ev.get("parent_session")
            if isinstance(parent, str) and parent:
                uf.union(key, parent if parent in sessions else "%s:%s" % (ev["source"], parent))
    # Original human evidence can be copied into a fork even when every typed
    # word is short. A path-free identity includes native id/text/original time;
    # common phrases at different times do not merge unrelated conversations.
    evidence_owners = {}
    for key, evs in sessions.items():
        for ev in evs:
            ident = ev.get("evidence_key")
            if ev["role"] == "user" and ev["actor"] == "human" and ident:
                if ident in evidence_owners:
                    uf.union(key, evidence_owners[ident])
                else:
                    evidence_owners[ident] = key

    humans = {}
    for key, evs in sessions.items():
        humans[key] = [_text(e) for e in evs if e["role"] == "user" and e["actor"] == "human" and _text(e)]

    # byte-identical copied human trajectories (needs real content: >=2 turns, at least one informative)
    by_trajectory = defaultdict(list)
    for key, texts in humans.items():
        if len(texts) >= 2 and any(_is_long(t) for t in texts):
            by_trajectory[_sha(*texts)].append(key)
    for keys in by_trajectory.values():
        if 1 < len(keys) <= MAX_DOC_FREQ:
            for other in keys[1:]:
                uf.union(keys[0], other)

    # shared long exact messages through an inverted index; templates (high document frequency) are ignored
    long_sets = {}
    postings = defaultdict(list)
    for key, texts in humans.items():
        hashes = sorted({_sha(t) for t in texts if _is_long(t)})
        long_sets[key] = len(hashes)
        for h in hashes:
            postings[h].append(key)
    shared = defaultdict(int)
    for keys in postings.values():
        if 1 < len(keys) <= MAX_DOC_FREQ:
            for i in range(len(keys)):
                for j in range(i + 1, len(keys)):
                    shared[(keys[i], keys[j])] += 1
    for (a, b), count in shared.items():
        smaller = min(long_sets[a], long_sets[b])
        if count >= MIRROR_MIN_SHARED and smaller and count / float(smaller) >= MIRROR_CONTAINMENT:
            uf.union(a, b)

    members = defaultdict(list)
    for key in sessions:
        members[uf.find(key)].append(key)
    out = {}
    for group in members.values():
        fid = "fam_" + _sha(*sorted(group))[:16]
        for key in group:
            out[key] = fid
    return out


# --------------------------------------------------------------------- examples

def _visible_agent_text(ev):
    if ev["role"] != "assistant" or ev["actor"] == "human":
        return ""
    kinds = ev.get("content_kinds")
    if isinstance(kinds, (list, tuple)) and any(k in HIDDEN_KINDS for k in kinds):
        return ""
    return _text(ev)


def _observation(ev):
    action = ev.get("action")
    if not isinstance(action, dict):
        return None
    summary = action.get("summary")
    return {
        "kind": action.get("kind"),
        "name": action.get("name"),
        "call_id": action.get("call_id"),
        "success": action.get("success"),
        "summary": summary[:MAX_SUMMARY_CHARS] if isinstance(summary, str) else summary,
    }


def build_examples(events, allow_unknown=False, max_seed_words=60):
    """Next-human-turn examples. Unknown-actor turns are only used when allow_unknown, as a 0.25-weight stratum."""
    if isinstance(max_seed_words, bool) or not isinstance(max_seed_words, int) or max_seed_words < MIN_SEED_WORDS:
        raise CaseError("max_seed_words must be an int >= %d" % MIN_SEED_WORDS)
    sessions = _sessions(events)
    fams = _families(sessions)
    out = []
    for key in sorted(sessions):
        out.extend(_session_examples(key, sessions[key], fams[key], bool(allow_unknown), max_seed_words))
    return out


def _session_examples(key, evs, family, allow_unknown, max_seed_words):
    examples = []
    prior_users = deque(maxlen=MAX_PRIOR_USERS)
    agent_tail = ""
    tools = deque(maxlen=MAX_PRIOR_TOOLS)
    prior_refs = deque(maxlen=MAX_PRIOR_USERS)
    prior_keys = deque(maxlen=MAX_PRIOR_USERS)
    pending = None
    for pos, ev in enumerate(evs):
        text = _text(ev)
        eligible = (ev["role"] == "user" and text and (ev["actor"] == "human" or (allow_unknown and ev["actor"] == "unknown")))
        if eligible:
            if pending is not None:
                examples.append(_finish(pending, ev))
            pending = None
            words = len(text.split())
            if MIN_SEED_WORDS <= words <= max_seed_words:
                pending = _start(key, family, pos, ev, list(prior_users), agent_tail, list(tools), list(prior_refs), list(prior_keys))
            prior_users.append(text[:MAX_PRIOR_CHARS])
            prior_refs.append(ev.get("source_ref"))
            prior_keys.append(ev.get("evidence_key"))
            continue
        if ev["role"] == "user" and ev["actor"] == "unknown" and pending is not None:
            pending["unknown_between"] += 1
        visible = _visible_agent_text(ev)
        obs = _observation(ev)
        if visible:
            agent_tail = (agent_tail + "\n\n" + visible[-MAX_PRIOR_AGENT_CHARS:] if agent_tail else visible[-MAX_PRIOR_AGENT_CHARS:])[-MAX_PRIOR_AGENT_CHARS:]
            if pending is not None:
                pending["texts"].append(visible)
        if obs is not None:
            tools.append(obs)
            if pending is not None:
                pending["tools"].append(obs)
    return examples


def _start(key, family, pos, ev, prior_users, agent_tail, tools, prior_refs, prior_keys):
    seed_time = _parse_time(ev.get("time"))
    inp = {
        "task": "pre_action_prediction",
        "seed": _text(ev),
        "prior_user_turns": prior_users,
        "prior_agent_text": agent_tail,
        "prior_tools": tools,
        "project": ev.get("project"),
        "time": _iso(seed_time),
    }
    return {
        "key": key, "family": family, "pos": pos, "ev": ev, "input": inp, "seed_time": seed_time,
        "prior_refs": prior_refs, "prior_keys": prior_keys, "texts": [], "tools": [], "unknown_between": 0,
    }


def _finish(p, target_ev):
    ev = p["ev"]
    seed_time = p["seed_time"]
    target_time = _parse_time(target_ev.get("time"))
    if seed_time is None or target_time is None:
        order = "unknown"
    else:
        order = "ok" if target_time >= seed_time else "target_before_seed"
    unknown = ev["actor"] == "unknown" or target_ev["actor"] == "unknown"
    joined = "\n\n".join(p["texts"])
    reaction = dict(p["input"])
    reaction["prior_user_turns"] = list(p["input"]["prior_user_turns"])
    reaction["prior_tools"] = list(p["input"]["prior_tools"])
    reaction["task"] = "post_action_reaction"
    reaction["post_action"] = {
        "assistant_text": joined[:MAX_REACTION_TEXT],
        "assistant_text_truncated": len(joined) > MAX_REACTION_TEXT,
        "tools": p["tools"][-MAX_REACTION_TOOLS:],
        "tools_truncated": len(p["tools"]) > MAX_REACTION_TOOLS,
    }
    seed_ref, target_ref = ev.get("source_ref"), target_ev.get("source_ref")
    return {
        "id": "ex_" + _sha(p["key"], ev.get("id"), ev.get("ordinal"), p["pos"])[:20],
        "family": p["family"],
        "session": p["key"],
        "source": ev["source"],
        "project": ev.get("project"),
        "machine": ev.get("machine"),
        "seed_time": p["input"]["time"],
        "target_time": _iso(target_time),
        "source_refs": {"seed": seed_ref, "target": target_ref, "prior_user_turns": list(p["prior_refs"])},
        "evidence_keys": {"seed": ev.get("evidence_key"), "target": target_ev.get("evidence_key"),
                          "prior_user_turns": list(p["prior_keys"])},
        "input": p["input"],
        "target": {"text": _text(target_ev), "source_ref": target_ref, "time": _iso(target_time)},
        "reaction_input": reaction,
        "weight": UNKNOWN_WEIGHT if unknown else 1.0,
        "origin": "unknown" if unknown else "human",
        "cut": "input ends at the seed turn; reaction_input adds only events strictly between seed and target",
        "audit": {
            "seed_clock": _clock_state(ev.get("time"), seed_time),
            "target_clock": _clock_state(target_ev.get("time"), target_time),
            "clock_order": order,
            "unknown_user_turns_between": p["unknown_between"],
        },
    }


# ------------------------------------------------------------------------ split

def _check_examples(examples):
    if not isinstance(examples, (list, tuple)):
        raise CaseError("examples must be a list of dicts")
    seen = set()
    for ex in examples:
        if not isinstance(ex, dict) or not isinstance(ex.get("id"), str) or not isinstance(ex.get("family"), str):
            raise CaseError("every example needs a string id and family")
        if ex["id"] in seen:
            raise CaseError("duplicate example id %s" % ex["id"])
        seen.add(ex["id"])
    return list(examples)


def _leak_keys(ex):
    """Native user references and long exact human texts an example carries (inputs and target alike)."""
    refs, texts = set(), set()
    evidence = ex.get("evidence_keys") or {}
    if isinstance(evidence, dict):
        refs.update(k for k in [evidence.get("seed"), evidence.get("target")] + list(evidence.get("prior_user_turns") or []) if isinstance(k, str) and k)
    src = ex.get("source_refs")
    if isinstance(src, dict):
        for ref in [src.get("seed"), src.get("target")] + list(src.get("prior_user_turns") or []):
            if isinstance(ref, str) and ref:
                refs.add(ref)
    target = ex.get("target") if isinstance(ex.get("target"), dict) else {}
    if isinstance(target.get("source_ref"), str) and target["source_ref"]:
        refs.add(target["source_ref"])
    inp = ex.get("input") if isinstance(ex.get("input"), dict) else {}
    candidates = [target.get("text"), inp.get("seed")] + list(inp.get("prior_user_turns") or [])
    for t in candidates:
        if isinstance(t, str) and _is_long(t):
            texts.add(_sha(t))
    return refs, texts


def _family_spans(examples):
    fams = {}
    for ex in examples:
        f = fams.setdefault(ex["family"], {"ids": [], "times": [], "bad": False})
        f["ids"].append(ex["id"])
        start, end = _parse_time(ex.get("seed_time")), _parse_time(ex.get("target_time"))
        if start is not None and end is not None and end < start:
            f["bad"] = True
        for name in ("seed_time", "target_time"):
            t = _parse_time(ex.get(name))
            if t is None:
                f["bad"] = True
            else:
                f["times"].append(t)
    for f in fams.values():
        if not f["times"]:
            f["bad"] = True
        f["start"] = min(f["times"]) if f["times"] else None
        f["end"] = max(f["times"]) if f["times"] else None
        f["n"] = len(f["ids"])
    return fams


def _leaks_across(examples, rank_of_family):
    """key -> set of ranks for refs and for long texts, over examples whose family has a rank."""
    refs, texts = defaultdict(dict), defaultdict(dict)
    for ex in examples:
        rank = rank_of_family.get(ex["family"])
        if rank is None:
            continue
        r, t = _leak_keys(ex)
        for k in r:
            refs[k].setdefault(rank, set()).add(ex["family"])
        for k in t:
            texts[k].setdefault(rank, set()).add(ex["family"])
    return refs, texts


def freeze_split(examples, train_fraction=0.7, validation_fraction=0.15, boundary_unit="examples"):
    if boundary_unit not in ("examples", "families"):
        raise CaseError("boundary_unit must be examples or families")
    for name, v in (("train_fraction", train_fraction), ("validation_fraction", validation_fraction)):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise CaseError("%s must be a number" % name)
    if not (0 < train_fraction < 1) or not (0 <= validation_fraction < 1) or train_fraction + validation_fraction >= 1:
        raise CaseError("need 0 < train, 0 <= validation and train + validation < 1")
    examples = _check_examples(examples)
    fams = _family_spans(examples)
    valid = {fid: f for fid, f in fams.items() if not f["bad"]}
    total = sum(f["n"] for f in valid.values())
    partition = {}
    reasons = {}
    for fid, f in fams.items():
        if f["bad"]:
            partition[fid], reasons[fid] = "excluded", "bad_clock"

    b_train = b_val = None
    train, val, test = set(), set(), set()
    if valid:
        cum = 0
        for fid in sorted(valid, key=lambda x: (valid[x]["end"], x)):
            cum += valid[fid]["n"]
            if cum >= train_fraction * total - 1e-9:
                b_train = valid[fid]["end"]
                break
        train = {fid for fid, f in valid.items() if f["end"] <= b_train}
        cands = {fid: f for fid, f in valid.items() if f["start"] > b_train}
        if validation_fraction == 0:
            b_val = b_train
        else:
            cum = 0
            for fid in sorted(cands, key=lambda x: (cands[x]["end"], x)):
                cum += cands[fid]["n"]
                if cum >= validation_fraction * total - 1e-9:
                    b_val = cands[fid]["end"]
                    break
            if b_val is None:      # too thin: validation takes every candidate, nothing is left for test
                b_val = max([f["end"] for f in cands.values()] or [b_train])
            val = {fid for fid, f in cands.items() if f["end"] <= b_val}
        test = {fid for fid, f in valid.items() if f["start"] > b_val}
        if boundary_unit == "families":
            # Family-macro is the estimand. A long conversation must not consume
            # the entire validation budget and push the test fence to the last
            # available session. Choose BOTH fences from all family end clocks,
            # before excluding straddlers and without reading any target content.
            import math
            ends = sorted(f["end"] for f in valid.values())
            b_train = ends[max(0, math.ceil(train_fraction * len(ends))-1)]
            b_val = ends[max(0, math.ceil((train_fraction + validation_fraction) * len(ends))-1)] if validation_fraction else b_train
            train = {fid for fid, f in valid.items() if f["end"] <= b_train}
            val = {fid for fid, f in valid.items() if f["start"] > b_train and f["end"] <= b_val}
            test = {fid for fid, f in valid.items() if f["start"] > b_val}
    for rank, group in enumerate((train, val, test)):
        for fid in group:
            partition[fid] = ("train", "validation", "test")[rank]
    for fid in valid:
        if fid not in partition:
            partition[fid], reasons[fid] = "excluded", "straddles_boundary"

    # shared native references or long exact texts across partitions: the later partition loses the family
    rank_of = {fid: ("train", "validation", "test").index(p) for fid, p in partition.items() if p != "excluded"}
    ref_map, text_map = _leaks_across(examples, rank_of)
    for table in (ref_map, text_map):
        for by_rank in table.values():
            if len(by_rank) > 1:
                keep = min(by_rank)
                for rank, group in by_rank.items():
                    if rank != keep:
                        for fid in group:
                            partition[fid], reasons[fid] = "excluded", "cross_partition_leak"

    ids = {"train": [], "validation": [], "test": [], "excluded": []}
    fam_count = defaultdict(int)
    for fid, p in partition.items():
        fam_count[p] += 1
    for ex in examples:
        ids[partition[ex["family"]]].append(ex["id"])
    for v in ids.values():
        v.sort()
    why = []
    if not ids["train"]:
        why.append("no_train_examples")
    if validation_fraction > 0 and not ids["validation"]:
        why.append("no_validation_examples")
    if not ids["test"]:
        why.append("no_test_examples")
    status = "ok" if not why else "insufficient"
    by_reason = defaultdict(int)
    for fid, r in reasons.items():
        if partition[fid] == "excluded":
            by_reason[r] += fams[fid]["n"]
    return {
        "version": SPLIT_VERSION,
        "status": status,
        "train_ids": ids["train"],
        "validation_ids": ids["validation"],
        "test_ids": ids["test"],
        "excluded_ids": ids["excluded"],
        "family_partition": {fid: partition[fid] for fid in sorted(partition)},
        "excluded_families": {fid: reasons[fid] for fid in sorted(reasons) if partition[fid] == "excluded"},
        "cutoffs": {"train_end": _iso(b_train), "validation_end": _iso(b_val)},
        "counts": {
            "examples": len(examples),
            "train": len(ids["train"]), "validation": len(ids["validation"]),
            "test": len(ids["test"]), "excluded": len(ids["excluded"]),
            "families": len(fams),
            "train_families": fam_count["train"], "validation_families": fam_count["validation"],
            "test_families": fam_count["test"], "excluded_families": fam_count["excluded"],
            "excluded_by_reason": dict(sorted(by_reason.items())),
            "insufficient_reasons": why,
        },
        "protocol": {
            "unit": "family",
            "order": "train < validation < test, strictly by family start and end clocks",
            "boundary_basis": "family start/end times and per-family example counts only; targets never inspected",
            "train_fraction": train_fraction,
            "validation_fraction": validation_fraction,
            "boundary_unit": boundary_unit,
            "straddling_families": "excluded",
            "bad_clock_families": "excluded",
            "leak_rule": "shared native references or long exact texts across partitions exclude the later family",
            "insufficient_data": "reported as status insufficient; boundaries are never relaxed and nothing is padded",
        },
    }


def audit_split(examples, split):
    """Re-check a frozen split against the examples: coverage, families, references, clocks, exact texts."""
    issues = []
    counts = {}
    names = ("train", "validation", "test")
    try:
        examples = _check_examples(examples)
    except CaseError as exc:
        return {"ok": False, "issues": ["examples: %s" % exc], "counts": {}}
    if not isinstance(split, dict):
        return {"ok": False, "issues": ["split is not a dict"], "counts": {}}
    lists = {n: split.get(n + "_ids") for n in names + ("excluded",)}
    for n, v in lists.items():
        if not isinstance(v, list) or not all(isinstance(i, str) for i in v):
            issues.append("split.%s_ids is not a list of id strings" % n)
    if issues:
        return {"ok": False, "issues": issues, "counts": {}}

    by_id = {ex["id"]: ex for ex in examples}
    where = {}
    for n, v in lists.items():
        for i in v:
            if i in where:
                issues.append("id in two partitions: %s (%s, %s)" % (i, where[i], n))
            where[i] = n
            if i not in by_id:
                issues.append("split names unknown example id %s" % i)
    for i in by_id:
        if i not in where:
            issues.append("example missing from split: %s" % i)

    fam_parts = defaultdict(set)
    for i, n in where.items():
        if i in by_id:
            fam_parts[by_id[i]["family"]].add(n)
    for fid, parts in sorted(fam_parts.items()):
        if len(parts) > 1:
            issues.append("family split across partitions: %s %s" % (fid, sorted(parts)))
        declared = (split.get("family_partition") or {}).get(fid)
        if declared is not None and declared not in parts:
            issues.append("family_partition disagrees with ids for %s" % fid)

    kept = [ex for ex in examples if where.get(ex["id"]) in names]
    for ex in kept:
        for name in ("seed_time", "target_time"):
            if _parse_time(ex.get(name)) is None:
                issues.append("bad clock in %s example %s" % (where[ex["id"]], ex["id"]))
                break
    spans = {n: [] for n in names}
    for n in names:
        for fid, f in _family_spans([ex for ex in kept if where[ex["id"]] == n]).items():
            if not f["bad"]:
                spans[n].append((f["start"], f["end"], fid))
    for earlier, later in (("train", "validation"), ("validation", "test"), ("train", "test")):
        if spans[earlier] and spans[later]:
            last_end = max(s[1] for s in spans[earlier])
            first_start = min(s[0] for s in spans[later])
            if first_start <= last_end:
                issues.append("temporal overlap: %s ends %s, %s starts %s" % (earlier, _iso(last_end), later, _iso(first_start)))

    rank_of = {}
    for fid, parts in fam_parts.items():
        part = sorted(parts)[0]
        if len(parts) == 1 and part in names:
            rank_of[fid] = names.index(part)
    ref_map, text_map = _leaks_across(examples, rank_of)
    for label, table in (("reference", ref_map), ("exact-text", text_map)):
        for key, by_rank in sorted(table.items()):
            if len(by_rank) > 1:
                issues.append("%s leak across %s (%s)" % (label, "/".join(names[r] for r in sorted(by_rank)), key[:12]))

    counts = {n: len(lists[n]) for n in lists}
    counts["examples"] = len(examples)
    counts["issues_total"] = len(issues)
    return {"ok": not issues, "issues": issues[:100], "counts": counts}
