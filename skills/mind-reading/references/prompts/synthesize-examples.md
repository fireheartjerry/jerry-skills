{name}'s own private data, used only to build a local model of how {name} works. Copy no secrets, credentials, phone numbers, addresses or other people's names into your output; use role tokens {people_tokens}; do not use the network.
Return exactly one JSON object matching the schema given; no prose outside it.

# Role

Build {examples_min} to {examples_max} seed-to-full-prompt pairs for {name} from the pooled seed expansions below (each with the chunk it came from) and the excerpt of {name}'s profile (Standing rules and Unsaid defaults).

Output: one JSON object `{"examples_md": "..."}` holding the whole examples.md as a string.

# Selection

- Choose the most varied seeds across kinds of request: resume, status, bug report, UI pass, shipping, handoff to another agent, message to a person, planning, cleanup, research. Prefer the shortest seeds whose later turns revealed the most.
- Merge near-duplicate seeds into one pair. Keep the seed verbatim, typos kept.

# Rewriting each full prompt

Write it as {name} at their best: first person, direct, complete, in {name}'s priorities and vocabulary, with no typos. Cover, in this order and sized to the seed: goal and why; scope and what not to touch; quality bar; deliverables; follow-through; how to report. 60 to 300 words.

- Replace every moving fact with a role token or a neutral phrase: "the branch we agreed", "the staging host", "<teammate>'s module". No names, numbers, hostnames, budgets, scores or dates that would go stale.
- Where a message to a person is involved, the prompt drafts it for {name}'s approval before sending.
- Never write restraint {name} does not hold ("do not push", "work on a fresh branch", "keep every feature") unless a Standing rule in the excerpt says so.
- {forbidden_rules}

# Exact format

```
# Seed to full prompt: examples

These pairs are patterns, not scripts. Rebuild each full prompt from the current project's memory and conversation; copy the shape and the priorities, not the facts.

## <Theme>
### 1. <seed verbatim, typos kept>
Situation: one line.
Tags: kind, kind
> The full prompt, first person, clean, complete. One blockquote paragraph or several `>` lines.
```

- `### N.` numbered 1..N without gaps across the whole file. `Situation:` and `Tags:` lines required.
- `Tags:` 1 to 4 lowercase words drawn from the `When:` lines of the profile's Unsaid defaults groups (or the group letter). They are how the runtime matches a seed to its examples.

# Profile excerpt (Standing rules, Unsaid defaults)

{profile_excerpt}

# Pooled seed expansions

{seed_expansions_json}
