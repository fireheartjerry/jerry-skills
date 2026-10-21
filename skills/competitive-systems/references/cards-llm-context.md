# LLM context cards

Covers choosing what goes into a model prompt from a large codebase or corpus under a token
budget, deterministically and offline. The must-keep set (glossary: direct targets,
verification commands, hard boundaries) is never dropped; everything else competes for the
remaining budget. Gate 1 evidence for this file is tokens, not timing: must-keep tokens plus
candidate tokens against B. If the packer's time is spent re-indexing on every call rather
than ranking, go to `cards-repeated-work.md`; if the graph it expands is stale or incomplete,
see `cards-graphs-impact.md`.

## Exact-first retrieval

**Problem shape:** The context packer runs embeddings or BM25 over everything and misses the
file the task literally names.

**Precondition test:** The task text names identifiers, paths, error strings or commands. If
it names none, skip to Sparse scoring.

**Complexity:** O(repo size) with a fast searcher (ripgrep or `grep -rw`), done once per task.

**Sketch:**

```python
import re

IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:[./][A-Za-z0-9_]+)+|[A-Za-z_][A-Za-z0-9_]{3,}")

def names_in(task_text):
    out = []
    for tok in IDENT.findall(task_text):
        looks_like_code = ("_" in tok or "." in tok or "/" in tok
                           or re.search(r"[a-z][A-Z]", tok))
        if looks_like_code and tok not in out:
            out.append(tok)
    return out                                  # task order: deterministic

def exact_hits(names, files):                   # files: path -> text
    hits = []                                   # (rank, path, line_no, name)
    for name in names:
        word = re.compile(r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % re.escape(name))
        for path in sorted(files):
            if name in path:
                hits.append((0, path, 0, name))  # path match: direct target
            for ln, line in enumerate(files[path].splitlines(), 1):
                if word.search(line):
                    rank = 1 if re.match(r"\s*(def|class|fn|func|type)\s", line) else 2
                    hits.append((rank, path, ln, name))
    return sorted(hits)                         # definitions before uses
```

Add the file containing each definition hit to the must-keep set as a direct target.

**Oracle:** For a labelled set of tasks with known relevant files, exact-first must find every
file whose path or defining symbol is named in the task (recall 1.0 on that subset).

**Traps:**
- Identifiers that are common words: require whole-word matching plus a non-word character,
  a capital, or a minimum length of 4 (the sketch does).
- Generated and vendored directories: exclude by a fixed list before searching.
- Binary files: skip by extension or by a null-byte check.

**Not when:**
- The task has no concrete names: Sparse scoring.

## Sparse scoring (BM25) over chunks

**Problem shape:** Ranking candidate chunks by relevance without a network call; results
must be the same every run.

**Precondition test:** Text can be tokenised into terms (split on non-alphanumerics, split
camelCase and snake_case, casefold), and chunks are defined (per function, or 100 to 200
lines). Fewer than about 200 chunks means the index adds nothing: score them all directly.

**Complexity:** Index O(total tokens) once; query O(query terms x postings) instead of
scoring every chunk against every term.

**Sketch:**

```python
import math, re
from collections import Counter, defaultdict

def terms(text):
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)      # camelCase -> camel Case
    return [t for t in re.split(r"[^A-Za-z0-9]+", text.casefold()) if t]

class BM25:
    def __init__(self, chunks, k1=1.2, b=0.75):               # chunks: [(path, start, text)]
        self.k1, self.b, self.keys = k1, b, [(p, s) for p, s, _ in chunks]
        self.post, self.len = defaultdict(list), []
        for cid, (_, _, text) in enumerate(chunks):
            tf = Counter(terms(text))
            self.len.append(sum(tf.values()))
            for t, n in tf.items():
                self.post[t].append((cid, n))
        self.N, self.avg = len(chunks), sum(self.len) / max(1, len(chunks))

    def query(self, text, k=10):
        score = defaultdict(float)
        for t in set(terms(text)):
            df = len(self.post.get(t, ()))
            idf = math.log(1 + (self.N - df + 0.5) / (df + 0.5))
            for cid, n in self.post.get(t, ()):
                norm = n + self.k1 * (1 - self.b + self.b * self.len[cid] / self.avg)
                score[cid] += idf * n * (self.k1 + 1) / norm
        ranked = sorted(score.items(), key=lambda s: (-s[1], self.keys[s[0]]))
        return ranked[:k]                                     # ties: (path, start_line)
```

**Oracle:** Score every chunk with the same formula by a direct loop and compare the top-k
list and scores to 1e-9 on 100 random queries over a corpus of 500 chunks.

**Traps:**
- Terms present in every chunk get idf near 0; the smoothing above keeps it positive.
- Chunk boundaries that split a function: chunk on definitions when a parser is available.
- Forgetting to casefold and split queries the same way as the index.
- Nondeterministic ordering on ties: always sort by (score, path, start_line).

**Not when:**
- Fewer than about 200 chunks: score them all directly (still deterministic).

## Bounded graph expansion

**Problem shape:** The direct targets are known but the model needs their callers, callees or
importers; the packer either loads the whole repo map or nothing.

**Precondition test:** A symbol or import graph is available (see `cards-graphs-impact.md`),
and a hop cap (1 or 2) and a node cap are chosen before expansion. No graph means this card
fails.

**Complexity:** Whole repo map O(V) -> O(nodes within k hops, capped).

**Sketch:**

```python
from collections import deque

def expand(targets, callers, callees, hops=2, node_cap=40, hub_degree=50):
    chosen, reason = [], {}
    q = deque((t, 0, "target") for t in sorted(targets))
    seen = set(targets)
    while q and len(chosen) < node_cap:
        node, depth, why = q.popleft()
        chosen.append(node)
        reason[node] = why
        if depth == hops:
            continue
        for kind, nbrs in (("caller", callers), ("callee", callees)):
            for n in sorted(nbrs.get(node, ())):
                degree = len(callers.get(n, ())) + len(callees.get(n, ()))
                if n in seen or degree > hub_degree:
                    continue                   # hubs would swamp the budget
                seen.add(n)
                q.append((n, depth + 1, "%s of %s" % (kind, node)))
    return chosen, reason
```

Stop early when the remaining token budget is reached; record why each node was added so the
output can show it.

**Oracle:** On small graphs compare against unbounded BFS truncated by the same caps.

**Traps:**
- Hubs (utility modules imported everywhere) swamp the budget: skip by degree.
- Stale graph: rebuild or key by content hash.
- Expanding from the whole must-keep set instead of the direct targets pulls in neighbours
  of test commands and constraints, which are not code.

**Not when:**
- No graph.
- The direct targets already fill the budget.

## Pack under a token budget

**Problem shape:** Candidate chunks with scores exceed the budget; the packer truncates from
the end or drops items at random; constraints or test commands get cut.

**Precondition test:** A token estimate per chunk (a real tokenizer if available, otherwise
chars/4 with about 20% error acknowledged); the must-keep set is identified; a budget B.

**Complexity:** O(c log c) for c candidates.

**Sketch:**

```python
# A chunk has path, start, end, score; its text is re-sliced from the file on demand, so a
# merged range needs no concatenation. Must-keep items that are not file ranges (commands,
# constraints) are chunks with a synthetic path and are never shortened.

def merge_ranges(chunks):                      # same file, overlapping or adjacent -> one chunk
    out = []
    for c in sorted(chunks, key=lambda c: (c.path, c.start)):
        if out and out[-1].path == c.path and c.start <= out[-1].end + 1:
            prev = out[-1]
            out[-1] = Chunk(c.path, prev.start, max(prev.end, c.end),
                            score=max(prev.score, c.score))
        else:
            out.append(c)
    return out

def shorten(chunk):
    # Keep the signature lines (def, class, imports) and the lines the task names; drop the
    # other body lines. Never drops the chunk itself.
    ...

def pack(must_keep, candidates, B, tokens, headroom=0.10):
    limit = int(B * (1 - headroom))            # room for system text and the reply
    chosen = merge_ranges(must_keep)           # no duplicated text inside the must-keep set
    used = sum(tokens(c) for c in chosen)
    note = ""
    if used > limit:                           # shorten within must-keep, never drop it
        chosen = [shorten(c) for c in chosen]
        used = sum(tokens(c) for c in chosen)
        note = "must-keep set exceeded budget; bodies trimmed"
        if used > limit:
            note = "must-keep set alone is %d tokens over budget" % (used - limit)
            chosen.sort(key=lambda c: (c.path, c.start))
            return chosen, used, note          # over budget by construction: say so, add nothing
    ranked = sorted(merge_ranges(candidates),
                    key=lambda c: (-c.score / max(1, tokens(c)), c.path, c.start))
    for c in ranked:
        if used + tokens(c) <= limit:
            chosen.append(c)
            used += tokens(c)
    chosen.sort(key=lambda c: (c.path, c.start))   # fixed order: prompt caching
    return chosen, used, note
```

Emit every chunk with its path and line numbers.

**Oracle:** For c <= 15 candidates brute-force the knapsack optimum and report the greedy gap
on 200 random instances; the must-keep set is present in every output; total tokens <= B in
every output, except when the must-keep set alone exceeds B, in which case the output carries
the "over budget" note and contains no candidate.

**Traps:**
- No headroom: the model's own output and system text need room; keep about 10%.
- Dropping must-keep items silently.
- Reporting "bodies trimmed" when the trimmed must-keep set is still over budget: the caller
  must see the overrun, so the note states the token count.
- Duplicated overlapping chunks: merge ranges on both the must-keep set and the candidates.
- Order that changes between runs breaks prompt caching.
- Counting tokens with one tokenizer and sending to another model.

**Not when:**
- Everything relevant fits: include it all, in fixed order.

See also: the graph used for expansion comes from `cards-graphs-impact.md`; caching the index
across tasks is `cards-repeated-work.md`.
