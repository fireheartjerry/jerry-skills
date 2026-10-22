# Seed to full prompt: examples

These pairs are patterns, not scripts. Rebuild each full prompt from the current project's memory and conversation; copy the shape and the priorities, not the facts.

## Running work

### 1. keep going
Situation: a standing goal is live; the agent ended its turn with a report.
Tags: resume
> Resume exactly where you stopped. Check what died while you were away, restart it, and continue down the queue without a recap. Report only when something needs me.

### 2. progress update?
Situation: a long rebuild and an import are running.
Tags: status
> Three lines: what finished, what is blocked and why, what is next with a time. Then keep working on the unblocked items.

### 3. try again
Situation: a step failed overnight.
Tags: try again, resume
> Retry the failed step; if it fails the same way, find the cause and fix it before retrying. Keep the rest of the queue moving meanwhile.

### 4. what r we waiting for
Situation: the pipeline looks idle.
Tags: status, where are we
> Tell me the exact blocker and who owns it. If it is me, say what you need in one line; if it is a job, say when it finishes and start the next unblocked item.

## Bugs

### 5. fix the export button
Situation: a user reported the CSV export failing.
Tags: bug, export
> The CSV export fails again. Find the root cause, fix it, then check every other download path for the same failure and fix those too. Reproduce in the browser before and after on real data. Add a regression test. Push when green and report cause, change and verification.

### 6. it's broken on my phone
Situation: a layout defect on mobile.
Tags: bug, mobile
> Reproduce on a phone-sized viewport, find every page with the same defect, fix the class, and show me before and after screenshots at phone width.

### 7. the nightly job failed again
Situation: a recurring scheduled job failure.
Tags: error, fix
> Find the root cause of the recurring failure, fix it rather than restarting it, add an alert for the next failure, and report what changed in two lines.

## Design

### 8. rq make this button less ugly
Situation: a screenshot of one control.
Tags: ui, ugly, button
> Restyle this control inside the existing theme, apply the same treatment to its siblings, check phone width and contrast, and show a screenshot.

### 9. the hero overflows
Situation: the landing page on small screens.
Tags: layout, mobile
> Fix the hero overflow at every breakpoint without changing the look on desktop, check the other full-width sections for the same issue, and show screenshots at phone and tablet widths.

### 10. go bolder
Situation: a redesign round came back too subtle.
Tags: design
> Make the change visibly bold within the app's design language: stronger hierarchy, clearer spacing, no new palette. Show before and after.

## Shipping

### 11. ship it
Situation: the feature is tested and reviewed.
Tags: ship, push
> Run the full verify command, push to the working branch, and tell me the commit and what was verified. Merging to main is my call unless this is my solo project.

### 12. deploy
Situation: a web project with a staging environment.
Tags: deploy, release
> Deploy to staging first, smoke-test the main flows there, and report the URL. Production only after I have looked.

### 13. tag it
Situation: release candidate is ready.
Tags: tag, release
> Update the changelog, bump the version, tag the release, and post the notes in the release doc. Report the tag and the notes link.

## People

### 14. reply to Sam
Situation: a teammate asked about the API change.
Tags: message, reply
> Draft a short reply to <teammate> in my casual voice explaining the change and what they need to do. Show me the draft; send only after I approve.

### 15. tell Jordan it's done
Situation: a stakeholder is waiting on a deliverable.
Tags: message, tell
> Draft a two-line note to <stakeholder> saying it is done, with the link and what to look at. Show it for my approval before sending.

## Planning

### 16. thoughts on moving to a queue?
Situation: an architecture question.
Tags: discuss, thoughts
> Discuss only: lay out the trade-offs of adding a job queue for this project, with the option you recommend and why. Change nothing until I say go.

### 17. plan the migration
Situation: a new multi-day initiative.
Tags: plan, roadmap
> Write the migration plan as a checklist with phases, risks and verification per phase. Ask for my yes before executing anything.

## Handoffs

### 18. write the brief for the other agent
Situation: a chunk of work goes to a second agent.
Tags: handoff, plan
> Write a standalone brief: goal, files it owns, acceptance criteria, exact verify command, what to report. No project history it does not need.

## Build

### 19. add dark mode
Situation: a settings page.
Tags: add, build, ui
> Add a dark mode toggle to settings using the existing palette tokens, persist it per user, check every screen for contrast, add a test, and show screenshots of both modes.

### 20. refactor the cart
Situation: a module with weak tests.
Tags: build, fix
> Refactor the cart module for clarity without behaviour changes, keep the tests green, add tests where coverage is thin, and report what moved and why.

## Upgrades

### 21. rewrite the checkout module
Situation: a drafted mandate asks for a large change (lesson W1).
Tags: upgrade, w1, implement a mandate
> Implement the mandate in small steps: start with the smallest piece, write its tests first, keep the suite green after each step, and report what landed and what is next.
Changed: rewrite the module in one pass -> start with the smallest piece and tests first, because the one-pass rewrite failed and was redone in steps (01-30) (W1).

### 22. deploy it to prod
Situation: a release is ready (lesson W3).
Tags: upgrade, w3, deploy
> Deploy to staging first, smoke-test the main flows there and report the URL. Production only after I have looked.
Changed: straight to production -> staging first, then production after a look, because the last direct production deploy was rolled back (01-05) (W3).
