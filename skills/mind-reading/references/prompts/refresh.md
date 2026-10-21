{name}'s own private data, used only to build a local model of how {name} works. Copy no secrets, credentials, phone numbers, addresses or other people's names into your output; use role tokens {people_tokens}; do not use the network.
Return exactly one JSON object matching the schema given; no prose outside it.

Apply these corrections to {name}'s model with minimal edits. Today is {date} (MM-DD).

- For `kind: fix` and `kind: add`, change or add the one sentence that makes the model agree with what {name} meant, ending it with `(corrected {date})`.
- For `kind: drop`, delete the sentence that matches and nothing else.
- Do not apply `scope: project:` lines; list them under `notes`.
- Keep every heading, `When:` line, `Tags:` line and numbering intact. Change nothing the corrections do not name.
- {forbidden_rules}
- Return the three files whole, plus `notes` (strings: anything you could not apply and why).

Output: `{"profile_md": "...", "light_md": "...", "examples_md": "...", "notes": ["..."]}`.

<corrections>
{corrections_text}
</corrections>

<profile_md>
{profile_md}
</profile_md>

<light_md>
{light_md}
</light_md>

<examples_md>
{examples_md}
</examples_md>
