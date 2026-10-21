{name}'s own private data, used only to build a local model of how {name} works. Copy no secrets, credentials, phone numbers, addresses or other people's names into your output; use role tokens {people_tokens}; do not use the network.
Return exactly one JSON object matching the schema given; no prose outside it.

# Role

Write the always-on file for {name} from the profile below. It is loaded into every interactive session, so it must be short and must only contain what changes behaviour on most prompts.

Output: one JSON object `{"light_md": "..."}`. Write only the six sections; the CLI prepends the fixed title and Scope paragraph (4 lines including the blank lines). Your body is at most {light_body_max} lines.

# Required sections, in this order, each a `## ` heading followed by terse imperative bullets

## Read the prompts
## Default behaviour
## Quality bar
## Report
## Standing rules
## Never

# Rules

- Terse imperatives. No evidence, no quotes, no dates, no names, no project facts, no strength tags.
- Pick only what changes behaviour on most prompts: how to read {name}'s shorthand, the defaults {name} never states, the quality bar, how to report, the hard rules.
- Use {name} by name, never a pronoun.
- {forbidden_rules}
- Never write restraint {name} does not hold. If the profile's Standing rules say finished work is pushed, do not write "ask before pushing".
- Never write an authorization. Permissions {name} granted in advance live in grants.md, which the runtime reads beside this file; here write how {name} works, not what the agent may do without asking.

# Profile

{profile_md}
