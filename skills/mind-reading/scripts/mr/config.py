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
    # a trace candidate directory (holding cases.jsonl) that feeds the packet's experience layer; empty = none.
    # MIND_READING_CANDIDATE overrides it.
    "candidate": "",
    "experience": {"enabled": False, "max_chars": 3500, "situation": True},
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
    # None derives a compact target from verified content. A numeric user setting
    # remains explicit; sparse history must never require invented filler.
    "synth": {"budget_chars": 600000, "profile_min_lines": None, "profile_max_lines": 600,
              "light_max_lines": 60, "examples_min": 20, "examples_max": 40, "lessons_max": 40},
    "eval": {"partial_weight": 0.5, "bootstrap_resamples": 10000, "bootstrap_seed": 0, "max_sessions": 40,
             "arms": ["none", "light", "full"]},
    # about 7k tokens per slice: the research's 4k to 7k band, and the point where the first backtest's recall
    # loss (3 groups, 4 examples, 18k chars) is recovered without reading the whole model
    "select": {"budget_chars": 28000, "max_groups": 4, "max_examples": 6, "initiative_budget_chars": 48000,
               "initiative_max_groups": 6, "initiative_max_examples": 10, "max_lessons": 2, "initiative_max_lessons": 3},
    "always_on_targets": [],
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
        self.runs = self.home / "runs"

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
        for d in (self.home, self.corpus_dir, self.derived, self.mined, self.model, self.runs, self.eval):
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
