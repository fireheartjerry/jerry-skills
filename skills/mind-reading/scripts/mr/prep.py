"""prep: normalize, dedup, twin detection, temporal split, leak removal, chunks, seeds and truth.

Deterministic on the corpus and config. Writes derived/ and eval/<split_id>/ (0700)."""
import datetime as _dt
import hashlib
import json
import math
import os
import shutil
from collections import defaultdict
from pathlib import Path

from .config import read_json, write_json, write_jsonl
from .corpus import load_corpus, normalize, now_iso

TWIN_MIN_NORM_CHARS = 20


class PrepRefused(Exception):
    pass


def parse_time(s):
    if not s or not isinstance(s, str):
        return None
    t = s.strip()
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    try:
        d = _dt.datetime.fromisoformat(t)
    except ValueError:
        try:
            d = _dt.datetime.strptime(t[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d


def _sessions_from_rows(rows):
    groups = defaultdict(list)
    for r in rows:
        groups[(r["source"], r["session"])].append(r)
    sessions = []
    for (source, sid), msgs in groups.items():
        msgs.sort(key=lambda m: ((m.get("time") or "￿"), m.get("ordinal", 0)))
        for m in msgs:
            m["text_norm"] = normalize(m.get("text", ""))
            m["time_reliable"] = True
        times = [parse_time(m.get("time")) for m in msgs]
        times = [t for t in times if t]
        start = min(times) if times else None
        sessions.append({
            "source": source, "session": sid, "id": "%s:%s" % (source, sid), "messages": msgs,
            "project": next((m.get("project") for m in msgs if m.get("project")), None),
            "start": start, "start_iso": msgs[0].get("time") if times else None,
            "time_reliable": True, "span_seconds": (max(times) - min(times)).total_seconds() if times else 0.0,
            "dropped_twin": 0, "dropped_dup": 0, "dropped_leak": 0, "dropped_bulk_copy": 0, "bulk_messages": 0,
        })
    sessions.sort(key=lambda s: (s["start"] or _dt.datetime.max.replace(tzinfo=_dt.timezone.utc), s["id"]))
    return sessions


def flag_bulk_stamped(sessions, p):
    """Mark import artifacts. Per message: when `bulk_cluster_messages` or more messages fall inside any window of
    `bulk_cluster_seconds`, every message in that window gets time_reliable False (a person does not send three
    messages in one second; an import does). Per session: time_reliable False when its first message sits in a cluster
    (the start date is fake, so it can never be test or dev or canonical) or when the whole session fits the legacy rule
    (`bulk_stamp_messages` messages inside `bulk_stamp_seconds`). Returns (flagged session ids, flagged messages)."""
    flagged, n_msgs = [], 0
    gap = float(p.get("bulk_cluster_seconds", 1.0))
    size = int(p.get("bulk_cluster_messages", 3))
    for s in sessions:
        msgs = s["messages"]
        times = [parse_time(m.get("time")) for m in msgs]
        j = 0
        for i, t in enumerate(times):
            if t is None:
                continue
            j = max(j, i)
            while j + 1 < len(times) and times[j + 1] is not None and (times[j + 1] - t).total_seconds() <= gap:
                j += 1
            if j - i + 1 >= size:
                for k in range(i, j + 1):
                    msgs[k]["time_reliable"] = False
        s["bulk_messages"] = sum(1 for m in msgs if not m["time_reliable"])
        n_msgs += s["bulk_messages"]
        legacy = len(msgs) >= p["bulk_stamp_messages"] and s["span_seconds"] < p["bulk_stamp_seconds"]
        if legacy or (msgs and not msgs[0]["time_reliable"]):
            s["time_reliable"] = False
            flagged.append(s["id"])
    return flagged, n_msgs


def _earlier(y, x):
    """Is Y canonical relative to X? A bulk-stamped session never counts as earlier than a reliable one."""
    if y["time_reliable"] != x["time_reliable"]:
        return y["time_reliable"]
    ky = (y["start"] or _dt.datetime.max.replace(tzinfo=_dt.timezone.utc), -len(y["messages"]), y["id"])
    kx = (x["start"] or _dt.datetime.max.replace(tzinfo=_dt.timezone.utc), -len(x["messages"]), x["id"])
    return ky < kx


def detect_twins(sessions, p):
    """Two passes over every session X with at least `twin_min_messages` messages.

    Twin pass: for each earlier session Y (reliable beats bulk-stamped, then start time), when Y contains at least
    `twin_containment` of X's long normalized texts, every X message whose text appears anywhere in Y is dropped.
    Bulk-copy pass: an X message whose own time is a bulk-cluster artifact and whose text appears in any earlier
    reliable session is an import copy and is dropped whatever the containment. Returns (twin pairs, bulk copies)."""
    norm_sets = {s["id"]: {m["text_norm"] for m in s["messages"] if len(m["text_norm"]) >= TWIN_MIN_NORM_CHARS}
                 for s in sessions}
    all_norms = {s["id"]: {m["text_norm"] for m in s["messages"]} for s in sessions}
    pairs, copies = [], []
    for x in sessions:
        if len(x["messages"]) < p["twin_min_messages"]:
            continue
        nx = norm_sets[x["id"]]
        drop_norms = set()
        earlier = [y for y in sessions if y is not x and _earlier(y, x)]
        if nx:
            for y in earlier:
                ny = norm_sets[y["id"]]
                if not ny:
                    continue
                containment = len(nx & ny) / len(nx)
                if containment >= p["twin_containment"]:
                    all_ny = all_norms[y["id"]]
                    dropped_here = sum(1 for m in x["messages"] if m["text_norm"] in all_ny and m["text_norm"] not in drop_norms)
                    drop_norms |= all_ny
                    pairs.append({"session": x["id"], "twin_of": y["id"], "containment": round(containment, 3),
                                  "dropped": dropped_here, "messages_before": len(x["messages"])})
        bulk_norms = set()
        if x["bulk_messages"]:
            reliable_earlier = set()
            for y in earlier:
                if y["time_reliable"]:
                    reliable_earlier |= all_norms[y["id"]]
            bulk_norms = {m["text_norm"] for m in x["messages"] if not m["time_reliable"] and m["text_norm"] in reliable_earlier}
            bulk_norms -= drop_norms
        if drop_norms or bulk_norms:
            kept = []
            for m in x["messages"]:
                if m["text_norm"] in drop_norms:
                    x["dropped_twin"] += 1
                elif m["text_norm"] in bulk_norms and not m["time_reliable"]:
                    x["dropped_bulk_copy"] += 1
                else:
                    kept.append(m)
            if x["dropped_bulk_copy"]:
                copies.append({"session": x["id"], "dropped": x["dropped_bulk_copy"], "messages_before": len(x["messages"])})
            x["messages"] = kept
    return pairs, copies


def dedup_within_session(sessions, p):
    window = _dt.timedelta(minutes=p["within_session_dup_minutes"])
    total = 0
    for s in sessions:
        kept, seen = [], defaultdict(list)
        for m in s["messages"]:
            t = parse_time(m.get("time"))
            dup = False
            for prev in seen[m["text_norm"]]:
                if t is None or prev is None or abs(t - prev) <= window:
                    dup = True
                    break
            if dup:
                total += 1
                continue
            seen[m["text_norm"]].append(t)
            kept.append(m)
        s["dropped_dup"] = len(s["messages"]) - len(kept)
        s["messages"] = kept
    return total


def choose_cutoff(eligible_reliable, p, cutoff):
    """Return the ISO cutoff string. Sessions starting strictly after it are test."""
    if cutoff and cutoff != "auto":
        d = parse_time(cutoff)
        if d is None:
            raise PrepRefused("cutoff is not an ISO date/time: %r" % cutoff)
        return d.isoformat().replace("+00:00", "Z")
    ordered = sorted(eligible_reliable, key=lambda s: (s["start"], s["id"]))
    n = len(ordered)
    if n == 0:
        return now_iso()
    n_test = max(math.ceil(p["test_frac"] * n), min(p.get("min_test_sessions", 10), n // 2))
    n_test = min(n_test, n - 1) if n > 1 else 0
    boundary = ordered[n - n_test - 1] if n_test < n else None
    if boundary is None:
        return ordered[0]["start"].isoformat().replace("+00:00", "Z")
    return boundary["start"].isoformat().replace("+00:00", "Z")


def split_sessions(sessions, p, cutoff, frozen=None):
    """frozen: an earlier split.json. Its cutoff, test_ids, dev_ids, split_id and truth_suffix are reused exactly;
    sessions that started after the cutoff since then join train (listed as post_cutoff_in_train)."""
    if frozen:
        return _split_frozen(sessions, p, frozen)
    eligible = [s for s in sessions if len(s["messages"]) >= p["min_session_messages"]]
    eligible_reliable = [s for s in eligible if s["time_reliable"] and s["start"] is not None]
    cutoff_iso = choose_cutoff(eligible_reliable, p, cutoff)
    cutoff_dt = parse_time(cutoff_iso)
    test = sorted([s for s in eligible_reliable if s["start"] > cutoff_dt], key=lambda s: (s["start"], s["id"]))
    pre = sorted([s for s in eligible_reliable if s["start"] <= cutoff_dt], key=lambda s: (s["start"], s["id"]))
    n_dev = int(round(p["dev_frac"] * len(pre)))
    if n_dev == 0 and len(pre) >= 4:
        n_dev = 1
    dev = pre[len(pre) - n_dev:] if n_dev else []
    dev_ids = {s["id"] for s in dev}
    test_ids = {s["id"] for s in test}
    train, dropped_post = [], []
    for s in sessions:
        if s["id"] in test_ids or s["id"] in dev_ids:
            continue
        if s["time_reliable"] and s["start"] is not None and s["start"] > cutoff_dt:
            dropped_post.append(s["id"])  # ineligible short session after the cutoff: not train, not test
            continue
        train.append(s)
    train.sort(key=lambda s: (s["start"] or _dt.datetime.max.replace(tzinfo=_dt.timezone.utc), s["id"]))
    split_id = hashlib.sha256((cutoff_iso + "\n" + "\n".join(sorted(test_ids))).encode("utf-8")).hexdigest()[:8]
    return {"cutoff": cutoff_iso, "split_id": split_id, "truth_suffix": hashlib.sha256(split_id.encode()).hexdigest()[:4],
            "test": test, "dev": dev, "train": train, "dropped_post_cutoff": dropped_post, "post_cutoff_in_train": [],
            "frozen": False}


def _split_frozen(sessions, p, frozen):
    cutoff_iso = frozen["cutoff"]
    cutoff_dt = parse_time(cutoff_iso)
    test_ids, dev_ids = set(frozen.get("test_ids") or []), set(frozen.get("dev_ids") or [])
    by_id = {s["id"]: s for s in sessions}
    test = sorted([by_id[i] for i in test_ids if i in by_id], key=lambda s: (s["start"] or _dt.datetime.max.replace(tzinfo=_dt.timezone.utc), s["id"]))
    dev = sorted([by_id[i] for i in dev_ids if i in by_id], key=lambda s: (s["start"] or _dt.datetime.max.replace(tzinfo=_dt.timezone.utc), s["id"]))
    train, newer = [], []
    for s in sessions:
        if s["id"] in test_ids or s["id"] in dev_ids:
            continue
        train.append(s)
        if s["time_reliable"] and s["start"] is not None and cutoff_dt is not None and s["start"] > cutoff_dt:
            newer.append(s["id"])
    train.sort(key=lambda s: (s["start"] or _dt.datetime.max.replace(tzinfo=_dt.timezone.utc), s["id"]))
    return {"cutoff": cutoff_iso, "split_id": frozen["split_id"], "truth_suffix": frozen["truth_suffix"],
            "test": test, "dev": dev, "train": train, "dropped_post_cutoff": [], "post_cutoff_in_train": sorted(newer),
            "frozen": True}


def remove_leaks(split, p):
    min_chars = p["leak_min_chars"]

    def long_norms(sessions):
        return {m["text_norm"] for s in sessions for m in s["messages"] if len(m["text_norm"]) >= min_chars}

    def short_norms(sessions):
        return {m["text_norm"] for s in sessions for m in s["messages"] if 0 < len(m["text_norm"]) < min_chars}

    test_long, dev_long = long_norms(split["test"]), long_norms(split["dev"])
    test_short = short_norms(split["test"])
    removed_train = removed_dev = exempt = 0
    for s in split["dev"]:
        kept = [m for m in s["messages"] if m["text_norm"] not in test_long]
        removed_dev += len(s["messages"]) - len(kept)
        exempt += sum(1 for m in kept if m["text_norm"] in test_short)
        s["dropped_leak"] = len(s["messages"]) - len(kept)
        s["messages"] = kept
    banned = test_long | dev_long
    for s in split["train"]:
        kept = [m for m in s["messages"] if m["text_norm"] not in banned]
        removed_train += len(s["messages"]) - len(kept)
        exempt += sum(1 for m in kept if m["text_norm"] in test_short)
        s["dropped_leak"] = len(s["messages"]) - len(kept)
        s["messages"] = kept
    return {"leak_removed_train": removed_train, "leak_removed_dev": removed_dev, "leak_exempt_short": exempt,
            "leak_min_chars": min_chars,
            "leak_exemption": "texts under leak_min_chars normalized characters may appear in every split (generic seeds)"}


def _msg_out(m):
    """A message as stored in derived/sessions.jsonl: full text, real time, per-message reliability."""
    return {"time": m.get("time"), "origin": m.get("origin", "typed"), "text": m.get("text", ""),
            "time_reliable": bool(m.get("time_reliable", True))}


NONTYPED_ORIGINS = ("drafted", "pasted", "goal_reminder")


def chunk_msg(m, p, omitted=None):
    """A message as the miner sees it. Non-typed text (drafted, pasted, goal reminders) is cut to
    `chunk_nontyped_head_chars`; every text is cut to `chunk_message_max_chars`; a bulk-stamped time becomes null.
    `omitted` accumulates the characters cut, for prep-report."""
    text = m.get("text", "") or ""
    origin = m.get("origin", "typed")
    cap = p.get("chunk_message_max_chars") or 0
    if origin in NONTYPED_ORIGINS and p.get("chunk_nontyped_head_chars"):
        cap = min(cap, p["chunk_nontyped_head_chars"]) if cap else p["chunk_nontyped_head_chars"]
    if cap and len(text) > cap:
        cut = len(text) - cap
        text = text[:cap].rstrip() + " [%d chars omitted]" % cut
        if omitted is not None:
            omitted[origin] = omitted.get(origin, 0) + cut
    return {"time": m.get("time") if m.get("time_reliable", True) else None, "origin": origin, "text": text}


def _block(s, part, n_parts, msgs, first_messages, p, omitted=None):
    b = {"source": s["source"], "session": s["session"], "part": "%d/%d" % (part, n_parts), "project": s["project"],
         "start": s["start_iso"] if s["time_reliable"] else None, "time_reliable": s["time_reliable"],
         "first_messages": first_messages if part > 1 else [], "messages": [chunk_msg(m, p, omitted) for m in msgs]}
    return b


def session_blocks(s, p, omitted=None):
    msgs = s["messages"]
    if not msgs:
        return []
    typed = [m for m in msgs if m.get("origin", "typed") == "typed"]
    first_messages = [{"time": cm["time"], "text": cm["text"]} for cm in (chunk_msg(m, p) for m in typed[:2])]
    overhead = len(json.dumps(_block(s, 2, 2, [], first_messages, p), ensure_ascii=False)) + 16
    budget = max(p["part_chars"] - overhead, p["part_chars"] // 2)
    parts, cur, size = [], [], 0
    for m in msgs:
        k = len(json.dumps(chunk_msg(m, p), ensure_ascii=False)) + 2
        if cur and size + k > budget:
            parts.append(cur)
            cur, size = [], 0
        cur.append(m)
        size += k
    if cur:
        parts.append(cur)
    return [_block(s, i + 1, len(parts), part, first_messages, p, omitted) for i, part in enumerate(parts)]


def build_chunks(train, p, omitted=None):
    chunks, cur, size = [], [], 0
    for s in train:
        for b in session_blocks(s, p, omitted):
            n = len(json.dumps(b, ensure_ascii=False)) + 1
            if cur and size + n > p["chunk_chars"]:
                chunks.append(cur)
                cur, size = [], 0
            cur.append(b)
            size += n
    if cur:
        chunks.append(cur)
    return chunks


def no_test_split(sessions, p, train_before):
    """A deployment split that holds nothing out: every session that started strictly before `train_before` (an ISO
    date or time; None means every session) is train. Sessions that started at or after it are excluded, and so are
    sessions whose start time is an import artifact (`time_reliable` false) when a boundary is given, because nothing
    proves they predate it. No test, no dev, no seeds: `eval` refuses such a split."""
    before = None
    if train_before:
        before = parse_time(train_before)
        if before is None:
            raise PrepRefused("--train-before is not an ISO date/time: %r" % train_before)
    train, after, unreliable, trimmed = [], [], [], 0
    for s in sessions:
        if before is not None:
            if not s["time_reliable"] or s["start"] is None:
                unreliable.append(s["id"])
                continue
            if s["start"] >= before:
                after.append(s["id"])
                continue
            # a session that started before the boundary can run past it: drop its later messages too
            kept = [m for m in s["messages"] if (parse_time(m.get("time")) or before) < before]
            trimmed += len(s["messages"]) - len(kept)
            if not kept:
                after.append(s["id"])
                continue
            s = dict(s, messages=kept)
        train.append(s)
    train.sort(key=lambda s: (s["start"] or _dt.datetime.max.replace(tzinfo=_dt.timezone.utc), s["id"]))
    cutoff_iso = before.isoformat().replace("+00:00", "Z") if before else now_iso()
    split_id = hashlib.sha256(("no-test\n" + cutoff_iso).encode("utf-8")).hexdigest()[:8]
    return {"cutoff": cutoff_iso, "split_id": split_id, "truth_suffix": hashlib.sha256(split_id.encode()).hexdigest()[:4],
            "test": [], "dev": [], "train": train, "dropped_post_cutoff": sorted(after), "post_cutoff_in_train": [],
            "frozen": False, "no_test": True, "train_before": cutoff_iso if before else None, "excluded_unreliable": sorted(unreliable),
            "trimmed_after_train_before": trimmed}


def _word_count(text):
    return len((text or "").split())


def context_seed_positions(msgs, p):
    """Indexes of the messages in one session that qualify as mid-session seeds: typed, at most
    `context_seed_max_words` words, at least one earlier message and at least one later message. At most
    `context_seeds_per_session` of them, spaced evenly through the session so a long session is sampled, not
    front-loaded."""
    max_words = int(p.get("context_seed_max_words", 60))
    per = int(p.get("context_seeds_per_session", 2))
    eligible = [i for i, m in enumerate(msgs)
                if 0 < i < len(msgs) - 1 and m.get("origin", "typed") == "typed" and 0 < _word_count(m.get("text")) <= max_words]
    if not eligible or per <= 0:
        return []
    if len(eligible) <= per:
        return eligible
    step = len(eligible) / float(per)
    return [eligible[int(k * step + step / 2.0)] for k in range(per)]


def write_context_seeds(sessions, seeds_dir, truth_dir, p):
    """Mid-session seeds: a short typed message with the previous `context_messages` user messages as context, and the
    user's later messages as truth. Written as c00.json ... beside the first-message seeds. Returns (written, skipped),
    where skipped lists sessions with no qualifying message."""
    seeds_dir.mkdir(parents=True, exist_ok=True)
    truth_dir.mkdir(parents=True, exist_ok=True)
    n_ctx = int(p.get("context_messages", 6))
    ctx_chars = int(p.get("context_max_chars", 1200))
    written, skipped = 0, []
    for s in sessions:
        msgs = s["messages"]
        positions = context_seed_positions(msgs, p)
        if not positions:
            skipped.append(s["id"])
            continue
        for i in positions:
            m = msgs[i]
            sid = "c%02d" % written
            context = [{"time": c.get("time"), "origin": c.get("origin", "typed"), "text": (c.get("text") or "")[:ctx_chars]}
                       for c in msgs[max(0, i - n_ctx):i]]
            write_json(seeds_dir / (sid + ".json"), {"id": sid, "source": s["source"], "session": s["session"],
                                                     "time": m.get("time"), "seed": m.get("text", ""),
                                                     "project": s["project"], "position": i, "context": context})
            later = [{"time": c.get("time"), "origin": c.get("origin", "typed"), "text": (c.get("text") or "")[:p["truth_max_chars"]]}
                     for c in msgs[i + 1:i + 1 + p["truth_max_messages"]]]
            write_json(truth_dir / (sid + ".json"), {"id": sid, "later": later})
            written += 1
    return written, skipped


def write_seeds(sessions, seeds_dir, truth_dir, p):
    """Seeds and truth for sessions whose first message is typed. Returns (written, skipped)."""
    seeds_dir.mkdir(parents=True, exist_ok=True)
    truth_dir.mkdir(parents=True, exist_ok=True)
    written, skipped = 0, []
    for s in sessions:
        if not s["messages"]:
            skipped.append(s["id"])
            continue
        first = s["messages"][0]
        if first.get("origin", "typed") != "typed":
            skipped.append(s["id"])
            continue
        sid = "s%02d" % written
        write_json(seeds_dir / (sid + ".json"), {"id": sid, "source": s["source"], "session": s["session"],
                                                 "time": first.get("time"), "seed": first.get("text", ""),
                                                 "project": s["project"]})
        later = [{"time": m.get("time"), "origin": m.get("origin", "typed"), "text": (m.get("text") or "")[:p["truth_max_chars"]]}
                 for m in s["messages"][1:p["truth_max_messages"]]]
        write_json(truth_dir / (sid + ".json"), {"id": sid, "later": later})
        written += 1
    return written, skipped


def _clear_dir(d):
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True, exist_ok=True)


def run_prep(paths, cfg, cutoff="auto", force=False, keep_split=False, no_twins=False, log=print, no_test=False, train_before=None):
    p = cfg["prep"]
    rows = load_corpus(paths)
    if not rows:
        raise PrepRefused("corpus is empty: run extract first")
    if train_before and not no_test:
        raise PrepRefused("--train-before goes with --no-test (a deployment model with no held-out set); use --cutoff to hold out a test set")
    frozen_split = None
    if keep_split:
        frozen_split = read_json(paths.split)
        if frozen_split and frozen_split.get("no_test"):
            # a kept no-test split re-applies its boundary; there are no test ids to freeze
            no_test, train_before, frozen_split = True, frozen_split.get("train_before"), None
        elif not frozen_split or not frozen_split.get("test_ids"):
            raise PrepRefused("--keep-split needs an existing derived/split.json with test_ids")
    sessions = _sessions_from_rows(rows)
    n_in = sum(len(s["messages"]) for s in sessions)
    bulk, bulk_msgs = flag_bulk_stamped(sessions, p)
    twins, copies = ([], []) if no_twins else detect_twins(sessions, p)
    dups = dedup_within_session(sessions, p)
    if no_test:
        split = no_test_split(sessions, p, train_before)
    else:
        split = split_sessions(sessions, p, cutoff, frozen=frozen_split)
    leaks = remove_leaks(split, p)
    split_dir = paths.eval / split["split_id"]
    frozen = False
    if not no_test and (split_dir / "results").exists():
        if not force:
            # The evaluated held-out set stays exactly as it was scored: derived/ is rebuilt, eval/<split_id> is not touched.
            frozen = True
            log("eval/%s already has results: keeping its seeds and truth frozen (use --force to archive and rewrite)" % split["split_id"])
        else:
            archive = paths.eval / "archive" / ("%s-%s" % (split["split_id"], now_iso()[:10]))
            archive.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(split_dir), str(archive))
            log("archived eval/%s to %s" % (split["split_id"], archive))
    paths.ensure_tree()
    _clear_dir(paths.chunks)
    _clear_dir(paths.dev)
    truth_name, ctx_truth_name = "truth-" + split["truth_suffix"], "truth-context-" + split["truth_suffix"]
    if split_dir.exists() and not frozen and not no_test:
        for sub in ("seeds", truth_name, "seeds-context", ctx_truth_name):
            if (split_dir / sub).exists():
                shutil.rmtree(split_dir / sub)
    omitted = {}
    chunks = build_chunks(split["train"], p, omitted)
    for k, c in enumerate(chunks):
        with open(paths.chunks / ("chunk-%02d.jsonl" % k), "w", encoding="utf-8") as fh:
            for b in c:
                fh.write(json.dumps(b, ensure_ascii=False) + "\n")
    test_ctx_written, test_ctx_skipped, dev_ctx_written, dev_ctx_skipped = 0, [], 0, []
    if no_test:
        test_written, test_skipped, dev_written, dev_skipped = 0, [], 0, []
    elif frozen:
        test_written = len(list((split_dir / "seeds").glob("s*.json")))
        test_skipped = [s["id"] for s in split["test"] if not s["messages"] or s["messages"][0].get("origin", "typed") != "typed"]
        if (split_dir / "seeds-context").exists():
            test_ctx_written = len(list((split_dir / "seeds-context").glob("c*.json")))
        else:
            # a new view of the same frozen sessions: the first-message seeds and truth are untouched
            test_ctx_written, test_ctx_skipped = write_context_seeds(split["test"], split_dir / "seeds-context", split_dir / ctx_truth_name, p)
    else:
        test_written, test_skipped = write_seeds(split["test"], split_dir / "seeds", split_dir / truth_name, p)
        test_ctx_written, test_ctx_skipped = write_context_seeds(split["test"], split_dir / "seeds-context", split_dir / ctx_truth_name, p)
    if not no_test:
        dev_written, dev_skipped = write_seeds(split["dev"], paths.dev / "seeds", paths.dev / truth_name, p)
        dev_ctx_written, dev_ctx_skipped = write_context_seeds(split["dev"], paths.dev / "seeds-context", paths.dev / ctx_truth_name, p)
    try:
        os.chmod(paths.eval, 0o700)
        if split_dir.exists():
            os.chmod(split_dir, 0o700)
    except OSError:
        pass
    split_doc = {
        "cutoff": split["cutoff"], "split_id": split["split_id"], "truth_suffix": split["truth_suffix"],
        "created": now_iso(),
        "counts": {"test": len(split["test"]), "dev": len(split["dev"]), "train": len(split["train"]),
                   "ineligible_post_cutoff_dropped": len(split["dropped_post_cutoff"]), "bulk_stamped": len(bulk),
                   "post_cutoff_in_train": len(split["post_cutoff_in_train"]),
                   "test_seeds": test_written, "dev_seeds": dev_written,
                   "test_context_seeds": test_ctx_written, "dev_context_seeds": dev_ctx_written},
        "frozen": split["frozen"], "no_test": bool(split.get("no_test")), "train_before": split.get("train_before"),
        "test_ids": sorted(s["id"] for s in split["test"]), "dev_ids": sorted(s["id"] for s in split["dev"]),
        "train_ids": sorted(s["id"] for s in split["train"]),
    }
    if no_test:
        split_doc["excluded_after_train_before"] = split["dropped_post_cutoff"]
        split_doc["excluded_unreliable"] = split.get("excluded_unreliable", [])
        split_doc["counts"]["excluded_unreliable"] = len(split.get("excluded_unreliable", []))
    if frozen_split:
        # the held-out set is exactly the one that was minted; keep the ids even if a session is now fully deduplicated
        split_doc["test_ids"] = sorted(frozen_split["test_ids"])
        split_doc["dev_ids"] = sorted(frozen_split.get("dev_ids") or [])
    write_json(paths.split, split_doc)
    all_kept = split["train"] + split["dev"] + split["test"]
    rows_out = []
    for s in sorted(all_kept, key=lambda s: (s["start"] or _dt.datetime.max.replace(tzinfo=_dt.timezone.utc), s["id"])):
        rows_out.append({"source": s["source"], "session": s["session"], "project": s["project"], "start": s["start_iso"],
                         "time_reliable": s["time_reliable"], "split": ("test" if s in split["test"] else "dev" if s in split["dev"] else "train"),
                         "dropped": {"twin": s["dropped_twin"], "duplicate": s["dropped_dup"], "leak": s["dropped_leak"],
                                     "bulk_copy": s["dropped_bulk_copy"]},
                         "bulk_messages": s["bulk_messages"],
                         "messages": [_msg_out(m) for m in s["messages"]]})
    write_jsonl(paths.sessions, rows_out)
    report = {
        "created": now_iso(), "messages_in": n_in, "messages_out": sum(len(s["messages"]) for s in all_kept),
        "sessions": len(sessions), "bulk_stamped_sessions": bulk, "bulk_stamped_messages": bulk_msgs,
        "bulk_copies": copies, "bulk_copies_removed": sum(c["dropped"] for c in copies), "twins": twins,
        "within_session_duplicates_removed": dups, "ineligible_post_cutoff_dropped": split["dropped_post_cutoff"],
        "post_cutoff_in_train": split["post_cutoff_in_train"], "split_frozen": split["frozen"],
        "no_test": bool(split.get("no_test")), "train_before": split.get("train_before"),
        "excluded_unreliable": split.get("excluded_unreliable", []),
        "seeds_skipped_non_typed_first": {"test": test_skipped, "dev": dev_skipped},
        "context_seeds_skipped_no_mid_session_message": {"test": test_ctx_skipped, "dev": dev_ctx_skipped},
        "context_seed_rules": {"max_words": p.get("context_seed_max_words", 60), "per_session": p.get("context_seeds_per_session", 2),
                               "context_messages": p.get("context_messages", 6)},
        "chunks": len(chunks), "chunk_chars": [sum(len(json.dumps(b, ensure_ascii=False)) + 1 for b in c) for c in chunks],
        "chunk_chars_omitted": omitted, "chunk_caps": {"message_max_chars": p.get("chunk_message_max_chars"),
                                                       "nontyped_head_chars": p.get("chunk_nontyped_head_chars")},
        "no_twins": no_twins,
    }
    report.update(leaks)
    write_json(paths.prep_report, report)
    log("prep: %d sessions, %d -> %d messages, twins %d, bulk copies %d, dups %d, split %s%s (test %d, dev %d, train %d), %d chunks" % (
        len(sessions), n_in, report["messages_out"], sum(t["dropped"] for t in twins), report["bulk_copies_removed"], dups,
        split["split_id"], " (kept)" if split["frozen"] else (" (no test, train before %s)" % split.get("train_before") if no_test else ""),
        len(split["test"]), len(split["dev"]), len(split["train"]), len(chunks)))
    if no_test:
        log("prep: no held-out set; %d session(s) at or after the boundary and %d with unreliable start times were excluded; eval refuses this split" % (
            len(split["dropped_post_cutoff"]), len(split.get("excluded_unreliable", []))))
    elif test_ctx_written or dev_ctx_written:
        log("prep: context seeds %d test, %d dev (eval --mode context)" % (test_ctx_written, dev_ctx_written))
    if split["post_cutoff_in_train"]:
        log("prep: %d session(s) newer than the kept cutoff joined train; mint a new test set with --cutoff <date> when enough exist" % len(split["post_cutoff_in_train"]))
    return {"split": split_doc, "report": report}


def chunk_files(paths):
    return sorted(Path(paths.chunks).glob("chunk-*.jsonl"))
