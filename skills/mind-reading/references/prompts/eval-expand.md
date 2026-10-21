{name}'s own private data, used only to build a local model of how {name} works. Copy no secrets, credentials, phone numbers, addresses or other people's names into your output; use role tokens {people_tokens}; do not use the network.
Return exactly one JSON object matching the schema given; no prose outside it.

{context_block}
{persona_line} sent this short prompt to a coding agent at {time}:

<seed>
{seed}
</seed>

Write the full prompt {subject} would have sent with unlimited time and stamina: first person, complete (goal and why, scope and what not to touch, quality bar, deliverables, follow-through, how to report), 120 to 300 words, no preamble. {context_note}

Output: one JSON object `{"prompt": "..."}`.

{arm_context}
