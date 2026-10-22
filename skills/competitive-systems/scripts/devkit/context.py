"""`dev.py context QUERY`: a token-budgeted context packet built with the algorithms of
references/cards-llm-context.md.

1. Exact-first retrieval: names in the query (code-like identifiers and paths, plus plain
   words of 4+ characters) are matched as whole words; definitions rank before uses; a file
   that defines a named symbol, or whose path names one, is a direct target and its
   defining chunk (or the whole file, when small) joins the must-keep set.
2. Sparse scoring: BM25 over definition-level chunks (an inverted index above 200 chunks,
   the same formula scored directly below that).
3. Bounded expansion: 2 hops, 40 files, hub degree 50, over importers and imports from the
   direct targets; call-site chunks of each reached file gain score.
4. Pack: the fixed card algorithm (must-keep ranges subtracted from candidates, ratio greedy
   with the best-single fallback, over-budget must-keep shortened to signature lines and
   reported, output sorted by path and line) with 10% headroom. Candidates scoring under
   --min-score are dropped first, so a large budget is not filled with noise; --must-keep-text
   reserves room for text that is not a file (the test command, a hard boundary).

Tokens are estimated as characters / 4 and labelled as an estimate; every chunk's cost
includes the `==> path:lines <==` header and separator --emit prints, so the emitted packet
stays within the limit. For a fixed repository
state the packet is byte-identical across runs (every iteration is over sorted keys).
"""
import math
import os
import re
from collections import Counter, defaultdict, deque, namedtuple

from devkit.common import Parser, UsageError, load_config, note, print_json, rel_to_root, repo_root

SCHEMA = "devkit-context/1"
DEFAULT_BUDGET = 12000
HEADROOM = 0.10
BM25_MIN_CHUNKS = 200         # above this, an inverted index; at or below, direct scoring
HOPS, NODE_CAP, HUB_DEGREE = 2, 40, 50
MAX_CHUNK_LINES = 120         # longer definitions split at methods, then into windows
WINDOW = 100
NAME_FILE_CAP = 5             # a name defined in (or path-matching) more files is too common
WHOLE_FILE_SHARE = 0.4        # a path-matched file joins must-keep whole up to this share
DEFAULT_MIN_SCORE = 0.25     # bm25 is normalised to 1.0 for the best chunk
USE_BONUS, USE_CAP = 0.5, 1.0
PATH_FILE_BONUS = 1.0
HOP_BONUS = {1: 0.6, 2: 0.3}

MUST = "must-keep: "
Chunk = namedtuple("Chunk", "path start end score reason keep", defaults=("", None))

# ---------------------------------------------------------------- exact-first retrieval

IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:[./][A-Za-z0-9_]+)+|[A-Za-z_][A-Za-z0-9_]{3,}")


def code_like(tok):
    return bool("_" in tok or "." in tok or "/" in tok or re.search(r"[a-z][A-Z]", tok))


# Plain words that task text uses for instructions rather than to name code; as weak names
# they would make any file defining `empty()` or `after()` a direct target. They still count
# in BM25 scoring.
STOP_WORDS = frozenset("""
about above after again against also always another answer before being below between both
broken bug change changes check could does doing done during each else error errors every
expect expected fail fails failing fast faster file files first from have here into issue
just keep last later like line lines make many more most must need never next none only
other output over please public raise raises rather really result return returns right
same seems should since slow slower some still such sure than that their them then there
these they thing think this those through under until used uses using very want when where
which while will with without work works would wrong your empty status value values true
false call calls code test tests unittest pytest python after break case data list name
type types first second third fix fixed run runs running time times way api does
""".split())


def names_in(task_text, weak=True):
    """Names in task order. Code-like tokens (underscore, dot, slash or camelCase) as in the
    card; with weak=True also plain words of 4+ characters (the card's IDENT already
    requires the length) that are not common instruction words (STOP_WORDS), which
    exact_hits treats more strictly."""
    out = []
    for tok in IDENT.findall(task_text):
        if tok in out:
            continue
        if code_like(tok) or (weak and tok.lower() not in STOP_WORDS):
            out.append(tok)
    return out


DEF_KEYWORDS = (r"(?:export\s+(?:default\s+)?)?(?:pub(?:\s*\([^)]*\))?\s+)?(?:async\s+)?"
                r"(?:def|class|fn|func|type|function\*?|interface|struct|enum|trait|mod|"
                r"const|let|var)\s+(?:\([^)]*\)\s*)?")


def exact_hits(names, files, defs=None, weak=frozenset()):
    """(rank, path, line, name) sorted: 0 path match, 1 definition, 2 use.

    A code-like name matches a path by substring (card semantics); a weak name only when it
    equals a directory name or a file stem. A line is a definition of a name when a
    definition keyword is followed by it, or when defs[path] (a list of (line, qualified
    names)) holds it; `Class.method` and `Type::method` find the method's definition line."""
    defs = defs or {}
    hits = []
    for name in names:
        qual = name.replace("::", ".")
        last = re.split(r"\.|::|/", name)[-1]
        dotted = "/" not in name and "." in qual
        word = re.compile(r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % re.escape(name))
        defn = re.compile(r"^\s*" + DEF_KEYWORDS + re.escape(last) + r"(?![A-Za-z0-9_$])")

        def defines(q):
            return q == qual or q.endswith("." + qual) or (not dotted and q.split(".")[-1] == qual)

        for path in sorted(files):
            if name in weak:
                parts = path.split("/")
                if name in parts[:-1] or name == os.path.splitext(parts[-1])[0]:
                    hits.append((0, path, 0, name))
            elif name in path:
                hits.append((0, path, 0, name))
            def_at = dict((ln, qs) for ln, qs in defs.get(path, ()) if any(defines(q) for q in qs))
            text = files[path]
            found = set()
            if name in text:
                for ln, line in enumerate(text.splitlines(), 1):
                    if word.search(line):
                        is_def = ln in def_at or (name == last and defn.match(line))
                        hits.append((1 if is_def else 2, path, ln, name))
                        found.add(ln)
            for ln in sorted(def_at):
                if ln not in found:
                    hits.append((1, path, ln, name))
    return sorted(hits)


# ---------------------------------------------------------------- sparse scoring


def terms(text):
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    return [t for t in re.split(r"[^A-Za-z0-9]+", text.casefold()) if t]


class BM25(object):
    """The card's index; query terms are iterated sorted so float sums are reproducible."""

    def __init__(self, chunks, k1=1.2, b=0.75):           # chunks: [(path, start, text)]
        self.k1, self.b, self.keys = k1, b, [(p, s) for p, s, _ in chunks]
        self.post, self.len = defaultdict(list), []
        for cid, (_, _, text) in enumerate(chunks):
            tf = Counter(terms(text))
            self.len.append(sum(tf.values()))
            for t in sorted(tf):
                self.post[t].append((cid, tf[t]))
        self.N, self.avg = len(chunks), sum(self.len) / max(1, len(chunks))

    def query(self, text, k=10):
        score = defaultdict(float)
        for t in sorted(set(terms(text))):
            df = len(self.post.get(t, ()))
            idf = math.log(1 + (self.N - df + 0.5) / (df + 0.5))
            for cid, n in self.post.get(t, ()):
                norm = n + self.k1 * (1 - self.b + self.b * self.len[cid] / self.avg)
                score[cid] += idf * n * (self.k1 + 1) / norm
        ranked = sorted(score.items(), key=lambda s: (-s[1], self.keys[s[0]]))
        return ranked[:k] if k is not None else ranked


def score_direct(chunks, text, k1=1.2, b=0.75):
    """BM25 scores by a direct loop over every chunk: the small-corpus path, same formula
    and the same (score, path, start) order as BM25.query(text, k=None)."""
    tfs = [Counter(terms(t)) for _, _, t in chunks]
    lens = [sum(tf.values()) for tf in tfs]
    N, avg = len(chunks), sum(lens) / max(1, len(chunks))
    qt = sorted(set(terms(text)))
    df = dict((t, sum(1 for tf in tfs if t in tf)) for t in qt)
    out = []
    for cid, tf in enumerate(tfs):
        s, hit = 0.0, False
        for t in qt:
            if t in tf:
                idf = math.log(1 + (N - df[t] + 0.5) / (df[t] + 0.5))
                n = tf[t]
                s += idf * n * (k1 + 1) / (n + k1 * (1 - b + b * lens[cid] / avg))
                hit = True
        if hit:
            out.append((cid, s))
    out.sort(key=lambda kv: (-kv[1], (chunks[kv[0]][0], chunks[kv[0]][1])))
    return out


# ---------------------------------------------------------------- bounded expansion


def expand(targets, callers, callees, hops=2, node_cap=40, hub_degree=50,
           labels=("caller", "callee")):
    """The card's expansion; `labels` names the two edge kinds in the reasons."""
    chosen, reason = [], {}
    q = deque((t, 0, "target") for t in sorted(targets))
    seen = set(targets)
    while q and len(chosen) < node_cap:
        node, depth, why = q.popleft()
        chosen.append(node)
        reason[node] = why
        if depth == hops:
            continue
        for kind, nbrs in ((labels[0], callers), (labels[1], callees)):
            for n in sorted(nbrs.get(node, ())):
                degree = len(callers.get(n, ())) + len(callees.get(n, ()))
                if n in seen or degree > hub_degree:
                    continue
                seen.add(n)
                q.append((n, depth + 1, "%s of %s" % (kind, node)))
    return chosen, reason


# ---------------------------------------------------------------- pack


def _merged_reason(a, b):
    """Union of two `; `-joined reasons, the `must-keep: ` prefix written once."""
    parts, keep = [], False
    for r in (a, b):
        if (r or "").startswith(MUST):
            keep, r = True, r[len(MUST):]
        for p in (r or "").split("; "):
            if p and p not in parts:
                parts.append(p)
    return (MUST if keep else "") + "; ".join(parts)


def merge_ranges(chunks):
    """Same file, overlapping or adjacent -> one chunk with the larger score."""
    out = []
    for c in sorted(chunks, key=lambda c: (c.path, c.start)):
        if out and out[-1].path == c.path and c.start <= out[-1].end + 1:
            prev = out[-1]
            merged = prev._replace(end=max(prev.end, c.end), score=max(prev.score, c.score))
            if "reason" in prev._fields:
                merged = merged._replace(reason=_merged_reason(prev.reason, c.reason))
            out[-1] = merged
        else:
            out.append(c)
    return out


def subtract_ranges(c, chosen):
    """Parts of c not covered by a chosen range on the same path; scores kept."""
    parts, start = [], c.start
    for k in sorted((k for k in chosen if k.path == c.path), key=lambda k: k.start):
        if k.end < start or k.start > c.end:
            continue
        if k.start > start:
            parts.append(c._replace(start=start, end=k.start - 1))
        start = max(start, k.end + 1)
    if start <= c.end:
        parts.append(c._replace(start=start, end=c.end))
    return parts


def pack(must_keep, candidates, B, tokens, headroom=HEADROOM, shorten=None,
         merge_candidates=True, reserve=0):
    """The card's fixed pack. `shorten` trims a must-keep chunk to its signature lines
    (identity when None). merge_candidates=False skips the adjacency merge for candidates
    that are already disjoint (a partition of files into chunks), where merging adjacent
    chunks would collapse a file into one chunk at the larger score. `reserve` tokens are
    taken off the limit first (text that must appear but is not a file chunk)."""
    shorten = shorten or (lambda c: c)
    limit = int(B * (1 - headroom)) - reserve
    chosen = merge_ranges(must_keep)
    used = sum(tokens(c) for c in chosen)
    note_ = ""
    if used > limit:
        chosen = [shorten(c) for c in chosen]
        used = sum(tokens(c) for c in chosen)
        note_ = "must-keep set exceeded budget; bodies trimmed"
        if used > limit:
            note_ = "must-keep set alone is %d tokens over budget" % (used - limit)
            chosen.sort(key=lambda c: (c.path, c.start))
            return chosen, used, note_
    room = limit - used
    base = merge_ranges(candidates) if merge_candidates else sorted(
        candidates, key=lambda c: (c.path, c.start))
    pool = [p for c in base for p in subtract_ranges(c, chosen)]
    pool = [c for c in pool if tokens(c) <= room]
    pool.sort(key=lambda c: (-c.score / max(1, tokens(c)), c.path, c.start))
    picked, spent = [], 0
    for c in pool:
        if spent + tokens(c) <= room:
            picked.append(c)
            spent += tokens(c)
    best = min(pool, key=lambda c: (-c.score, c.path, c.start), default=None)
    if best is not None and best.score > sum(c.score for c in picked):
        picked, spent = [best], tokens(best)
    chosen = sorted(chosen + picked, key=lambda c: (c.path, c.start))
    return chosen, used + spent, note_


# ---------------------------------------------------------------- chunking


def chunk_ranges(lines, defs):
    """[(start, end, label)] partitioning the file: a header before the first definition,
    one chunk per top-level definition (up to the next one), and definitions longer than
    MAX_CHUNK_LINES split at their methods, then into WINDOW-line windows."""
    n = len(lines)
    if n == 0:
        return []
    tops, seen = [], set()
    for d in sorted((d for d in defs if d["depth"] == 0), key=lambda d: (d["start"], d["line"])):
        s = max(1, min(d["start"], n))
        if s not in seen:
            seen.add(s)
            tops.append((s, d["name"]))
    raw = []
    if not tops:
        raw.append((1, n, "file"))
    elif tops[0][0] > 1:
        if any(line.strip() for line in lines[:tops[0][0] - 1]):
            raw.append((1, tops[0][0] - 1, "module header"))
        else:
            tops[0] = (1, tops[0][1])            # leading blank lines join the first chunk
    for i, (s, name) in enumerate(tops):
        raw.append((s, (tops[i + 1][0] - 1) if i + 1 < len(tops) else n, name))
    kids = sorted((k["start"], k["name"]) for k in defs if k["depth"] == 1)
    out = []
    for a, b, label in raw:
        if b - a + 1 <= MAX_CHUNK_LINES:
            out.append((a, b, label))
            continue
        cuts = [(a, label)] + [(s, name) for s, name in kids if a < s <= b]
        for j, (s, name) in enumerate(cuts):
            e = (cuts[j + 1][0] - 1) if j + 1 < len(cuts) else b
            if e - s + 1 <= MAX_CHUNK_LINES:
                out.append((s, e, name))
                continue
            for w in range(s, e + 1, WINDOW):
                we = min(e, w + WINDOW - 1)
                out.append((w, we, "%s (lines %d-%d)" % (name, w, we)))
    return out


SIGNATURE = re.compile(r"^\s*(?:@|#\[|(?:export\s+)?(?:pub(?:\s*\([^)]*\))?\s+)?(?:async\s+)?"
                       r"(?:def|class|fn|func|type|function|interface|struct|enum|trait|impl|"
                       r"mod|import|from|use|package|const|let|var)\b)")


class Repo(object):
    """File texts, definitions, chunks and file-level edges for one repository state."""

    def __init__(self, root, cfg=None, info=None):
        from devkit import repomap
        from devkit.detect import detect
        self.root = root
        self.cfg = cfg if cfg is not None else load_config(root, warn_unknown=False)
        self.info = info if info is not None else detect(root, self.cfg)
        files = repomap.mapped_files(self.info)
        texts = repomap.read_texts(root, files)
        self.texts = dict((f, t) for f, t in texts.items()
                          if "\x00" not in t and len(t) <= repomap.MAX_BYTES)
        self.files = sorted(self.texts)
        self.lines = dict((f, self.texts[f].splitlines()) for f in self.files)
        self.defs, self.exports = {}, {}
        for f in self.files:
            defs, exports, _ = repomap.symbols(f, self.texts[f])
            self.defs[f], self.exports[f] = defs, exports
        self.imports, self.importers = repomap.edges(root, self.cfg, self.info, self.files,
                                                     self.texts)
        self.chunks, self.by_file = [], {}
        for f in self.files:
            for a, b, label in chunk_ranges(self.lines[f], self.defs[f]):
                self.chunks.append((f, a, b, label))
                self.by_file.setdefault(f, []).append((f, a, b, label))

    def text_of(self, c):
        lines = self.lines.get(c.path, [])
        idx = c.keep if c.keep is not None else range(c.start, c.end + 1)
        return [(i, lines[i - 1]) for i in idx if 1 <= i <= len(lines)]

    def tokens(self, c):
        chars = sum(len(line) + 1 for _, line in self.text_of(c))
        return (chars + 3) // 4

    def cost(self, c):
        """Tokens of the chunk as --emit prints it: the text plus its header and blank line."""
        chars = sum(len(line) + 1 for _, line in self.text_of(c))
        chars += len(chunk_header(c.path, c.start, c.end,
                                  _ranges(c.keep) if c.keep is not None else None)) + 2
        return (chars + 3) // 4

    def chunk_at(self, path, line):
        for _, a, b, label in self.by_file.get(path, ()):
            if a <= line <= b:
                return a, b, label
        return None


def parse_must_keep(repo, spec):
    path, rng = spec, None
    m = re.match(r"^(.*):(\d+)-(\d+)$", spec)
    if m:
        path, rng = m.group(1), (int(m.group(2)), int(m.group(3)))
    full = path if os.path.isabs(path) else os.path.join(os.getcwd(), path)
    if not os.path.isfile(full) and os.path.isfile(os.path.join(repo.root, path)):
        full = os.path.join(repo.root, path)
    if not os.path.isfile(full):
        raise UsageError("--must-keep %s: no such file" % spec)
    rel = rel_to_root(repo.root, full)
    if rel not in repo.lines:
        from devkit.graph import read_text
        repo.lines[rel] = read_text(repo.root, rel).splitlines()
    n = len(repo.lines[rel])
    start, end = rng if rng else (1, max(1, n))
    if start < 1 or end < start or end > max(1, n):
        raise UsageError("--must-keep %s: range outside 1-%d" % (spec, n))
    return Chunk(rel, start, end, 0.0, MUST + "--must-keep")


def _mentions(text, names):
    for name in names:
        if name and re.search(r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % re.escape(name), text):
            return True
    return False


def _rank_hits(names, repo, notes):
    """Exact hits with names defined in too many files demoted to uses, the direct targets
    (files a name's path matches or that define it) best first, and why each is direct."""
    weak = frozenset(n for n in names if not code_like(n))
    defs = {}
    for f in repo.files:
        at = {}
        for d in repo.defs[f]:
            at.setdefault(d["line"], set()).add(d["name"].replace("::", "."))
        defs[f] = sorted(at.items())
    hits = exact_hits(names, repo.texts, defs, weak)
    per_name = defaultdict(set)
    for rank, path, _, name in hits:
        if rank < 2:
            per_name[name].add(path)
    common = set(n for n in names if len(per_name[n]) > NAME_FILE_CAP)
    for n in names:
        if n in common:
            notes.append("%r is defined in or names %d files; only its uses are scored (pass "
                         "--must-keep PATH to choose)" % (n, len(per_name[n])))
    demoted = []
    for rank, path, ln, name in hits:
        if name in common and rank < 2:
            if rank == 1:
                demoted.append((2, path, ln, name))
            continue
        demoted.append((rank, path, ln, name))
    hits = sorted(demoted)
    order = dict((n, i) for i, n in enumerate(names))
    first = {}
    why_direct = defaultdict(list)
    for rank, path, ln, name in hits:
        if rank < 2:
            key = (rank, order[name])
            if path not in first or key < first[path]:
                first[path] = key
            label = ("path matches %s" % name) if rank == 0 else ("defines %s" % name)
            if label not in why_direct[path]:
                why_direct[path].append(label)
    return hits, sorted(first, key=lambda p: (first[p], p)), why_direct


def _must_and_bonus(repo, names, hits, direct, why_direct, limit, must, add):
    """Adds defining chunks and path-matched files (whole when small) to must, and scores
    chunks that use a queried name."""
    order = dict((n, i) for i, n in enumerate(names))
    for rank, path, ln, name in hits:
        c = repo.chunk_at(path, ln) if rank == 1 else None
        if c:
            must.append(Chunk(path, c[0], c[1], 0.0, MUST + "defines %s" % name))
    for path in direct:
        if not any(r == 0 and p == path for r, p, _, _ in hits):
            continue
        whole = Chunk(path, 1, max(1, len(repo.lines[path])), 0.0,
                      MUST + "; ".join(l for l in why_direct[path] if l.startswith("path")))
        if repo.tokens(whole) <= WHOLE_FILE_SHARE * limit:
            must.append(whole)
            continue
        head = repo.by_file.get(path, [])[:1]
        if head:
            must.append(Chunk(path, head[0][1], head[0][2], 0.0, whole.reason))
        for f, a, b, _ in repo.by_file.get(path, ()):
            add((f, a), PATH_FILE_BONUS, "in a path-matched file")
    uses = defaultdict(set)
    for rank, path, ln, name in hits:
        if rank == 2:
            c = repo.chunk_at(path, ln)
            if c:
                uses[(path, c[0])].add(name)
    for key in sorted(uses):
        names_used = sorted(uses[key], key=lambda n: order[n])
        add(key, min(USE_CAP, USE_BONUS * len(names_used)), "uses " + ", ".join(names_used))


def _expand(repo, direct, add):
    """Importers and imports of the direct targets within HOPS, each scoring the chunks that
    mention the other side. Returns the expanded list for the packet."""
    expanded = []
    if not direct:
        return expanded
    chosen, why = expand(direct, repo.importers, repo.imports, HOPS, NODE_CAP, HUB_DEGREE,
                         labels=("importer", "import"))
    depth = dict((t, 0) for t in direct)
    for node in chosen:
        if node in depth:
            continue
        kind, _, src = why[node].partition(" of ")
        depth[node] = depth.get(src, 0) + 1
        expanded.append({"path": node, "reason": why[node], "hop": depth[node]})
        if node not in repo.lines:
            continue
        node_chunks = repo.by_file.get(node, [])
        if kind == "importer":
            wanted = set(re.split(r"\.|::", d["name"])[-1] for d in repo.defs.get(src, [])
                         if d["depth"] == 0)
            wanted.add(os.path.splitext(os.path.basename(src))[0])
            hit = [c for c in node_chunks
                   if _mentions("\n".join(repo.lines[node][c[1] - 1:c[2]]), sorted(wanted))]
        else:
            src_text = repo.texts.get(src, "")
            hit = [c for c in node_chunks
                   if _mentions(src_text, [re.split(r"\.|::| ", c[3])[-1]])]
        for c in (hit or node_chunks[:1]):
            add((c[0], c[1]), HOP_BONUS[depth[node]], why[node])
    return expanded


def _candidates(repo, query, bonus, reasons, min_score=0.0):
    """(candidate chunks scored by normalised BM25 plus bonuses, retrieval description,
    number dropped under min_score)."""
    docs = [(f, a, "%s %s\n%s" % (f, label, "\n".join(repo.lines[f][a - 1:b])))
            for f, a, b, label in repo.chunks]
    if len(docs) > BM25_MIN_CHUNKS:
        retrieval = "bm25 index over %d chunks" % len(docs)
        ranked = BM25(docs).query(query, k=None)
    else:
        retrieval = "direct BM25 scoring over %d chunks (index above %d)" % (
            len(docs), BM25_MIN_CHUNKS)
        ranked = score_direct(docs, query)
    top = ranked[0][1] if ranked and ranked[0][1] > 0 else 0.0
    lexical = dict((cid, s / top) for cid, s in ranked) if top else {}
    candidates, dropped = [], 0
    for cid, (f, a, b, label) in enumerate(repo.chunks):
        lex = lexical.get(cid, 0.0)
        score = round(lex + bonus.get((f, a), 0.0), 9)
        if score <= 0:
            continue
        if score < min_score:
            dropped += 1
            continue
        why = list(reasons.get((f, a), []))
        if lex > 0:
            why.append("bm25 %.2f" % lex)
        candidates.append(Chunk(f, a, b, score, "; ".join(why)))
    return candidates, retrieval, dropped


TEXT_PATH = "<text>"


def text_cost(text):
    return (len(text) + 1 + len(chunk_header(TEXT_PATH, 1, 1, None)) + 2 + 3) // 4


def build_packet(root, query, budget=DEFAULT_BUDGET, must_keep=(), cfg=None, info=None,
                 emit=False, min_score=DEFAULT_MIN_SCORE, keep_text=()):
    """The packet as a dict (schema devkit-context/1)."""
    if budget <= 0:
        raise UsageError("--budget must be a positive number of tokens")
    if min_score < 0:
        raise UsageError("--min-score must be >= 0")
    repo = Repo(root, cfg, info)
    notes = []
    names = names_in(query)
    hits, direct, why_direct = _rank_hits(names, repo, notes)
    limit = int(budget * (1 - HEADROOM))
    must = [parse_must_keep(repo, s) for s in must_keep]
    bonus = defaultdict(float)
    reasons = defaultdict(list)

    def add(key, amount, why):
        bonus[key] += amount
        if why not in reasons[key]:
            reasons[key].append(why)

    _must_and_bonus(repo, names, hits, direct, why_direct, limit, must, add)
    expanded = _expand(repo, direct, add)
    candidates, retrieval, dropped = _candidates(repo, query, bonus, reasons, min_score)
    reserve = sum(text_cost(t) for t in keep_text)

    def shorten(c):
        if c.keep is not None or c.path not in repo.lines:
            return c
        kept = [i for i, line in repo.text_of(c)
                if i == c.start or SIGNATURE.match(line) or _mentions(line, names)]
        return c._replace(keep=tuple(kept))

    out, used, pack_note = pack(must, candidates, budget, repo.cost, HEADROOM, shorten,
                                merge_candidates=False, reserve=reserve)
    chunks = []
    for t in keep_text:
        item = {"path": TEXT_PATH, "start": 1, "end": 1, "lines": None,
                "tokens": text_cost(t), "score": 0.0, "must_keep": True,
                "reason": MUST + "--must-keep-text"}
        if emit:
            item["text"] = t
        chunks.append(item)
    used += reserve
    for c in out:
        item = {"path": c.path, "start": c.start, "end": c.end,
                "lines": _ranges(c.keep) if c.keep is not None else None,
                "tokens": repo.cost(c), "score": round(c.score, 6),
                "must_keep": c.reason.startswith(MUST), "reason": c.reason}
        if emit:
            item["text"] = "\n".join(line for _, line in repo.text_of(c))
        chunks.append(item)
    if dropped:
        notes.append("%d candidate chunk(s) scored under --min-score %g and were left out; "
                     "%d of %d tokens unused" % (dropped, min_score, max(0, limit - used), limit))
    if not names:
        notes.append("the query names no identifier or path; ranking is lexical only")
    if not candidates and not must:
        notes.append("no chunk matched the query")
    return {"schema": SCHEMA, "query": query, "budget": budget, "limit": limit,
            "headroom": HEADROOM, "token_estimate": "chars/4", "names": names,
            "direct_targets": direct, "direct_reasons": dict((p, why_direct[p]) for p in direct),
            "retrieval": retrieval, "expanded": expanded, "files": len(repo.files),
            "chunks_total": len(repo.chunks), "chunks": chunks, "used": used,
            "min_score": min_score, "dropped_below_min_score": dropped,
            "note": pack_note, "notes": notes}


def _ranges(keep):
    out = []
    for i in keep:
        if out and i == out[-1][1] + 1:
            out[-1][1] = i
        else:
            out.append([i, i])
    return out


def chunk_header(path, start, end, lines):
    span = "%d-%d" % (start, end)
    if lines is not None:
        span += " (signature lines %s)" % ", ".join(
            "%d-%d" % tuple(r) if r[0] != r[1] else str(r[0]) for r in lines)
    return "==> %s:%s <==" % (path, span)


def emit_blocks(chunks):
    """[(header, text)] with adjacent full chunks of one file merged into one block."""
    blocks = []
    for c in chunks:
        prev = blocks[-1] if blocks else None
        if (prev and c["path"] != TEXT_PATH and prev["path"] == c["path"]
                and prev["lines"] is None and c["lines"] is None
                and c["start"] == prev["end"] + 1):
            prev["end"] = c["end"]
            prev["text"] += "\n" + c["text"]
            continue
        blocks.append(dict(c))
    return [(chunk_header(b["path"], b["start"], b["end"], b["lines"])
             if b["path"] != TEXT_PATH else "==> %s <==" % TEXT_PATH, b["text"])
            for b in blocks]


def render(p, emit=False):
    lines = ['context: "%s"; budget %d tokens, limit %d after %d%% headroom; tokens are '
             'chars/4 estimates' % (p["query"], p["budget"], p["limit"], int(p["headroom"] * 100))]
    if p["direct_targets"]:
        lines.append("direct targets: " + "; ".join(
            "%s (%s)" % (t, ", ".join(p["direct_reasons"][t])) for t in p["direct_targets"]))
    else:
        lines.append("direct targets: none")
    lines.append("retrieval: %s; expansion: %d file(s) within %d hops"
                 % (p["retrieval"], len(p["expanded"]), HOPS))
    width = max([len(c["path"]) for c in p["chunks"]] + [4])
    lines.append("  %-*s  %-11s %7s  %s" % (width, "path", "lines", "tokens", "reason"))
    for c in p["chunks"]:
        span = "%d-%d" % (c["start"], c["end"])
        if c["lines"] is not None:
            span += " sig"
        lines.append("  %-*s  %-11s %7d  %s" % (width, c["path"], span, c["tokens"], c["reason"]))
    lines.append("used %d of %d tokens (budget %d; estimate)" % (p["used"], p["limit"], p["budget"]))
    if p["note"]:
        lines.append("note: " + p["note"])
    for n in p["notes"]:
        lines.append("note: " + n)
    if emit:
        body = []
        for header, text in emit_blocks(p["chunks"]):
            body.extend(["", header, text])
        emitted = "\n".join(body).lstrip("\n")
        lines.append("emitted %d tokens (chars/4, from the first ==> on)" % (
            (len(emitted) + 3) // 4))
        lines.extend(body)
    return "\n".join(lines)


def cmd_context(args):
    p = Parser(prog="dev.py context", description="Token-budgeted context packet for a task: "
               "exact names first, BM25, bounded import-graph expansion, then the card's pack.")
    p.add_argument("query", help="identifiers, paths and error text from the task (leave out "
                                 "instructions such as the test command: pass those with "
                                 "--must-keep-text)")
    p.add_argument("--budget", type=int, default=DEFAULT_BUDGET,
                   help="token budget (default %(default)s; 10%% is kept as headroom)")
    p.add_argument("--must-keep", action="append", default=[], metavar="PATH[:START-END]",
                   help="a file or line range that is never dropped (repeatable)")
    p.add_argument("--must-keep-text", action="append", default=[], metavar="TEXT",
                   help="text that is not a file but must be in the packet and counted, such "
                        "as the verification command or a hard boundary (repeatable)")
    p.add_argument("--min-score", type=float, default=DEFAULT_MIN_SCORE,
                   help="drop candidate chunks scoring under this (bm25 normalised to 1 for "
                        "the best chunk, plus bonuses; default %(default)s; 0 keeps all)")
    p.add_argument("--emit", action="store_true", help="also print the text of every chunk")
    p.add_argument("--repo", default=None, help="repository root (default: git top level)")
    p.add_argument("--json", action="store_true", help="print one JSON object")
    a = p.parse_args(args)
    root, root_note = repo_root(a.repo)
    if root_note:
        note(root_note)
    packet = build_packet(root, a.query, a.budget, a.must_keep, load_config(root), emit=a.emit,
                          min_score=a.min_score, keep_text=a.must_keep_text)
    if a.json:
        print_json(packet)
    else:
        print(render(packet, a.emit))
    return 0
