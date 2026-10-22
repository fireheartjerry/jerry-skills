"""Incremental chunks for refresh --auto: mine only what is new since the last build.

A full `prep` regroups every session into chunks, so one new session can shift every chunk after it and force a
re-mine of all of them. With pins, chunk membership is fixed once a chunk exists: `derived/chunk-pins.json` lists, for
each chunk, its blocks as (session id, part label, corpus record keys). A pinned chunk is re-rendered from exactly its
pinned messages, so its bytes, its hash and the mined outputs reused for it stay the same. Every train message no pin
holds is new material: a new session, or later messages of a session already pinned (its continuation blocks number
their parts after the pinned ones and carry the session's first messages). New material is packed into new chunks,
numbered after the last one, once it reaches `min_new_chars` (otherwise it waits for the next run), in chunks of at most
`delta_chunk_chars` of the user's text so one capped run can mine them.

The first pinned run bootstraps the pins from the chunk files a full prep wrote: each block is matched to the run of
session messages it was rendered from. A block that no longer matches (its messages were deduplicated away since, or
the corpus was rescrubbed) is left out, so its chunk re-renders without it and is mined again; nothing is lost, it only
costs that chunk's re-mine. A full `prep` deletes the pins."""
import json
import re
from collections import defaultdict

from . import prep as _prep
from .config import read_json, write_json
from .corpus import now_iso

PINS_VERSION = 1
_VIEW_KEYS = ("time", "origin", "text")


def _num(cid):
    m = re.match(r"chunk-(\d+)$", cid or "")
    return int(m.group(1)) if m else -1


def _part(label):
    m = re.match(r"^(\d+)/(\d+)$", str(label or ""))
    return (int(m.group(1)), int(m.group(2))) if m else (1, 1)


def match_block(session, block, p):
    """Corpus keys of the contiguous run of session messages a chunk block was rendered from, or None."""
    target = [{k: bm.get(k) for k in _VIEW_KEYS} for bm in block.get("messages") or []]
    if not session or not target:
        return None
    msgs = session["messages"]
    views = [_prep.chunk_msg(m, p) for m in msgs]
    n = len(target)
    for i in range(len(views) - n + 1):
        if views[i] == target[0] and views[i:i + n] == target:
            keys = [m.get("key") for m in msgs[i:i + n]]
            return keys if all(keys) else None
    return None


def bootstrap(train, p, prior):
    """Pins derived from chunk files a full prep wrote. Returns (pins, blocks that could not be matched)."""
    by_id = {s["id"]: s for s in train}
    chunks, unmatched = [], 0
    for cid, raw in prior.items():
        blocks = []
        for line in raw.decode("utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                b = json.loads(line)
            except json.JSONDecodeError:
                unmatched += 1
                continue
            sid = "%s:%s" % (b.get("source"), b.get("session"))
            keys = match_block(by_id.get(sid), b, p)
            if not keys:
                unmatched += 1
                continue
            blocks.append({"session": sid, "part": b.get("part") or "1/1", "keys": keys})
        chunks.append({"id": cid, "blocks": blocks})
    return {"version": PINS_VERSION, "created": now_iso(), "bootstrapped": True, "chunks": chunks}, unmatched


def _render(blocks):
    return "".join(json.dumps(b, ensure_ascii=False) + "\n" for b in blocks).encode("utf-8")


def pinned_chunks(paths, train, p, prior, omitted=None, ctx_stats=None, min_new_chars=0, delta_chunk_chars=None, log=print):
    """[(chunk id, blocks)] for every chunk, pinned ones first, then new delta chunks; and an info dict. Writes the pins.
    `prior` maps chunk id to the bytes of the chunk file before this run."""
    info = {"bootstrapped": False, "bootstrap_unmatched": 0, "pinned_chunks": 0, "changed_chunks": [], "new_chunks": [],
            "new_chars": 0, "deferred_chars": 0}
    pins = read_json(paths.chunk_pins)
    if not isinstance(pins, dict) or pins.get("version") != PINS_VERSION:
        if prior:
            pins, unmatched = bootstrap(train, p, prior)
            info.update(bootstrapped=True, bootstrap_unmatched=unmatched)
            log("prep: pinned %d existing chunk(s) for incremental refresh%s" % (
                len(pins["chunks"]), (" (%d block(s) no longer match and will be re-mined)" % unmatched) if unmatched else ""))
        else:
            pins = {"version": PINS_VERSION, "created": now_iso(), "chunks": []}
    by_id = {s["id"]: s for s in train}
    session_keys, session_nparts = defaultdict(list), defaultdict(int)
    for c in pins["chunks"]:
        for blk in c["blocks"]:
            session_keys[blk["session"]] += blk["keys"]
            session_nparts[blk["session"]] += 1
    pinned = {k for keys in session_keys.values() for k in keys}
    cap = _prep.chunk_context_cap(p)

    def first_of(s):
        held = set(session_keys[s["id"]])
        return _prep.first_messages_of([m for m in s["messages"] if m.get("key") in held], p)

    named = []
    for c in pins["chunks"]:
        blocks = []
        for blk in c["blocks"]:
            s = by_id.get(blk["session"])
            if s is None:
                continue
            keyset = set(blk["keys"])
            msgs = [m for m in s["messages"] if m.get("key") in keyset]
            if not msgs:
                continue
            k, n = _part(blk["part"])
            b = _prep._block(s, k, n, msgs, first_of(s), p, omitted, ctx_stats)
            if _prep.context_chars(b) > cap:
                _prep.cap_block_context(b, cap, ctx_stats)
            blocks.append(b)
        if blocks:
            named.append((c["id"], blocks))
            if prior.get(c["id"]) != _render(blocks):
                info["changed_chunks"].append(c["id"])
    info["pinned_chunks"] = len(named)

    delta = [(s, [m for m in s["messages"] if m.get("key") not in pinned]) for s in train]
    delta = [(s, new) for s, new in delta if new]
    info["new_chars"] = sum(len(json.dumps(_prep.chunk_msg(m, p), ensure_ascii=False)) for _, new in delta for m in new)
    if delta and info["new_chars"] >= (min_new_chars or 0):
        blocks, keys = [], []
        for s, new in delta:
            sid = s["id"]
            prior_n = session_nparts.get(sid, 0)
            if prior_n:
                first = first_of(s)
                parts, _ = _prep.session_parts(dict(s, messages=new), p, first_messages=first)
                total = prior_n + len(parts)
                for i, part in enumerate(parts):
                    blocks.append(_prep._block(s, prior_n + i + 1, total, part, first, p, omitted, ctx_stats))
                    keys.append({"session": sid, "part": "%d/%d" % (prior_n + i + 1, total), "keys": [m["key"] for m in part]})
            else:
                sub = dict(s, messages=new)
                parts, first = _prep.session_parts(sub, p)
                for i, part in enumerate(parts):
                    blocks.append(_prep._block(sub, i + 1, len(parts), part, first, p, omitted, ctx_stats))
                    keys.append({"session": sid, "part": "%d/%d" % (i + 1, len(parts)), "keys": [m["key"] for m in part]})
        chunks, kchunks = _prep.pack_blocks(blocks, p, ctx_stats, chunk_chars=delta_chunk_chars, keys=keys)
        next_n = max([_num(c["id"]) for c in pins["chunks"]] + [-1]) + 1
        for chunk, kc in zip(chunks, kchunks):
            cid = "chunk-%02d" % next_n
            next_n += 1
            named.append((cid, chunk))
            pins["chunks"].append({"id": cid, "created": now_iso(), "blocks": kc})
            info["new_chunks"].append(cid)
        log("prep: %d new chunk(s) from %d characters of new material" % (len(info["new_chunks"]), info["new_chars"]))
    elif delta:
        info["deferred_chars"] = info["new_chars"]
        log("prep: %d characters of new material wait for more (below auto.min_new_chars %d)" % (info["new_chars"], min_new_chars))
    pins["updated"] = now_iso()
    write_json(paths.chunk_pins, pins)
    return named, info
