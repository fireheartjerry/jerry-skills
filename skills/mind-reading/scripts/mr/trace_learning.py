"""Local optional representation learning. Nothing imports this on the legacy path.

The frozen encoder is a language prior. A small adapter learns a particular
history's prefix-to-next-turn relation. Neither is a model of private thoughts.
All targets passed to fit_adapter must belong to training families only.
"""
import hashlib
import json
import os
from pathlib import Path


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for b in iter(lambda: f.read(1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


class LocalEncoder:
    def __init__(self, path, device="cpu", max_length=256):
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
        import torch
        from sentence_transformers import SentenceTransformer
        self.path = str(Path(path).resolve())
        if not Path(self.path).is_dir():
            raise ValueError("local encoder weights missing; downloads must be explicit")
        torch.set_num_threads(min(6, torch.get_num_threads()))
        self.model = SentenceTransformer(self.path, device=device, local_files_only=True,
                                        model_kwargs={"torch_dtype": torch.float16 if device != "cpu" else torch.float32})
        self.model.max_seq_length = max_length

    def encode(self, texts, batch_size=16):
        import numpy as np
        if not texts:
            return np.empty((0, self.model.get_sentence_embedding_dimension()), dtype="float32")
        return self.model.encode(texts, batch_size=batch_size, normalize_embeddings=True,
                                 show_progress_bar=False).astype("float32")


def fit_adapter(x, y, families, rank=32, steps=300, seed=42, learning_rate=0.002):
    """Family-balanced low-rank nonlinear adaptation, trained on fixed embeddings.

    A full family is sampled uniformly, then one of its turns. The objective
    combines cosine regression and in-batch discrimination. Duplicate target
    vectors are positives rather than false negatives. No held-out arrays enter.
    Returns plain arrays, not a pickle or executable checkpoint.
    """
    import numpy as np
    import torch
    from .trace_forecast import normalize
    x, y = np.asarray(x, dtype="float32"), np.asarray(y, dtype="float32")
    if x.ndim != 2 or x.shape != y.shape or len(x) != len(families) or len(x) < 2:
        raise ValueError("aligned finite train X,Y,families required")
    if not np.isfinite(x).all() or not np.isfinite(y).all() or not 1 <= rank <= x.shape[1] or steps < 1:
        raise ValueError("invalid adapter data or hyperparameters")
    torch.manual_seed(seed)
    rng = np.random.default_rng(seed)
    groups = {}
    for i, f in enumerate(families):
        groups.setdefault(f, []).append(i)
    keys = sorted(groups)
    batch = min(64, len(keys))
    d = x.shape[1]
    down = torch.nn.Linear(d, rank)
    up = torch.nn.Linear(rank, d)
    torch.nn.init.normal_(down.weight, std=0.05)
    torch.nn.init.zeros_(down.bias)
    torch.nn.init.zeros_(up.weight)
    torch.nn.init.zeros_(up.bias)
    optimizer = torch.optim.AdamW(list(down.parameters()) + list(up.parameters()), lr=learning_rate, weight_decay=0.01)
    tx, ty = torch.from_numpy(normalize(x)), torch.from_numpy(normalize(y))
    losses = []
    for _ in range(steps):
        fs = rng.choice(len(keys), size=batch, replace=False)
        ids = [int(rng.choice(groups[keys[int(f)]])) for f in fs]
        a, b = tx[ids], ty[ids]
        pred = torch.nn.functional.normalize(a + up(torch.tanh(down(a))), dim=1)
        logits = pred @ b.T / 0.1
        positive = (b @ b.T > 0.99999).float()
        positive /= positive.sum(dim=1, keepdim=True)
        contrast = -(positive * torch.log_softmax(logits, dim=1)).sum(dim=1).mean()
        loss = 0.5 * contrast + (1 - (pred * b).sum(dim=1)).mean()
        optimizer.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(list(down.parameters()) + list(up.parameters()), 1.0)
        optimizer.step()
        losses.append(float(loss.detach()))
    return {"down_weight": down.weight.detach().numpy(), "down_bias": down.bias.detach().numpy(),
            "up_weight": up.weight.detach().numpy(), "up_bias": up.bias.detach().numpy(),
            "loss_first": losses[0], "loss_last": losses[-1], "rank": rank, "steps": steps,
            "seed": seed, "parameter_count": 2*d*rank+d+rank, "sampling": "uniform family then uniform turn"}


def predict_adapter(model, x):
    import numpy as np
    from .trace_forecast import normalize
    x = np.asarray(x, dtype="float32")
    if x.ndim != 2 or not np.isfinite(x).all():
        raise ValueError("finite matrix required")
    return normalize(x + np.tanh(x @ model["down_weight"].T + model["down_bias"]) @ model["up_weight"].T + model["up_bias"])


def hard_candidates(examples, seed_vectors, count=8):
    """Fixed candidate task: continuations of the most similar *seed* situations.

    Gold text is only used to remove duplicates. Neither future assistant text
    nor future target embeddings select distractors. Prefer the same engine and
    project where enough unique alternatives exist; otherwise disclose fallback.
    One row's gold position is randomized by its immutable id.
    """
    import numpy as np
    from .trace_forecast import normalize
    v = normalize(seed_vectors)
    if len(examples) != len(v) or count < 2:
        raise ValueError("aligned examples and count >=2 required")
    normalized = [" ".join(e["target"]["text"].casefold().split()) for e in examples]
    rows, gold, strata = [], [], []
    for i, ex in enumerate(examples):
        similarity = v @ v[i]
        order = np.argsort(-similarity, kind="stable")
        candidates = [j for j in order if examples[j]["family"] != ex["family"] and normalized[j] != normalized[i]]
        strata_options = [
            ("same_project_engine", lambda j: examples[j].get("project") == ex.get("project") and examples[j]["source"] == ex["source"]),
            ("same_engine", lambda j: examples[j]["source"] == ex["source"]),
            ("all_sources", lambda j: True)]
        selected, label = [], None
        for label, predicate in strata_options:
            seen = {normalized[i]}
            seen_families = {ex["family"]}
            selected = []
            for j in candidates:
                if predicate(j) and normalized[j] not in seen and examples[j]["family"] not in seen_families:
                    selected.append(int(j)); seen.add(normalized[j])
                    seen_families.add(examples[j]["family"])
                    if len(selected) == count - 1:
                        break
            if len(selected) == count-1:
                break
        if len(selected) != count-1:
            raise ValueError("too few distinct other-family targets for fixed candidate count")
        row = [i] + selected
        rng = np.random.default_rng(int(hashlib.sha256(ex["id"].encode()).hexdigest()[:16], 16))
        rng.shuffle(row)
        rows.append(row); gold.append(row.index(i)); strata.append(label)
    return np.asarray(rows, dtype="int64"), np.asarray(gold, dtype="int64"), strata


def temperature(scores, gold, grid=(0.03, 0.06, 0.1, 0.2, 0.5, 1.0)):
    """Validation-only scalar calibration. Caller must not supply test scores."""
    import numpy as np
    scores, gold = np.asarray(scores), np.asarray(gold)
    def nll(t):
        z = scores/t
        z -= z.max(axis=1, keepdims=True)
        return float(np.mean(np.log(np.exp(z).sum(axis=1)) - z[np.arange(len(z)), gold]))
    return min(grid, key=nll)
