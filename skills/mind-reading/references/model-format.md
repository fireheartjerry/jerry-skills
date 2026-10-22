# Model format

The contract between the pipeline (which writes the model) and the runtime (which reads it). Anything the synthesizer emits outside this format fails `synthesize index` and never ships. All paths are relative to `$MIND_READING_HOME/model/` (default `~/.local/share/mind-reading/model/`). `{name}` is the user's first name from `config.json`; the examples below use "Alex".

## 1. Files

| File | Written by | Read by the runtime | Edited by hand |
|---|---|---|---|
| `MODEL.json` | `synthesize`, `refresh` | `status` only | no |
| `light.md` | `synthesize light`, `refresh` | every call, unless the always-on block is already in context | yes, then `refresh --always-on` |
| `profile.md` | `synthesize profile`, `synthesize lessons` (its `What works and what doesn't` section), `refresh` | selected line ranges via `index.json` | yes, then `synthesize index` |
| `examples.md` | `synthesize examples`, `refresh` | selected pairs via `index.json` | yes, then `synthesize index` |
| `grants.md` | `synthesize grants` (deterministic, no engine; merges into an existing file) | every call: the active, valid blocks only, inside the slice | yes, then `synthesize index` |
| `index.json` | `synthesize index` | `select` (the agent never reads it directly) | never |
| `corrections.md` | the runtime (append, through `MR correct`) | every call, whole, inside the slice | yes |
| `decisions.jsonl` | the runtime (append, through `MR log-decision`) | `status` | no |
| `archive/<model_hash>/` | `synthesize`, `refresh` | never | no |

`corrections.md` and `decisions.jsonl` are never archived or overwritten. `refresh --corrections` and `refresh --always-on` never touch `grants.md`; `refresh --extend` runs `synthesize grants`, which only appends and applies typed revocations. The runtime never opens `$MIND_READING_HOME/eval/`. Every directory under the data home is mode 0700 and every file the CLI writes is 0600 (`init`, `extract` and `prep` tighten an older tree; `status` warns about anything looser).

A model built before this format (no `index.json`, headings that differ, groups without `When:` lines, examples without `Tags:`) still loads: `select` falls back to a lenient parse and returns every example plus the groups whose titles match the seed, with a header that says `legacy model`. `status` reports it as legacy and suggests a rebuild; nothing fails.

## 2. profile.md

```
# Alex: the full model

Reading notes
- Strength: [strong] = seen in three or more chunks, or in two with a recent one; [moderate] = two chunks, or one long recent run; [weak] = one chunk.
- [endorsed] marks evidence Alex accepted, pasted or answered rather than typed; a trait resting only on it stays [weak].
- Dates are MM-DD (year in MODEL.json). Quotes verbatim, typos kept. Later preference wins; changes carry (changed MM-DD).
- Project facts (budgets, scores, branch names, owners, deadlines) live in project memory, not here.
- The harness's permission and safety rules beat every line here.

## 1. How Alex thinks
- **One example means the class.** Reports one broken instance and expects every sibling fixed. [strong] "the export is broken again, check the others too" (03-04); "same bug on the invoices page?" (03-19)
## 2. Quality bar
## 3. How Alex builds
## 4. Design taste
## 5. How Alex communicates
## 6. Standing rules
## 7. Likes
## 8. Dislikes
## 9. Workflow habits
## 10. Recurring contexts

## Unsaid defaults
### A. Any request to build, fix or do something
When: build, fix, implement, add, make, do, change
- Finish the whole list, then verify; skip nothing. "fix the login" -> "and the signup, it's the same form" (02-11)
### B. Resuming
When: keep going, continue, resume, try again
- Resume exactly where the last turn stopped; no recap, no re-plan.

## What works and what doesn't
### W1. Deploy
When: deploy, staging, release
- **Deploy to staging.** Did: deployed straight to production. Outcome: bad, the release was rolled back twice. Better: deploy to staging first, smoke-test, then production after a look. [strong] [adopted] "why did u deploy to prod, it broke again" (03-04); "staging first this time" (03-19)

## Vocabulary
- **rq / real quick**: small and now; no planning, no questions.
```

Rules, checked by `synthesize index`:
- Headings exactly as above: `# {name}: the full model`, the `Reading notes` block with the five fixed lines, layers `## 1.` to `## 10.` in that order with those titles, then `## Unsaid defaults`, then the optional `## What works and what doesn't` (section 2a; a model built before lessons has none), then `## Vocabulary`.
- Every `###` group under Unsaid defaults is `### <Letter>. <Title>` with letters in order from A, and the next line is `When:` followed by 3 to 12 lowercase keywords or short phrases, comma separated, that a seed would contain. Group A is the generic build-or-fix group and is the fallback when nothing matches.
- Bullets start with `- `. A layer item is `- **Short label.** One or two sentences. [strength] "quote" (MM-DD); "quote" (MM-DD)`: the strength tag comes before the quotes, one or two quotes, each under 200 characters.
- `[endorsed]` after a quote whose origin was `drafted`, `pasted`, `question_answer` or `goal_reminder`; on the trait line (after the strength tag) when every quote is endorsed, and such a trait is `[weak]` (`synthesize consolidate` caps it before the profile prompt sees it). Typed evidence carries nothing extra. Seeds and vocabulary come only from typed text: consolidate drops a seed expansion whose seed, and a vocabulary phrase that, never occurs in a typed corpus record.
- A changed preference keeps the later one and the line carries `(changed MM-DD)`.
- `scope: project` items from mining never appear here. No budgets, scores, branch names, hostnames, instance types, owners or deadlines.
- Third parties appear only as role tokens: `<teammate>`, `<stakeholder>`, `<repo-owner>`, `<vendor>`, `<friend>`, `<family>`. No other `<` tags.
- No absolute paths, emails, phone numbers or keys. No line over 600 characters. 250 to 600 lines total.
- Nothing from the forbidden-rule list (section 9).

## 2a. What works and what doesn't: lessons

The profile's outcome layer. The rest of the model says what the user prefers; this section says what actually worked for the user and what did not, by the user's own later words, so the runtime can take the better way when a seed asks for one the history shows is worse (SKILL.md section 3a). The pipeline writes it deterministically (`synthesize lessons`, `scripts/mr/lessons.py`); the profile engine never does, and anything it writes under that heading is replaced.

Format, checked by `synthesize index`:
- `### W<n>. <request kind>` groups, numbered from 1, each followed by a `When:` line with 3 to 12 lowercase keywords a seed would contain (the request kinds first), like the Unsaid-defaults groups.
- One bullet per lesson: `- **<label>.** Did: <the approach taken>. Outcome: bad|good, <what happened>. Better: <the better way>.` (`Keep:` instead of `Better:` for a good outcome) then the strength tag, `[adopted]` when the user later used the better way unprompted or `[not adopted]` when they kept the worse one, `(changed MM-DD)` when a newer lesson about the same approach replaced an opposite one, then one or two of the user's own quotes with their dates. No line over 600 characters.
- Evidence in the file is the user's typed words only. Agent context never reaches a model file.

How it is built:
1. Extract. Beside each user message the corpus keeps an `agent_context` field (`{"origin": "agent_context", "head", "markers"}`): the head of the agent's last text reply before that message (`extract.agent_context_chars`, default 400, 0 turns it off), scrubbed like the text, and markers for how the agent's turn ended: `command_failed` (the last command exited nonzero), `tool_error` (the last tool call failed), `interrupted` (the user stopped the turn, rejected a tool call, or the turn was aborted) and `reverted` (the agent ran `git revert`, `git reset --hard`, `git checkout --` or `git restore` in a shell; `git restore --staged` alone only unstages and is not one). Both readers fill it (Claude Code: assistant text and tool_use blocks, tool_result errors, `[Request interrupted`; Codex: assistant output_text, shell commands and the exit code each output's own header reports (JSON metadata, or an `Exit code: N` / `Process exited with code N` line before `Output:`; exit-code text inside the command's output is never read), the `exec` tool's `Script failed`, `turn_aborted`). It is never in `text`, so dedup, twins, leaks, seeds, truth, grants, vocabulary and every typed-text check ignore it; `prep` drops a context that quotes a held-out message (any run of six of its words). A corpus built before the field gets it once, in place, on the next `extract`.
2. Chunk. A chunk message carries `agent_context` in compact form: the head cut to `prep.chunk_agent_context_chars` (300), and per session part the heads together under `prep.chunk_agent_context_ratio` (0.5) times the user's own text (heads beside a marker first, then beside a reaction such as "why", "again", "good"; then shortened to 120 characters, then dropped; markers always kept). Chunk sizes count the user's text only, so the agent context never crowds it out; a chunk's agent context (counted with its JSON overhead) is also capped at `prep.chunk_agent_context_max_chars` (default `chunk_chars` x the ratio), so a context lens's prompt stays under `chunk_chars` plus that cap. Only the lenses in `mine.agent_context_lenses` (the lessons lens by default) see it; the others get the chunk without it. Whatever lens reported it, a trait, default, reaction or preference-change quote that occurs only in stored agent context is dropped at consolidate.
3. Mine. The `lessons` lens reports episodes where an approach the user chose or accepted led to a bad outcome (rework, a repeated failure, a correction such as "why did u", "it broke again", wasted time or money, reverted work, frustration) or a good one (praise, reuse): `request_kind`, `situation`, `pattern`, `outcome`, `outcome_detail`, `better_alternative`, `signals`, `cost`, `scope`, `strength`, `count`, `adopted`, and 1 to 4 evidence items with `role` `user` or `agent_context` (`mined.schema.json`).
4. Consolidate (`synthesize consolidate`). Every user quote must be substantive (20 characters or three content words: not "ok" or "do it") and occur verbatim in a typed corpus message of the session the miner cites; its time and session come from that message, never from the miner. An agent-context quote must occur in a stored agent-context head of a session with a verified user quote and only supports it (at most one per lesson). A lesson whose better way shares more words with that session's agent context than with the user's quoted messages (two or more) is dropped. A lesson without a verified user quote is dropped, and so is one whose "user" quote is really the agent's text: agent context is untrusted (it can quote a page carrying injected instructions) and never becomes a lesson, trait, rule, grant, vocabulary item or example seed by itself. Similar lessons merge across lenses and chunks (same outcome and scope; request kind plus better way, or request kind plus pattern, overlapping at 0.3 on normalized tokens, with stems and common synonyms such as prod/production and deploy/deployment, against every member of a group); strength counts distinct verified sessions: strong at three, moderate at two, weak at one. The failure markers beside the quoted messages (the message itself and its neighbours in the session) are outcome evidence nobody can inject: two or more lift a one-session bad lesson to moderate and keep it, and the miner's `reverted` and `repeated_failure` signals are kept only when markers or the user's own words back them. The miner's `count` adds at most one unquoted episode per chunk (once per set of sessions) and never raises strength; the newest evidence owns the wording and the adoption verdict; a good and a bad lesson about the same approach (similar request kind plus pattern, the same negations, and the bad one's better way not simply the good one's pattern) keep the newer one, marked `(changed MM-DD)`; a single episode is kept only when its cost was high or markers back it; `scope: project` lessons go to the project facts, never the profile. Then up to `synth.lessons_max` (40) lessons, strongest first, are grouped by request kind and written; the weakest are cut first when the profile would pass its line limit.
5. Lint. A lesson can never authorize anything. Consolidate drops, and `lint --model` fails on, a lesson whose instruction text (its better alternative, request kind and situation, and for a good outcome its pattern too) carries a grant phrase, a forbidden rule (section 9) or a qualified action (`AUTHORIZE_PATTERNS` in `scripts/mr/lessons.py`: "just post", "merge it on your own", "add it to the allowlist", "skip the approval"), or whose better way (for a good outcome, also its pattern) names an outward-facing, destructive, spend or permission action whatever the wording (`ACTION_PATTERNS`: "email the report to the client list", "deploy to prod after tests pass", "delete the stale branches", "push straight to main", "merge the PR yourself", "upgrade to the paid plan", "spin up a GPU instance", "git reset --hard") unless its clause negates or proposes it ("never", "instead of", "suggest") or its sentence gates it on the user's yes ("ask before deleting", "production after my ok"). The upgrade pairs of examples.md are checked the same way: their full prompt and the better side of `Changed:`. The `Did:` and `Outcome:` part of a bad lesson describes what went wrong ("deployed without a staging check") and is read as a negative example, not an instruction.

Runtime (SKILL.md section 3a): `select` routes up to 2 lesson groups (3 in plan mode) by their `When:` words like the Unsaid-defaults groups; none when nothing matches. When the seed asks for an approach a matching lesson shows is worse for the same goal and its literal words do not insist on it, the expansion takes the better way, tags that sentence `[S:Wn]`, `[L:Wn]` or `[G:Wn]` by the lesson's strength, and adds one `Changed:` line saying what changed and why. When the literal words insist, the expansion follows them and adds one `Lesson:` line. A lesson never lifts a hold, never stands in for a grant and never overrides a correction or newest words that insist.

## 3. examples.md

```
# Seed to full prompt: examples

These are patterns, not scripts. Rebuild every expansion from the current project's memory and conversation.

## Running work
### 1. keep going
Situation: a standing goal is live; the last turn ended with a report.
Tags: resume
> Keep going on the standing goal from exactly where you stopped: no recap, no re-plan, no questions. Take the next unfinished item in the plan file; if the plan is done, take the largest known gap against the target we agreed. Finish each item with tests and docs, commit it to the working branch, then move to the next. Report in three lines: finished, running, next.
```

Rules, checked by `synthesize index`:
- Title line `# Seed to full prompt: examples`, then the two fixed sentences.
- `## <Theme>` headings group the pairs. Each pair is `### N. <seed verbatim, typos kept>` with N numbered 1..N without gaps across the whole file, then a `Situation:` line (one line), then a `Tags:` line, then the full prompt as one or more `>` blockquote lines.
- `Tags:` has 1 to 4 lowercase tags drawn from the `When:` vocabulary of the profile groups. `index` warns on a tag that matches no group.
- The full prompt is 60 to 300 words, first person, clean and complete: goal and why, scope and what not to touch, quality bar, deliverables, follow-through, how to report, as far as each applies.
- No names, numbers, hostnames or dates that would go stale: role tokens and neutral phrases ("the branch we agreed", "the target we set") instead.
- No restraint the user does not hold ("do not push", "work on a fresh branch", "keep every feature") unless a Standing rule says so.
- Upgrade pairs: when the profile has a lesson with a bad outcome, 2 to 4 pairs under `## Upgrades` show the expansion taking the better way for a seed that asked for the worse one. Their `Tags:` include `upgrade` and the lesson group id (`w1`), and after the blockquote they carry one plain line `Changed: <the literal ask> -> <the better way>, because <the evidence, dated> (W1).` `synthesize examples` rejects a file with too few or too many, or one without its `Changed:` line, and rejects `upgrade` tags when no bad lesson exists.
- 20 to 40 pairs. Nothing from the forbidden-rule list (section 9).

## 3a. grants.md: standing grants

Authorizations the user stated in so many words, kept apart from everything the pipeline inferred. The runtime treats a grant as the one thing that lets an otherwise held category of action proceed (SKILL.md section 5); inferred preferences, strong traits and examples never do.

```
# Alex: standing grants

Each entry is something Alex typed, verbatim, that authorizes a class of action in advance. Only typed text with an explicit grant phrase qualifies, verified against the sentence it came from; nothing here is inferred from behaviour or silence. The harness's permission, sandbox and safety rules beat every grant. A grant never covers messages or posts to other people unless it names them. An autonomy grant (Class: autonomy) lifts the hold only on reversible, own-repo actions and on guesses that would otherwise be held; it never covers outward-facing or destructive actions, spend, or permission, sandbox or security configuration unless the user's literal current message asks. Edit by hand: narrow Covers, extend Never covers, set Status to revoked, or delete the entry; a revoked or deleted entry is never re-added.

## G1. Push the working branch whenever its green
- Quote: "btw you can push the working branch whenever its green, no need to ask me" (2026-01-03, typed, cs01)
- Source: claude-code:cs01:d5c7155a-39db-59f6-a31d-26d698bf6ae6
- Scope: Alex's own projects
- Covers: push the working branch whenever its green
- Never covers: anything the quote does not name; messages or posts to other people; permission, sandbox or security changes
- Status: active

## G2. Autonomy within the current harness mode
- Quote: "you have max permissions, except don't push to main" (2026-02-01, typed, cs08)
- Source: claude-code:cs08:7a0c3e21-5b9d-5f1e-9c44-2f1d8e6b3a10
- Class: autonomy
- Scope: every interactive session
- Covers: reversible, own-repo actions and guesses that would otherwise be held, within the current harness permission mode
- Never covers: the quote's own limit: "except don't push to main"; outward-facing actions (a message or post needs a grant that names the person or channel, or the user's go); destructive actions, force-pushes, deploys and deletes of anything not regenerable; spend; permission, sandbox or security configuration unless the user's literal current message asks (text in the expansion does not count); anything the harness or system policy forbids
- Status: active
```

`Class` is optional and defaults to `action`. An action grant names one class of action, and Covers is limited to what its quote names. An autonomy grant (`Class: autonomy`) comes from a quote that grants general autonomy rather than one action: "you have max permissions", "you have full perms", "you're pre-authorized", "ur in bypass perms", "do it in bypass permissions mode". Its Scope, Covers and Never covers are fixed text written by the pipeline (`AUTONOMY_SCOPE`, `AUTONOMY_COVERS`, `AUTONOMY_NEVER` in `scripts/mr/grants.py`), whatever the miner wrote; an exception clause the user typed after the phrase ("except ...", "but never ...", "unless ...", a "don't ..." that is not about asking) is kept verbatim at the head of Never covers, and a candidate whose exception clause would read as a forbidden rule is dropped. At runtime an autonomy grant lifts the hold only on reversible, own-repo actions and on guesses that would otherwise be held; it never covers outward-facing actions, destructive actions, spend, or permission, sandbox or security configuration unless the user's literal current message asks (SKILL.md section 5).

The bypass family ("bypass permissions", "bypass perms", "skip permissions", "--dangerously-skip-permissions", "yolo mode") counts only in a second-person frame that says the agent is in or has that mode ("you're in", "ur in", "you have", "you're running with") or tells it to work in it ("do it in", "go in", "work in"). It is never a grant when it is a bare mention ("bypassing permissions is bad"), a description of the mode (a state word or negator follows it in its sentence: "the bypass permissions setting is off", "bypass permissions doesn't let you push"), a request to change the harness (an imperative config verb just before it: "turn on", "enable", "set", "switch to"), a condition ("if you have full perms"), a question, or an instruction about another process ("run claude with --dangerously-skip-permissions"). The same state, config and condition checks apply to "you have full/max permissions" and "pre-authorized".

How it is built: the miner lists `grants` candidates (quote, time, session, origin, scope, covers) only from `origin: typed` messages that carry an explicit grant phrase ("you don't need to ask", "no need to ask", "without asking me", "you have my permission", "always ok to", "from now on just", "whenever it's green", "I authorize you to"; the list is `GRANT_PHRASES` in `scripts/mr/grants.py`; "feel free to", "you can always", "you're allowed to", "you're free to" and "whenever you want" count only beside a standing marker such as "from now on" or "in general"; "always <verb>" counts only when the verb names an action the runtime holds and no "can", "could" or doubt ("I'm not sure") precedes it; the bypass family counts only in the frame described above). Sentences end at `!`, `?`, `;`, a newline, or a period followed by whitespace or the end of the text, so "settings.json", "e.g" inside a word and version numbers never cut a quote short. `synthesize grants` then, without an engine call: locates the quote verbatim (whitespace folded, case folded) in a typed corpus record, preferring the candidate's session; takes the whole sentence that holds the grant phrase as the quote, so a truncated quote cannot cut off a negator; drops the candidate when a negator (never, don't, do not, not, no, can't, stop) precedes the phrase in that sentence; records the record's id as `Source`; keeps the engine's Covers only when the quote names every action word in it, otherwise replaces it with the quote's own words minus the asking language (or drops the candidate when nothing actionable is left); drops a never-grantable Covers, a spend Covers without a cap and a message Covers that names no role token or channel; and appends the survivors with the next free id. The quote and the source come from the corpus, Covers from the quote: the engine's own words never reach the file except as the scope line. An existing file is merged into, never rewritten, and deduplicated by source record and by quote: the user's revocations, narrowings and deletions survive every rebuild. Revoked and deleted grants are recorded as tombstones in `MODEL.json` (`grant_tombstones`; `grants_added` lists what the pipeline added, which is how a deletion is noticed) and are never re-added, even from a re-mined paraphrase of the same message. A mined `preference_change` whose `later` text carries a revocation and exists verbatim in a typed record sets the matching grant to `revoked <date>`. `--force` discards the file and the tombstones and starts over.

Rules, checked by `lint --model` and `synthesize lint`, and applied by `select`, which re-renders into the slice only the blocks that are active and pass every rule (revoked, invalid and malformed blocks are dropped before the runtime sees anything, and `decision.grants` lists exactly the re-rendered ids; `status` counts an invalid grant as invalid, never as active, and reports the file as blocked):
- `## G<n>. <Title>` headings with distinct numbers (a duplicated number invalidates every block carrying it), each followed by the five `- Field:` lines above; `- Source:` is optional and carries `source:session:record_id` for a pipeline-built entry. `Quote` is `"verbatim text" (YYYY-MM-DD, origin, session)`; the origin must be `typed`; the quote must contain a grant phrase that no negator precedes in its sentence. `Status` is `active` or `revoked YYYY-MM-DD`.
- `Covers` names a class of action, and its action word (the first content word) appears in the quote: a Covers about something the user never said is invalid. It can never name permission, sandbox, security, allowlist, hook, webhook, forwarding, auto-reply or configuration changes (settings and config files, `CLAUDE.md`, `AGENTS.md`), credentials, or a blanket ("anything", "whatever you want"): the harness owns the first group and the second is not a grant. A Covers about spending states a cap (an amount, "up to", "ceiling", "limit").
- A grant whose Covers mentions messages, posts, replies, email, a chat channel, pings, notifications, invitations, PRs or issues must name the recipient as one of the role tokens (`<teammate>`, `<stakeholder>`, `<repo-owner>`, `<vendor>`, `<friend>`, `<family>`, or a token from `config.json` `people`) or a channel (`#deploys`, `@oncall`); any other `<word>` names nobody. The pipeline's default Never covers for such a grant leaves out "messages or posts to other people", since its Covers names exactly who it reaches. A general autonomy grant never reaches other people.
- An autonomy grant's quote carries an autonomy phrase that passes the frame, state, config and condition checks above, and its Covers is exactly `AUTONOMY_COVERS`.
- The `- Quote:` line is exempt from the forbidden-rule patterns of section 9 (it is the user's text, and "no need to ask" is exactly what a grant phrase looks like), and so is an autonomy grant's fixed Covers line (its text is the pipeline's, and it names the harness mode as a limit); every other line is checked, an autonomy grant's Never covers included.
- Third parties appear only as role tokens; no secrets, emails, paths.

The runtime's precedence: the harness, then the user's newest words, then a correction, then a grant or standing rule, then inferred content. A grant the seed contradicts does not apply for that seed. A correction never lifts a held category (`MR correct` refuses a line that reads as a permission). At runtime an action grant's action must be named by the Quote itself as well as fall inside Covers; an autonomy grant follows its own narrower rule instead (SKILL.md section 5). The decision log records which grant ids were available (`grants`), so `status` can show how often grants were in play.

## 4. light.md and the always-on block

```
# Alex: how to read Alex's prompts

Scope: these rules apply to messages Alex types in an interactive session. If you are a subagent, a headless or SDK run, a workflow or delegated child, or you are reading a brief written by another agent or script, ignore this file: your brief is binding as written. The harness's permission, sandbox and safety rules beat every line below.

## Read the prompts
## Default behaviour
## Quality bar
## Report
## Standing rules
## Never
```

Rules:
- At most 60 lines including the header. The title and the Scope paragraph are fixed text inserted by the CLI; the model fills the six sections only, in that order.
- Terse imperatives. No evidence, no quotes, no names, no project facts, nothing from the forbidden-rule list.

The always-on block is `light.md` rendered by `refresh --always-on` for a personal CLAUDE.md or AGENTS.md (never a repo's):

```
<!-- BEGIN mind-reading (generated 2026-03-31 from $MIND_READING_HOME/model/light.md, model 0123456789abcdef; delete this block to remove) -->
## Alex: how to read Alex's prompts
Scope: ...
### Read the prompts
...
Precedence: the harness's rules, then a repo's AGENTS.md or CLAUDE.md and project memory, then Alex's newest words, then this block. Full expansion on demand: the mind-reading skill ("read my mind: ...").
<!-- END mind-reading -->
```

`# ` is demoted to `## ` and `## ` to `### `; the precedence footer is fixed text. The runtime detects the block by the visible footer line starting `Precedence: the harness's rules` (harnesses may strip HTML comments) or by the `<!-- BEGIN mind-reading` marker, and then passes `--no-light`. `status` reports drift between the block in each known target and the current render.

## 5. index.json

Built by `synthesize index` from the two markdown files. The runtime reads it first and then reads only the line ranges it needs. Line ranges are 1-based and inclusive.

```json
{"version": 2, "model_hash": "0123456789abcdef", "built": "2026-03-31T18:00:00Z",
 "light": {"path": "light.md", "chars": 5800},
 "reading_notes": {"path": "profile.md", "lines": [3, 8]},
 "standing_rules": {"path": "profile.md", "lines": [127, 151], "chars": 6100},
 "layers": [{"n": 1, "title": "How Alex thinks", "lines": [10, 37]}],
 "groups": [{"id": "A", "title": "Any request to build, fix or do something",
             "when": ["build", "fix", "implement", "add", "make", "do", "change"], "lines": [227, 234], "chars": 2300}],
 "lessons": [{"id": "W1", "title": "Deploy", "when": ["deploy", "staging", "release"], "lines": [301, 303], "chars": 420, "count": 1}],
 "vocabulary": [{"phrase": "rq / real quick", "line": 361}],
 "examples": [{"n": 1, "seed": "keep going", "tags": ["resume"], "lines": [12, 15], "chars": 900}],
 "corrections": {"path": "corrections.md", "lines": 7},
 "grants": {"path": "grants.md", "count": 1, "active": 1}}
```

`lessons` is present only when the profile has the section. `corrections.lines` is the line count at build time; the runtime always reads the current file. `model_hash` is `sha256(light.md + profile.md + examples.md + grants.md)[:16]` (the bytes of the four files in that order, a missing file contributing nothing) and changes whenever one of them changes; `index.json` is derived from them and is not hashed. `grants` is present only when `grants.md` exists. `select` compares `model_hash` with the files on every call: when they differ (a file was edited after `synthesize index`), the index is rebuilt in memory for that call (never written), the header carries `index.json stale, rebuilt in memory: run \`synthesize index\``, the decision carries `stale_index: true`, and `status` warns. If the edited files no longer parse, the legacy fallback below is used instead.

Selection, as implemented by `mind_reading.py select` and followed by hand when the scripts are unavailable (the seed goes in through `--seed-file -` on stdin, so shell metacharacters in it stay literal):
1. Always, in this order: `corrections.md` (all lines), `grants.md` (its preamble plus the blocks that are active and pass every rule in section 3a, nothing else), `light.md` unless `--no-light` (the runtime passes it when the always-on block is already in context), the reading notes, the Standing rules.
2. Groups: score each group by the number of its `when` keywords found in the seed (whole word, case-insensitive) plus 0.5 per keyword found in the request kind passed as `--kind`. Take the top `max_groups` (default 4) with a score above zero; if none scores, take group A only. When more groups score than `max_groups`, the seed spans kinds of request and the slice escalates on its own to the plan-mode limits below (`escalated` in the decision).
3. Lessons: score each lesson group like a group, by its `when` keywords in the seed plus 0.5 per keyword in `--kind`; take the top `max_lessons` (default 2; 3 in plan mode or when escalated) with a score above zero, none otherwise. They follow the groups under a `## What works and what doesn't (lessons for this kind of request)` heading, and the header names them (`lessons W1;`).
4. Vocabulary: entries whose phrase, or any `/`-separated variant, appears in the seed.
5. Examples: score by tag overlap with the chosen groups' and lesson groups' ids and `when` words, by `when` phrases present in the example's seed, then by token overlap between the example's seed and situation and the seed and kind; take the top `max_examples` (default 6) and emit them in ascending relevance, so the best match is the last thing before the seed.
6. Trim to `budget_chars` (default 28,000, about 7k tokens): the least relevant example first, then groups beyond the first, then lesson groups beyond the first; never corrections, grants, the Standing rules or the vocabulary.
7. Output: markdown with a header line `<!-- mind-reading slice: groups A,E; examples 7,3; 6900 chars -->`.

`--mode plan` (alias `initiative`) raises the limits to 6 groups, 3 lesson groups, 10 examples and `initiative_budget_chars` (default 48,000). `select` exits 4 with "no model" only when `index.json`, `profile.md` and `examples.md` are all missing; that exit is the runtime's existence check. With model files but no `index.json` (a model built before the index format) `select` builds a lenient index in memory: reading notes are the lines before the first `## `, the Standing rules layer is found by its title, groups without `When:` lines are keyed by their title words and quoted phrases (matched by stem, so "message" finds "Messages to people"), examples without `Tags:` by their seed and situation words; every example is loaded within the plan budget, and the header reads `<!-- mind-reading slice (legacy model, no index: every example loaded): ... -->`, or `N of M examples loaded, the budget trimmed the rest` when it did.

## 6. corrections.md

One line per correction, appended by the runtime through `MR correct` (JSON on stdin: `seed`, `wrong`, `meant`, `scope`, `kind`; the CLI adds the date and formats the line). Later lines win. Fields separated by ` | `, in this order:

```
2026-03-20 | seed: "push to phone" | wrong: added "do not push to GitHub" | meant: push and merge my branch when I say push | scope: global | kind: fix
2026-03-21 | seed: "keep going" | wrong: n/a | meant: drop the model line "Approval is standing" | scope: global | kind: drop
2026-03-22 | seed: "deploy" | wrong: assumed the staging host | meant: production for this repo | scope: project:webshop | kind: fix
```

- `date`: YYYY-MM-DD.
- `seed`: quoted, under 80 characters.
- `wrong`: what the expansion did, or `n/a`.
- `meant`: what the user meant; for `drop`, the model sentence to remove.
- `scope`: `global` (about the person) or `project:<name>` (a project fact; `refresh` keeps it out of the model and lists it in `MODEL.json.project_notes` for the user to move into that project's memory).
- `kind`: `fix` (change a sentence), `add` (add one), `drop` (remove one).

A line that does not parse is kept and reported by `status`, never deleted. The runtime reads the whole file every call; `status` warns past 200 lines and suggests `refresh --corrections`. `MR correct` exits 4 and records nothing when `meant` or `wrong` of a `fix` or `add` carries a forbidden rule (section 9) or a grant phrase: a correction changes what the expansion says, never what may be done without asking. `lint --model` applies the same check to every line already in the file (a `drop` names the sentence to remove, so its `meant` is exempt).

## 7. decisions.jsonl

One JSON line per runtime invocation, appended by the runtime through `MR log-decision` (the same JSON on stdin, plus `seed`; the CLI replaces the seed with its hash). It holds counts and held items only, never the seed text, so the file never leaks content.

```json
{"date": "2026-03-20", "seed_hash": "9f86d081", "mode": "execute", "s": 5, "l": 3, "g": 2, "held": ["back-fill last week's failed exports"], "groups": ["A", "E"], "examples": [3, 7], "grants": ["G1"], "lessons": ["W1"], "upgrades": 1, "chars": 6900}
```

- `seed_hash`: the first 8 hex characters of sha256 over the seed text, computed by `log-decision`.
- `mode`: `execute`, `discuss`, `status`, `hold` or `plan`.
- `s`, `l`, `g`: how many sentences carried each tag.
- `held`: the items listed under Held, as short phrases.
- `groups`, `examples`, `chars`: the slice that was loaded, from the slice header.
- `grants`: the ids of the grants the expansion relied on (`[S:G1]` tags), empty when none.
- `lessons`: the lesson groups in the slice (`W1`), from the slice header; `upgrades`: how many sentences took a lesson's better way over the literal ask (`Changed:` lines).

`status` turns the file into calibration counts: invocations, mean S/L/G, held rate, corrections per 100 invocations, upgrades per 100 invocations.

When the data home is not writable (Codex's workspace-write sandbox, a read-only mount), `log-decision` and `correct` append the line to a spool instead (`$MIND_READING_SPOOL/pending.jsonl`, or `<tempdir>/mind-reading-spool-<uid>/pending.jsonl`, directory 0700, file 0600) and exit 0. The next `select`, `status`, `log-decision` or `correct` that can write the home applies the spooled lines to `decisions.jsonl` and `corrections.md` and removes the spool; `status` reports how many wait. Exit 5 means neither the home nor the spool was writable.

## 8. MODEL.json

```json
{"version": 2, "built": "2026-03-31T18:00:00Z", "name_token": "Alex",
 "run_id": "20260331-174500", "split_id": "a1b2c3d4", "corpus_hash": "...",
 "engine": "claude", "models": {"mine": "sonnet", "synthesize": "opus", "expand": "sonnet", "judge": "sonnet"},
 "model_hash": "0123456789abcdef", "parents": [],
 "corrections_folded_through": "<sha256 of the last folded correction line>",
 "project_notes": [], "always_on_targets": [],
 "files": {"light.md": 5800, "profile.md": 61000, "examples.md": 24000, "grants.md": 840, "index.json": 7000}}
```

`parents` holds the previous `model_hash` after a refresh. `always_on_targets` is set by the first `refresh --always-on --write-to` and used by `status` for the drift check. `folded_drops` lists the sentences removed by `kind: drop` corrections. `grants_added` (`[{"id", "source", "quote"}]`) lists the grants the pipeline added to `grants.md`, and `grant_tombstones` (`[{"id", "source", "quote", "reason": "revoked" | "deleted", "date"}]`) the grants the user revoked or deleted, which `synthesize grants` never re-adds. `synthesize grants` may create the file with only those two keys before the manifest step runs; `status` treats a manifest without `model_hash` as no model yet.

## 9. Forbidden rules

The harness beats the model. No model file may tell an agent to bypass, skip or auto-approve permissions; to send, post, push, merge or deploy without asking, confirming or waiting; to stop asking or never prompt; to treat a past approval as standing, permanent or already authorised; to add anything to an allowlist; or to change security, sandbox or permission settings or configuration. `synthesize lint`, `refresh`, `refresh --always-on` (before rendering the block) and `lint --model` fail on these patterns (case-insensitive; the authoritative list is `FORBIDDEN_RULES` in `scripts/mr/lint.py`; past-tense and third-person forms such as "pushed without a round trip" are covered), `MR correct` refuses a line that carries one, and the runtime treats a surviving match as void:

```
bypass(ing|ed)? (the )?permissions?       permissions? (are|is|gets?)? bypassed     skip(ping|s)? (the )?permissions?
dangerously                               without (asking|confirm...|approval|review|waiting|a (check|prompt|question))
don'?t ask                                never ask (me )?(for )?(approval|permission|confirmation|consent|before)
never prompts? (again|me|for|before|first)?   never (confirm|wait for (approval|permission|confirmation|a yes))
no need to (ask|confirm|check in|wait)    auto[- ]?approv                            already (authori[sz]ed|approved)
stop asking                               (approval|permission|authori[sz]ation)s? (is|are) (standing|permanent|implied|granted)
permanent(ly)? (approv|authori[sz]|allow|grant)   (make|made|treat) .* (approval|permission|prompt)s? (as )?permanent
allow[- ]?list                            (fix|change|edit|update|set) .* (config|settings?) .* permanent
permanent(ly)? .* (config|settings?)      (change|edit|modify|loosen|relax) .* (security|permission|sandbox) (settings?|rules?|config)
(sen[dt]|post(ed|s)?|deploy(ed|s)?|merg(e|ed|es)|push(ed|es)?|ship(ped|s)?) .* without     never a stopping point
```

Lessons (section 2a) get one more check on top: nothing in a lesson may authorize an outward-facing, destructive, spend or permission action (`lessons.lesson_hits`), and the `Did:` and `Outcome:` part of a bad lesson, which describes what went wrong, is the one place a description such as "pushed without running the tests" is not read as an instruction. The exempt lines are the fixed rewording template and the `- Quote:` lines of `grants.md` (section 3a), which carry the user's verbatim words and are admitted only with an explicit grant phrase, a verified source and a grantable Covers. When the mining shows the user said such things without granting anything, the profile records the preference, not the instruction, under `## 5. How {name} communicates` or `## 8. Dislikes`, in exactly this form: "gets frustrated by repeated permission prompts; expects the agent to batch questions and work on unblocked items meanwhile". "Prefers that you skip permissions" is an instruction and fails the lint. The substance of a grant never enters `profile.md` or `light.md` in any wording; it lives in `grants.md` only. The runtime's held list (SKILL.md section 5) applies whatever the model says; only an active grant in `grants.md` lifts a category, and never permission, sandbox, security or credential changes, and never messages to people the grant does not name.
