# Lookup and dedup cards

Covers nested loops over the same or two collections, `x in list` inside a loop, duplicate
detection, range joins, and windowed aggregates recomputed per window. If the issue is
repeated calls with the same arguments rather than a nested loop, go to
`cards-repeated-work.md`. If the "duplicates" turn out to be similar rather than equal, the
exact cards fail their precondition and the Blocking card below applies.

## Hash-key dedup (exact duplicates)

**Problem shape:** `for i: for j: if same(a[i], a[j])`, or `if row not in seen_list`, or
`list.count(x)` inside a loop. The user says "dedup is slow" or "removing duplicates takes
hours".

**Precondition test:** Write a function `key(row)` returning a hashable tuple such that
`same(a, b)` is exactly `key(a) == key(b)`. Check it with a property test on random pairs,
including the edge cases below. If you cannot write such a key (equality is fuzzy, or depends
on context), this card fails: go to Blocking for near-duplicates.

**Complexity:** O(n^2) comparisons -> O(n) expected hashing, plus O(n) extra memory for the key
set.

**Sketch:**

```python
import math

def dedup_keep_first(rows, key):
    seen, out = set(), []
    for r in rows:
        k = key(r)
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out

def key(e):                       # example for (user_id, type, timestamp) equality
    ts = e.ts
    if ts != ts:                  # NaN: the old code never matched it, keep that
        return (e.user_id, e.type, "nan", id(e))
    return (e.user_id, e.type, math.floor(ts))   # floor, not int(): int truncates toward 0
```

**Oracle:** Nested-loop keep-first on lists of 0 to 60 rows, 300 seeded cases. Include rows
that differ only in case, surrounding whitespace, Unicode normalisation form, float
representation (1.0 vs 1), negative timestamps, and a NaN field.

**Traps:**
- NaN != NaN, so a key containing a float NaN never matches itself. Decide what the old code
  did and reproduce it (map NaN to a sentinel, or make NaN rows unique as in the sketch).
- Floats as keys: round to a fixed precision or use Decimal; `int()` truncates toward zero,
  so negative values round the wrong way compared with `floor`.
- Unicode: apply NFC and casefold inside `key` if, and only if, the old comparison did.
- dict and list fields are unhashable: convert to tuples (sorted items for dicts).
- Keep-first versus keep-last changes the result when fields outside the key differ.
- Memory: for n beyond RAM, hash to shards on disk, or sort externally and use Sort then scan.

**Not when:**
- "Duplicate" means similar, not equal: use Blocking for near-duplicates.
- Small n: still use the set (free tier) and skip the measurement ceremony; Gate 2 only
  refuses the cheap and expensive tiers.
- The data is already sorted by the key: use Sort then scan, no extra memory.

## Dict index join

**Problem shape:** `for a in A: for b in B: if a.id == b.fk`, or a lookup list searched once
per row.

**Precondition test:** The join condition is equality on one or a few fields, and one side
fits in memory as a dict. If the condition is a range or inequality, this card fails.

**Complexity:** O(n*m) -> O(n + m) expected, with n and m the two side sizes.

**Sketch:**

```python
from collections import defaultdict

def join(A, B, akey, bkey):
    index = defaultdict(list)          # build over the smaller side
    for b in B:
        index[bkey(b)].append(b)
    out = []
    for a in A:                        # output stays in A order
        for b in index.get(akey(a), ()):   # missing key: no pairs
            out.append((a, b))
    return out
```

**Oracle:** Nested loop on 0 to 40 rows per side, 300 cases, including duplicate keys on both
sides and keys of mixed type (`"42"` vs `42`).

**Traps:**
- Type mismatch between sides (string vs int ids): reproduce the old equality exactly. If the
  old loop compared `"42" == 42` and matched nothing, the dict must match nothing too; coerce
  both sides only when the user confirms the old mismatch was a bug, and say so in the report.
- `None` as a key matches other `None` rows; decide whether that is wanted.
- Index the smaller side.
- Preserve the original output order if the old code emitted pairs in A order (the sketch
  does); within one A row, B order follows insertion order.

**Not when:**
- The condition is a range or inequality: use Sort then scan with two pointers.
- Both sides are already in a database: write the join in SQL.

## Sort then scan

**Problem shape:** Dedup, grouping, or merging where memory is tight or sorted output is
wanted; range joins such as `a.start <= b.t < a.end`.

**Precondition test:** A total order on the key exists and comparing two keys is cheap. Mixed
types with no order (int vs str) fail this test.

**Complexity:** O(n^2) -> O(n log n), with O(1) extra memory beyond the sort.

**Sketch:**

```python
def dedup_sorted(rows, key):           # keep-first within each key group
    out, prev = [], object()
    for r in sorted(rows, key=key):    # sorted is stable
        k = key(r)
        if k != prev:
            out.append(r)
            prev = k
    return out

def merge_ranges(intervals, points):   # both sorted; emit (interval, point) pairs
    out, j = [], 0
    for start, end, tag in intervals:
        while j < len(points) and points[j] < start:
            j += 1
        k = j
        while k < len(points) and points[k] < end:
            out.append((tag, points[k]))
            k += 1
    return out
```

**Oracle:** Nested loop on small inputs; assert equal multisets and, if order matters, equal
sequences.

**Traps:**
- Unstable sorts change which duplicate survives: use `sorted` (stable) and sort by a full
  key, with the original index as the last component when needed.
- Mixed types raise `TypeError` in Python 3.
- Locale-dependent string order: sort on the raw code points unless the old code used a
  collation.
- The two-pointer merge is only linear if both inputs are sorted on the same axis.

**Not when:**
- A hashable key exists and memory is fine: Hash-key dedup is simpler and faster.

## Blocking for near-duplicates

**Problem shape:** Records that are "the same" despite typos, spacing, punctuation or
formatting; pairwise similarity over all pairs.

**Precondition test:** (1) Canonicalise everything that can be made exact: trim, casefold,
Unicode NFC, digits-only phone numbers, parsed dates. Dedup exactly on that first. (2) Name a
cheap blocking key that true duplicates almost always share (for example the first three
letters of the canonical surname plus the postcode district, or the normalised phone). If no
such key exists, consider MinHash/LSH on token shingles; if that is also unavailable, the card
fails and full pairwise is the only exact method.

**Complexity:** O(n^2) -> O(sum over blocks of b_i^2), which is O(n*b) when the largest block
has b records. Report the largest block size.

**Sketch:**
1. Canonicalise every field; exact-dedup on the canonical tuple (Hash-key dedup).
2. Group the remainder by the blocking key.
3. Within each block run the pairwise similarity; cap block size (for example 2000) and
   sub-block by a second key when exceeded.
4. Union-find over matching pairs to form clusters; pick a deterministic representative
   (smallest original index).
5. Report: block count, largest block, pairs compared, clusters formed.

**Oracle:** Full pairwise similarity on a sample that actually contains duplicate pairs: either
every record from 50 randomly chosen blocks plus a uniform sample of 2000 more, or a sample
seeded with labelled duplicate pairs (clean records plus generated typo variants). A uniform
sample of 2000 from millions holds almost no true pairs and estimates nothing. Report recall
(pairs the blocked method found over pairs the oracle found) together with the number of
oracle pairs it rests on. Precision is unchanged because the same similarity function runs
inside blocks.

**Traps:**
- Skewed blocks: one block holding half the data is quadratic again. Sub-block it.
- Recall loss across blocks is silent, so always report measured recall and its pair count.
- Similarity thresholds tuned on one dataset do not transfer; re-measure.
- Canonicalisation that merges distinct people (two "J Smith" at the same postcode); keep the
  canonical dedup separate from the fuzzy step so the exact part is auditable.

**Not when:**
- n <= about 3,000 (4.5M pairs, fine once): run full pairwise.
- Exact equality after canonicalisation is good enough: Hash-key dedup.

## Prefix sums and sliding window

**Problem shape:** `sum(a[i:i+w])` or `max(a[i:i+w])` recomputed for every i; rolling
averages; "count of events in the last 5 minutes" or "distinct users in the last hour"
recomputed per event.

**Precondition test:** The aggregate is invertible (sum, count, product without zeros) for
prefix sums; or it is min/max (monotone deque); or it is a distinct count (frequency map,
decrement on leave, remove at zero); or the window is defined on a sorted axis (two pointers).
If the aggregate is none of these (median, percentile), this card fails.

**Complexity:** O(n*w) -> O(n), w the window width.

**Sketch:**

```python
from collections import deque

def window_sums(a, w):
    P = [0]
    for x in a:
        P.append(P[-1] + x)
    return [P[i + w] - P[i] for i in range(len(a) - w + 1)]

def window_max(a, w):
    dq, out = deque(), []              # indices, values decreasing
    for i, x in enumerate(a):
        while dq and a[dq[-1]] <= x:
            dq.pop()
        dq.append(i)
        if dq[0] <= i - w:
            dq.popleft()
        if i >= w - 1:
            out.append(a[dq[0]])
    return out

def window_distinct(a, w):
    freq, out = {}, []
    for i, x in enumerate(a):
        freq[x] = freq.get(x, 0) + 1
        if i >= w:
            y = a[i - w]
            freq[y] -= 1
            if freq[y] == 0:
                del freq[y]
        if i >= w - 1:
            out.append(len(freq))
    return out
```

**Oracle:** Recompute each window from scratch on arrays of 0 to 50 elements, windows 1 to n.

**Traps:**
- Float drift over long prefix sums: use integers in fixed units, or recompute from scratch
  every k windows.
- Off-by-one at window edges (the oracle catches it).
- Time windows with duplicate timestamps: define whether the window is half-open.

**Not when:**
- The aggregate is not decomposable (median, percentiles): a different structure is needed
  (two heaps, or a sorted container), and only if Gate 2 says the input is big enough.
