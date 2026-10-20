---
name: competitive-systems
description: Apply Codeforces 3000+ rating competitive-programming techniques to proven CI/CD, LLM-context, dependency-analysis, scheduling, and repeated-work bottlenecks. Use when an agent needs an asymptotic or algorithmic performance improvement with benchmarks and correctness invariants.
---

# Competitive Systems

“3000+” means the actual Codeforces rating threshold, not an algorithm count.

1. Measure the baseline and state input bounds, latency target, and correctness invariant.
2. Run `python tools/dev.py strategy --kind <kind>` to shortlist applicable techniques.
3. Read only the relevant file under `references/`; reject techniques whose preconditions are absent.
4. Implement the narrowest safe version, preserving fail-closed checks and public compatibility.
5. Add an oracle/property test where practical, then report before/after wall-clock evidence.

Never add advanced machinery solely for prestige. Do not modify invention-loop code while using this skill.

References:

- CI scheduling and coverage: `references/ci.md`
- LLM retrieval and context: `references/llm.md`
- Graph and data-structure patterns: `references/foundations.md`
