"""eval: expand held-out seeds under arms, extract requirements once per session, score each expansion blind,
pool, bootstrap, report, and record the one look in the ledger."""
import json
import random
import shutil
from pathlib import Path

from . import engine as _engine
from . import select as _select
from .config import read_json, write_json
from .corpus import now_iso
from .mine import privacy_line

RUBRIC_VERSION = 4
EXPAND_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["prompt"],
                 "properties": {"prompt": {"type": "string", "minLength": 20}}}
CONTROL_PERSONA = "a demanding, experienced user of coding agents"
MODES = ("first", "context")
# Requirement kinds the judge may assign (requirements.schema.json). The report breaks results down by each one that
# appears; these five are always listed because they are the kinds the skill claims to predict.
REPORTED_KINDS = ("constraint", "follow_through", "scope", "preference", "task_fact")
LIMITATIONS = {
    "first": "No arm saw project memory or the conversation; the eval measures the model on context-free first-message seeds.",
    "context": ("Every arm saw the user's earlier messages in the session but no project memory, files or agent replies; the eval "
                "measures the model on short mid-session seeds with conversational context."),
}


class EvalRefused(Exception):
    pass


# ---------------------------------------------------------------- scoring math (pure)

def count_errors(rows, historical=False):
    """Reject impossible count artifacts, including historical fractional counts; never round them into validity."""
    errors = []
    for row in rows:
        sid = row.get("id", row.get("sid", "?"))
        n = row.get("n_req") if historical else row.get("n")
        if not isinstance(n, int) or isinstance(n, bool) or n < 0:
            errors.append("%s: requirement count is not a nonnegative integer" % sid)
            continue
        arms = {a: row[a] for a in ("old", "new") if a in row} if historical else row.get("arms", {})
        for arm, counts in arms.items():
            fields = ("full", "partial") if historical else ("full", "partial", "none", "contra")
            valid = True
            for field in fields:
                value = counts.get(field, 0)
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    errors.append("%s %s: %s is not a nonnegative integer (%r)" % (sid, arm, field, value))
                    valid = False
            if valid and counts.get("full", 0) + counts.get("partial", 0) > n:
                errors.append("%s %s: full + partial exceeds requirements" % (sid, arm))
            if valid and not historical and "none" in counts and sum(counts.get(k, 0) for k in ("full", "partial", "none")) != n:
                errors.append("%s %s: verdict counts do not sum to requirements" % (sid, arm))
    return errors


def session_row(sid, requirements, judgments_by_arm):
    """requirements: list of {id, kind?, ...}; judgments_by_arm: {arm: score json}. Returns one table row, with the
    verdict counts per requirement kind under `by_kind` for each arm."""
    kinds = {r["id"]: (r.get("kind") or "unknown") for r in requirements}
    row = {"sid": sid, "n": len(requirements), "arms": {}, "kinds": {}}
    for k in kinds.values():
        row["kinds"][k] = row["kinds"].get(k, 0) + 1
    for arm, j in judgments_by_arm.items():
        expected = [r["id"] for r in requirements]
        items = j.get("items", [])
        if any(not isinstance(i.get("id"), int) or isinstance(i.get("id"), bool) for i in items) or \
                sorted(i["id"] for i in items) != sorted(expected) or len(set(expected)) != len(expected):
            raise EvalRefused("%s %s: judgment IDs do not match unique requirement IDs" % (sid, arm))
        if any(i.get("verdict") not in ("full", "partial", "none") for i in items):
            raise EvalRefused("%s %s: invalid requirement verdict" % (sid, arm))
        counts = {"full": 0, "partial": 0, "none": 0}
        by_kind = {}
        for it in j.get("items", []):
            counts[it["verdict"]] = counts.get(it["verdict"], 0) + 1
            k = kinds.get(it["id"], "unknown")
            bk = by_kind.setdefault(k, {"full": 0, "partial": 0, "none": 0})
            bk[it["verdict"]] = bk.get(it["verdict"], 0) + 1
        row["arms"][arm] = {"full": counts["full"], "partial": counts["partial"], "none": counts["none"],
                            "contra": len(j.get("contradictions", [])),
                            "contra_texts": [c.get("text", "") for c in j.get("contradictions", [])],
                            "upgrades": len(j.get("upgrades") or []),
                            "wrong_upgrades": len(j.get("wrong_upgrades") or []),
                            "upgrade_texts": [c.get("text", "") for c in j.get("upgrades") or []],
                            "wrong_upgrade_texts": [c.get("text", "") for c in j.get("wrong_upgrades") or []],
                            "by_kind": by_kind}
    return row


def pooled_by_kind(rows, arm, w):
    """{kind: {n, full, partial, hit_rate}} pooled over sessions for one arm."""
    out = {}
    for r in rows:
        a = r["arms"].get(arm)
        if not a:
            continue
        for k, c in (a.get("by_kind") or {}).items():
            o = out.setdefault(k, {"n": 0, "full": 0, "partial": 0})
            o["n"] += c.get("full", 0) + c.get("partial", 0) + c.get("none", 0)
            o["full"] += c.get("full", 0)
            o["partial"] += c.get("partial", 0)
    for k in REPORTED_KINDS:
        out.setdefault(k, {"n": 0, "full": 0, "partial": 0})
    for o in out.values():
        o["hit_rate"] = (o["full"] + w * o["partial"]) / o["n"] if o["n"] else None
    return out


def pooled_rate(rows, arm, w):
    num = sum(r["arms"][arm]["full"] + w * r["arms"][arm]["partial"] for r in rows if arm in r["arms"])
    den = sum(r["n"] for r in rows if arm in r["arms"])
    return num / den if den else 0.0


def session_rate(row, arm, w):
    a = row["arms"][arm]
    return (a["full"] + w * a["partial"]) / row["n"] if row["n"] else 0.0


def per_session_rates(rows, arm, w):
    return {r["sid"]: session_rate(r, arm, w) for r in rows if arm in r["arms"]}


def paired_differences(rows, a, b, w):
    return {r["sid"]: session_rate(r, a, w) - session_rate(r, b, w) for r in rows if a in r["arms"] and b in r["arms"]}


def win_tie_loss(rows, a, b, w, eps=1e-9):
    win = tie = loss = 0
    for d in paired_differences(rows, a, b, w).values():
        if abs(d) <= eps:
            tie += 1
        elif d > 0:
            win += 1
        else:
            loss += 1
    return {"win": win, "tie": tie, "loss": loss}


def contradiction_rate(rows, arm):
    num = sum(r["arms"][arm]["contra"] for r in rows if arm in r["arms"])
    den = sum(r["n"] for r in rows if arm in r["arms"])
    return (num / den if den else 0.0), num


def upgrade_counts(rows, arm):
    """{upgrades, wrong_upgrades, net, sessions_with_upgrade, sessions_with_wrong_upgrade} for one arm. An upgrade is
    a better path the user later moved to; a wrong upgrade overrode something the user wanted (rubric v4; a v3
    judgment counts as none)."""
    up = sum(r["arms"][arm].get("upgrades", 0) for r in rows if arm in r["arms"])
    wrong = sum(r["arms"][arm].get("wrong_upgrades", 0) for r in rows if arm in r["arms"])
    return {"upgrades": up, "wrong_upgrades": wrong, "net": up - wrong,
            "sessions_with_upgrade": sum(1 for r in rows if arm in r["arms"] and r["arms"][arm].get("upgrades", 0)),
            "sessions_with_wrong_upgrade": sum(1 for r in rows if arm in r["arms"] and r["arms"][arm].get("wrong_upgrades", 0))}


def _percentiles(values, lo=2.5, hi=97.5):
    if not values:
        return (0.0, 0.0)
    vs = sorted(values)
    def pct(p):
        k = (len(vs) - 1) * p / 100.0
        f, c = int(k), min(int(k) + 1, len(vs) - 1)
        return vs[f] + (vs[c] - vs[f]) * (k - f)
    return (pct(lo), pct(hi))


def bootstrap(rows, arms, w, resamples, seed):
    """Resample sessions with replacement; 95% intervals for pooled rates and pairwise differences."""
    rng = random.Random(seed)
    n = len(rows)
    arm_samples = {a: [] for a in arms}
    pair_samples = {}
    pairs = [(a, b) for i, a in enumerate(arms) for b in arms[i + 1:]]
    for p in pairs:
        pair_samples[p] = []
    if n == 0:
        return {a: (0.0, 0.0) for a in arms}, {"%s-%s" % p: (0.0, 0.0) for p in pairs}
    for _ in range(resamples):
        sample = [rows[rng.randrange(n)] for _ in range(n)]
        rates = {a: pooled_rate(sample, a, w) for a in arms}
        for a in arms:
            arm_samples[a].append(rates[a])
        for a, b in pairs:
            pair_samples[(a, b)].append(rates[a] - rates[b])
    return ({a: _percentiles(v) for a, v in arm_samples.items()},
            {"%s-%s" % p: _percentiles(v) for p, v in pair_samples.items()})


def build_report(rows, arms, cfg_eval, meta):
    errors = count_errors(rows)
    if errors:
        raise EvalRefused("invalid judge count artifacts:\n" + "\n".join(errors))
    w = cfg_eval["partial_weight"]
    arms = [a for a in arms if any(a in r["arms"] for r in rows)]
    pooled = {a: {"hit_rate": pooled_rate(rows, a, w), "hit_rate_full_only": pooled_rate(rows, a, 0.0),
                  "contradiction_rate": contradiction_rate(rows, a)[0], "contradictions": contradiction_rate(rows, a)[1],
                  "full": sum(r["arms"][a]["full"] for r in rows if a in r["arms"]),
                  "partial": sum(r["arms"][a]["partial"] for r in rows if a in r["arms"]),
                  **upgrade_counts(rows, a)} for a in arms}
    pairs = {}
    for i, a in enumerate(arms):
        for b in arms[i + 1:]:
            pairs["%s-%s" % (a, b)] = {"diff": pooled[a]["hit_rate"] - pooled[b]["hit_rate"],
                                       "win_tie_loss": win_tie_loss(rows, a, b, w)}
    ci_arms, ci_pairs = bootstrap(rows, arms, w, cfg_eval["bootstrap_resamples"], cfg_eval["bootstrap_seed"])
    for a in arms:
        pooled[a]["ci95"] = list(ci_arms[a])
    for k in pairs:
        pairs[k]["ci95"] = list(ci_pairs[k])
    mode = meta.get("mode") or "first"
    kinds_present = sorted({k for r in rows for k in (r.get("kinds") or {})} | set(REPORTED_KINDS))
    report = {"rubric_version": RUBRIC_VERSION, "mode": mode, "arms": arms, "sessions": len(rows),
              "requirements": sum(r["n"] for r in rows), "partial_weight": w, "pooled": pooled, "pairs": pairs,
              "kinds": {k: sum((r.get("kinds") or {}).get(k, 0) for r in rows) for k in kinds_present},
              "by_kind": {a: pooled_by_kind(rows, a, w) for a in arms},
              "per_session": [{"sid": r["sid"], "n": r["n"], **{a: {k: v for k, v in r["arms"][a].items() if not k.endswith("_texts")} for a in r["arms"]}} for r in rows],
              "limitation": LIMITATIONS.get(mode, LIMITATIONS["first"]),
              "measurement": "automated judge diagnostic", "acceptance_eligible": False,
              "judge_calibration": {"status": "uncalibrated", "human_agreement": None},
              "calibration_warning": "Automated judgments have not been calibrated against completed human labels; these scores cannot establish acceptance."}
    report.update(meta)
    return report


def _kind_label(kind):
    return kind.replace("_", " ").replace("follow through", "follow-through")


def report_markdown(report, rows):
    lines = []
    mode = report.get("mode") or "first"
    head = "# mind-reading eval: model %s on split %s%s" % (report.get("model_hash"), report.get("split_id"),
                                                             " (context mode)" if mode == "context" else "")
    if report.get("forced"):
        head += "  TUNED: this test set has been seen by this model before"
    lines += [head, "", "Sessions %d, requirements %d, partial weight %.2f, rubric v%d, %s, %s." % (
        report["sessions"], report["requirements"], report["partial_weight"], report["rubric_version"],
        "dev split (repeatable)" if report.get("dev") else "held-out test split",
        "mid-session seeds with the earlier messages as context" if mode == "context" else "first-message seeds, no context"), "",
        report.get("calibration_warning", "Automated judge diagnostic; acceptance requires completed human preference review."), "",
        "| arm | hit rate | full only | contradictions | 95% CI |", "|---|---|---|---|---|"]
    for a in report["arms"]:
        p = report["pooled"][a]
        lines.append("| %s | %.3f | %.3f | %d (%.3f) | %.3f to %.3f |" % (
            a, p["hit_rate"], p["hit_rate_full_only"], p["contradictions"], p["contradiction_rate"], p["ci95"][0], p["ci95"][1]))
    lines.append("")
    for k, v in report["pairs"].items():
        wtl = v["win_tie_loss"]
        lines.append("- %s: diff %+.3f (CI %+.3f to %+.3f), sessions win/tie/loss %d/%d/%d" % (
            k, v["diff"], v["ci95"][0], v["ci95"][1], wtl["win"], wtl["tie"], wtl["loss"]))
    if report["arms"]:
        lines += ["", "## Upgrades", "",
                  "An upgrade is a different approach than the seed literally asked for that the user later moved to; a wrong "
                  "upgrade overrode something the user wanted. Higher net is better.", "",
                  "| arm | upgrades | wrong upgrades | net | sessions with an upgrade |", "|---|---|---|---|---|"]
        for a in report["arms"]:
            p = report["pooled"][a]
            lines.append("| %s | %d | %d | %+d | %d |" % (a, p.get("upgrades", 0), p.get("wrong_upgrades", 0), p.get("net", 0),
                                                       p.get("sessions_with_upgrade", 0)))
    by_kind = report.get("by_kind") or {}
    if by_kind and report["arms"]:
        kinds = [k for k in REPORTED_KINDS] + sorted(k for k in (report.get("kinds") or {}) if k not in REPORTED_KINDS)
        lines += ["", "## Hit rate by requirement type", "",
                  "| type | n | " + " | ".join(report["arms"]) + " |", "|---|---|" + "---|" * len(report["arms"])]
        for k in kinds:
            n = (report.get("kinds") or {}).get(k, 0)
            cells = []
            for a in report["arms"]:
                o = (by_kind.get(a) or {}).get(k) or {}
                cells.append("%.3f" % o["hit_rate"] if o.get("hit_rate") is not None else "-")
            lines.append("| %s | %d | %s |" % (_kind_label(k), n, " | ".join(cells)))
    if report["arms"]:
        best = max(report["arms"], key=lambda a: report["pooled"][a]["hit_rate"])
        w = report["partial_weight"]
        worst = sorted((r for r in rows if best in r["arms"]), key=lambda r: session_rate(r, best, w))[:5]
        lines += ["", "## Largest losses of the best arm (%s)" % best, ""]
        for r in worst:
            a = r["arms"][best]
            lines.append("- %s: %d/%d requirements hit (full %d, partial %d), contradictions: %s%s" % (
                r["sid"], a["full"] + a["partial"], r["n"], a["full"], a["partial"],
                "; ".join(a["contra_texts"]) or "none",
                ("; wrong upgrades: " + "; ".join(a.get("wrong_upgrade_texts") or [])) if a.get("wrong_upgrade_texts") else ""))
    lines += ["", "Limitation: " + report["limitation"], ""]
    lines.append("Engines: %s" % json.dumps(report.get("engines", {})))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- flow

def _seed_files(seeds_dir, mode="first"):
    return sorted(Path(seeds_dir).glob("c*.json" if mode == "context" else "s*.json"))


def _truth_dir(split_dir, mode="first"):
    """The truth directory for a mode: `truth-<suffix>` for first-message seeds, `truth-context-<suffix>` for
    mid-session seeds (the first glob must not pick up the second)."""
    if mode == "context":
        found = sorted(Path(split_dir).glob("truth-context-*"))
    else:
        found = sorted(p for p in Path(split_dir).glob("truth-*") if not p.name.startswith("truth-context-"))
    return found[0] if found else None


def seeds_dir_name(mode):
    return "seeds-context" if mode == "context" else "seeds"


def results_name(model_hash, mode):
    return model_hash + ("-context" if mode == "context" else "")


def context_block(seed_doc, mode):
    """The earlier-messages block the requirements and expand prompts carry in context mode; empty in first mode."""
    if mode != "context":
        return ""
    ctx = seed_doc.get("context") or []
    return ("The user's earlier messages in this session, oldest first (the agent's replies are not shown):\n\n"
            "<earlier_messages>\n%s\n</earlier_messages>\n" % json.dumps(ctx, ensure_ascii=False, indent=0))


CONTEXT_NOTES = {
    "first": ("You have no project memory and no conversation; do not invent project facts, write them as placeholders "
              "(\"the branch we agreed\")."),
    "context": ("You have the user's earlier messages above and nothing else: no project memory, no files, no agent replies. "
                "Use what the earlier messages establish; do not invent anything they do not, write such facts as placeholders "
                "(\"the branch we agreed\")."),
}
REQUIREMENT_SCOPE = {
    "first": "that were NOT already in the seed",
    "context": "that were NOT already in the seed or in the earlier messages",
}


LESSONS_NOTE = ("\n\nLessons: where a lesson above shows that the approach the prompt asks for is worse for the same goal, "
                "write the full prompt with the better way instead, unless the prompt insists on its approach in so many words. "
                "A lesson never adds an outward-facing, destructive, spending or permission-changing step.")


def arm_context(arm, paths, cfg, seed, name):
    if arm == "none":
        return CONTROL_PERSONA, "that user", ""
    if arm == "light":
        return name, name, "Use these always-on reading rules for %s:\n\n%s" % (name, paths.light.read_text(encoding="utf-8"))
    if arm == "full":
        slice_md, decision = _select.select_slice(paths.model, seed, cfg["select"], no_light=False)
        return name, name, "Use this slice of %s's model:\n\n%s%s" % (name, slice_md, LESSONS_NOTE if decision.get("lessons") else "")
    if arm == "full-all":
        profile = paths.profile.read_text(encoding="utf-8")
        return name, name, "Use %s's full model:\n\n%s\n\n%s\n\n%s%s" % (
            name, paths.light.read_text(encoding="utf-8"), profile, paths.examples.read_text(encoding="utf-8"),
            LESSONS_NOTE if "\n## What works and what doesn't" in profile else "")
    raise EvalRefused("unknown arm %r" % arm)


def ledger_has(paths, model_hash, split_id, mode="first"):
    """One look per (model, split, mode): an entry without a mode is a first-message look."""
    for e in read_json(paths.ledger, []) or []:
        if e.get("model_hash") == model_hash and e.get("split_id") == split_id and (e.get("mode") or "first") == mode:
            return True
    return False


def run_eval(paths, cfg, kind, dev=False, arms=None, judge_kind=None, force=False, run_id=None, collect=False,
             concurrency=4, max_sessions=None, fake_dir=None, log=print, backoff=None, model_expand=None,
             model_judge=None, max_calls=None, max_cost_usd=None, mode="first"):
    name = cfg["name"]
    arms = arms or cfg["eval"]["arms"]
    if mode not in MODES:
        raise EvalRefused("unknown eval mode %r (choose from %s)" % (mode, ", ".join(MODES)))
    split = read_json(paths.split)
    if not split:
        raise EvalRefused("derived/split.json missing: run prep first")
    if split.get("no_test"):
        raise EvalRefused("split %s holds nothing out (prep --no-test, train before %s): a deployment model cannot be scored; "
                          "run prep --cutoff <date> to mint a held-out set" % (split["split_id"], split.get("train_before")))
    split_id = split["split_id"]
    if not paths.index.exists():
        raise EvalRefused("model/index.json missing: run synthesize first")
    # Runtime files are hand-editable (including episodes.json). A manifest can lag those edits;
    # using its old hash would reuse another model's expansions and one-look ledger entry.
    model_hash = _select.files_hash(paths.model)
    split_dir = paths.dev if dev else paths.eval / split_id
    seeds_dir, truth_dir = split_dir / seeds_dir_name(mode), _truth_dir(split_dir, mode)
    if not seeds_dir.exists() or truth_dir is None:
        raise EvalRefused("no %s seeds under %s: run prep%s" % (mode, split_dir, " (context seeds are written by prep since version 2.4)" if mode == "context" else ""))
    forced = False
    if not dev and ledger_has(paths, model_hash, split_id, mode):
        if not force:
            raise EvalRefused("one-look rule: model %s was already scored on split %s in %s mode; use --dev to iterate, or --force "
                              "to record a TUNED look" % (model_hash, split_id, mode))
        forced = True
    results = split_dir / "results" / results_name(model_hash, mode)
    for sub in ("requirements", "expansions", "judgments"):
        (results / sub).mkdir(parents=True, exist_ok=True)
    seeds = _seed_files(seeds_dir, mode)[: (max_sessions or cfg["eval"]["max_sessions"])]
    if not seeds:
        raise EvalRefused("no seeds in %s" % seeds_dir)
    judge_kind = judge_kind or kind
    # Engine runs live beside the results, under eval/<split_id>/runs (mode 0700) or derived/dev/runs: the requirements
    # and score prompts quote the held-out later messages, so they never land under runs/ outside the guarded tree.
    runs_dir = split_dir / "runs"
    base_run = run_id if run_id and run_id != "latest" else None
    if run_id == "latest":
        base_run = (_engine.latest_run_id(paths, "eval-requirements", runs_dir) or "").replace("-req", "") or None
    if base_run is None:
        base_run = _engine.new_run_id(paths, runs_dir)
    resume = "eval --run %s%s%s" % (base_run, " --dev" if dev else "", " --mode context" if mode == "context" else "")
    suffix = {"eval-requirements": "req", "eval-expand": "expand", "eval-score": "score"}
    mk = lambda stage, k, m: _engine.Engine(paths, cfg, k, stage, run_id="%s-%s" % (base_run, suffix[stage]),
                                            model=m, concurrency=concurrency, fake_dir=fake_dir, log=log, backoff=backoff,
                                            max_calls=max_calls, max_cost_usd=max_cost_usd, runs_dir=runs_dir,
                                            resume=resume, private=not dev)
    req_schema, score_schema = _engine.load_schema("requirements"), _engine.load_schema("score")
    base_map = {"name": name, "people_tokens": _engine.people_tokens(cfg), "privacy": privacy_line(cfg),
                "context_note": CONTEXT_NOTES[mode], "requirement_scope": REQUIREMENT_SCOPE[mode]}
    seed_docs = {}
    for sp in seeds:
        sd = read_json(sp)
        td = read_json(truth_dir / sp.name) or {"later": []}
        seed_docs[sd["id"]] = (sd, td)

    # stage 1: requirements, once per session, shared by every arm
    eng_req = mk("eval-requirements", judge_kind, _engine.stage_model(cfg, judge_kind, "judge", model_judge))
    req_tpl = _engine.load_prompt("eval-requirements")
    units = []
    for sid, (sd, td) in seed_docs.items():
        mapping = dict(base_map, time=sd.get("time") or "unknown time", seed=sd["seed"], context_block=context_block(sd, mode),
                       later_json=json.dumps(td["later"], ensure_ascii=False, indent=0))
        units.append(_engine.Unit(sid, _engine.render(req_tpl, mapping), req_schema, results / "requirements" / (sid + ".json")))
    s1 = eng_req.run(units, force=False, collect=collect)
    summary = {"run_id": base_run, "model_hash": model_hash, "split_id": split_id, "dev": dev, "forced": forced, "mode": mode,
               "stages": {"requirements": _short(s1)}}
    if s1["pending"]:
        summary["instructions"] = s1["instructions"]
        return summary
    ready = [sid for sid in seed_docs if (results / "requirements" / (sid + ".json")).exists()]

    # stage 2: expansions per arm
    eng_exp = mk("eval-expand", kind, _engine.stage_model(cfg, kind, "expand", model_expand))
    exp_tpl = _engine.load_prompt("eval-expand")
    units = []
    for sid in ready:
        sd, _ = seed_docs[sid]
        for arm in arms:
            persona, subject, ctx = arm_context(arm, paths, cfg, sd["seed"], name)
            mapping = dict(base_map, persona_line=persona, subject=subject, time=sd.get("time") or "unknown time",
                           seed=sd["seed"], arm_context=ctx, context_block=context_block(sd, mode))
            units.append(_engine.Unit("%s:%s" % (sid, arm), _engine.render(exp_tpl, mapping), EXPAND_SCHEMA,
                                      results / "expansions" / ("%s-%s.json" % (sid, arm))))
    s2 = eng_exp.run(units, force=False, collect=collect)
    summary["stages"]["expand"] = _short(s2)
    if s2["pending"]:
        summary["instructions"] = s2["instructions"]
        return summary

    # stage 3: score each expansion blind, arm order shuffled per session
    eng_score = mk("eval-score", judge_kind, _engine.stage_model(cfg, judge_kind, "judge", model_judge))
    score_tpl = _engine.load_prompt("eval-score")
    units = []
    for sid in ready:
        sd, td = seed_docs[sid]
        reqs = (read_json(results / "requirements" / (sid + ".json")) or {}).get("requirements", [])
        ids = sorted(r["id"] for r in reqs)
        order = list(arms)
        random.Random("%s:%s" % (split_id, sid)).shuffle(order)
        for arm in order:
            ep = results / "expansions" / ("%s-%s.json" % (sid, arm))
            if not ep.exists():
                continue
            expansion = (read_json(ep) or {}).get("prompt", "")
            mapping = dict(base_map, requirements_json=json.dumps(reqs, ensure_ascii=False, indent=0),
                           later_json=json.dumps(td["later"], ensure_ascii=False, indent=0), expansion=expansion,
                           seed=sd["seed"])
            units.append(_engine.Unit("%s:%s" % (sid, arm), _engine.render(score_tpl, mapping), score_schema,
                                      results / "judgments" / ("%s-%s.json" % (sid, arm)), check=_ids_check(ids)))
    s3 = eng_score.run(units, force=False, collect=collect)
    summary["stages"]["score"] = _short(s3)
    if s3["pending"]:
        summary["instructions"] = s3["instructions"]
        return summary

    rows = []
    for sid in ready:
        reqs = (read_json(results / "requirements" / (sid + ".json")) or {}).get("requirements", [])
        js = {}
        for arm in arms:
            jp = results / "judgments" / ("%s-%s.json" % (sid, arm))
            if jp.exists():
                js[arm] = read_json(jp)
        if js:
            rows.append(session_row(sid, reqs, js))
    engines = {"expand": {"engine": kind, "model": eng_exp.model}, "judge": {"engine": judge_kind, "model": eng_score.model}}
    meta = {"model_hash": model_hash, "split_id": split_id, "dev": dev, "forced": forced, "engines": engines, "mode": mode,
            "run_id": base_run, "date": now_iso(),
            "calls": s1["calls"] + s2["calls"] + s3["calls"], "tokens": s1["tokens"] + s2["tokens"] + s3["tokens"],
            "cost_usd": round(s1["cost_usd"] + s2["cost_usd"] + s3["cost_usd"], 4),
            "failed_units": s1["failed"] + s2["failed"] + s3["failed"],
            "stopped_units": s1["stopped"] + s2["stopped"] + s3["stopped"]}
    meta["completion"] = {"requested_sessions": len(seed_docs), "requested_arms": list(arms),
                          "missing_judgments": [{"sid": sid, "arm": arm} for sid in seed_docs for arm in arms
                                                if not (results / "judgments" / ("%s-%s.json" % (sid, arm))).exists()]}
    meta["completion"]["complete"] = not meta["completion"]["missing_judgments"]
    report = build_report(rows, arms, cfg["eval"], meta)
    write_json(results / "report.json", report)
    (results / "report.md").write_text(report_markdown(report, rows), encoding="utf-8")
    if not dev:
        ledger = read_json(paths.ledger, []) or []
        ledger.append({"model_hash": model_hash, "split_id": split_id, "date": now_iso(), "arms": arms, "mode": mode,
                       "report_path": str(results / "report.json"), "forced": forced})
        write_json(paths.ledger, ledger)
    if kind == judge_kind and shutil.which("claude") and shutil.which("codex"):
        other = "codex" if kind == "claude" else "claude"
        log("recommendation: both CLIs are installed; re-run with --judge-engine %s to reduce same-family bias" % other)
    summary.update({"report": report, "report_path": str(results / "report.md"), "run_dir": str(runs_dir)})
    for stage_summary in (s1, s2, s3):
        if stage_summary.get("budget_note"):
            summary["budget_note"] = stage_summary["budget_note"]
            log("eval: " + stage_summary["budget_note"])
    for a in report["arms"]:
        log("eval %s: hit %.3f (full-only %.3f), contradictions %d, upgrades %d, wrong upgrades %d, CI %.3f..%.3f" % (
            a, report["pooled"][a]["hit_rate"], report["pooled"][a]["hit_rate_full_only"],
            report["pooled"][a]["contradictions"], report["pooled"][a]["upgrades"], report["pooled"][a]["wrong_upgrades"],
            report["pooled"][a]["ci95"][0], report["pooled"][a]["ci95"][1]))
    return summary


def _ids_check(expected_ids):
    def check(data):
        got = sorted(it["id"] for it in data.get("items", []))
        if got != list(expected_ids):
            return "items ids %s do not match requirement ids %s exactly" % (got, list(expected_ids))
        return None
    return check


def _short(s):
    return {k: (len(v) if isinstance(v, list) else v) for k, v in s.items() if k in ("ok", "failed", "pending", "skipped", "stopped", "calls", "tokens", "cost_usd")}


def result_dirs(paths, split_id, dev=False):
    """Where eval writes for a split: (results root, engine runs root). Context-mode results sit under
    results/<model_hash>-context."""
    split_dir = paths.dev if dev else paths.eval / split_id
    return split_dir / "results", split_dir / "runs"
