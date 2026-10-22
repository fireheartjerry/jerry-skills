"""Pure statistics for the measurement engine. Standard library only, Python 3.8+.

Every function states its formula. Nothing here prints, reads files or draws unseeded
random numbers; every resampling function takes a seed and uses its own random.Random.
"""
import functools
import math
import random

from devkit.common import fmt_time

SCALE_FLOOR = 0.5       # seconds; the smallest scale median should be at least this
BASELINE_RATIO = 5.0    # with the baseline subtracted, >= 5x it (about 0.1 s) fits reliably
EXACT_LIMIT = 200000    # Mann-Whitney: enumerate all splits when C(na+nb, na) is at most this


# ------------------------------------------------------------------ location and spread


def median(xs):
    """Middle value of the sorted list, or the mean of the two middle values for even n."""
    s = sorted(xs)
    n = len(s)
    if n == 0:
        raise ValueError("median of empty list")
    mid = n // 2
    if n % 2:
        return float(s[mid])
    return (s[mid - 1] + s[mid]) / 2.0


def quartiles(xs):
    """(q1, q3) by the median-of-halves method; the middle element is excluded for odd n."""
    s = sorted(xs)
    n = len(s)
    if n == 0:
        raise ValueError("quartiles of empty list")
    if n == 1:
        return float(s[0]), float(s[0])
    half = n // 2
    lower = s[:half]
    upper = s[half + 1:] if n % 2 else s[half:]
    return median(lower), median(upper)


def mad(xs):
    """Median absolute deviation: median(|x - median(xs)|), unscaled."""
    m = median(xs)
    return median([abs(x - m) for x in xs])


def cv(xs):
    """Coefficient of variation as a percentage: sample standard deviation / mean x 100.

    Zero for fewer than two values or a zero mean.
    """
    n = len(xs)
    if n < 2:
        return 0.0
    mean = sum(xs) / float(n)
    if mean == 0:
        return 0.0
    var = sum((x - mean) ** 2 for x in xs) / (n - 1.0)
    return 100.0 * math.sqrt(var) / abs(mean)


def tukey_counts(xs):
    """(mild, severe) outlier counts by Tukey fences on the median-of-halves quartiles.

    mild: outside [q1 - 1.5 IQR, q3 + 1.5 IQR] but inside the 3 IQR fences; severe: outside
    [q1 - 3 IQR, q3 + 3 IQR]. Values are counted, never removed.
    """
    q1, q3 = quartiles(xs)
    iqr = q3 - q1
    mild = severe = 0
    for x in xs:
        if x < q1 - 3 * iqr or x > q3 + 3 * iqr:
            severe += 1
        elif x < q1 - 1.5 * iqr or x > q3 + 1.5 * iqr:
            mild += 1
    return mild, severe


def percentile(sorted_xs, q):
    """Linear-interpolation percentile of an already sorted list, q in [0, 1]."""
    n = len(sorted_xs)
    if n == 0:
        raise ValueError("percentile of empty list")
    pos = q * (n - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return sorted_xs[lo] * (1 - frac) + sorted_xs[hi] * frac


# ------------------------------------------------------------------ ranks and correlation


def midranks(values):
    """1-based ranks with ties given the average of the ranks they span (midranks)."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(xs, ys):
    """Spearman rho: Pearson correlation of the midranks of xs and ys.

    Returns 0.0 when either series is constant (no trend can be read from it).
    """
    if len(xs) != len(ys) or len(xs) < 2:
        return 0.0
    rx, ry = midranks(xs), midranks(ys)
    mx = sum(rx) / len(rx)
    my = sum(ry) / len(ry)
    sxx = sum((a - mx) ** 2 for a in rx)
    syy = sum((b - my) ** 2 for b in ry)
    if sxx == 0 or syy == 0:
        return 0.0
    sxy = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    return sxy / math.sqrt(sxx * syy)


# ------------------------------------------------------------------ effect sizes


def bootstrap_ratio_ci(before, after, resamples=5000, seed=0, level=0.95):
    """Percentile bootstrap CI of median(before) / median(after).

    Each draw resamples within each arm with replacement (sizes unchanged), takes both
    medians and records their ratio; the CI is the (1-level)/2 and 1-(1-level)/2 percentiles
    of the draws. Draws with a zero after-median are skipped. Seeded with random.Random(seed).
    """
    if not before or not after:
        raise ValueError("bootstrap needs non-empty arms")
    rng = random.Random(seed)
    nb, na = len(before), len(after)
    ratios = []
    for _ in range(resamples):
        mb = median(rng.choices(before, k=nb))
        ma = median(rng.choices(after, k=na))
        if ma > 0:
            ratios.append(mb / ma)
    if not ratios:
        raise ValueError("every resample had a zero after-median, so the ratio is undefined")
    ratios.sort()
    tail = (1.0 - level) / 2.0
    ma = median(after)
    point = median(before) / ma if ma > 0 else float("inf")
    return {"ratio": point, "lo": percentile(ratios, tail), "hi": percentile(ratios, 1 - tail),
            "resamples": resamples, "seed": seed, "level": level}


def hodges_lehmann(before, after):
    """Hodges-Lehmann shift: median over all pairs of (before_i - after_j).

    Positive means the after arm is lower (time saved per run), in the arms' units.
    """
    return median([b - a for b in before for a in after])


@functools.lru_cache(maxsize=64)
def _ranksum_distribution(ranks2, k):
    """Count, for every doubled rank sum, the k-subsets of ranks2 that reach it.

    Dynamic programming over the items; the counts equal those from enumerating every
    C(n, k) split explicitly. ranks2 are doubled midranks so sums stay integers.
    """
    dp = [dict() for _ in range(k + 1)]
    dp[0][0] = 1
    for i, r in enumerate(ranks2):
        for j in range(min(k, i + 1), 0, -1):
            cur = dp[j]
            for s, c in dp[j - 1].items():
                cur[s + r] = cur.get(s + r, 0) + c
    return dp[k]


def norm_cdf(z):
    """Standard normal CDF via erfc: Phi(z) = erfc(-z / sqrt 2) / 2."""
    return 0.5 * math.erfc(-z / math.sqrt(2.0))


def mann_whitney(a, b, alternative="two-sided", exact_limit=EXACT_LIMIT):
    """Mann-Whitney U test of a against b.

    U = (number of pairs (x in a, y in b) with x > y) + 0.5 x (ties) = R_a - na(na+1)/2 with
    R_a the midrank sum of a in the pooled sample. p_less = P(U <= u) under H0 (a tends to be
    smaller), p_greater = P(U >= u), two-sided = min(1, 2 min(p_less, p_greater)). When
    C(na+nb, na) <= exact_limit the null distribution is the set of all splits of the pooled
    ranks (exact, ties included); otherwise the normal approximation with the tie correction
    var = na nb / 12 x ((n+1) - sum(t^3 - t) / (n(n-1))) and a 0.5 continuity correction.
    Returns a dict with u, p (for the alternative), p_less, p_greater, p_two_sided, method, z.
    """
    if alternative not in ("two-sided", "less", "greater"):
        raise ValueError("alternative must be two-sided, less or greater")
    na, nb = len(a), len(b)
    if na == 0 or nb == 0:
        raise ValueError("mann_whitney needs two non-empty samples")
    pooled = list(a) + list(b)
    ranks = midranks(pooled)
    ra = sum(ranks[:na])
    u = ra - na * (na + 1) / 2.0
    n = na + nb
    z = None
    if math.comb(n, na) <= exact_limit:
        ranks2 = tuple(sorted(int(round(2 * r)) for r in ranks))
        dist = _ranksum_distribution(ranks2, na)
        total = float(math.comb(n, na))
        ra2 = int(round(2 * ra))
        p_less = sum(c for s, c in dist.items() if s <= ra2) / total
        p_greater = sum(c for s, c in dist.items() if s >= ra2) / total
        method = "exact"
    else:
        counts = {}
        for v in pooled:
            counts[v] = counts.get(v, 0) + 1
        tie_term = sum(t ** 3 - t for t in counts.values())
        mean = na * nb / 2.0
        var = na * nb / 12.0 * ((n + 1) - tie_term / float(n * (n - 1)))
        if var <= 0:
            p_less = p_greater = 1.0
            z = 0.0
        else:
            sd = math.sqrt(var)
            z_less = (u + 0.5 - mean) / sd
            z_greater = (u - 0.5 - mean) / sd
            p_less = norm_cdf(z_less)
            p_greater = 1.0 - norm_cdf(z_greater)
            z = z_less if alternative == "less" else z_greater
        method = "normal"
    p_two = min(1.0, 2.0 * min(p_less, p_greater))
    p = {"less": p_less, "greater": p_greater, "two-sided": p_two}[alternative]
    return {"u": u, "p": p, "p_less": p_less, "p_greater": p_greater, "p_two_sided": p_two,
            "method": method, "z": z, "n": [na, nb]}


# ------------------------------------------------------------------ t distribution


def _betacf(a, b, x):
    """Continued fraction for the incomplete beta function (modified Lentz method)."""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 400):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / (c if abs(c) > tiny else tiny)
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / (c if abs(c) > tiny else tiny)
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-14:
            break
    return h


def betainc(a, b, x):
    """Regularised incomplete beta I_x(a, b) = B(x; a, b) / B(a, b), via continued fraction.

    Uses the symmetry I_x(a, b) = 1 - I_{1-x}(b, a) when x > (a+1)/(a+b+2) for convergence.
    """
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(lbeta + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def t_sf(t, df):
    """One-sided survival P(T > t) for Student t with df degrees of freedom.

    For t >= 0: P(T > t) = I_x(df/2, 1/2) / 2 with x = df / (df + t^2); for t < 0 by symmetry.
    """
    if math.isinf(t):
        return 0.0 if t > 0 else 1.0
    x = df / (df + t * t)
    tail = 0.5 * betainc(df / 2.0, 0.5, x)
    return tail if t >= 0 else 1.0 - tail


def t_two_sided(t, df):
    """Two-sided p = P(|T| > |t|) = I_x(df/2, 1/2) with x = df / (df + t^2)."""
    if math.isinf(t):
        return 0.0
    return betainc(df / 2.0, 0.5, df / (df + t * t))


def welch_t(xs, ys):
    """Welch's t and Welch-Satterthwaite degrees of freedom for two samples.

    t = (mean_x - mean_y) / sqrt(s_x^2/n_x + s_y^2/n_y);
    df = (s_x^2/n_x + s_y^2/n_y)^2 / ((s_x^2/n_x)^2/(n_x-1) + (s_y^2/n_y)^2/(n_y-1)).
    With both variances zero t is +-inf when the means differ and 0 when equal (df = 1).
    """
    nx, ny = len(xs), len(ys)
    if nx < 2 or ny < 2:
        raise ValueError("welch_t needs at least two values per side")
    mx, my = sum(xs) / float(nx), sum(ys) / float(ny)
    vx = sum((x - mx) ** 2 for x in xs) / (nx - 1.0)
    vy = sum((y - my) ** 2 for y in ys) / (ny - 1.0)
    ax, ay = vx / nx, vy / ny
    se2 = ax + ay
    if se2 == 0:
        if mx == my:
            return 0.0, 1.0
        return (math.inf if mx > my else -math.inf), 1.0
    df_den = (ax * ax / (nx - 1.0) if ax else 0.0) + (ay * ay / (ny - 1.0) if ay else 0.0)
    df = se2 * se2 / df_den if df_den > 0 else float(nx + ny - 2)
    return (mx - my) / math.sqrt(se2), df


# ------------------------------------------------------------------ scaling exponents


def loglog_slope(sizes, values):
    """Least-squares slope of log(value) on log(size), unrounded."""
    if len(sizes) != len(values) or len(set(sizes)) < 2:
        raise ValueError("fit_exponent needs at least two distinct sizes")
    if any(s <= 0 for s in sizes) or any(v <= 0 for v in values):
        raise ValueError("fit_exponent needs positive sizes and medians")
    xs = [math.log(float(s)) for s in sizes]
    ys = [math.log(float(v)) for v in values]
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / sxx


def fit_exponent(sizes, medians):
    """Least-squares slope of log(median) on log(size), rounded to 2 decimals."""
    return round(loglog_slope(sizes, medians), 2)


def exponent_ci(sizes, runs_per_size, baseline=0.0, resamples=2000, seed=0, level=0.95):
    """Bootstrap CI of the scaling exponent.

    Each draw resamples the runs of every size with replacement, takes the medians, subtracts
    the baseline and refits the log-log slope; draws where a median falls inside the baseline
    are skipped. Returns (lo, hi) or None when fewer than 20 draws could be fitted.
    """
    rng = random.Random(seed)
    slopes = []
    for _ in range(resamples):
        meds = [median(rng.choices(r, k=len(r))) - baseline for r in runs_per_size]
        if all(m > 0 for m in meds):
            slopes.append(loglog_slope(sizes, meds))
    if len(slopes) < 20:
        return None
    slopes.sort()
    tail = (1.0 - level) / 2.0
    return percentile(slopes, tail), percentile(slopes, 1.0 - tail)


def scale_exponent(sizes, medians, baseline):
    """Exponent fitted after subtracting the startup baseline from every median.

    Returns (exponent or None, warnings). None means the medians sit within the baseline
    and nothing can be fitted; the warnings say what to do.
    """
    warnings = []
    smallest = min(medians)
    if baseline > 0 and smallest < BASELINE_RATIO * baseline:
        warnings.append("smallest median %s is under %dx the startup baseline %s; raise the "
                        "sizes until it is at least %s and refit"
                        % (fmt_time(smallest), int(BASELINE_RATIO), fmt_time(baseline),
                           fmt_time(BASELINE_RATIO * baseline)))
    elif smallest < SCALE_FLOOR:
        warnings.append("smallest median %s is under %s; raise the sizes until it is at "
                        "least 0.5 s and refit" % (fmt_time(smallest), fmt_time(SCALE_FLOOR)))
    adjusted = [m - baseline for m in medians]
    if any(a <= 0 for a in adjusted):
        warnings.append("a median is within the startup baseline; exponent not fitted")
        return None, warnings
    return fit_exponent(sizes, adjusted), warnings


def classify_exponent(e):
    """Band label for a fitted exponent (1.3 / 1.7 / 2.5 / 3.5 boundaries)."""
    if e < 1.3:
        return "about linear"
    if e < 1.7:
        return "between linear and quadratic"
    if e < 2.5:
        return "about quadratic"
    if e < 3.5:
        return "about cubic"
    return "worse than cubic"


# ------------------------------------------------------------------ decisions


def decide_verdict(lo, hi, p_faster, p_slower, min_effect, alpha=0.05):
    """The compare decision rule on a before/after ratio CI [lo, hi].

    FASTER when lo > 1 + min_effect and p_faster < alpha; SLOWER when hi < 1/(1 + min_effect)
    and p_slower < alpha; NO DETECTABLE CHANGE when [lo, hi] lies inside
    [1/(1 + min_effect), 1 + min_effect]; INCONCLUSIVE otherwise. Returns (verdict, reason).
    """
    upper = 1.0 + min_effect
    lower = 1.0 / upper
    band = "band %.3fx..%.3fx" % (lower, upper)
    if lo > upper and p_faster < alpha:
        return "FASTER", "CI lower bound %.3fx > %.3fx and one-sided p %.4g < %g" % (
            lo, upper, p_faster, alpha)
    if hi < lower and p_slower < alpha:
        return "SLOWER", "CI upper bound %.3fx < %.3fx and one-sided p %.4g < %g" % (
            hi, lower, p_slower, alpha)
    if lo >= lower and hi <= upper:
        return "NO DETECTABLE CHANGE", "CI %.3fx..%.3fx lies inside the %s" % (lo, hi, band)
    return "INCONCLUSIVE", "CI %.3fx..%.3fx crosses the %s (one-sided p faster %.4g, slower %.4g)" % (
        lo, hi, band, p_faster, p_slower)


def suggest_runs(runs, half_width, min_effect):
    """Run count to try next: 2x when the CI half-width is under 2x min_effect, else 4x."""
    return 2 * runs if half_width < 2 * min_effect else 4 * runs


def changepoint(series, min_size=5, alpha=0.01):
    """Deterministic binary segmentation with Welch's t and a Bonferroni split correction.

    For each segment, every split with both sides at least min_size long is a candidate; the
    split with the largest |t| is accepted when its two-sided p is below
    alpha / (number of candidates in the segment), then both sides are searched recursively.
    Returns change points sorted by index; each has index (first element of the new
    segment), t, df, p, before_median, after_median and percent_change.
    """
    series = [float(x) for x in series]
    bad = [i for i, x in enumerate(series) if x != x or x in (float("inf"), float("-inf"))]
    if bad:
        raise ValueError("non-finite value at index %d; one NaN or inf would make every split "
                         "test NaN and hide every step" % bad[0])
    points = []

    def search(lo, hi):
        candidates = list(range(lo + min_size, hi - min_size + 1))
        if not candidates:
            return
        best = None
        for k in candidates:
            t, df = welch_t(series[lo:k], series[k:hi])
            if t != t:                  # NaN: skip the split rather than poison the search
                continue
            if best is None or abs(t) > abs(best[1]):
                best = (k, t, df)
        if best is None:
            return
        k, t, df = best
        p = t_two_sided(t, df)
        if p < alpha / len(candidates):
            before, after = median(series[lo:k]), median(series[k:hi])
            change = (after - before) / before * 100.0 if before else None
            points.append({"index": k, "t": t, "df": df, "p": p, "before_median": before,
                           "after_median": after, "percent_change": change,
                           "candidates": len(candidates)})
            search(lo, k)
            search(k, hi)

    search(0, len(series))
    return sorted(points, key=lambda d: d["index"])
