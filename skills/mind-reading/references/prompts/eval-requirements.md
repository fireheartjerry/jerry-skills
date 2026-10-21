{name}'s own private data, used only to build a local model of how {name} works. Copy no secrets, credentials, phone numbers, addresses or other people's names into your output; use role tokens {people_tokens}; do not use the network.
Return exactly one JSON object matching the schema given; no prose outside it.

Rubric version 3.

{context_block}
Below is a short prompt a user sent at {time}, then the user's later messages in the same session, in order. List the distinct requirements, preferences and intentions the user expressed later {requirement_scope}. Skip pure reactions ("thanks", "ok") unless they reveal a preference. Merge duplicates. One item per line of thought, 1 to 30 items, each with an integer `id` starting at 1 and a `kind` from:

- `goal`: what the task is for, or a change of goal.
- `scope`: what is in or out, what not to touch, how far to go.
- `constraint`: a limit the work must respect (a rule, a boundary, a way of working the user insists on).
- `quality`: the bar the result must meet, verification, evidence.
- `deliverable`: a concrete output and where it lands.
- `follow_through`: what should happen after the task without being asked (return to standing work, keep going, next item).
- `reporting`: how and when to report.
- `preference`: a taste or habit the user reveals (style, tone, tooling, order of work).
- `task_fact`: a project or task detail the user supplied later (a path, a name, a number, a choice between options) that no model of the user could have predicted. Keep these separate so the report can show them apart from what a model of the user should predict.

Each `text` under 200 characters, written as a neutral statement of what the user wanted.

Output: `{"requirements": [{"id": 1, "text": "...", "kind": "..."}]}`.

<seed>
{seed}
</seed>

<later_messages>
{later_json}
</later_messages>
