# Cross-engine dialogue

Two agents from different engines arguing about a problem is worth more than
two from the same engine, because Codex has not read your context window and
will not inherit your framing. It is also the easiest thing here to waste money
on, so the protocol matters.

## The parent arbitrates

You relay. Neither child talks to the other directly, and neither decides when
it is finished — you do. This is not ceremony: agents left to converge on their
own will either agree politely long past the point of new information, or
oscillate between two positions forever. You are the one who can tell the
difference between genuine convergence and mutual exhaustion.

## Turn cap

**Three exchanges, four at the outside.** Each exchange is one message to each
side, so four exchanges is eight agent turns plus your own relaying — that is
already a substantial spend.

If they have not converged by then, they are not going to. That is itself the
finding: report the disagreement and what each side rests on, rather than
burning more turns hoping one caves.

## Shape that works

```bash
# Proposer builds the thing. Reviewer never sees the proposer's reasoning,
# only its output -- that independence is the entire point.
agent-session spawn claude proposer ~/code/app \
  "Design a retry policy for src/queue/. Output the design as a short spec." \
  --tier standard --steerable

agent-session spawn codex reviewer ~/code/app \
  "You review designs adversarially. Reply READY." \
  --steerable
```

Then, for each exchange:

1. Take the proposer's output.
2. Send it to the reviewer, asking specifically for what is *wrong* — a
   reviewer asked "what do you think?" will mostly compliment.
3. Send the critique back to the proposer, asking it to either fix the design
   or explain why the critique is mistaken. Both are legitimate answers, and
   saying so out loud stops the proposer from caving to every objection.
4. Decide: converged, still moving, or stuck.

Stop as soon as an exchange produces nothing new. Converging early is a good
outcome, not a wasted setup.

## Ask for disagreement explicitly

The failure mode is politeness. "Review this design" produces a list of things
the reviewer liked. Ask instead:

- "What breaks under concurrency that this design does not handle?"
- "Name the strongest argument against this approach."
- "If this fails in production in six months, what is the most likely cause?"

## Report the disagreement, do not average it

If the two engines end up disagreeing, say so and give the reader both
positions with their reasoning. Splitting the difference produces a design
neither agent would defend, and hides the one genuinely useful signal the
exercise produced: that a competent reviewer thinks this is wrong.

## Cost

A three-exchange dialogue between two `standard`-tier children is a real
expense. Before starting one, ask whether a single `--tier deep` child would
answer the question — often it will, for less. Dialogue earns its cost when
independence matters (review, adversarial checking, second opinion), not when
you simply want a better answer.
