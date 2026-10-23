# Seed to full prompt: examples

These are patterns, not scripts. Rebuild every expansion from the current project's memory and conversation.

This is an authored fictional engineer persona, not a model of the repository's
author or a promise about a new user's preferences. This engineer builds a local-first
issue tracker, values reversible SQLite migrations and keyboard access, and wants
concise verification evidence. A real expansion may infer these details only when
the current conversation, project instructions or the user's own model supports them.

Literal seed words are untagged. `[S]` marks stated evidence, `[L]` a recurring
pattern, and `[G]` a guess that remains visible. An example never supplies authority
to push, deploy, spend or contact someone.

## Data changes

### 1. add the migration
Situation: the schema change is already agreed in the conversation; the engineer has repeatedly requested reversible migrations.
Tags: migration, sqlite
> Add the migration for the agreed schema change. [S] Preserve existing rows and include a rollback path. [L] Check it against a populated database copy, an interrupted run and a second run, because recovery and repeat execution have mattered in previous migrations. [S] Keep offline behavior working and report the exact checks with their actual outcomes. [G: I mean the schema change already agreed here; resolve that from the conversation before editing.]

### 2. tighten the import
Situation: an existing importer is losing rows; the engineer has defined tighten as removing edge cases while preserving behavior.
Tags: fix, import
> Tighten the existing import. [S] Reproduce the lost-row case before editing and keep it as a deterministic regression test. [L] Check neighboring parsing paths that share the cause, including retry behavior, while preserving the import format callers already use. [S] Report the cause, the exact check and its actual outcome. [G: I take the import to mean the one from the latest bug report in this conversation; verify that referent before changing files.]

## Interface changes

### 3. add the setting
Situation: the requested setting is clear; the engineer has repeatedly asked for command palette access and offline use.
Tags: settings, keyboard
> Add the setting to the existing settings screen. [S] Make it reachable from the command palette as well. [L] Preserve keyboard shortcuts and focus order, and verify the behavior in offline mode. [S] Show the keyboard path a user can follow and report what was exercised. [G: use the app's existing default unless the conversation specifies another, and state that assumption.]

### 4. make this easier to use
Situation: the conversation identifies a cumbersome settings flow and the app already has a visual language.
Tags: settings, design
> Make the settings flow easier to use. [S] Keep the current design language and command palette entry points. [L] Group controls around the task a user is doing, and check the entire keyboard path, including focus after save and after an error. [S] Show before and after evidence with the checks you actually ran. [G: start with the flow just discussed; do not infer a redesign of unrelated screens.]

## Running work

### 5. keep going
Situation: an agreed checklist is active and the last report identifies an unfinished item.
Tags: resume
> Keep going from the next unfinished checklist item. [S] Preserve the goal and constraints already agreed in this conversation. [L] Finish its verification and any documentation the change affects before moving to the next unblocked item. [S] Report the result and evidence concisely, with anything unverified made explicit. [G: if a referent cannot be resolved from the checklist and the last report, state that gap while continuing independent work.]

### 6. status?
Situation: work is already running and the engineer wants a concise result with evidence.
Tags: status, report
> Give me the current status. [S] Lead with any input you need from me, then what is verified, what is running and what remains. [L] Include the exact check or artifact supporting a completed claim, and keep unverified behavior explicit. [S] Continue the agreed unblocked work after the status update. [G: include a completion time only if the running job provides a basis for one.]

## Handoffs

### 7. write a brief for another agent
Situation: an agreed change needs a standalone implementation brief for a separate session.
Tags: handoff
> Write a standalone brief for another agent with no access to this conversation. [S] Include the goal, owned files, interfaces, acceptance criteria and exact verification command with a passing outcome. [L] Carry over the constraints the change depends on, including reversible migrations and existing keyboard paths when relevant. [S] State the authority already granted in this conversation accurately, and distinguish unresolved assumptions. Deliver the brief in the location requested here.

### 8. tell the maintainer
Situation: the user wants a message drafted about an already verified fix; no wording has been approved.
Tags: message
> Draft a short message to the maintainer about the verified fix. [S] Include the issue, the result and the evidence they need to review it. [L] Use my concise style and make the requested next action clear. [G: resolve which maintainer and channel from the current conversation; leave an unresolved recipient explicit.] Show the exact text for review before sending it, while continuing any already authorized work that does not depend on the message.
