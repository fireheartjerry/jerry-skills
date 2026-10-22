"""Small exact statistics for the human metrics: Wilson score intervals and the two-sided sign test. Pure functions."""
import math

Z95 = 1.959964  # the 97.5th percentile of the standard normal


def wilson(k, n, z=Z95):
    """(rate, low, high) for k successes in n trials; (None, None, None) when n is 0. The Wilson score interval keeps
    its bounds inside [0, 1] and stays sensible at k = 0 or k = n, where the normal approximation collapses."""
    if not n:
        return None, None, None
    p = k / float(n)
    z2 = z * z
    den = 1.0 + z2 / n
    centre = (p + z2 / (2.0 * n)) / den
    half = z * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n)) / den
    lo = 0.0 if k == 0 else max(0.0, centre - half)
    hi = 1.0 if k == n else min(1.0, centre + half)
    return p, lo, hi


def rate_block(k, n):
    """{"k", "n", "rate", "lo", "hi"} with the rate and bounds rounded to 4 places (None when n is 0)."""
    p, lo, hi = wilson(k, n)
    r = (lambda x: None if x is None else round(x, 4))
    return {"k": int(k), "n": int(n), "rate": r(p), "lo": r(lo), "hi": r(hi)}


def sign_test(wins_a, wins_b):
    """Two-sided exact sign test: the probability, under no preference (p = 0.5), of a split at least as uneven as
    wins_a against wins_b. Ties are excluded before calling. 1.0 when there are no decisive pairs."""
    n = int(wins_a) + int(wins_b)
    if n == 0:
        return 1.0
    k = min(int(wins_a), int(wins_b))
    tail = sum(math.comb(n, i) for i in range(0, k + 1)) / float(2 ** n)
    return min(1.0, 2.0 * tail)


def format_rate(block, pct=True):
    """`12% (3/25, 95% CI 4-30%)`, or `n/a (0/0)` when nothing was classified."""
    if not block or not block.get("n"):
        return "n/a (0/0)"
    f = (lambda x: "%d%%" % round(100 * x)) if pct else (lambda x: "%.3f" % x)
    return "%s (%d/%d, 95%% CI %s-%s)" % (f(block["rate"]), block["k"], block["n"], f(block["lo"]).rstrip("%"), f(block["hi"]))
