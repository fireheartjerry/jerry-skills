"""Shared test helpers: path setup, fixture locations, a fast in-process pipeline builder, and a CLI runner."""
import json
import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
import re
from pathlib import Path

TESTS = Path(__file__).resolve().parent
SKILL = TESTS.parent
SCRIPTS = SKILL / "scripts"
FIX = TESTS / "fixtures"
FAKE = FIX / "fake-engine"
CLI = SCRIPTS / "mind_reading.py"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from mr.config import Paths, load_config, save_config  # noqa: E402

PEOPLE = {"Sam Taylor": "<teammate>", "Jordan Lee": "<stakeholder>"}


def tmpdir(prefix="mr-test-"):
    return Path(tempfile.mkdtemp(prefix=prefix))


def quiet(_msg):
    return None


def init_home(home, name="Alex", engine="fake", people=None, prep_overrides=None, cfg_overrides=None):
    paths = Paths(home)
    paths.ensure_tree()
    cfg = load_config(paths)
    cfg["name"] = name
    cfg["engine"] = engine
    cfg["people"] = PEOPLE if people is None else people
    cfg["claude_settings_path"] = str(Path(home) / "no-settings.json")
    if prep_overrides:
        cfg["prep"].update(prep_overrides)
    if cfg_overrides:
        for k, v in cfg_overrides.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    save_config(paths, cfg)
    cfg = load_config(paths)
    return paths, cfg


def build_home(home=None, through="prep", cutoff="auto", **init_kwargs):
    """Run the pipeline in-process on the fixtures up to `through` (extract, prep, mine, synthesize, eval)."""
    from mr.corpus import run_extract
    home = Path(home) if home else tmpdir()
    paths, cfg = init_home(home, **init_kwargs)
    order = ["extract", "prep", "mine", "synthesize", "eval"]
    run_extract(paths, cfg, claude_dir=FIX / "claude", codex_dir=FIX / "codex", log=quiet)
    if order.index(through) >= 1:
        from mr.prep import run_prep
        run_prep(paths, cfg, cutoff=cutoff, log=quiet)
    if order.index(through) >= 2:
        from mr.mine import run_mine
        run_mine(paths, cfg, "fake", fake_dir=FAKE, log=quiet)
    if order.index(through) >= 3:
        from mr.synthesize import run_synthesize
        run_synthesize(paths, cfg, "fake", fake_dir=FAKE, log=quiet)
    if order.index(through) >= 4:
        from mr.evaluate import run_eval
        run_eval(paths, cfg, "fake", fake_dir=FAKE, log=quiet)
    return paths, cfg


def run_cli(home, *args, cwd=None, env_extra=None, stdin=None):
    """Run the CLI in a subprocess with python3 -I. Returns CompletedProcess. The spool directory is kept inside the
    test home so a test never reads or writes the machine's real spool."""
    cmd = [sys.executable, "-I", str(CLI), "--home", str(home)] + [str(a) for a in args]
    env = dict(os.environ)
    env.pop("MIND_READING_HOME", None)
    env.pop("MIND_READING_CANDIDATE", None)
    env["MIND_READING_SPOOL"] = str(Path(home).parent / ("spool-" + Path(home).name))
    env.update(env_extra or {})
    return subprocess.run(cmd, input=stdin, capture_output=True, text=True, cwd=str(cwd or SKILL.parent.parent), env=env, timeout=300)


def read_jsonl(path):
    return [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]


def copy_fixture_sources(dest):
    """Copy the transcript fixtures so a test can mutate them."""
    dest = Path(dest)
    shutil.copytree(FIX / "claude", dest / "claude")
    shutil.copytree(FIX / "codex", dest / "codex")
    return dest / "claude", dest / "codex"


@contextlib.contextmanager
def fixture_upgrade_lesson_ids():
    """Adapt canned deployment examples to the verified lesson ids of a new split.

    A cutoff can admit a formerly held-out lesson and renumber every later group.
    Only test responses are adapted; production validation still checks each id
    against a real bad-outcome group from its actual synthesis prompt.
    """
    from unittest import mock
    from mr.engine import FakeBackend
    original = FakeBackend.call

    def call(backend, unit, prompt, attempt):
        result = original(backend, unit, prompt, attempt)
        if backend.stage != "synthesize" or unit.id != "examples" or not result.ok:
            return result
        current, deploy = None, None
        for line in prompt.splitlines():
            match = re.match(r"^### (W\d+)\. (.+)$", line)
            if match:
                current = match.groups()
            elif current and line.startswith("- ") and "Outcome: bad" in line and current[1].strip().lower() == "deploy":
                deploy = current[0]
        if deploy is None:
            raise AssertionError("fixture needs a verified bad deployment lesson")
        text = result.data["examples_md"]
        text = re.sub(r"\blesson W\d+\b", "lesson " + deploy, text)
        text = re.sub(r"\(W\d+\)\.", "(" + deploy + ").", text)
        text = re.sub(r"(?m)^(Tags: upgrade, )w\d+(, deploy)$", r"\g<1>" + deploy.lower() + r"\2", text)
        result.data = dict(result.data, examples_md=text)
        return result

    with mock.patch.object(FakeBackend, "call", call):
        yield
