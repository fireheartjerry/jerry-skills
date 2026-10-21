# Seed to full prompt: examples

This file is a format template and the fallback for shape when no model exists; it is not a model of anyone. The persona is synthetic: a solo developer who owns a small web app and shares one team repository with a `<teammate>` and a `<repo-owner>`, verifies on the real path, pushes finished work to the working branch of their own projects, and wants results before explanations.

These are patterns, not scripts. Rebuild every expansion from the current project's memory and conversation.

## Running work

### 1. keep going
Situation: a standing goal is live; the last turn ended with a report and no question.
Tags: resume
> Keep going on the standing goal from exactly where you stopped: no recap, no re-plan, no questions. Take the next unfinished item in the plan file. If the plan is done, take the largest gap you already know about against the target we set and close it. Finish each item properly: tests pass, docs updated, committed to the working branch with a message that says what changed and why. Do not touch `<teammate>`'s area of the repo or the frozen baseline numbers. If something blocks an item, park it with one line saying what is needed from me and move to the next unblocked one. Report in three lines when you stop: finished, running, next.

### 2. status?
Situation: a long job is running and the user has been away for a few hours.
Tags: status, report
> Give me the state of play in the shortest form that is still complete. First anything blocked on me, with the exact thing you need. Then what is finished since my last message, with the evidence (test output, a screenshot, a number against the target), what is running now with an expected finish time in my timezone, and what is queued next. Keep verified results apart from anything unproven. No narrative, no restating the plan. Then carry on with the next unblocked item while I read.

## Fixing and polishing

### 3. login is flaky on mobile
Situation: a user reported one failed login on a phone; the app has a web and a mobile layout.
Tags: bug, verify
> Login fails intermittently on mobile. Find the root cause, not a symptom: reproduce it on a real phone-sized viewport in the browser against the real backend, not a mock, until it fails for you. Fix the cause, then check every other form that shares the same auth path or the same components (signup, password reset, the session refresh) for the same failure and fix those too. Add a regression test that would have caught it. Confirm the fix on the same device path before and after. Commit to the working branch with the cause in the message. Report: the cause in one sentence, what changed, what you verified and how, and anything you could not reproduce.

### 4. make the settings page less ugly
Situation: a working but plain settings page; the rest of the app has an established look.
Tags: ui, design
> Redo the settings page so it looks like it belongs in this app: match the existing design language, spacing, type and colour tokens exactly, and make the improvement obvious, not subtle. Group the controls by what a user would look for, with clear labels and sensible defaults, and keep every existing setting working. Check it at phone width and at desktop width, including overflow, alignment, contrast and the empty and error states. Do not introduce a new visual style or new dependencies. Show me before and after screenshots of both widths rather than describing the changes. Commit to the working branch when it passes your own check.

## Shipping and handoffs

### 5. ship it
Situation: the user's own app; the feature branch is green locally and the standing rule is that finished work is pushed.
Tags: ship
> Ship the current feature. Run the full verify command, not only the unit tests, and fix anything red before going further. Bump the version, update the changelog with what a user would notice, and push the working branch. Merge it the way we agreed for this project and deploy to the environment we use for releases. Then confirm the release on the live path yourself: load the app, exercise the new feature once, and watch the logs for the first minutes. Report: the version, the commit, where it is live, what you verified live, and anything you saw that I should know about. If any step needs my credentials or a physical device, do everything else first and tell me exactly what to do.

### 6. write a prompt for codex to do the migration
Situation: a database migration is planned and another agent will implement it in its own session.
Tags: handoff
> Write a standalone brief for another agent that will run with zero context from this conversation. It must state the goal and why; the files it owns and the files it must not touch; the exact migration steps in order with the current and target schema; the acceptance criteria; the exact verify command to run and what a pass looks like; and what to report back, in short bullets. Include the constraints it cannot infer: no pushing, no merging, no messages to anyone, stop and report if the verify command fails twice. Save it as a markdown file in the project's notes directory and give me a short paste-ready prompt that points at that file. Keep my name, the project's name and any secrets out of it.

## People and planning

### 7. tell the repo owner the fix is in
Situation: the shared team repository; the user has fixed a bug the `<repo-owner>` reported and the PR is open.
Tags: message
> Draft a message to `<repo-owner>` saying the fix for the bug they reported is in: one or two short sentences in my casual voice, the PR link, what changed in plain words, and what I would like from them (a review, or a merge if they are happy). Show me the draft and send nothing until I say yes. While you wait, make sure the PR itself is ready for them: description complete, checks green, the review comments from `<teammate>` addressed. If anything is still red, fix it first and tell me.

### 8. lets build the notifications system
Situation: a new subsystem that will take several days and touches the backend, the app and the email provider.
Tags: plan, build
> Plan the notifications system before writing code. Read the project memory and the existing event and email code first so the plan fits what is there. Give me one checklist file with every step in dependency order: data model, delivery channels (in-app and email to start), user preferences, batching and quiet hours, the provider integration, tests, docs, rollout. For each step: what it changes, which files, how it is verified. Call out the decisions that are genuinely mine (spend on the provider, anything users will see, anything that messages people) with a recommended default for each so I can answer in one word. Estimate the order of work, not durations. Then stop and wait for my yes; once I give it, execute the checklist end to end without stopping at design gates, pushing finished steps to the working branch as they land.
