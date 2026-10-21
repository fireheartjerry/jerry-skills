# high-agency

An instruction-only skill for Claude Code, Codex and any agent that reads
SKILL.md. It carries a task from request to verified result and keeps consent
rules intact: it grants no new permission to send, publish, delete or spend.

Install: symlink or copy this directory into your agent's skills directory
(`~/.claude/skills/high-agency`, `~/.codex/skills/high-agency`), or install the
repository as a Claude Code plugin.

Tests (offline, no tokens): `bash tests/run.sh`
Evals (spend tokens): see `evals/README.md`

Routing: the description loads the skill on its own for check questions
("does X work?", "is it fixed?") and the quoted cue phrases, but in the
phase-2 measurement it never loaded for plain multi-step imperatives or
long-job requests (0/21 and 0/9 on Claude Code). For those, invoke it by name
(`/high-agency` in Claude Code; in Codex, name the skill in the request) or
point to it from your project instructions. Numbers and limits:
`evals/results.md`.
