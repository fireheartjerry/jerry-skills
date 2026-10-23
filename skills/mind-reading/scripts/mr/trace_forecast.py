"""Transparent numerical predictors of the embedding of an actual next human turn.

This is prediction, not endorsement: a close forecast says what the person tended to write next, never that it was
right or wanted. Callers pass in train and validation embeddings; nothing here reads histories, models or targets, and
nothing picks hyperparameters. NumPy is imported lazily so the stdlib-only skill still loads without it.
"""

BATCH = 128


def _np():
    try:
        import numpy
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise RuntimeError("trace_forecast needs numpy (use the local optional ML environment)") from exc
    return numpy


def _matrix(a, name, dims=2, dtype=None):
    np = _np()
    arr = np.asarray(a)
    if arr.dtype.kind not in "fiub":
        raise ValueError("%s must be numeric" % name)
    arr = arr.astype(dtype or np.float64, copy=False)
    if arr.ndim != dims:
        raise ValueError("%s must have %d dimensions, got shape %s" % (name, dims, arr.shape))
    if not np.isfinite(arr).all():
        raise ValueError("%s must be finite" % name)
    return arr


def normalize(array):
    """Row unit vectors; zero rows stay zero. A 1D input is treated as one row and returned 1D."""
    np = _np()
    arr = np.asarray(array)
    if arr.dtype.kind not in "fiub":
        raise ValueError("array must be numeric")
    out_dtype = np.float32 if arr.dtype == np.float32 else np.float64
    a = arr.astype(np.float64)
    if a.ndim not in (1, 2):
        raise ValueError("array must be 1D or 2D")
    if not np.isfinite(a).all():
        raise ValueError("array must be finite")
    norm = np.linalg.norm(a, axis=-1, keepdims=True)
    out = np.divide(a, norm, out=np.zeros_like(a), where=norm > 0)
    return out.astype(out_dtype)


def fit_ridge(X, Y, alpha=10.0):
    """Centered ridge map X -> Y. Dual solve when N < D, primal otherwise. Prediction is (x - x_mean) @ weight + y_mean."""
    np = _np()
    if not alpha > 0 or not np.isfinite(alpha):
        raise ValueError("alpha must be a finite number > 0")
    X = _matrix(X, "X")
    Y = _matrix(Y, "Y")
    if X.shape != Y.shape:
        raise ValueError("X and Y must have the same shape, got %s and %s" % (X.shape, Y.shape))
    n, d = X.shape
    if n < 2:
        raise ValueError("need at least 2 rows")
    x_mean, y_mean = X.mean(axis=0), Y.mean(axis=0)
    Xc, Yc = X - x_mean, Y - y_mean
    if n < d:
        gram = Xc @ Xc.T + alpha * np.eye(n)
        weight = Xc.T @ np.linalg.solve(gram, Yc)
    else:
        gram = Xc.T @ Xc + alpha * np.eye(d)
        weight = np.linalg.solve(gram, Xc.T @ Yc)
    return {"weight": weight.astype(np.float32), "x_mean": x_mean.astype(np.float32),
            "y_mean": y_mean.astype(np.float32), "alpha": float(alpha)}


def predict_ridge(model, X):
    """Unit-normalized ridge predictions for each row of X."""
    np = _np()
    weight = _matrix(model["weight"], "weight")
    x_mean = _matrix(model["x_mean"], "x_mean", 1)
    y_mean = _matrix(model["y_mean"], "y_mean", 1)
    X = _matrix(X, "X")
    d = weight.shape[0]
    if weight.shape != (d, d) or x_mean.shape != (d,) or y_mean.shape != (d,) or X.shape[1] != d:
        raise ValueError("shape mismatch between model and X %s" % (X.shape,))
    return normalize(((X - x_mean) @ weight + y_mean).astype(np.float32))


def retrieve_forecast(X, train_X, train_Y, k=8, temperature=0.1, exclude=None):
    """Softmax-weighted mean of the train_Y rows of the k nearest train_X rows by cosine, unit normalized.

    Ties break by ascending train index. `exclude` is an explicit per-query list of train indices to drop (for example
    the query's own session); it is never inferred. Queries run in blocks so no full N x M matrix is built."""
    np = _np()
    X = normalize(_matrix(X, "X")).astype(np.float64)
    tx = normalize(_matrix(train_X, "train_X")).astype(np.float64)
    ty = _matrix(train_Y, "train_Y")
    if tx.shape[0] != ty.shape[0] or X.shape[1] != tx.shape[1]:
        raise ValueError("shape mismatch: X %s, train_X %s, train_Y %s" % (X.shape, tx.shape, ty.shape))
    m = tx.shape[0]
    if int(k) != k or k < 1:
        raise ValueError("k must be a positive integer")
    if not temperature > 0 or not np.isfinite(temperature):
        raise ValueError("temperature must be a finite number > 0")
    if exclude is not None:
        if len(exclude) != X.shape[0]:
            raise ValueError("exclude needs one list per query")
        for row in exclude:
            if any(int(i) != i or not 0 <= i < m for i in row):
                raise ValueError("exclude index out of range")
        if any(len(set(int(i) for i in row)) >= m for row in exclude):
            raise ValueError("exclude leaves no train rows for a query")
    take = min(int(k), m)
    out = np.zeros((X.shape[0], ty.shape[1]), dtype=np.float64)
    for start in range(0, X.shape[0], BATCH):
        sims = X[start:start + BATCH] @ tx.T
        if exclude is not None:
            for r, row in enumerate(exclude[start:start + BATCH]):
                if len(row):
                    sims[r, [int(i) for i in row]] = -np.inf
        order = np.argsort(-sims, axis=1, kind="stable")[:, :take]
        top = np.take_along_axis(sims, order, axis=1) / temperature
        w = np.exp(top - top.max(axis=1, keepdims=True))  # excluded rows have -inf and weigh 0
        w /= w.sum(axis=1, keepdims=True)
        out[start:start + BATCH] = np.einsum("nk,nkd->nd", w, ty[order])
    return normalize(out)


def rank_candidates(predictions, candidates):
    """Cosine of each prediction against candidates [C,D] (-> N,C) or per-row candidates [N,K,D] (-> N,K)."""
    np = _np()
    p = normalize(_matrix(predictions, "predictions"))
    c = np.asarray(candidates)
    if c.ndim == 2:
        c = normalize(_matrix(c, "candidates"))
        if c.shape[1] != p.shape[1]:
            raise ValueError("dimension mismatch")
        return p @ c.T
    c = normalize(_matrix(c, "candidates", 3).reshape(-1, c.shape[-1])).reshape(c.shape)
    if c.shape[0] != p.shape[0] or c.shape[2] != p.shape[1]:
        raise ValueError("candidates %s do not match predictions %s" % (c.shape, p.shape))
    return np.einsum("nd,nkd->nk", p, c)


def _macro(values, families):
    np = _np()
    labels = list(dict.fromkeys(families))
    index = {f: i for i, f in enumerate(labels)}
    ids = np.array([index[f] for f in families])
    sums = np.bincount(ids, weights=values, minlength=len(labels))
    counts = np.bincount(ids, minlength=len(labels))
    return sums / counts


def _check_families(families, n):
    if len(families) != n:
        raise ValueError("families must align with the examples")
    return [f if isinstance(f, (str, int)) else str(f) for f in families]


def metrics(scores, gold_indices, families=None):
    """top1/top3/MRR/logloss of the gold column. Exact score ties share the average rank, and top1/top3 give the
    expected credit of a uniformly random order inside the tie. Logloss is the softmax of the raw (uncalibrated)
    scores. With `families`, also family_macro_top1 and family_macro_mrr (each family weighs the same)."""
    np = _np()
    s = _matrix(scores, "scores")
    n, c = s.shape
    if n < 1 or c < 1:
        raise ValueError("scores must be non-empty")
    gold = np.asarray(gold_indices)
    if gold.shape != (n,) or gold.dtype.kind not in "iu" or (gold < 0).any() or (gold >= c).any():
        raise ValueError("gold_indices must be %d integers in [0, %d)" % (n, c))
    g = s[np.arange(n), gold]
    greater = (s > g[:, None]).sum(axis=1)
    tied = (s == g[:, None]).sum(axis=1)  # includes the gold column
    avg_rank = 1.0 + greater + (tied - 1) / 2.0
    top1 = np.where(greater == 0, 1.0 / tied, 0.0)
    top3 = np.clip(np.minimum(tied, 3 - greater), 0, None) / tied
    mrr = 1.0 / avg_rank
    peak = s.max(axis=1)
    logloss = peak + np.log(np.exp(s - peak[:, None]).sum(axis=1)) - g
    out = {"n": int(n), "top1": float(top1.mean()), "top3": float(top3.mean()), "mrr": float(mrr.mean()),
           "logloss": float(logloss.mean()),
           "per_example": {"top1": top1, "top3": top3, "mrr": mrr, "logloss": logloss}}
    if families is not None:
        fam = _check_families(families, n)
        out["n_families"] = len(set(fam))
        out["family_macro_top1"] = float(_macro(top1, fam).mean())
        out["family_macro_mrr"] = float(_macro(mrr, fam).mean())
    return out


def paired_bootstrap(values_a, values_b, families, seed=42, draws=2000):
    """Family-macro mean of (a - b) with a 95% percentile interval from resampling whole families with replacement.

    Examples in a family are correlated, so each family is first reduced to its mean difference and every family
    weighs the same; duplicating examples inside a family cannot move the estimate."""
    np = _np()
    a, b = _matrix(values_a, "values_a", 1), _matrix(values_b, "values_b", 1)
    if a.shape != b.shape or a.size < 1:
        raise ValueError("values_a and values_b must align and be non-empty")
    if int(draws) != draws or draws < 1:
        raise ValueError("draws must be a positive integer")
    fam = _check_families(families, a.size)
    per_family = _macro(a - b, fam)
    f = per_family.size
    if f < 2:
        raise ValueError("need at least 2 families")
    rng = np.random.default_rng(seed)
    means = per_family[rng.integers(0, f, size=(int(draws), f))].mean(axis=1)
    lo, hi = np.percentile(means, [2.5, 97.5])
    return {"mean_diff": float(per_family.mean()), "ci_low": float(lo), "ci_high": float(hi),
            "n_families": int(f), "draws": int(draws), "seed": seed, "estimand": "family_macro_mean(a - b)"}
