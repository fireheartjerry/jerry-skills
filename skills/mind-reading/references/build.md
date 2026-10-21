# Building your mind-reading model

One CLI takes you from your own transcripts to a scored model: `scripts/mind_reading.py`. Python 3.9+, standard library only, run it with `python3 -I`. Everything it writes lives under `$MIND_READING_HOME` (default `~/.local/share/mind-reading`), never in a repo.

In the commands below, `MR` stands for `python3 -I <path-to-this-skill>/scripts/mind_reading.py`. Define it as a shell function or alias, not a plain variable: zsh does not split a variable into words, so `$MR init` fails there. Add `--home PATH` after the script path to build into another data home.

## 0. For the agent running this on someone's behalf

- Nothing here runs unasked. The user asked for a build, rebuild or refresh by name; that covers reading this file and running `init`, `extract`, `prep`, `status`, `select` and `lint`, which spend no tokens.
- Before every engine stage (`mine`, `synthesize`, `eval`, `refresh --corrections`, `refresh --extend`): run `MR status --estimate`, show the token estimate and the call count, and ask. One stage per yes.
- Before any write outside the data home (`refresh --always-on --write-to <file>`, a `settings.json` edit): show the exact block or diff and the target path, and ask. `MR refresh --always-on` with no `--write-to` prints the block for review.
- Never open `$MIND_READING_HOME/eval/` or quote anything from it. Report the eval through `report.md` only.
- When the user is not present, stop at the first question and leave the resume command in the reply.

## 1. What you get and what it costs

Three model files (`light.md`, `profile.md`, `examples.md`) plus `grants.md` (your verbatim standing authorizations, built without an engine call) and an index, built by a long-context model from chunks of your typed messages, and a score on sessions the model never saw.

Token figures are estimates derived from what each prompt carries (characters divided by 4, plus the instructions and the model's output); `MR status --estimate` computes them for your corpus, and `runs/*/calls.jsonl` records what was spent for mine, synthesize and refresh. Eval spend is recorded under `eval/<split_id>/runs/<run_id>/calls.jsonl`, which an agent must not open and `report.md` does not summarize: read it yourself if you need the eval's token total.

| Stage | Calls | Estimated tokens |
|---|---|---|
| mine | 1 per chunk per lens: 6 per chunk by default, 1 with `--single-pass`; about 1 chunk per 300 to 500 typed messages (200k characters) | 50k to 100k per call, so 300k to 600k per chunk with the six lenses |
| synthesize | 3 (profile, light, examples) | 100k to 250k in total |
| eval | 7 per held-out session with three arms (1 requirements, 3 expand, 3 score) | 20k to 60k per session; 20 sessions is roughly 0.4M to 1.2M |

One measurement, on synthetic data with the default `claude` engine (Claude Code 2.1.305, Sonnet): a 172k-character chunk mined in 18 s for 92k tokens as the CLI reports them (input, cache writes and output together), about $0.39 at API prices; a three-call eval of one short session took 22k to 24k tokens per call on `codex` (codex-cli 0.162). Each call's usage breakdown lands in `calls.jsonl`.

Mining lenses. Each lens rereads the whole chunk with one question, so the miner spends its output budget on one kind of evidence at a time: `core` (traits, seed expansions, preference changes, session counts), `defaults` (unsaid defaults), `reactions` (praise, frustration, corrections and the preference changes they reveal), `followthrough` (what was expected after the task), `vocabulary` and `grants`. A single combined prompt returned a thin profile on a real corpus (about 16 traits and 7 unsaid defaults per chunk); the lenses trade about six times the mining tokens for a fuller one. `synthesize consolidate` merges the lens outputs inside each chunk (the same trait from two lenses is one item, its evidence unioned and its count not double-counted) and counts distinct chunks, not files. `MR mine --single-pass` keeps the old one-call prompt for comparison or a tight budget; `MR mine --lens core --lens grants` (repeatable) runs a subset, and `mine.lenses` in `config.json` sets the default list. `status --estimate` multiplies the mine figures by the configured lens count, and `status` reports progress as chunks with every lens mined.

Wall time at concurrency 4: mining under a minute per call (six calls per chunk with the lenses), synthesis 5 to 20 minutes, eval about a minute per session. `--max-calls` and `--max-cost-usd` are hard stops on every engine stage; a stop is printed with the resume command and exits 3.

## 2. Prerequisites

- python3 3.9 or newer.
- Transcripts on this machine: Claude Code (`~/.claude/projects/*/*.jsonl`) and/or Codex (`~/.codex/sessions/**/*.jsonl`).
- One of: the Claude Code CLI logged in (`claude`; the engine uses your login, so a subscription is billed as usual and `ANTHROPIC_API_KEY` is optional), the Codex CLI logged in (`codex`), or neither, using print mode from inside an agent session (section 5).

## 3. Privacy before anything

- The corpus is your own typed messages: the agent's replies, tool output, injected harness text, subagent and headless runs are filtered out. It stays under `$MIND_READING_HOME`.
- Scrub is best effort. It redacts common key formats (AWS, Anthropic, OpenAI, Stripe, GitHub, GitLab, Hugging Face, npm, Slack, Google, JWTs, private key blocks), labelled secrets (`password:`, `GITHUB_TOKEN=`, `aws_secret_access_key =`, `Authorization: Bearer ...`), emails, phone numbers, card numbers (Luhn-checked), URL credentials and signed query parameters, and rewrites home paths to `~/`. It does not understand context; read `corpus/scrub-report.json` and spot-check `corpus/messages.jsonl`. A new scrub version is applied to the stored corpus with `MR extract --rescrub`.
- Add the people you work with to the config so their names become role tokens: `MR init --person "Full Name=<teammate>" --person "Other Name=<stakeholder>"`. Tokens in use: `<teammate>`, `<stakeholder>`, `<repo-owner>`, `<vendor>`, `<friend>`, `<family>`. Do this before `extract`; if you add people later, run `MR extract --rescrub` so the stored corpus picks up the tokens (the first name alone is replaced too).
- Never commit `$MIND_READING_HOME`. The model files describe you; treat them as private. The always-on block goes into your personal `CLAUDE.md` or `AGENTS.md`, not into a repo.
- What an engine run sees. `claude`: the prompt and the inline chunk only; no tools, no session persistence, no `CLAUDE.md`, settings or memory files (`--tools "" --setting-sources ""`). `codex`: the prompt and the chunk, with `CODEX_HOME` pointed at an isolated directory inside the run that holds only a link to your `auth.json`, so your global `AGENTS.md` and `config.toml` never enter the prompt (verified: `--ignore-user-config` alone still loads `AGENTS.md`); the read-only sandbox blocks writes and the network, but the model keeps its file tools and could read files if it chose to. Neither engine writes into your transcript history.
- `eval/` is held out. It is created mode 0700, eval prompts and outputs are written inside it, and the runtime never opens it. Step 4 adds a harness-level guard.
- The whole data home is private: the CLI runs with `umask 077`, every directory is 0700 and every file it writes is 0600. `init`, `extract` and `prep` tighten a tree created by an older version; `status` warns about anything still looser (fix with `chmod -R go-rwx $MIND_READING_HOME`).

## 4. Steps

```
MR init --name <YourFirstName> --engine claude        # or --engine codex; adds --timezone, --person as needed
MR extract                                            # reads both transcript trees, scrubs, appends to the corpus
MR status                                             # counts per source, unknown tags, scrub counts
MR prep --cutoff auto                                 # dedup, twins, bulk-stamp detection, time split, leak removal, chunks
```

Read `derived/prep-report.json`: twin pairs, bulk-stamped sessions and messages (imports whose timestamps are fake), bulk copies removed, within-session duplicates, leak removals, chunk sizes and the characters cut by the chunk caps. `--cutoff auto` holds out the last 20% of reliable sessions (at least 10); to hold out a time window instead, read the session dates in `derived/split.json` and `sessions.jsonl` and pass `--cutoff <ISO>`. Re-running `prep` with another cutoff writes a new `eval/<split_id>/` and leaves the earlier one in place; delete it when it has no `results/`. Then:

```
MR status --estimate                                  # tokens and calls for the stages below
MR mine                                               # one call per chunk per lens (6 by default), resumable; --run latest to resume; --single-pass for one call per chunk
MR synthesize                                         # consolidate, profile, light, examples, grants, index, lint, manifest
```

Read the model files. Fix anything wrong by hand (a wrong fact, a name that slipped through), then `MR synthesize index manifest` so the index and hash follow your edit.

Read `model/grants.md` with particular care. The file exists only when at least one candidate verified; `synthesize` logs each dropped candidate with its reason, and `status` then shows `grants: 0 total`. To add grants by hand when it is absent, create it with the header and entry format in model-format.md section 3a, then run `MR synthesize index manifest`. It holds the standing authorizations the pipeline found in your own words: a typed message with an explicit grant phrase such as "you don't need to ask before pushing", located in the corpus, checked against the whole sentence it sits in (a negated phrase such as "never push without asking" is a prohibition and is left out), with `Covers` limited to what that sentence names and `Source` naming the corpus record. Nothing is inferred. Each entry is what lets the runtime do an otherwise-held class of action without asking you again, so: delete or set `Status: revoked YYYY-MM-DD` on anything you no longer mean (both are final: the pipeline records a tombstone in `MODEL.json` and never re-adds that record or quote), narrow `Covers` and extend `Never covers` where the quote was broader than you intended, and add entries in the same format for things you have decided but never typed (the quote must still carry a grant phrase and name the action). An entry with `Class: autonomy` comes from a quote that grants general autonomy ("you have max permissions", "ur in bypass perms", "you're pre-authorized"); its Covers is fixed text, it lifts the hold only on reversible, own-repo actions and on guesses that would otherwise be held, and it never reaches messages, destructive actions, spend or configuration changes (SKILL.md section 5); a bare mention of bypass permissions, a description of the setting or a request to turn it on is never a grant. A grant never covers permission, sandbox, security, hook, webhook, forwarding, configuration or credential changes, never spending without a stated cap, and never messages, posts, PRs or issues to people it does not name; `lint --model` enforces the format, and the runtime loads only grants that are active and pass every check. An entry that fails a check is fatal for `lint --model`, so `synthesize` and `refresh --extend` stop at their lint step and `status` is BLOCKED until you fix, revoke or delete it; a one-off approval ("you are free to delete regenerable things", "yes u can push") carries no standing grant phrase and will not pass. `MR synthesize grants` only appends newly verified records to this file; your edits, revocations and deletions survive every rebuild (`--force` discards the file and the tombstones and starts over). A later typed message that revokes a grant ("actually never push without asking me again"), mined as a preference change, sets that grant to revoked. Then:

```
MR eval                                               # one clean look at the held-out split; writes report.md
MR status                                             # the ledger line shows the look
```

Install the skill in your agent's skills directory, then put the always-on block where every session loads it (review it first with `MR refresh --always-on`):

```
MR refresh --always-on --write-to ~/.claude/CLAUDE.md
MR refresh --always-on --write-to ~/.codex/AGENTS.md
```

Each target is backed up to `<file>.bak-<date>` first. Finally, guard the held-out set from every Claude Code session by adding to `~/.claude/settings.json`:

```json
{"permissions": {"deny": ["Read(~/.local/share/mind-reading/eval/**)"]}}
```

Use that `~/` form, or `//` plus the absolute path (`Read(//<absolute data home>/eval/**)`). A single leading slash is relative to the settings file and guards nothing; `MR status` reports such a rule as broken. If `$MIND_READING_HOME` is elsewhere, name that path. Permission rules for `Bash` match commands, not paths, so shell reads are covered by the directory mode (`chmod 700 <HOME_MR>/eval`, set by `prep`) and by the skill's rule never to open `eval/`. `MR status` reports both guards.

Useful flags: `--engine codex` on any stage, `--model <name>` to override the configured model for one stage, `--concurrency N` (default 4), `--json` for machine-readable summaries, `--home PATH` to use another data home. Less common: `init --model-for <stage>=<model>` (repeatable; stages mine, synthesize, expand, judge), `init --claude-dir` and `init --codex-dir` to save other transcript roots in the config; `eval --arm full-all` (the full arm with the plan-mode slice), `eval --max-sessions N`, `eval --judge-engine` and `eval --judge-model` to score with a different engine or model than the one that expands. After any hand edit to `profile.md`, `examples.md`, `light.md` or `grants.md`, run `MR synthesize index manifest`: until then `select` detects the stale index, rebuilds it in memory on every call and says so in the slice header, and `status` warns.

## 5. Print mode from inside an agent

If you are running inside Claude Code or Codex and do not want a second CLI login, add `--engine print` to `mine`, `synthesize`, `eval` or `refresh --corrections`. The stage writes every prompt to `<runs>/<run_id>/prompts/<stage>-<unit>.md` with its schema beside it and prints an instruction block: run each prompt in its own subagent with Read and Write as its only tools; the subagent reads its prompt file and writes one JSON object to `<runs>/<run_id>/out/<stage>-<unit>.json`. `<runs>` is `runs/` for mine, synthesize and refresh, and `eval/<split_id>/runs/` (or `derived/dev/runs/` with `--dev`) for eval, because eval prompts quote held-out truth. Then run the printed command, for example:

```
MR mine --run <run_id> --engine print --collect
MR eval --run <run_id> --engine print --collect
```

`--collect` validates whatever landed, writes the stage outputs, and lists units still missing. Eval has three stages (requirements, expand, score); repeat the print and collect cycle with the same `--run` until it reports a result.

## 6. Reading the eval

The report (`eval/<split_id>/results/<model_hash>/report.md`) scores three arms on the same held-out sessions:

- `none`: a control with the same instructions and word budget and no model text.
- `light`: the control plus `light.md`.
- `full`: the control plus the exact slice the runtime would load for that seed.

For each session a judge first lists the requirements the user expressed later (without seeing any expansion), then scores each expansion blind against that list: `full`, `partial` or `none` per requirement, plus contradictions. Hit rate is `(full + 0.5 partial) / requirements` pooled over sessions; full-only is the same with partial weighted 0. Contradictions count expansion claims the user's later messages contradict: lower is better, and this is where over-generated restraint shows up. The 95% interval comes from resampling sessions; when it includes zero for an arm difference, the sessions do not yet separate the arms.

The one-look rule: each model version gets one clean look at the test split. The ledger records `(model_hash, split_id)`, and `eval` refuses a second look. If you revise the model because of what you saw in the report, that test set is spent for the revised model: iterate with `MR eval --dev` (the dev split, repeatable, no ledger), and mint a fresh test set with `MR prep --cutoff <later date>` once enough new sessions exist. `--force` records a look anyway, marked TUNED in the ledger and the report.

Limitation stated in every report: no arm sees project memory or the conversation, so the eval measures the model on context-free seeds. The runtime has both.

## 7. Keeping it current

- Monthly: `MR refresh --extend` runs extract (from the transcript directories saved in `config.json`; it prints them), re-preps with the held-out set frozen, mines only chunks whose content changed, and updates the profile with the previous one as prior. Frozen means exactly that: the test and dev session ids, the cutoff and the `split_id` do not change. Sessions that started after the cutoff since the split was minted join train and are listed under `post_cutoff_in_train` in `prep-report.json`; the eval then measures the model relative to the original cutoff, not forward in time from the newest data. When enough new sessions exist (`min_test_sessions`, default 10), mint a new held-out set with `MR refresh --extend --new-cutoff <date>`; the old test sessions become training data and the next `eval` is a clean look for the rebuilt model.
- Corrections accumulate in `model/corrections.md` as you use the skill (the runtime appends them with `MR correct`). `MR refresh --corrections` folds them in with minimal edits; `kind: drop` lines remove a model sentence, `scope: project:<name>` lines are set aside in `MODEL.json` for that project's memory.
- `MR status` reports drift between `light.md` and every always-on target you wrote with `--write-to`; `MR refresh --always-on --write-to <file>` re-syncs. It also warns when the block sits in a Codex `AGENTS.md` and codex home isolation is off, because the `none` control arm and mining would then see it. `status` exits 2 whenever it prints a `BLOCKED:` list, so a script or CI step can gate on it.
- `grants.md` is yours. `refresh --corrections` and `refresh --always-on` never touch it; `refresh --extend` runs `synthesize grants`, which appends newly verified records, applies typed revocations it mined, and leaves your edits alone. Revoke by hand with `Status: revoked YYYY-MM-DD` or delete the entry: either way it stays gone (tombstones in `MODEL.json`).
- Decisions and corrections written from a sandbox that cannot reach the data home (Codex workspace-write) wait in a spool and are applied by the next `select` or `status` that can write; `status` shows the pending count. Point `MIND_READING_SPOOL` at a writable directory if the default temp directory is blocked too.

## 8. Troubleshooting

- `status` lists unknown tags: a harness started injecting a new kind of text. If it is not something you typed, add its prefix to `SKIP_PREFIXES` in `scripts/mr/filters.py` (with a fixture) and re-run `extract`; records are append-only, so run `prep` again to rebuild chunks.
- A chunk fails validation twice: open `runs/<run_id>/calls.jsonl` for the validator message, then `MR mine --run <run_id>` to retry only the failed units.
- A stage exits 3 with `budget stop`: `--max-calls` or `--max-cost-usd` was reached; the message carries the resume command.
- `claude` fails with "not a valid JSON Schema": the engine strips `$schema`, `$id` and `title` before passing a schema; if you edited a schema file, keep the body standard JSON Schema. "Not logged in": run `claude` once interactively, or export `ANTHROPIC_API_KEY`; do not add `--bare` to `engine_cmd`, it disables the OAuth login.
- The engine's flags changed: edit `engine_cmd` in `config.json`. Placeholders: `{model}`, `{schema}` (path), `{schema_json}` (inline), `{out}`, `{tmpdir}`. `codex_isolate_home: false` disables the isolated `CODEX_HOME` (not recommended while the always-on block is in `~/.codex/AGENTS.md`).
- A session was wrongly dropped as a twin: check the pair in `prep-report.json`, confirm with `MR prep --no-twins`, then raise `prep.twin_containment` in `config.json`.
- Bulk-stamped sessions and messages: an import re-stamped old messages with one timestamp (three or more messages inside one second). Such messages carry `time_reliable: false`, their times reach the miner as null, a session that starts with one is never test or dev, and copies of text already present in a real session are dropped.
- `eval` refuses with exit 2: the one-look rule (section 6). Use `--dev` or a new cutoff.
- `log-decision` or `correct` says "spooled": the data home is not writable from this sandbox (Codex workspace-write); the line waits in the spool and the next writable run applies it. Exit 5 means the spool was unwritable too: run Codex with `--add-dir ~/.local/share/mind-reading`, or set `MIND_READING_SPOOL` to a writable directory.
- `status` says the model is legacy: the model files predate the index format. `select` still works (it loads every example and the groups whose titles match); rebuild with `MR synthesize` when you next refresh to get the indexed slice and `grants.md`.
- `status` lists an invalid `grants.md` entry: run `MR lint --model` for the line and reason. `select` drops invalid, revoked and malformed blocks before the slice is built, so an invalid grant never reaches the runtime; `status` counts it as invalid, not active.
- `refresh --always-on` exits 4 naming a line of `light.md`: a hand edit added a forbidden rule (bypass, auto-approve, never ask, push without asking). Remove the line; nothing was rendered or written.
- `correct` exits 4 with "correct refused": the correction read as a permission or a forbidden rule. Record the preference in the fixed wording instead; only `grants.md`, edited by hand, lifts a held category.
- `status` warns that `index.json` is stale: a model file changed after `synthesize index`. `select` still works (it rebuilds the index in memory each call); run `MR synthesize index manifest` to settle it.

## 9. Uninstall

Delete the block between `<!-- BEGIN mind-reading` and `<!-- END mind-reading -->` in each file you wrote it to, remove the `Read(...)` deny rule, remove the skill directory, and delete `$MIND_READING_HOME`.
