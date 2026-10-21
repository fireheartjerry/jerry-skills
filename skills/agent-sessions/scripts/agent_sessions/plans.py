"""`plan run` and `plan status`: a small DAG of spawns described in one JSON
file, replayed by content hash.

  {"name": "feat", "dir": "/repo", "defaults": {"tier": "standard"},
   "nodes": [{"name": "research", "engine": "claude", "role": "research", "prompt": "..."},
             {"name": "impl", "engine": "codex", "after": ["research"], "worktree": true,
              "verify": "make test", "prompt": "Spec: {{answer:research}}"}]}

Node options are spawn options by long name (`budget-usd` or `budget_usd`).
Each node becomes the child `<plan>-<node>`; `after` and templates that name
nodes are rewritten to those child names.
"""

import json
import os
import sys
import uuid

from agent_sessions.common import (
    DEFAULT_BUDGET, ENGINES, NAME_RE, die, fmt_cost, kv, now_iso, options_of, out, parse_args, parse_count,
    parse_usd, read_text, sha256_text,
)
from agent_sessions.procs import owner_token
from agent_sessions.state import (
    check_terminal, is_queued, load_group, load_ledger, load_meta, normalize_result, write_group, write_ledger,
)
from agent_sessions.engines import engine_for
from agent_sessions.launch import SpawnError, TEMPLATE_RE, create, finalize_queued, parse_spawn

__all__ = ["cmd_plan", "load_plan", "PLAN_KEYS"]

PLAN_KEYS = ("name", "dir", "defaults", "nodes")
NODE_ONLY = ("name", "engine", "prompt", "prompt_file", "prompt-file", "after")
SPAWN_VALUED, SPAWN_FLAGS = options_of("spawn")["valued"], options_of("spawn")["flags"]
NOT_IN_PLANS = ("n", "engines", "replace", "json", "queue", "prompt-file")


def _option_argv(opts, where):
    argv = []
    for raw_key in sorted(opts):
        key = raw_key.replace("_", "-")
        value = opts[raw_key]
        if key in NOT_IN_PLANS:
            die("%s: option '%s' is not allowed in a plan (plan run manages it)" % (where, raw_key))
        if key in SPAWN_FLAGS:
            if value is True:
                argv.append("--" + key)
            elif value not in (False, None):
                die("%s: option '%s' takes true or false" % (where, raw_key))
        elif key in SPAWN_VALUED:
            if value is None or value is False:
                continue
            if isinstance(value, list):
                value = ",".join(str(v) for v in value)
            argv += ["--" + key, str(value)]
        else:
            die("%s: unknown option '%s' (spawn options by long name)" % (where, raw_key))
    return argv


def load_plan(path):
    """Parse and validate a plan file. Returns (plan, nodes in topological order)."""
    text = read_text(path)
    if text is None:
        die("cannot read plan file %s" % path)
    try:
        plan = json.loads(text)
    except ValueError as exc:
        die("plan file %s is not valid JSON: %s" % (path, exc))
    if not isinstance(plan, dict):
        die("plan file must hold a JSON object")
    for key in plan:
        if key not in PLAN_KEYS:
            die("plan file: unknown key '%s' (expected %s)" % (key, ", ".join(PLAN_KEYS)))
    name = plan.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        die("plan file: name must match [A-Za-z0-9_-]{1,40}")
    base_dir = os.path.dirname(os.path.realpath(path))
    pdir = plan.get("dir") or "."
    if not isinstance(pdir, str):
        die("plan file: dir must be a path")
    pdir = os.path.realpath(os.path.join(base_dir, os.path.expanduser(pdir)))
    if not os.path.isdir(pdir):
        die("plan file: dir %s does not exist" % pdir)
    plan["dir"] = pdir
    defaults = plan.get("defaults") or {}
    if not isinstance(defaults, dict):
        die("plan file: defaults must be an object")
    nodes = plan.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        die("plan file: nodes must be a non-empty list")
    by_name = {}
    for i, node in enumerate(nodes):
        where = "node %d" % (i + 1)
        if not isinstance(node, dict):
            die("plan file: %s must be an object" % where)
        nname = node.get("name")
        if not isinstance(nname, str) or not NAME_RE.match(nname):
            die("plan file: %s needs a name matching [A-Za-z0-9_-]" % where)
        where = "node '%s'" % nname
        if nname in by_name:
            die("plan file: two nodes are named '%s'" % nname)
        if len("%s-%s" % (name, nname)) > 40:
            die("plan file: child name '%s-%s' is longer than 40 characters" % (name, nname))
        engine = node.get("engine") or defaults.get("engine")
        if engine not in ENGINES:
            die("plan file: %s needs engine claude, codex or gemini" % where)
        if not engine_for(engine).binary():
            die("plan file: %s uses %s, which is not installed" % (where, engine), "agent-session engines")
        prompt = node.get("prompt")
        pfile = node.get("prompt_file") or node.get("prompt-file")
        if pfile:
            prompt = read_text(os.path.join(base_dir, pfile))
            if prompt is None:
                die("plan file: %s cannot read prompt_file %s" % (where, pfile))
        if not isinstance(prompt, str) or not prompt.strip():
            die("plan file: %s needs a prompt" % where)
        after = node.get("after") or []
        if isinstance(after, str):
            after = [after]
        node = dict(node, engine=engine, prompt=prompt, after=after)
        by_name[nname] = node
    for nname, node in by_name.items():
        for a in node["after"]:
            if a not in by_name:
                die("plan file: node '%s' runs after '%s', which is not a node" % (nname, a))
        for m in TEMPLATE_RE.finditer(node["prompt"]):
            ref = m.group(2)
            if ref in by_name and ref not in node["after"] and ref != nname:
                die("plan file: node '%s' uses {{%s:%s}} but does not run after '%s'" % (nname, m.group(1), ref, ref))
    order, state = [], {}

    def visit(n, path):
        if state.get(n) == "done":
            return
        if state.get(n) == "active":
            die("plan file: nodes form a cycle: %s" % " -> ".join(path + [n]))
        state[n] = "active"
        for a in by_name[n]["after"]:
            visit(a, path + [n])
        state[n] = "done"
        order.append(n)
    for n in [x.get("name") for x in nodes]:
        visit(n, [])
    plan["defaults"] = defaults
    return plan, [by_name[n] for n in order]


def node_argv(plan, node):
    """(argv for parse_spawn, prompt hash, options hash)."""
    pname = plan["name"]
    child = "%s-%s" % (pname, node["name"])
    node_names = set(n["name"] for n in plan["nodes"] if isinstance(n, dict))

    def rewrite(m):
        ref = m.group(2)
        if ref in node_names:
            return "{{%s:%s-%s}}" % (m.group(1), pname, ref)
        return m.group(0)
    prompt = TEMPLATE_RE.sub(rewrite, node["prompt"])
    opts = dict((k, v) for k, v in plan["defaults"].items() if k not in NODE_ONLY)
    opts.update(dict((k, v) for k, v in node.items() if k not in NODE_ONLY))
    if node["after"]:
        opts["after"] = ["%s-%s" % (pname, a) for a in node["after"]]
    where = "node '%s'" % node["name"]
    argv = [node["engine"], child, plan["dir"], prompt] + _option_argv(opts, where)
    options_hash = sha256_text(json.dumps([node["engine"], plan["dir"], sorted(opts.items(), key=lambda kv_: kv_[0])],
                                          default=str))
    return argv, sha256_text(node["prompt"]), options_hash


TREE_KEYS = ("tree-budget-usd", "tree_budget_usd", "tree-max-tokens", "tree_max_tokens")


def plan_ledger(plan, group):
    """The plan's tree budget, shared by every node: `tree-budget-usd` and
    `tree-max-tokens` in `defaults` describe one ledger for the whole plan
    (kept across re-runs, limits refreshed from the file), never one per node.
    Returns (ledger dict to write or None, ledger id or None)."""
    defaults = plan["defaults"]
    for node in plan["nodes"]:
        for key in TREE_KEYS:
            if key in node:
                die("plan file: node '%s' sets %s; a tree budget covers the whole plan, so set it in defaults"
                    % (node.get("name"), key))
    usd = defaults.pop("tree-budget-usd", defaults.pop("tree_budget_usd", None))
    tokens = defaults.pop("tree-max-tokens", defaults.pop("tree_max_tokens", None))
    if usd is None and tokens is None:
        return None, None
    if os.environ.get("AGENT_SESSIONS_LEDGER"):
        die("plan file sets a tree budget, but this process already runs under tree budget %s; drop it from the "
            "plan (its nodes join %s)" % (os.environ["AGENT_SESSIONS_LEDGER"], os.environ["AGENT_SESSIONS_LEDGER"]))
    usd = parse_usd(str(usd), "plan defaults tree-budget-usd") if usd is not None else None
    tokens = parse_count(str(tokens), "plan defaults tree-max-tokens", 1) if tokens is not None else None
    ledger = load_ledger((group or {}).get("ledger") or "")
    if ledger is None:
        ledger = {"id": uuid.uuid4().hex[:12], "owner": owner_token(), "spent_usd": 0.0, "spent_tokens": 0,
                  "children": {}, "created_iso": now_iso(), "root": plan["name"]}
    ledger["limit_usd"], ledger["limit_tokens"] = usd, tokens
    return ledger, ledger["id"]


def plan_run(path, replace_failed):
    plan, order = load_plan(path)
    pname = plan["name"]
    if load_meta(pname):
        die("'%s' is a child name; give the plan another name" % pname)
    group = load_group(pname)
    if group and group.get("kind") != "plan":
        die("group '%s' exists and is not a plan" % pname)
    ledger, ledger_id = plan_ledger(plan, group)
    old_nodes = (group or {}).get("nodes") or {}
    specs = []
    for node in order:
        argv, phash, ohash = node_argv(plan, node)
        try:
            spec = parse_spawn(argv, stdin_ok=False)
        except SystemExit:
            sys.stderr.write("  (in plan node '%s'; nothing was spawned)\n" % node["name"])
            raise
        spec["queue"] = True
        spec["plan"] = pname
        spec["group"] = pname
        if ledger_id:
            spec["ledger"] = ledger_id
        specs.append((node, spec, phash, ohash))
    # Decide every node before spawning any.
    decisions = {}
    failed, running_changed, foreign = [], [], []
    rerun = set()
    for node, spec, phash, ohash in specs:
        child = spec["name"]
        meta = load_meta(child)
        if meta is not None and meta.get("plan") != pname:
            foreign.append(child)
            continue
        old = old_nodes.get(node["name"]) or {}
        same = old.get("prompt_sha256") == phash and old.get("options_sha256") == ohash
        upstream_rerun = any(a in rerun for a in node["after"])
        if meta is None:
            decisions[child] = "spawn"
            rerun.add(node["name"])
            continue
        res = check_terminal(child, meta)
        if res is None:
            if same and not upstream_rerun:
                decisions[child] = "running"
            elif is_queued(child, meta):
                decisions[child] = "replace"
                rerun.add(node["name"])
            else:
                running_changed.append(child)
            continue
        res = normalize_result(res)
        if res.get("status") == "ok" and same and not upstream_rerun:
            decisions[child] = "skip"
            continue
        if res.get("status") != "ok" and not replace_failed and same and not upstream_rerun:
            failed.append("%s (%s%s)" % (child, res.get("status"),
                                         ", %s" % res["failure_class"] if res.get("failure_class") else ""))
            continue
        decisions[child] = "replace"
        rerun.add(node["name"])
    if foreign:
        die("plan '%s': %s already exist%s and %s not part of this plan; reap or rename %s, or rename the plan"
            % (pname, ", ".join(foreign), "s" if len(foreign) == 1 else "", "is" if len(foreign) == 1 else "are",
               "it" if len(foreign) == 1 else "them"))
    if running_changed:
        die("plan '%s': %s changed (or an upstream re-runs) while still running; wait for it or kill it first"
            % (pname, ", ".join(running_changed)))
    if failed:
        die("plan '%s' has failed nodes: %s; fix the cause and run again with --replace-failed"
            % (pname, ", ".join(failed)))
    # Validate every node that will be (re)spawned against the state root
    # before the first one starts, so a bad node never leaves half a plan.
    members = [spec["name"] for _n, spec, _p, _o in specs]
    for node, spec, _p, _o in specs:
        if decisions[spec["name"]] in ("spawn", "replace"):
            spec["replace"] = decisions[spec["name"]] == "replace"
            try:
                create(spec, dry_run=True, pending=members)
            except SpawnError as exc:
                die("plan '%s', node '%s': %s (nothing was spawned)" % (pname, node["name"], exc.msg), exc.hint,
                    exc.code)
    if ledger:
        write_ledger(ledger)
    # Node hashes are recorded only once a node really was (re)spawned, so a
    # run that stops part-way never marks an unspawned node as up to date.
    nodes_doc = dict((n, v) for n, v in old_nodes.items() if any(x["name"] == n for x, _s, _p, _o in specs))
    group = {"name": pname, "kind": "plan", "plan_file": os.path.realpath(path), "members": members,
             "nodes": nodes_doc, "ledger": ledger_id,
             "owner": owner_token(), "created_iso": (group or {}).get("created_iso") or now_iso(),
             "updated_iso": now_iso(), "dir": plan["dir"]}
    write_group(pname, group)
    rows = []
    for node, spec, phash, ohash in specs:
        child = spec["name"]
        decision = decisions[child]
        entry = {"child": child, "prompt_sha256": phash, "options_sha256": ohash}
        if decision in ("skip", "running"):
            rows.append((node["name"], child, "skipped (%s)" % ("finished ok, unchanged" if decision == "skip"
                                                              else "running, unchanged")))
            nodes_doc[node["name"]] = entry
            continue
        if decision == "replace":
            meta = load_meta(child)
            if meta and meta.get("queue"):
                finalize_queued(meta, "killed", "killed", "replaced by plan run", "killed")
        try:
            meta, info = create(spec)
        except SpawnError as exc:
            write_group(pname, group)
            die("plan '%s', node '%s': %s" % (pname, node["name"], exc.msg), exc.hint, exc.code)
        nodes_doc[node["name"]] = entry
        write_group(pname, group)
        state = info["state"]
        if state == "queued":
            state = "queued (%s)" % info.get("queue_reason")
        rows.append((node["name"], child, ("respawned, " if decision == "replace" else "spawned, ") + state))
    write_group(pname, group)
    return pname, rows


def plan_status(pname, as_json):
    group = load_group(pname)
    if not group or group.get("kind") != "plan":
        die("no plan '%s'" % pname, "agent-session list")
    rows = []
    total = 0.0
    counts = {}
    for node, info in group.get("nodes", {}).items():
        child = info["child"]
        meta = load_meta(child)
        res = check_terminal(child, meta) if meta else None
        if meta is None:
            state = "missing"
        elif res is None:
            state = "queued" if isinstance((load_meta(child) or {}).get("queue"), dict) else "running"
        else:
            state = res.get("status")
        res = normalize_result(res or {})
        cost = res.get("cost_usd")
        if isinstance(cost, (int, float)):
            total += cost
        v = res.get("verify") if isinstance(res.get("verify"), dict) else None
        rows.append({"node": node, "child": child, "state": state, "failure_class": res.get("failure_class"),
                     "verify": v.get("status") if v else None, "cost_usd": cost})
        counts[state] = counts.get(state, 0) + 1
    if as_json:
        out(json.dumps({"plan": pname, "nodes": rows, "cost_usd": round(total, 6), "states": counts}, indent=2))
    else:
        out("%-16s %-28s %-9s %-14s %-8s %s" % ("NODE", "CHILD", "STATE", "FAILURE", "VERIFY", "COST"))
        for r in rows:
            out("%-16s %-28s %-9s %-14s %-8s %s" % (r["node"][:16], r["child"][:28], r["state"],
                                                   r["failure_class"] or "-", r["verify"] or "-",
                                                   fmt_cost(r["cost_usd"])))
        kv("total", "%s; %s" % (fmt_cost(round(total, 6)),
                                ", ".join("%d %s" % (v, k) for k, v in sorted(counts.items()))))
    if any(r["state"] in ("running", "queued") for r in rows):
        return 3
    return 0 if all(r["state"] == "ok" for r in rows) else 1


def cmd_plan(args):
    pos, o = parse_args(args, **options_of("plan"))
    if len(pos) != 2 or pos[0] not in ("run", "status"):
        die("usage: plan run <file.json> [--replace-failed] | plan status <name> [--json]")
    if pos[0] == "status":
        return plan_status(pos[1], o.get("json"))
    pname, rows = plan_run(pos[1], bool(o.get("replace-failed")))
    if o.get("json"):
        out(json.dumps({"plan": pname, "nodes": [{"node": n, "child": c, "action": a} for n, c, a in rows]},
                       indent=2))
        return 0
    kv("plan", pname)
    for node, child, action in rows:
        kv("node", "%-14s %-28s %s" % (node, child, action))
    if all(a.startswith("skipped") for _n, _c, a in rows):
        out("nothing to do: every node finished ok with an unchanged prompt and options")
    kv("next", "agent-session wait %s --timeout %d   (then: agent-session plan status %s)" % (
        pname, DEFAULT_BUDGET, pname))
    return 0
