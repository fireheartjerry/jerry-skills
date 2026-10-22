{name}'s own private data, used only to build a local model of how {name} works. Copy no secrets, credentials, phone numbers, addresses or other people's names into your output; use role tokens {people_tokens}; do not use the network.
Return exactly one JSON object matching the schema given; no prose outside it.

Rubric version 4.

Below are the short prompt a user sent, the requirements the user expressed after sending it, the user's later messages they were drawn from, and ONE candidate expansion of that prompt written before those messages existed. For each requirement decide:

- `full`: the expansion states it or an equivalent.
- `partial`: the expansion gestures at it but misses a material part.
- `none`: absent.

Be strict: generic good practice that happens to overlap is `partial` at most. Then list anything in the expansion that the requirements or the user's later messages contradict (at most 10), each tied to a requirement id when one applies, else null.

Then judge the expansion's upgrades: places where it chose a different approach than the one the short prompt literally asked for (or its obvious default), for the same goal.

- `upgrades`: such a change that the user's later messages show the user moved to (asked for it, corrected toward it, praised it, or adopted it). At most 10, each tied to a requirement id when one applies, else null.
- `wrong_upgrades`: such a change that overrode something the user wanted: the later messages insist on, return to or restore the approach the prompt asked for. At most 10, same form.
- An expansion that merely adds detail the prompt left open is neither. When the later messages say nothing either way, list nothing.

`items` must contain exactly one entry per requirement id, no more and no fewer.

Output: `{"items": [{"id": 1, "verdict": "full"}], "contradictions": [{"text": "...", "requirement_id": 3}], "upgrades": [{"text": "...", "requirement_id": 2}], "wrong_upgrades": []}`.

<prompt>
{seed}
</prompt>

<requirements>
{requirements_json}
</requirements>

<later_messages>
{later_json}
</later_messages>

<expansion>
{expansion}
</expansion>
