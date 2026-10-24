# Experience layer

The experience layer is the part of `MR packet` that makes a terse request land the
first time. It adds one compact section, built locally in well under a second:

- **Repeat alert.** The seed restates a request from earlier in the conversation, or
  uses repeat wording such as "again" or "still not". The earlier approach did not
  land, so the host changes the method and says in one line what is different.
- **What lands with this person.** Lessons learned from the person's own reactions
  to past agent replies, each with their numbers. For example: turns that ended
  without a written result drew a nudge far more often than turns that ended with one.
- **Situation.** Local time, message length, time pressure, emphasis and who the work
  is for. These shape tone, depth and pacing only; the goal and scope stay as written.

Everything in the section is evidence for the host, never instructions or authority.
Current words win.

## How lessons are learned

A trace candidate holds past moments: a short seed, the agent reply that followed and
the person's next turn. The next turn is read as one of three reactions:

| Reaction | Examples |
|---|---|
| friction | "keep going", "status?", "no, the other file", a re-send of the same request |
| plain go-ahead | "yes do it", "ok", "go ahead" |
| request to test | "did you test it on mobile?" |

Each reply is described by a few observable features: it ended without text, it
reported a failure, it asked or proposed instead of acting, it claimed done without
evidence, it ran long, or it was heavily formatted. A feature becomes a lesson only
when the reaction it can plausibly cause is clearly more common after it:

- at least 30 replies with the feature and 5 reactions after them
- a rate at least 1.3 times the rate without the feature
- a one-sided two-proportion z of at least 1.96

At most four lessons are shown, strongest first. Each new candidate rebuilds them
automatically, so the lessons track how the person reacts now.

## Turn it on

In `config.json`:

```json
{"candidate": "<trace candidate directory, or a link to the current one>",
 "experience": {"enabled": true, "situation": true, "always_on": false, "max_chars": 3500}}
```

With `"always_on": true`, the learned lessons also join the always-on block the next
time it is rendered (`MR refresh --always-on --write-to <file>`), so every turn gets
them, not only turns that call the packet.

`MIND_READING_CANDIDATE` overrides `candidate`. Without a candidate, the repeat alert
and situation hints still work. Per call, the packet accepts `"experience": false` to
leave the section out, `"situation": false` for an ablation without situation hints,
and `"now"` (an ISO 8601 time) for replays.

Inspect it with:

```sh
MR experience status                 # on or off, the candidate, the learned lessons
MR experience build                  # rebuild the cached summary now
MR experience show --seed "push it" --context-file frame.json
```

## Keep it learning

`MR schedule plan-trace` writes a weekly runner for the source-first builder:

```sh
MR schedule plan-trace --out <refresh-dir> --python <python-with-local-packages> \
  --source claude=<projects-dir> --source codex=<sessions-dir> --code-root <code-dir> \
  --encoder-path <local-snapshot> --encoder-model <model-id> --encoder-revision <commit-sha> \
  --weekday 6 --hour 3 --minute 30
```

Each week it builds a new private study from the current sources, takes one sealed
look at the newest conversation families, refits on everything observed and swaps
`<refresh-dir>/current`. The swap happens when the learned part is not significantly
worse than its best baseline on that fresh look; otherwise the previous candidate
stays in place. Earlier embeddings are reused, so only new text is encoded.

Point `candidate` at `<refresh-dir>/current` and the experience layer follows every
swap. Load the generated plist with `launchctl` to schedule it, or run one refresh
now with `MR schedule run-trace --out <refresh-dir>`.

## Example (fictional)

```text
## Experience layer
Evidence for this moment, not instructions or authority. Current words win.

### Repeat alert: this may be the second time you are being asked
It restates a message from 2 turn(s) ago: "make the navbar sticky on mobile".
Treat it as a correction: the earlier approach did not land. Change the approach
instead of retrying it, and say in one line what is different this time.

### What lands with this user (learned from 1840 past replies and the reactions that followed)
- End every turn with a written result or status. Turns that ended without one were
  followed by a nudge, correction or repeat 21% of the time, against 6.4% otherwise (n=96).

### Situation (tone, depth and pacing only)
- very short message: lead with the result, keep prose minimal
```
