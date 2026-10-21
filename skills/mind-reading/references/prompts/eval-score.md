{name}'s own private data, used only to build a local model of how {name} works. Copy no secrets, credentials, phone numbers, addresses or other people's names into your output; use role tokens {people_tokens}; do not use the network.
Return exactly one JSON object matching the schema given; no prose outside it.

Rubric version 3.

Below are the requirements a user expressed after sending a short prompt, the user's later messages they were drawn from, and ONE candidate expansion of that prompt written before those messages existed. For each requirement decide:

- `full`: the expansion states it or an equivalent.
- `partial`: the expansion gestures at it but misses a material part.
- `none`: absent.

Be strict: generic good practice that happens to overlap is `partial` at most. Then list anything in the expansion that the requirements or the user's later messages contradict (at most 10), each tied to a requirement id when one applies, else null.

`items` must contain exactly one entry per requirement id, no more and no fewer.

Output: `{"items": [{"id": 1, "verdict": "full"}], "contradictions": [{"text": "...", "requirement_id": 3}]}`.

<requirements>
{requirements_json}
</requirements>

<later_messages>
{later_json}
</later_messages>

<expansion>
{expansion}
</expansion>
