"""Offline fictional onboarding through the real runtime selector."""
import copy
import os
from pathlib import Path

from .config import DEFAULT_HOME, save_config, skill_dir, write_json
from .select import legacy_index, select_slice

DEMO_VERSION = 1
MODEL_NAMES = ("light.md", "profile.md", "examples.md", "corrections.md")
SCENARIOS = (
    ("add the migration", "sqlite migration", "rollback path", 1),
    ("add the setting", "settings keyboard", "command palette", 2),
    ("fix the crash", "fix crash", "deterministic regression", 3),
)


class DemoRefused(ValueError):
    """The requested target could contain a real model or private data."""


def _check_home(paths, force):
    if paths.home.is_symlink():
        raise DemoRefused("use a separate real directory, not a symlink")
    home = paths.home.resolve()
    if home == Path(DEFAULT_HOME).expanduser().resolve():
        raise DemoRefused("use --home with a separate demo directory, not the live data home")
    marker = home / "demo.json"
    if home.exists() and any(home.iterdir()):
        if not marker.is_file():
            raise DemoRefused("target is not an empty directory or an existing fictional demo")
        import json
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise DemoRefused("demo marker is unreadable; choose a new empty directory")
        if data.get("fictional_demo") is not True or data.get("version") != DEMO_VERSION:
            raise DemoRefused("unrecognized demo marker; choose a new empty directory")
        allowed = {"demo.json", "demo-output.md", "config.json"}
        allowed.update("model/" + name for name in MODEL_NAMES)
        for path in home.rglob("*"):
            if path.is_symlink() or (path.is_file() and path.relative_to(home).as_posix() not in allowed):
                raise DemoRefused("target contains files beyond the fictional demo; choose a new empty directory")
        if not force:
            raise DemoRefused("demo already exists; use --force to reset its fictional files")


def run_demo(paths, cfg, force=False, log=print):
    """Seed an isolated demo and print real selected context. Never invokes an engine.

    A compact, supported legacy model is intentional: the demo is not a mined
    production model and must not invent hundreds of lines to resemble one.
    """
    _check_home(paths, force)
    source = skill_dir() / "assets" / "demo"
    contents = {name: (source / name).read_text(encoding="utf-8") for name in MODEL_NAMES}
    paths.ensure_tree()
    demo_cfg = copy.deepcopy(cfg)
    demo_cfg.update({"name": "Fictional engineer", "engine": "print", "people": {}, "always_on_targets": []})
    demo_cfg["sources"] = {
        "claude_projects_dir": str(paths.home / "fictional-sources" / "claude"),
        "codex_sessions_dir": str(paths.home / "fictional-sources" / "codex"),
    }
    save_config(paths, demo_cfg)
    os.chmod(paths.config, 0o600)
    for name, content in contents.items():
        target = paths.model / name
        target.write_text(content, encoding="utf-8")
        os.chmod(target, 0o600)
    write_json(paths.home / "demo.json", {"version": DEMO_VERSION, "fictional_demo": True})
    os.chmod(paths.home / "demo.json", 0o600)

    report = ["# What the seed left unsaid", "",
              "Offline. No transcripts read. No model calls. No real user's data.", "",
              "The context below comes from the real runtime selector. It uses a compact legacy model;",
              "the blockquoted examples inside each slice are authored illustrations, not generated results.",
              "This demonstrates retrieval and the evidence format, not accuracy on a real person.", ""]
    scenarios, slices = [], []
    indexed = legacy_index(paths.model)
    example_lines = contents["examples.md"].splitlines()
    for seed, kind, expected, example_n in SCENARIOS:
        sliced, decision = select_slice(paths.model, seed, demo_cfg["select"], kind=kind)
        if expected not in sliced:
            raise ValueError("demo selector did not retrieve its supporting constraint: " + expected)
        example = next(item for item in indexed["examples"] if item["n"] == example_n)
        start, end = example["lines"]
        authored = "\n".join(line for line in example_lines[start - 1:end] if line.startswith(">"))
        report.extend(["## You type: " + seed, "", "The model supplies: **" + expected + "**.", "",
                       "Illustrative brief (authored, supported by the retrieved slice):", "", authored, "",
                       "Real selector: groups %s; %d characters; no grants." % (
                           ", ".join(decision["groups"]), decision["chars"]), ""])
        slices.extend(["## Raw slice: " + seed, "", sliced.rstrip(), ""])
        scenarios.append({"seed": seed, "omitted_constraint": expected, "decision": decision})
    target = paths.home / "demo-output.md"
    target.write_text("\n".join(report + ["# Selector evidence", ""] + slices), encoding="utf-8")
    os.chmod(target, 0o600)
    log("\n".join(report))
    log("Demo output: %s" % target)
    log("Try your own seed: select --seed-file - --kind '<kind>' against this same --home.")
    return {"fictional_demo": True, "home": str(paths.home), "output": str(target),
            "engine_calls": 0, "transcripts_read": 0, "scenarios": scenarios}
