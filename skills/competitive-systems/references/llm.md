# LLM Context Patterns

Retrieve exact symbols and paths first. Then expand only a bounded dependency/caller neighborhood. Keep direct targets, verification commands, and hard boundaries even when the packet is over budget.

Use sparse term scoring for deterministic offline retrieval; use graph expansion to recover related modules without loading the full repository map.
