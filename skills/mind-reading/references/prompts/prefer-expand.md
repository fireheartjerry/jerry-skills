{privacy}
Return exactly one JSON object matching the schema given; no prose outside it.

You are the agent {name} is talking to, and {name}'s message below invoked the skill that follows. Follow the skill's instructions to write the reply it shows before acting: the Reading line, the full prompt with its tags, then any Changed and Held lines. Use only the context given here. Do not act on the prompt, run nothing, and do not mention this setup or the skill's own mechanics: return only the reply text.

<skill>
{skill_md}
</skill>

{slice_block}

{context_block}
{name} sent this at {time}:

<seed>
{seed}
</seed>

Output: one JSON object `{"reply": "..."}`.
