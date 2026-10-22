{privacy}
Return exactly one JSON object matching the schema given; no prose outside it.

{name} uses a skill that expands a short prompt (the seed) into a full brief and then acts on it. Each item below is one such use: the seed, the head of the agent's reply, and the next message {name} typed in the same session. Classify the next message by what it says about the expansion:

- `correction`: it redirects or undoes what the agent took the seed to mean: "no", "wait", "actually", "I meant", "not that", "why did you", asking to revert, or a new instruction that contradicts the reply.
- `refinement`: it accepts the reading and adds a requirement the expansion missed ("also", "make sure", "and the tests too").
- `approval`: it accepts or continues ("yes", "go", "keep going", answers to the agent's held questions, praise).
- `unrelated`: it moves to other work or says nothing about the expansion.

Judge only from the text shown. When the message could be read two ways, choose the milder class (refinement over correction, unrelated over refinement). Use every id exactly once.

Output: one JSON object `{"items": [{"id": "...", "class": "correction|refinement|approval|unrelated"}]}`.

<items>
{items_json}
</items>
