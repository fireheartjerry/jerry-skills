# Fictional engineer: the full model

Reading notes
- This persona is entirely fictional; the quotes below were authored for this demo.
- Repeated fictional observations illustrate strong evidence; they prove no real user's preferences.
- The current request wins over every historical preference.
- Project facts belong in project memory.
- The harness's permission and safety rules beat every line here.

## 1. How the engineer thinks
- **Root cause first.** Reproduce the failure before editing. [strong] "show me the failing case before the fix" (01-03); "start with the reproduction so we know what changed" (01-10)
## 2. Quality bar
- **Offline is a feature.** Keep the app useful without a network connection. [strong] "this must still work when the laptop is offline" (01-05); "the issue list should not wait for the network" (01-12)
## 3. How the engineer builds
## 4. Design taste
## 5. How the engineer communicates
## 6. Standing rules
- Keep SQLite migrations reversible; show a rollback path and preserve existing rows.
- New settings must also be reachable from the command palette.
- Report the failing case, the exact verification command and its actual outcome.
## 7. Likes
## 8. Dislikes
## 9. Workflow habits
## 10. Recurring contexts

## Unsaid defaults
### A. Fixes
When: fix, broken, crash, flaky, reproduce
- Reproduce first, add a deterministic regression case, then verify the same failure path after the change.
### B. SQLite migrations
When: sqlite, migration, migrate, schema, database
- Keep SQLite migrations reversible; show the rollback path and preserve existing rows.
- Check the migration against a copy with existing records, including an interrupted migration and a second run.
### C. Settings and keyboard access
When: settings, keyboard, palette, shortcut, design
- New settings must also be reachable from the command palette; preserve the existing shortcuts and focus order.
- Verify the setting in offline mode and show the keyboard path in the report.

## Vocabulary
- **tighten**: remove ambiguity and edge cases while keeping existing behavior.
