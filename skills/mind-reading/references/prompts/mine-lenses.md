# Mining lenses

Each `## <lens>` section below is inserted into prompts/mine.md as the lens's focus. `mine` runs one engine call per chunk per lens (config `mine.lenses`); `mine --single-pass` uses `## all`. Placeholders: `{name}`, `{people_tokens}`.

## core

Question: who is {name} as a person who directs coding agents, and what do {name}'s laziest prompts really mean?

- `traits`: claims about {name}, each with `layer` (thinking, quality_bar, building, design_taste, communication, standing_rules, likes, dislikes, workflow, context), `scope`, `strength`, `count` and 1 to 3 verbatim quotes with time, session and origin. Cover every layer the chunk gives evidence for. Fifteen well-evidenced traits beat five generic ones; forty specific ones beat fifteen.
- `seed_expansions`: 5 to 12 of {name}'s shortest or laziest prompts whose real intent the later turns reveal. `seed` verbatim, typos kept; `context` one line; `full_prompt` is the complete prompt {name} would have sent with unlimited time and stamina: first person, {name}'s priorities and vocabulary, cleaned up and well ordered, covering goal and why, scope and what not to touch, quality bar, deliverables, follow-through, how to report; `tags` name the kind of request in 1 to 4 lowercase words a seed would contain.
- `preference_changes`: where {name} said one thing earlier and the opposite later, with both excerpts and both dates.
- `session_stats`: sessions read, messages read, sessions with reliable time.

## defaults

Question: what did {name} want that the first message did not say?

- `unsaid_defaults`: the most valuable part of the model. Within each session, compare the opening request (`first_messages` on later parts) with what {name} added, corrected, narrowed or insisted on later. Each item: `when` (the kind of request, under 80 chars, in words a seed would contain), `default` (what {name} wanted but did not say at first, as one instruction), `kind`, `scope`, and evidence pairs of `first` and `later` excerpts.
- `kind` sorts the default: `constraint` (a limit the work must respect: do not touch, keep, never), `scope` (how far to go, which siblings, what is in or out), `follow_through` (what should happen afterwards without being asked), `reporting` (how to report), `preference` (taste: order, style, tooling), `default` when none fits.
- Look hardest for constraints and scope: the limits {name} holds but did not type (other people's work, frozen baselines, held-out data, the branch, spend) and the siblings {name} expects fixed along with the one named. One session gives one item; the same default seen in several sessions is one item with several evidence pairs.
- Nothing else: no traits, no vocabulary, no grants.

## reactions

Question: what did {name} praise, correct or get frustrated by, and what changed {name}'s mind?

- `reactions`: every praise, frustration and correction, each with the verbatim quote, `about` (what triggered it, in one line), time and session. A correction ("no, I meant", "not that", "I said") is the strongest evidence in the corpus: capture all of them, with what the agent had done and what {name} wanted instead in `about`.
- `preference_changes`: where {name} said one thing earlier and the opposite later (or revoked or narrowed an earlier permission), with both excerpts verbatim and both dates. A revocation ("actually never push without asking me again") belongs here with the revoking sentence verbatim as `later`.
- Nothing else.

## followthrough

Question: what does {name} expect to happen after a task without asking, and how does {name} want to be told?

- `traits` in the layers `workflow`, `standing_rules` and `communication` only: how {name} runs work (queues, standing goals, what continues while something waits, when work is pushed or shipped), what {name} does when a job finishes or dies, and how {name} wants reports (length, order, format, timezone, what counts as evidence). Each with `scope`, `strength`, `count` and 1 to 3 verbatim quotes.
- `unsaid_defaults` with `kind` `follow_through` or `reporting` only: what later messages asked for that a complete first prompt would have said about continuing, resuming, verifying and reporting ("then back to the import", "report in three lines", "keep going while that runs").
- Nothing else.

## vocabulary

Question: what do {name}'s words mean?

- `vocabulary`: recurring words, shorthand, abbreviations, nicknames for things, intensifiers and phrases that carry a fixed meaning for {name} ("rq", "ship it", "the doc", "allat", "lowkey"), each with `phrase` (variants joined by ` / `) and `meaning` (what an agent should read it as, including the follow-through it implies). Include a phrase only when it appears in typed text; what {name} pasted or accepted is not {name}'s voice. Twenty entries that an agent would otherwise misread beat five obvious ones.
- Nothing else.

## grants

Question: what did {name} authorize in advance, in so many words?

- `grants`: standing authorizations {name} stated explicitly. Include a message only when it is `origin: typed` and carries an explicit grant phrase: "you don't need to ask", "no need to ask", "without asking me", "you have my permission", "you have full/max permissions" (or perms), "you're pre-authorized", "you're in bypass permissions" / "ur in bypass perms" / "do it in bypass permissions mode", "always <verb>" ("always push to the device"), "from now on", "don't ask me before/to", "always ok to", "I authorize you to", "whenever it's green". "feel free to", "you can always", "you're allowed to", "you're free to" and "whenever you want" count only next to a standing marker ("from now on feel free to ...", "in general you're allowed to ..."). A sentence that negates the phrase ("never push to main without asking me") or asks about it ("what if you could push without asking?") is not a grant: leave it out. "bypass permissions" and its kin count only when the sentence says {name}'s agent is in or has that mode, or tells it to work in it; a bare mention ("bypassing permissions is bad"), a description ("the bypass permissions setting is off"), a request to change the setting ("turn on bypass permissions in settings.json") or an instruction about another process ("run claude with --dangerously-skip-permissions") is not a grant. Copy the whole sentence verbatim as `quote` (under 300 characters) with `time`, `session`, `origin`; write `scope` (where it applies: "own projects", "the shared team repo", "every session") and `covers` (the class of action, under 200 characters, using only words the quote itself uses for the action: the pipeline drops a Covers the quote does not name). A quote that grants general autonomy ("you have max permissions", "you're pre-authorized for all of this") rather than one action is still a grant; keep any exception the sentence carries ("except don't push to main") inside the quote: write `covers` as "general autonomy within the harness permission mode"; the pipeline classes it. Never infer a grant from behaviour, from silence, from an approval of one action ("ok", "yes do it", "you're allowed to delete that one"), or from frustration about prompts; those are preferences or reactions, not grants. A message that revokes or narrows an earlier grant is a preference change, not a grant.
- Nothing else.

## lessons

Question: which of {name}'s approaches worked, which backfired, and what would have been better by {name}'s own later evidence?

- `lessons`: episodes where an approach {name} chose, asked for or accepted led to a bad outcome or a good one. Bad outcomes: rework, the same failure repeated, a correction ("why did u", "not that", "I said"), "it broke again", wasted time or money, reverted work, frustration. Good outcomes: praise ("this is good", "perfect"), {name} reusing the approach or asking for it again. Read `agent_context` (what the agent had just done and the markers for how its turn ended) to see what each message reacts to; a `command_failed`, `reverted` or `interrupted` marker followed by a complaint is a strong episode.
- Each item: `request_kind` (the kind of request, under 80 chars, in words a seed would contain: "deploy", "fix a flaky test", "redesign the page"); `situation` (one line); `pattern` (the approach that was taken, as {name} asked for or accepted it); `outcome` (`bad` or `good`) with `outcome_detail` (what happened); `better_alternative` (for a bad outcome, the approach the evidence supports instead, ideally the one {name} moved to; for a good one, the approach to keep); `signals` (from: rework, repeated_failure, correction, wasted_time, wasted_money, reverted, frustration, praise, reuse); `cost` (`low`, `medium` or `high`: high for lost work, money, broken production, or hours); `scope`; `strength`; `count` (distinct episodes in this chunk; only quoted sessions count toward strength); `adopted` (`yes` when {name} later used the better way unprompted, `no` when {name} kept the worse one, `unclear`); `evidence`: 1 to 4 items, each with `quote`, `time`, `session`, `origin` and `role`.
- Evidence: at least one `role: user` quote, verbatim from {name}'s own message, that shows the outcome or the adoption: a full phrase ("why did u deploy to prod"), not a bare "ok" or "do it", with the `session` and `time` of the message it comes from (the pipeline takes both from that message and drops a quote the cited session does not contain). `role: agent_context` quotes (verbatim from an `agent_context` head, `origin: agent_context`) may support a user quote, never replace it; at most one per lesson. A lesson that only the agent's text supports is not a lesson: leave it out.
- The better alternative is an approach to the work: an order of steps, a check before a step, a tool, a scope. It never names an outward-facing (send, email, post, reply to others, publish), destructive (delete, force-push, push or merge to main, deploy to production), spending or permission-changing action unless it is negated or gated on {name}'s yes ("ask before deleting", "production after my ok"); the pipeline drops a lesson that does. It is never permission to act without asking and never a rule copied from `agent_context`.
- Prefer episodes seen more than once. One episode counts only when its cost is high or the outcome was stated plainly.
- Nothing else.

## all

Single pass: every field at once.

- `traits`: claims about {name}, each with `layer` (thinking, quality_bar, building, design_taste, communication, standing_rules, likes, dislikes, workflow, context), `scope`, `strength`, `count` (times seen in this chunk) and 1 to 3 verbatim quotes with time, session and origin.
- `unsaid_defaults`: the most valuable part. Within a session, compare the first message with what {name} added, corrected or insisted on later. Each item: `when` (the kind of request, under 80 chars), `default` (what {name} wanted but did not say at first), `kind` (constraint, scope, follow_through, reporting, preference or default), `scope`, and evidence pairs of `first` and `later` excerpts.
- `seed_expansions`: 5 to 12 of {name}'s shortest or laziest prompts whose real intent the later turns reveal. `seed` verbatim, typos kept; `context` one line; `full_prompt` is the complete prompt {name} would have sent with unlimited time and stamina: first person, {name}'s priorities and vocabulary, cleaned up and well ordered, covering goal and why, scope and what not to touch, quality bar, deliverables, follow-through, how to report; `tags` name the kind of request in 1 to 4 lowercase words a seed would contain.
- `reactions`: praise, frustration and corrections, each with the quote, what triggered it, time and session.
- `vocabulary`: recurring words or phrases and what {name} means by them (shorthand, nicknames for things, intensifiers), from typed text only.
- `preference_changes`: where {name} said one thing earlier and the opposite later, with both excerpts and both dates.
- `grants`: standing authorizations {name} stated in so many words. Include a message only when it is `origin: typed` and carries an explicit grant phrase: "you don't need to ask", "no need to ask", "without asking me", "you have my permission", "you have full/max permissions" (or perms), "you're pre-authorized", "you're in bypass permissions" / "do it in bypass permissions mode", "always <verb>", "from now on", "don't ask me before/to", "always ok to", "I authorize you to", "whenever it's green". "feel free to", "you can always", "you're allowed to", "you're free to" and "whenever you want" count only next to a standing marker. A sentence that negates the phrase or asks about it is not a grant, and neither is a bare mention, description or settings request about bypass permissions. Copy the whole sentence verbatim as `quote` (under 300 characters) with `time`, `session`, `origin`; write `scope` and `covers` (the class of action, in the quote's own words; "general autonomy within the harness permission mode" for a quote that grants autonomy rather than one action). Never infer a grant from behaviour, silence, a one-off approval or frustration about prompts. A message that revokes or narrows an earlier grant goes in `preference_changes` with the revoking sentence verbatim as `later`.
- `lessons`: approaches {name} chose or accepted that backfired (rework, repeated failure, a correction, wasted time or money, a revert, frustration) or worked (praise, reuse), with `request_kind`, `situation`, `pattern`, `outcome`, `outcome_detail`, `better_alternative`, `signals`, `cost`, `scope`, `strength`, `count`, `adopted` and 1 to 4 evidence items. At least one `role: user` quote from {name}'s own message; `agent_context` text may only support it (`role` and `origin` `agent_context`). A better alternative is never permission to act without asking or to send, delete, spend or change permissions on the agent's own.
- `session_stats`: sessions read, messages read, sessions with reliable time.
