"""Deduplicate (Codex forks replay earlier messages), split sessions into mining (80%) and evaluation (20%, by a hash of
the session id, so the split is fixed), and cut the mining sessions into chunks of about 200k characters, in time order."""
import hashlib, json, os
from collections import defaultdict
from pathlib import Path
ROOT = Path(os.path.expanduser(os.environ.get("MIND_READING_HOME", "~/.local/share/mind-reading")))
rows = [json.loads(l) for l in open(ROOT / "corpus/messages.jsonl")]
rows.sort(key=lambda r: (r["time"] or "", r["session"], r["turn"]))
seen, kept = set(), []
for r in rows:
    key = r["text"].strip()
    if len(key) > 40 and key in seen:
        continue
    seen.add(key); kept.append(r)
sessions = defaultdict(list)
for r in kept:
    sessions[(r["source"], r["session"])].append(r)
def held(sid): return int(hashlib.sha256(sid.encode()).hexdigest(), 16) % 5 == 0
ev = {k: v for k, v in sessions.items() if held(k[1]) and len(v) >= 3}
mine = {k: v for k, v in sessions.items() if k not in ev}
(ROOT / "eval").mkdir(exist_ok=True); (ROOT / "chunks").mkdir(exist_ok=True)
with open(ROOT / "eval/sessions.jsonl", "w") as f:
    for (src, sid), msgs in sorted(ev.items(), key=lambda kv: kv[1][0]["time"] or ""):
        f.write(json.dumps({"source": src, "session": sid, "messages": [{"time": m["time"], "text": m["text"]} for m in msgs]}, ensure_ascii=False) + "\n")
order = sorted(mine.items(), key=lambda kv: kv[1][0]["time"] or "")
chunks, cur, size = [], [], 0
def parts(src, sid, msgs):
    """A session as one block, or as consecutive parts of at most about 180k characters (its messages kept whole)."""
    out, part, n = [], [], 0
    for m in msgs:
        k = len(json.dumps(m, ensure_ascii=False))
        if part and n + k > 180_000:
            out.append(part); part, n = [], 0
        part.append(m); n += k
    out.append(part)
    return [{"source": src, "session": sid, "part": f"{i + 1}/{len(out)}", "project": msgs[0]["project"],
             "start": p[0]["time"], "messages": [{"time": m["time"], "text": m["text"]} for m in p]} for i, p in enumerate(out)]
for (src, sid), msgs in order:
    for block in parts(src, sid, msgs):
        n = len(json.dumps(block, ensure_ascii=False))
        if cur and size + n > 200_000:
            chunks.append(cur); cur, size = [], 0
        cur.append(block); size += n
if cur: chunks.append(cur)
for k, c in enumerate(chunks):
    with open(ROOT / f"chunks/chunk-{k:02d}.jsonl", "w") as f:
        for b in c: f.write(json.dumps(b, ensure_ascii=False) + "\n")
print(f"kept {len(kept)} of {len(rows)} messages; sessions: mining {len(mine)}, evaluation {len(ev)}; chunks {len(chunks)}")
print("chunk sizes (k chars):", [round(sum(len(json.dumps(b, ensure_ascii=False)) for b in c) / 1000) for c in chunks])
