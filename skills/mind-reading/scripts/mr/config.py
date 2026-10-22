"""config.json: defaults, load, save, and the data-home layout."""
import copy
import json
import os
from pathlib import Path

DEFAULT_HOME = "~/.local/share/mind-reading"

DEFAULTS = {
    "version": 2,
    "name": "User",
    "timezone": "UTC",
    "sources": {
        "claude_projects_dir": "~/.claude/projects",
        "codex_sessions_dir": "~/.codex/sessions",
    },
    "people": {},
    "engine": "claude",
    "models": {
        "claude": {"mine": "sonnet", "synthesize": "opus", "expand": "sonnet", "judge": "sonnet"},
        "codex": {"mine": "", "synthesize": "", "expand": "", "judge": ""},
    },
    # claude: no --bare, so the logged-in OAuth session is used (bare mode reads neither OAuth nor the keychain and
    # would bill an API key). --tools "" --setting-sources "" load no tools, no CLAUDE.md and no memory files.
    # codex: the engine runs it with CODEX_HOME set to an isolated directory holding only a link to auth.json, so the
    # global AGENTS.md and config.toml never enter the prompt (see engine.SubprocessBackend).
    "engine_cmd": {
        "claude": ["claude", "-p", "--model", "{model}", "--output-format", "json", "--json-schema", "{schema_json}",
                   "--no-session-persistence", "--tools", "", "--setting-sources", ""],
        "codex": ["codex", "exec", "--ephemeral", "--skip-git-repo-check", "--ignore-user-config",
                  "-s", "read-only", "-C", "{tmpdir}", "--output-schema", "{schema}", "-o", "{out}", "-"],
    },
    "codex_home": "~/.codex",
    "codex_isolate_home": True,
    "engine_timeout_seconds": 900,
    "codex_usd_per_million_tokens": 0.0,
    "claude_settings_path": "~/.claude/settings.json",
    # agent context: the head of the agent's last reply before each user message plus how that turn ended (a failed
    # command, a tool error, an interruption, a revert). Stored beside the message, never as the user's voice; 0 = off.
    "extract": {"agent_context_chars": 400},
    "prep": {
        "chunk_chars": 200000, "part_chars": 180000, "test_frac": 0.2, "dev_frac": 0.1,
        "min_session_messages": 3, "leak_min_chars": 40, "truth_max_messages": 40, "truth_max_chars": 2500,
        "twin_containment": 0.8, "twin_min_messages": 2, "bulk_stamp_messages": 20, "bulk_stamp_seconds": 5,
        "bulk_cluster_messages": 3, "bulk_cluster_seconds": 1.0,
        "within_session_dup_minutes": 10, "min_test_sessions": 10,
        "chunk_message_max_chars": 4000, "chunk_nontyped_head_chars": 1500,
        # eval --mode context: a mid-session typed message of at most this many words is a seed, with the previous
        # context_messages user messages as its context and the next messages as its truth
        "context_seed_max_words": 60, "context_seeds_per_session": 2, "context_messages": 6, "context_max_chars": 1200,
        # agent context in chunks: each head cut to chunk_agent_context_chars; per session part the heads together stay
        # under chunk_agent_context_ratio x the user's own text (heads beside failures and reactions kept first, then
        # shortened to chunk_agent_context_min_chars, then dropped; markers always kept). Chunk sizes count user text only.
        "chunk_agent_context_chars": 300, "chunk_agent_context_ratio": 0.5, "chunk_agent_context_min_chars": 120,
        # hard cap on one chunk's agent context, JSON overhead included (None: chunk_chars x chunk_agent_context_ratio)
        "chunk_agent_context_max_chars": None,
    },
    # mine: one engine call per chunk per lens; "all" is the single-pass prompt (mine --single-pass).
    # endorsed_evidence: drafted, pasted and answered text reaches the miner as weak [endorsed] evidence of what the
    # user accepts, never as the user's voice; false removes it from the chunks the miner sees.
    # agent_context_lenses: the lenses whose chunk keeps the agent context; every other lens sees the user's text only.
    # Lessons only by default: every lesson is verified against typed text; adding a lens here lets the agent's words
    # reach its prompt (agent-only quotes are still dropped at consolidate).
    "mine": {"lenses": ["core", "defaults", "reactions", "followthrough", "vocabulary", "grants", "lessons"], "endorsed_evidence": True,
             "agent_context_lenses": ["lessons"]},
    "synth": {"budget_chars": 600000, "profile_min_lines": 250, "profile_max_lines": 600,
              "light_max_lines": 60, "examples_min": 20, "examples_max": 40, "lessons_max": 40},
    "eval": {"partial_weight": 0.5, "bootstrap_resamples": 10000, "bootstrap_seed": 0, "max_sessions": 40,
             "arms": ["none", "light", "full"]},
    # about 7k tokens per slice: the research's 4k to 7k band, and the point where the first backtest's recall
    # loss (3 groups, 4 examples, 18k chars) is recovered without reading the whole model
    "select": {"budget_chars": 28000, "max_groups": 4, "max_examples": 6, "initiative_budget_chars": 48000,
               "initiative_max_groups": 6, "initiative_max_examples": 10, "max_lessons": 2, "initiative_max_lessons": 3},
    "always_on_targets": [],
    # track: the correction tracker. Scans the configured transcript roots plus the standard local ones for real
    # mind-reading invocations and classifies the user's next typed message (track.py). The engine classifier is off
    # unless `track --engine` is passed; its spend is capped by engine_max_cost_usd.
    "track": {"claude_dirs": [], "codex_dirs": [], "include_default_dirs": True, "promote_after": 3, "drafts": True,
              "engine_model": {"claude": "haiku", "codex": ""}, "engine_max_cost_usd": 0.25, "engine_max_calls": 10,
              "engine_batch": 20},
    # auto: refresh --auto, the unattended low-effort refresh (auto.py). Spend per run is capped by max_cost_usd and
    # max_calls; mining and synthesis use the models below, not the build's. Delta chunks hold only material no chunk
    # has pinned yet, sized by delta_chunk_chars so one capped run can mine them; mining resumes on the next run when
    # the cap stops it. usd_per_mtok (input, output) and estimate_safety only size the per-call reservation that keeps a
    # run under its cap; the engine's reported cost is what counts.
    "auto": {"max_cost_usd": 3.0, "max_calls": 30, "synth_reserve_usd": 1.0, "min_new_chars": 20000,
             "delta_chunk_chars": 60000, "settle_hours": 2, "advance_train_before": True, "keep_versions": 5,
             "rollback_on_regression": True, "min_invocations": 10, "concurrency": 1,
             "models": {"claude": {"mine": "sonnet", "synthesize": "sonnet"}, "codex": {"mine": "", "synthesize": ""}},
             "usd_per_mtok": {"haiku": [1.0, 5.0], "sonnet": [3.0, 15.0], "opus": [5.0, 25.0], "default": [5.0, 25.0]},
             "estimate_safety": 2.0, "out_tokens": {"mine": 8000, "synthesize": 30000, "refresh": 30000, "track": 2000,
                                                    "prefer": 3000}},
    # prefer: blind A/B pages (prefer.py)
    "prefer": {"max_cost_usd": 2.0, "max_calls": 120, "max_pairs": 30},
    # schedule: the LaunchAgent (macOS) or cron line (Linux) that runs refresh --auto
    "schedule": {"label": "local.mind-reading.refresh", "weekday": 0, "hour": 9, "minute": 0, "daily": False},
}

DIR_MODE = 0o700
FILE_MODE = 0o600


def resolve_home(explicit=None):
    """--home beats $MIND_READING_HOME beats the default."""
    raw = explicit or os.environ.get("MIND_READING_HOME") or DEFAULT_HOME
    return Path(os.path.expanduser(raw)).resolve()


def deep_merge(base, override):
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class Paths:
    """Every path the pipeline reads or writes, derived from the home."""

    def __init__(self, home):
        self.home = Path(home)
        self.config = self.home / "config.json"
        self.corpus_dir = self.home / "corpus"
        self.messages = self.corpus_dir / "messages.jsonl"
        self.manifest = self.corpus_dir / "manifest.json"
        self.scrub_report = self.corpus_dir / "scrub-report.json"
        self.unknown_tags = self.corpus_dir / "unknown-tags.json"
        self.derived = self.home / "derived"
        self.sessions = self.derived / "sessions.jsonl"
        self.prep_report = self.derived / "prep-report.json"
        self.split = self.derived / "split.json"
        self.chunks = self.derived / "chunks"
        self.dev = self.derived / "dev"
        self.eval = self.home / "eval"
        self.ledger = self.eval / "ledger.json"
        self.mined = self.home / "mined"
        self.model = self.home / "model"
        self.model_json = self.model / "MODEL.json"
        self.light = self.model / "light.md"
        self.profile = self.model / "profile.md"
        self.examples = self.model / "examples.md"
        self.index = self.model / "index.json"
        self.corrections = self.model / "corrections.md"
        self.grants = self.model / "grants.md"
        self.decisions = self.model / "decisions.jsonl"
        self.archive = self.model / "archive"
        self.history = self.model / "history.jsonl"
        self.runs = self.home / "runs"
        self.chunk_pins = self.derived / "chunk-pins.json"
        self.track = self.home / "track"
        self.track_records = self.track / "invocations.jsonl"
        self.track_summary = self.track / "summary.json"
        self.track_state = self.track / "state.json"
        self.auto = self.home / "auto"
        self.logs = self.home / "logs"
        self.prefer = self.home / "prefer"

    def ensure_tree(self):
        """Create the tree. Every directory is mode 0700: the corpus and model describe one person and were created
        world-readable before (the research flags 755/644 as a leak), so existing directories are tightened too."""
        for d in (self.home, self.corpus_dir, self.derived, self.chunks, self.dev, self.eval, self.mined,
                  self.model, self.archive, self.runs):
            d.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(d, DIR_MODE)
            except OSError:
                pass

    def private(self):
        """Directories whose mode is not 0700, for `status`."""
        out = []
        for d in (self.home, self.corpus_dir, self.derived, self.mined, self.model, self.runs, self.eval, self.track,
                  self.auto, self.logs, self.prefer):
            try:
                if d.exists() and (os.stat(d).st_mode & 0o777) != DIR_MODE:
                    out.append(str(d))
            except OSError:
                continue
        return out


def spool_path():
    """Where `log-decision` and `correct` park their line when the data home is not writable (Codex workspace-write).
    $MIND_READING_SPOOL beats the per-user temp directory. The next run with a writable home drains it."""
    import tempfile
    env = os.environ.get("MIND_READING_SPOOL")
    if env:
        return Path(os.path.expanduser(env)) / "pending.jsonl"
    try:
        uid = os.getuid()
    except AttributeError:
        uid = 0
    return Path(tempfile.gettempdir()) / ("mind-reading-spool-%d" % uid) / "pending.jsonl"


def load_config(paths, config_path=None):
    """Config on disk merged over defaults. Missing file: defaults (and `exists` False)."""
    p = Path(config_path) if config_path else paths.config
    if p.exists():
        with open(p, encoding="utf-8") as fh:
            data = json.load(fh)
        cfg = deep_merge(DEFAULTS, data)
        cfg["_exists"] = True
    else:
        cfg = copy.deepcopy(DEFAULTS)
        cfg["_exists"] = False
    cfg["_path"] = str(p)
    return cfg


def save_config(paths, cfg, config_path=None):
    p = Path(config_path) if config_path else paths.config
    p.parent.mkdir(parents=True, exist_ok=True)
    clean = {k: v for k, v in cfg.items() if not k.startswith("_")}
    tmp = p.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(clean, fh, indent=1, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, p)
    return p


def expand(path):
    return Path(os.path.expanduser(str(path)))


def skill_dir():
    """The skill directory (parent of scripts/)."""
    return Path(__file__).resolve().parent.parent.parent


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def read_json(path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def read_jsonl(path):
    path = Path(path)
    if not path.exists():
        return []
    out = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def write_jsonl(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, path)
