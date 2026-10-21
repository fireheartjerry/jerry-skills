{name}'s own private data, used only to build a local model of how {name} works. Copy no secrets, credentials, phone numbers, addresses or other people's names into your output; use role tokens {people_tokens}; do not use the network.
Return exactly one JSON object matching the schema given; no prose outside it.

# Role

Write {name}'s full model from the consolidated mining results below: {chunk_count} chunks of {name}'s own messages to coding agents, spanning {date_range}. The model lets an agent take a short seed prompt from {name} and produce the full prompt {name} would have written with unlimited time and stamina.

Output: one JSON object `{"profile_md": "..."}` holding the whole profile.md as a string.

# Merge rules

- A pattern seen in many chunks and recently outweighs a one-off. Strength tags by chunk count: `[strong]` seen in three or more chunks, or in two with a recent one; `[moderate]` two chunks, or one long recent run; `[weak]` one chunk.
- Where a preference changed, the later one wins and the line carries `(changed MM-DD)` from `preference_changes`.
- Evidence drawn from `drafted`, `pasted`, `question_answer` or `goal_reminder` messages is marked `[endorsed]` after the quote. A trait supported only by endorsed evidence arrives with `"endorsed": true` and `"strength": "weak"`: it carries `[weak]` and `[endorsed]` on the trait line and is never raised above weak, however many chunks repeat it. Typed evidence carries nothing extra. Endorsed text is never evidence of voice: no vocabulary entry and no communication-style trait comes from it.
- Each trait and default lists the mining `lenses` that reported it inside its chunk and a `count` already merged across them; `chunk` is the chunk id. Weigh by distinct chunks, not by lenses: two lenses reporting the same item in one chunk are one observation.
- Dates are MM-DD. Quotes verbatim, typos kept, under 200 characters, one or two per item. Never a date from an unreliable session (those arrive as null).

# Exclusions

- Project facts never enter this file: budgets, scores, numbers, branch names, hostnames, instance types, deadlines, who owns what, file paths. They live in project memory. Items with `scope: project` were already removed; drop any that slipped through.
- Third parties appear only as role tokens ({people_tokens}). No names, handles, emails, phone numbers, keys or absolute paths.
- {forbidden_rules}
  If the mining shows {name} said such things, record it under `## 5. How {name} communicates` or `## 8. Dislikes` in this form: "gets frustrated by repeated permission prompts; expects the agent to batch questions and work on unblocked items meanwhile", never as a standing rule to act on.
- Authorizations are not profile content. Anything {name} explicitly granted in advance ("you don't need to ask before X") is kept in a separate file, grants.md, built from the verbatim quotes by the pipeline. Leave the substance of such a grant out of this file entirely: no restatement of what may be done without asking, in any tense or wording ("pushed without a round trip", "merges on their own"). If the frustration behind it is a pattern, record only the fixed preference sentence named above.

# Required structure (exact headings, in this order)

```
# {name}: the full model

Reading notes
- Strength: [strong] seen in three or more chunks, or two with a recent one; [moderate] two chunks, or one long recent run; [weak] one chunk.
- [endorsed] marks evidence {name} accepted, pasted or answered rather than typed; a trait resting only on it stays [weak].
- Dates are MM-DD (year in MODEL.json). Quotes verbatim, typos kept. Later preference wins; changes carry (changed MM-DD).
- Project facts (budgets, scores, branch names, owners, deadlines) live in project memory, not here.
- The harness's permission and safety rules beat every line here.

## 1. How {name} thinks
- **Short label.** One or two sentences. [strong] "quote" (MM-DD); "quote" (MM-DD)
## 2. Quality bar
## 3. How {name} builds
## 4. Design taste
## 5. How {name} communicates
## 6. Standing rules
## 7. Likes
## 8. Dislikes
## 9. Workflow habits
## 10. Recurring contexts

## Unsaid defaults
### A. <Title: kind of request>
When: keyword, keyword, keyword
- default sentence. "first" -> "later" (MM-DD)
### B. ...

## Vocabulary
- **phrase / variant**: meaning.
```

Rules for the structure:
- Exactly the headings above: numbered layers 1 to 10, then `## Unsaid defaults`, then `## Vocabulary`. No other `## ` headings.
- Every `###` group under Unsaid defaults has a `When:` line with 3 to 12 lowercase keywords or short phrases, comma separated, that a seed would contain. Good keywords are the words {name} actually types for that kind of request: "keep going", "fix", "status", "push", "design", "ugly", "deploy", "message". Bad keywords are abstract nouns a seed never contains.
- Group A is the generic build-or-fix group and comes first. Order the rest by how often the kind of request appears.
- Bullets start with `- `. Each layer item ends with its strength tag before the quotes. No line over 600 characters. Between {synth_min_lines} and {synth_max_lines} lines in total.
- Vocabulary entries use `- **phrase / variant**: meaning.` Variants separated by ` / `.

{prior_block}

# Consolidated mining results

{consolidated_json}
