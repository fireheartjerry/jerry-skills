"""Per-call cost estimates for capped runs (refresh --auto, track --engine, prefer build).

An estimate only sizes the reservation the engine makes before a call (engine.Engine cost_estimate), so a run stops
before a call that would carry it past --max-cost-usd. The engine's reported cost is what is charged against the cap.
Two sources, the larger wins: the price table in config (`auto.usd_per_mtok`, input and output USD per million tokens,
matched by a substring of the model name) times `auto.estimate_safety`, and the cost per prompt character observed in
earlier runs of the same stage (runs/*/calls.jsonl rows that carry prompt_chars) with a 20% margin."""
import json
from pathlib import Path

CHARS_PER_TOKEN = 4


def price(cfg, model):
    """(input, output) USD per million tokens for a model name."""
    table = (cfg.get("auto") or {}).get("usd_per_mtok") or {}
    name = (model or "").lower()
    for key, val in table.items():
        if key != "default" and key in name:
            return float(val[0]), float(val[1])
    d = table.get("default") or [5.0, 25.0]
    return float(d[0]), float(d[1])


def table_estimate(cfg, model, prompt_chars, out_tokens):
    p_in, p_out = price(cfg, model)
    safety = float((cfg.get("auto") or {}).get("estimate_safety") or 1.0)
    return safety * ((prompt_chars / CHARS_PER_TOKEN) * p_in + out_tokens * p_out) / 1e6


def observed_rate(runs_dir, stage, limit_runs=20):
    """USD per prompt character over the newest runs of a stage, or None when no run recorded both."""
    root = Path(runs_dir)
    if not root.exists():
        return None
    runs = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name)[-200:]
    cost, chars, seen = 0.0, 0, 0
    for run in reversed(runs):
        try:
            info = json.loads((run / "engine.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if info.get("stage") != stage:
            continue
        seen += 1
        try:
            lines = (run / "calls.jsonl").read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        for line in lines:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("cost_usd") and r.get("prompt_chars"):
                cost += float(r["cost_usd"])
                chars += int(r["prompt_chars"])
        if seen >= limit_runs:
            break
    return (cost / chars) if chars else None


def estimator(cfg, model, stage, runs_dir=None):
    """A cost_estimate(unit, prompt) callable for engine.Engine."""
    out_tokens = int(((cfg.get("auto") or {}).get("out_tokens") or {}).get(stage, 8000))
    learned = observed_rate(runs_dir, stage) if runs_dir else None

    def estimate(unit, prompt):
        n = len(prompt or "")
        est = table_estimate(cfg, model, n, out_tokens)
        if learned:
            est = max(est, learned * n * 1.2)
        return est
    return estimate


class Budget:
    """What is left of one run's cap across several stages."""

    def __init__(self, max_cost_usd, max_calls=None):
        self.max_cost = float(max_cost_usd) if max_cost_usd is not None else None
        self.max_calls = int(max_calls) if max_calls is not None else None
        self.spent = 0.0
        self.calls = 0

    def charge(self, summary):
        """Add one engine summary's spend (cost_usd, calls)."""
        if not summary:
            return
        self.spent += float(summary.get("cost_usd") or 0.0)
        self.calls += int(summary.get("calls") or 0)

    def remaining_cost(self, reserve=0.0):
        if self.max_cost is None:
            return None
        return max(0.0, self.max_cost - self.spent - reserve)

    def remaining_calls(self):
        if self.max_calls is None:
            return None
        return max(0, self.max_calls - self.calls)

    def exhausted(self, reserve=0.0):
        rc = self.remaining_cost(reserve)
        if rc is not None and rc <= 0:
            return True
        r = self.remaining_calls()
        return r is not None and r <= 0

    def as_dict(self):
        return {"max_cost_usd": self.max_cost, "spent_usd": round(self.spent, 4), "max_calls": self.max_calls, "calls": self.calls}
