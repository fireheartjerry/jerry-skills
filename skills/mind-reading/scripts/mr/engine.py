"""Engine: runs prompt units through claude / codex / print / fake, validates JSON output, retries, resumes.

Every call: prompt text on stdin (or written to disk in print mode), one JSON object out, validated against the
unit's schema. Invalid output: one retry with the validator's error appended. Nonzero exit or timeout: retry with
backoff, then the unit fails and the others continue. runs/<run_id>/calls.jsonl records every attempt."""
import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import schema as _schema
from .config import read_json, skill_dir, write_json

ROLE_TOKENS_DEFAULT = ["<teammate>", "<stakeholder>", "<repo-owner>", "<vendor>", "<friend>", "<family>"]
_PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*)\}")
# Keys that document a schema file but break CLI schema validators (Claude Code rejects an unknown $schema URI).
SCHEMA_META_KEYS = ("$schema", "$id", "title")


def engine_schema(schema):
    """The schema as handed to an engine: the top-level documentation keys removed, everything else untouched."""
    return {k: v for k, v in (schema or {}).items() if k not in SCHEMA_META_KEYS}


class EngineError(Exception):
    pass


class RenderError(KeyError):
    pass


def render(template, mapping):
    """Replace {identifier} placeholders. Unknown identifiers raise RenderError; JSON braces are left alone."""
    def _sub(m):
        key = m.group(1)
        if key not in mapping:
            raise RenderError("prompt placeholder {%s} has no value" % key)
        v = mapping[key]
        return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    return _PLACEHOLDER.sub(_sub, template)


def load_prompt(name, prompts_dir=None):
    d = Path(prompts_dir) if prompts_dir else skill_dir() / "references" / "prompts"
    p = d / (name + ".md")
    with open(p, encoding="utf-8") as fh:
        return fh.read()


def load_schema(name, schemas_dir=None):
    d = Path(schemas_dir) if schemas_dir else skill_dir() / "references" / "schemas"
    return read_json(d / (name + ".schema.json"))


def people_tokens(cfg):
    toks = sorted(set((cfg.get("people") or {}).values()))
    return ", ".join(toks or ROLE_TOKENS_DEFAULT)


def new_run_id(paths, runs_dir=None):
    base = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    root = Path(runs_dir) if runs_dir else paths.runs
    rid, k = base, 1
    while (root / rid).exists():
        rid = "%s-%d" % (base, k)
        k += 1
    return rid


def latest_run_id(paths, stage=None, runs_dir=None):
    base = Path(runs_dir) if runs_dir else paths.runs
    if not base.exists():
        return None
    runs = sorted(p.name for p in base.glob("*") if p.is_dir())
    if stage:
        runs = [r for r in runs if (read_json(base / r / "engine.json", {}) or {}).get("stage") == stage]
    return runs[-1] if runs else None


class Unit:
    """One engine call: id, rendered prompt, output schema, and where the validated JSON lands."""

    def __init__(self, uid, prompt, schema, out_path, check=None):
        self.id = uid
        self.prompt = prompt
        self.schema = schema
        self.out_path = Path(out_path)
        self.check = check  # optional callable(data) -> error string or None


class CallResult:
    def __init__(self, ok, data=None, error=None, tokens=None, cost=None, exit_code=None, pending=False, usage=None):
        self.ok, self.data, self.error, self.tokens, self.cost, self.exit_code, self.pending, self.usage = (
            ok, data, error, tokens, cost, exit_code, pending, usage)


def _cli_version(cmd):
    try:
        out = subprocess.run([cmd, "--version"], capture_output=True, text=True, timeout=30)
        return (out.stdout or out.stderr).strip().splitlines()[0][:80] if (out.stdout or out.stderr) else None
    except (OSError, subprocess.SubprocessError):
        return None


class SubprocessBackend:
    """claude or codex CLI, command template from config (data, so flags can be adapted).

    codex: when cfg.codex_isolate_home is true (default), CODEX_HOME points at <run_dir>/codex-home, which holds only a
    symlink to the real auth.json. Verified on codex-cli 0.162: --ignore-user-config alone still loads the global
    AGENTS.md (the always-on block included) into every call; the isolated home does not."""

    def __init__(self, kind, template, model, run_dir, timeout, cfg=None):
        self.kind, self.template, self.model, self.run_dir, self.timeout = kind, template, model, Path(run_dir), timeout
        self.cfg = cfg or {}
        self.version = _cli_version(template[0]) if template else None
        self.isolate = kind == "codex" and bool(self.cfg.get("codex_isolate_home", True))

    def environment(self):
        """Process environment for one call: the isolated CODEX_HOME is (re)created on every call so a cleanup between
        runs of the same Engine does not break the next run."""
        if not self.isolate:
            return None
        env = dict(os.environ)
        env["CODEX_HOME"] = str(isolated_codex_home(self.run_dir, self.cfg.get("codex_home", "~/.codex")))
        return env

    def command(self, unit, work):
        """The argv for one call. Schema documentation keys are stripped before rendering."""
        schema = engine_schema(unit.schema)
        schema_path = work / "schema.json"
        write_json(schema_path, schema)
        out_path = work / "out.json"
        tmpdir = work / "cwd"
        tmpdir.mkdir(parents=True, exist_ok=True)
        mapping = {"model": self.model or "", "schema": str(schema_path), "schema_json": json.dumps(schema),
                   "out": str(out_path), "tmpdir": str(tmpdir)}
        cmd = []
        skip_next = False
        for i, tok in enumerate(self.template):
            if skip_next:
                skip_next = False
                continue
            if not self.model and tok == "{model}":
                continue  # empty model: omit the value (its flag was omitted below)
            if not self.model and i + 1 < len(self.template) and self.template[i + 1] == "{model}":
                skip_next = True
                continue  # empty model: omit the flag and its value
            cmd.append(render(tok, mapping))
        return cmd, out_path, tmpdir

    def call(self, unit, prompt, attempt):
        work = self.run_dir / "work" / ("%s-%d" % (_safe(unit.id), attempt))
        work.mkdir(parents=True, exist_ok=True)
        cmd, out_path, tmpdir = self.command(unit, work)
        t0 = time.time()
        try:
            proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=self.timeout, cwd=str(tmpdir),
                                  env=self.environment())
        except subprocess.TimeoutExpired:
            return CallResult(False, error="timeout after %ds" % self.timeout, exit_code=-1)
        except OSError as exc:
            return CallResult(False, error="cannot run %s: %s" % (cmd[0], exc), exit_code=-2)
        if proc.returncode != 0:
            return CallResult(False, error=(proc.stderr or proc.stdout or "")[-2000:], exit_code=proc.returncode)
        data, tokens, cost, usage = None, None, None, None
        if self.kind == "claude":
            try:
                result = json.loads(proc.stdout)
            except json.JSONDecodeError:
                return CallResult(False, error="claude output is not JSON: %s" % proc.stdout[-500:], exit_code=0)
            usage = result.get("usage") or {}
            # the CLI reports cache writes beside plain input; the total below counts every billed token kind once
            tokens = sum(int(usage.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                                                              "cache_creation_input_tokens")) or None
            usage = {k: int(usage.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                                                          "cache_creation_input_tokens") if usage.get(k) is not None}
            cost = result.get("total_cost_usd")
            data = result.get("structured_output")
            if data is None:
                data = _parse_json_text(result.get("result"))
        else:
            if out_path.exists():
                data = _parse_json_text(out_path.read_text(encoding="utf-8", errors="replace"))
            else:
                data = _parse_json_text(proc.stdout)
            m = re.search(r"tokens used[:\s]+([\d,]+)", proc.stderr or "", re.I)
            if m:
                tokens = int(m.group(1).replace(",", ""))
        if data is None:
            return CallResult(False, error="no JSON object in output (%.0fs)" % (time.time() - t0), exit_code=0)
        return CallResult(True, data=data, tokens=tokens, cost=cost, exit_code=0, usage=usage)


def isolated_codex_home(run_dir, real_home="~/.codex"):
    """<run_dir>/codex-home with auth.json linked from the real CODEX_HOME. Nothing else is copied, so no AGENTS.md,
    config.toml, skills or plugins reach the engine. The link lets an in-place token refresh land in the real file."""
    home = Path(run_dir) / "codex-home"
    home.mkdir(parents=True, exist_ok=True)
    real = Path(os.path.expanduser(os.environ.get("CODEX_HOME") or real_home))
    link = home / "auth.json"
    if not link.exists() and not link.is_symlink() and (real / "auth.json").exists():
        try:
            os.symlink(real / "auth.json", link)
        except OSError:
            shutil.copy2(real / "auth.json", link)
    return home


def remove_isolated_codex_home(run_dir):
    home = Path(run_dir) / "codex-home"
    if home.exists():
        shutil.rmtree(home, ignore_errors=True)


def _parse_json_text(text):
    if not isinstance(text, str):
        return text if isinstance(text, dict) else None
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None


class PrintBackend:
    """Writes each prompt for a host agent to run; results are collected later."""

    def __init__(self, run_dir, stage):
        self.run_dir, self.stage = Path(run_dir), stage
        self.version = "print"

    def call(self, unit, prompt, attempt):
        pdir = self.run_dir / "prompts"
        pdir.mkdir(parents=True, exist_ok=True)
        base = "%s-%s" % (self.stage, _safe(unit.id))
        (pdir / (base + ".md")).write_text(prompt, encoding="utf-8")
        write_json(pdir / (base + ".schema.json"), unit.schema)
        return CallResult(False, pending=True)

    def out_file(self, unit):
        return self.run_dir / "out" / ("%s-%s.json" % (self.stage, _safe(unit.id)))


class FakeBackend:
    """Canned responses from <fake_dir>/<stage>.json keyed by unit id with a "default". Tests only.

    Entry forms: a JSON object (the response); {"$from_files": {"key": "relative/path.md"}} to read text into keys;
    {"$json_file": "relative/path.json"} to load a whole response. Add "$project": true to keep only the keys the
    unit's schema declares (one canned mined chunk then serves every mine lens). Lookup order: the unit id, then
    "default:<lens>" for a unit id of the form "<chunk>:<lens>", then "default". "fail_first": true or [unit ids]
    makes the first attempt fail with a nonzero exit so retry and resume paths are exercised."""

    def __init__(self, fake_dir, stage, hook=None):
        self.fake_dir, self.stage, self.hook = Path(fake_dir), stage, hook
        self.version = "fake"
        self.table = read_json(self.fake_dir / (stage + ".json"), {}) or {}
        self.attempts = {}
        self.lock = threading.Lock()

    def _resolve(self, entry, schema=None):
        if isinstance(entry, dict) and entry.get("$project"):
            data = self._resolve({k: v for k, v in entry.items() if k != "$project"})
            props = (schema or {}).get("properties")
            if isinstance(data, dict) and isinstance(props, dict):
                data = {k: v for k, v in data.items() if k in props}
            return data
        if isinstance(entry, dict) and "$json_file" in entry:
            return read_json(self.fake_dir / entry["$json_file"])
        if isinstance(entry, dict) and "$from_files" in entry:
            out = {k: v for k, v in entry.items() if k != "$from_files"}
            for key, rel in entry["$from_files"].items():
                out[key] = (self.fake_dir / rel).read_text(encoding="utf-8")
            return out
        return entry

    def call(self, unit, prompt, attempt):
        with self.lock:
            n = self.attempts.get(unit.id, 0) + 1
            self.attempts[unit.id] = n
        if self.hook:
            self.hook(unit, prompt, attempt)
        ff = self.table.get("fail_first")
        if n == 1 and (ff is True or (isinstance(ff, list) and unit.id in ff)):
            return CallResult(False, error="fake: scripted first-attempt failure", exit_code=1)
        entry = self.table.get(unit.id)
        if entry is None and ":" in str(unit.id):
            entry = self.table.get("default:" + str(unit.id).rsplit(":", 1)[1])
        if entry is None:
            entry = self.table.get("default")
        if entry is None:
            return CallResult(False, error="fake: no canned response for %s" % unit.id, exit_code=1)
        return CallResult(True, data=self._resolve(entry, getattr(unit, "schema", None)), tokens=1000, cost=0.0, exit_code=0)


def _safe(s):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(s))


class Engine:
    """runs_dir: where runs/<run_id>/ lives (default paths.runs; eval passes a directory under eval/ so prompts that
    quote held-out truth never leave the guarded tree). resume: the CLI words that resume this run, for example
    `eval --run 20260401-120000`; print mode appends `--engine print --collect`."""

    def __init__(self, paths, cfg, kind, stage, run_id=None, model=None, concurrency=4, max_calls=None,
                 max_cost_usd=None, backoff=None, timeout=None, fake_dir=None, log=print, backend=None, hook=None,
                 runs_dir=None, resume=None, private=False, cost_estimate=None):
        self.paths, self.cfg, self.kind, self.stage, self.log = paths, cfg, kind, stage, log
        self.runs_dir = Path(runs_dir) if runs_dir else paths.runs
        self.run_id = run_id or new_run_id(paths, self.runs_dir)
        self.run_dir = self.runs_dir / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        if private:
            for d in (self.runs_dir, self.run_dir):
                try:
                    os.chmod(d, 0o700)
                except OSError:
                    pass
        self.resume = resume or "%s --run %s" % (stage.split("-")[0], self.run_id)
        self.model = model
        self.concurrency = max(1, int(concurrency or 1))
        self.max_calls, self.max_cost = max_calls, max_cost_usd
        self.timeout = timeout or cfg.get("engine_timeout_seconds", 900)
        self.backoff = list(backoff) if backoff is not None else ([0, 0] if kind in ("fake", "print") else [10, 60])
        self.lock = threading.Lock()
        self.calls = 0
        self._inflight = 0
        self.cost = 0.0
        self.tokens = 0
        # cost_estimate(unit) -> USD: with --max-cost-usd, a call starts only when the spend so far plus the estimates
        # of the calls in flight plus its own estimate stay under the cap, so a capped run does not overshoot by a
        # whole round of concurrent calls. Without it the cap is checked against the spend so far only.
        self.cost_estimate = cost_estimate
        self.reserved = 0.0
        self._paid_chars = 0  # prompt characters of calls that reported a cost: the run learns its own price
        self._paid_cost = 0.0
        if backend is not None:
            self.backend = backend
        elif kind == "fake":
            self.backend = FakeBackend(fake_dir or (skill_dir() / "tests" / "fixtures" / "fake-engine"), stage, hook)
        elif kind == "print":
            self.backend = PrintBackend(self.run_dir, stage)
        elif kind in ("claude", "codex"):
            template = cfg["engine_cmd"][kind]
            self.backend = SubprocessBackend(kind, template, model, self.run_dir, self.timeout, cfg=cfg)
        else:
            raise EngineError("unknown engine %r" % kind)
        info = read_json(self.run_dir / "engine.json", {}) or {}
        info.update({"run_id": self.run_id, "stage": stage, "engine": kind, "model": model,
                     "cli_version": getattr(self.backend, "version", None), "started": info.get("started") or _now()})
        write_json(self.run_dir / "engine.json", info)

    def _record(self, row):
        with self.lock:
            with open(self.run_dir / "calls.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _budget_ok(self, reserve=0.0):
        """True when one more call fits; reserves `reserve` USD for it (released by _release)."""
        with self.lock:
            if self.max_calls is not None and self.calls + self._inflight >= self.max_calls:
                return False
            if self.max_cost is not None:
                if self.cost >= self.max_cost:
                    return False
                if reserve and self.cost + self.reserved + reserve > self.max_cost:
                    return False
            self.reserved += reserve
            self._inflight += 1
            return True

    def _release(self, reserve):
        with self.lock:
            self.reserved = max(0.0, self.reserved - reserve)
            self._inflight = max(0, self._inflight - 1)

    def _estimate(self, unit, prompt):
        if self.cost_estimate is None or self.max_cost is None:
            return 0.0
        try:
            est = max(0.0, float(self.cost_estimate(unit, prompt)))
        except Exception:  # an estimator bug must not stop a run; the cap still applies to the spend so far
            est = 0.0
        with self.lock:
            if self._paid_chars:
                est = max(est, 1.2 * self._paid_cost / self._paid_chars * len(prompt or ""))
        return est

    def _call_cost(self, res):
        """The engine's reported cost; for codex, which reports tokens only, tokens times codex_usd_per_million_tokens
        when that rate is set (0 by default: the cap then binds through --max-calls only)."""
        if res.cost is not None:
            return res.cost
        rate = float(self.cfg.get("codex_usd_per_million_tokens") or 0.0) if self.kind == "codex" else 0.0
        if rate and res.tokens:
            return round(res.tokens * rate / 1e6, 6)
        return None

    def _validate(self, unit, data):
        errs = _schema.validate(data, unit.schema)
        if errs:
            return "\n".join(errs[:12])
        if unit.check:
            return unit.check(data)
        return None

    def existing_valid(self, unit):
        if not unit.out_path.exists():
            return False
        data = read_json(unit.out_path)
        if not isinstance(data, dict):
            return False
        body = {k: v for k, v in data.items() if k != "_meta"}
        return self._validate(unit, body) is None

    def _write(self, unit, data, attempts, tokens, cost, duration):
        out = dict(data)
        out["_meta"] = {"unit": unit.id, "stage": self.stage, "run_id": self.run_id, "engine": self.kind,
                        "model": self.model, "tokens": tokens, "cost_usd": cost, "duration_s": round(duration, 1),
                        "attempts": attempts, "time": _now()}
        write_json(unit.out_path, out)

    def _run_unit(self, unit):
        prompt = unit.prompt
        attempts, failures, validation_retried = 0, 0, False
        t_start = time.time()
        while True:
            reserve = self._estimate(unit, prompt)
            if not self._budget_ok(reserve):
                return ("stopped", unit.id, "budget reached before call")
            attempts += 1
            t0 = time.time()
            try:
                res = self.backend.call(unit, prompt, attempts)
            finally:
                self._release(reserve)
            dur = time.time() - t0
            cost = self._call_cost(res)
            with self.lock:
                self.calls += 1
                self.tokens += res.tokens or 0
                self.cost += cost or 0.0
                if cost:
                    self._paid_cost += cost
                    self._paid_chars += len(prompt or "")
            if res.pending:
                self._record({"unit": unit.id, "attempt": attempts, "status": "pending", "time": _now()})
                return ("pending", unit.id, None)
            err = None if not res.ok else self._validate(unit, res.data)
            status = "ok" if res.ok and err is None else ("invalid" if res.ok else "error")
            self._record({"unit": unit.id, "attempt": attempts, "status": status, "exit_code": res.exit_code,
                          "duration_s": round(dur, 1), "tokens": res.tokens, "cost_usd": cost, "usage": res.usage,
                          "prompt_chars": len(prompt), "reserved_usd": round(reserve, 4) if reserve else None,
                          "error": (err or res.error or "")[:500], "time": _now()})
            if status == "ok":
                self._write(unit, res.data, attempts, res.tokens, cost, time.time() - t_start)
                return ("ok", unit.id, None)
            if status == "invalid":
                if validation_retried:
                    return ("failed", unit.id, "validation failed twice: " + err)
                validation_retried = True
                prompt = unit.prompt + "\n\nYour previous output failed validation:\n" + err + \
                    "\nReturn one corrected JSON object matching the schema."
                continue
            failures += 1
            if failures > len(self.backoff):
                return ("failed", unit.id, res.error)
            wait = self.backoff[failures - 1]
            if wait:
                self.log("%s: %s failed (attempt %d), retrying in %ds" % (self.stage, unit.id, attempts, wait))
                time.sleep(wait)

    def run(self, units, force=False, collect=False):
        """Run (or collect) units. Returns a summary with ok/failed/pending/skipped/stopped lists."""
        summary = {"run_id": self.run_id, "stage": self.stage, "engine": self.kind, "ok": [], "failed": [],
                   "pending": [], "skipped": [], "stopped": [], "errors": {}}
        todo = []
        for u in units:
            if not force and self.existing_valid(u):
                summary["skipped"].append(u.id)
            else:
                todo.append(u)
        if collect:
            for u in todo:
                self._collect_unit(u, summary)
        elif todo:
            with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
                for status, uid, err in pool.map(self._run_unit, todo):
                    summary[status].append(uid)
                    if err:
                        summary["errors"][uid] = err
        summary.update({"calls": self.calls, "tokens": self.tokens, "cost_usd": round(self.cost, 4)})
        if summary["pending"]:
            summary["instructions"] = self.print_instructions(todo)
        if summary["stopped"]:
            summary["budget_note"] = ("budget stop: %d unit(s) not attempted (--max-calls or --max-cost-usd reached); "
                                      "resume with: mind_reading.py %s" % (len(summary["stopped"]), self.resume))
        if isinstance(self.backend, SubprocessBackend) and self.kind == "codex":
            remove_isolated_codex_home(self.run_dir)
        return summary

    def _collect_unit(self, unit, summary):
        src = PrintBackend(self.run_dir, self.stage).out_file(unit)
        if not src.exists():
            if isinstance(self.backend, PrintBackend):
                self.backend.call(unit, unit.prompt, 1)  # write the prompt so the next print/collect cycle can run it
            summary["pending"].append(unit.id)
            return
        data = read_json(src)
        if not isinstance(data, dict):
            summary["failed"].append(unit.id)
            summary["errors"][unit.id] = "not a JSON object: %s" % src
            return
        err = self._validate(unit, data)
        self._record({"unit": unit.id, "attempt": 1, "status": "ok" if not err else "invalid", "collected": True,
                      "error": (err or "")[:500], "time": _now()})
        if err:
            summary["failed"].append(unit.id)
            summary["errors"][unit.id] = err
            return
        self._write(unit, data, 1, None, None, 0.0)
        summary["ok"].append(unit.id)

    def print_instructions(self, units):
        pdir = self.run_dir / "prompts"
        odir = self.run_dir / "out"
        lines = ["Print mode: %d prompt(s) written to %s" % (len(units), pdir),
                 "Run each prompt in its own subagent with Read and Write as its only tools. Each subagent reads one",
                 "<stage>-<unit>.md, follows it, and writes exactly one JSON object matching the schema in the",
                 "matching .schema.json to: %s/<stage>-<unit>.json" % odir,
                 "Then run: mind_reading.py %s --engine print --collect" % self.resume]
        return "\n".join(lines)


def _now():
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def stage_model(cfg, kind, stage_key, override=None):
    if override:
        return override
    return ((cfg.get("models") or {}).get(kind) or {}).get(stage_key) or None


def cleanup_work(run_dir):
    w = Path(run_dir) / "work"
    if w.exists():
        shutil.rmtree(w, ignore_errors=True)


def tmp_dir(prefix="mr-"):
    return Path(tempfile.mkdtemp(prefix=prefix))


def read_calls(run_dir):
    p = Path(run_dir) / "calls.jsonl"
    rows = []
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


__all__ = ["Engine", "Unit", "render", "load_prompt", "load_schema", "engine_schema", "people_tokens", "new_run_id",
           "latest_run_id", "stage_model", "FakeBackend", "PrintBackend", "SubprocessBackend", "EngineError", "RenderError",
           "read_calls", "cleanup_work", "tmp_dir", "isolated_codex_home", "remove_isolated_codex_home", "os"]
