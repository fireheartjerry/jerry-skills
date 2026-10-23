# Seed to full prompt: examples

These are patterns, not scripts. Rebuild every expansion from the current project's memory and conversation.

## Fictional demonstrations
### 1. add the migration
Situation: the agreed schema change is ready to implement in a local-first issue tracker.
Tags: migration, sqlite
> Implement the schema change we agreed. [S] Keep the SQLite migration reversible and preserve existing rows; include the rollback path. [L] Verify it against a populated copy, an interrupted run and a second run so recovery and repeat execution are covered. [S] Keep offline operation working. [S] Report the exact checks and their actual outcomes. [G: the schema change means the one already agreed in this conversation; resolve it from project context before editing.]

### 2. add the setting
Situation: the requested setting already has a clear purpose and an existing settings screen.
Tags: settings, keyboard
> Add the requested setting to the existing settings screen. [S] Make it reachable through the command palette as well. [L] Preserve keyboard shortcuts and focus order, and verify the behavior in offline mode. [S] Report the exact check and outcome, with the keyboard path a user can follow. [G: use the app's existing default unless the conversation specifies another; state the assumption.]

### 3. fix the crash
Situation: a crash was reported but the cause has not been established.
Tags: fix, crash
> Fix the reported crash. [S] Establish a reproducible failing case before editing and keep it as a deterministic regression test. [L] Check the neighboring code paths that share the cause. [S] Re-run the failure path after the fix and report the actual outcome, the cause and anything still unverified. [G: use the latest crash report in this conversation; if it cannot be resolved, state the missing evidence instead of inventing a cause.]
