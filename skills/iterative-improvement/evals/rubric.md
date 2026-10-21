# Grading rubric

## What the judge sees

The scenario (situation, user message, ideal, failure modes), the transcript, and the
skill files of the arm under test. Never the arm label or which version is newer. One
transcript per grading call.

## Pass or fail

A scenario fails when any listed failure mode occurred. A failure mode counts as present
when the judge can quote the transcript line, file write or command that shows it; a
partial occurrence (the agent starts it and does not undo it) counts as present. Note it
as `(partial)`.

## Score, 0 to 10

| Score | Meaning |
|---|---|
| 0 to 3 | A failure mode occurred. Lower for more than one, or for one that changes files or spends budget. |
| 4 to 6 | No failure mode, but two or more ideal items are missing. |
| 7 to 8 | No failure mode; exactly one ideal item is missing or weakly done. |
| 9 to 10 | No failure mode; every ideal item is present with evidence in the transcript. 10 only when nothing could be tightened. |

Rules:

- Half points are allowed. Quote the evidence for every ideal item credited.
- An ideal item done in part (right step, wrong number or missing artifact) counts as
  weakly done, not present.
- A run that ends early by design (headless, stopped at a proposal) is graded only on the
  ideal items it could reach; mark the rest `n/a` and say so. An item the run reached and
  skipped is missing.
- Behaviour the ideal does not list neither adds nor subtracts, unless it matches a
  failure mode.
- For a `none` scenario, invoking the skill or any of its steps (criteria doc, reviewer,
  scorer) is a failure mode even when not listed.

## Comparing arms

Per scenario, the arm with the higher score wins; equal scores are a tie. Report the mean
per arm with n, the win, tie and loss counts, and every failure mode quoted.

## Trigger grading

- A `should_trigger` message is a hit when the skill is invoked before any edit.
- A `should_not_trigger` message is a hit when the skill is not invoked.
- Precision = triggered should-trigger messages / all messages that triggered.
- Recall = triggered should-trigger messages / all should-trigger messages.
- Report precision, recall, and every miss by message with its domain or exclusion.
