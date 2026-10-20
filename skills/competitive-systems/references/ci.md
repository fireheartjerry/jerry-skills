# CI/CD Patterns

Use weighted coverage when optional tests compete under a fixed runtime budget. Keep direct mappings and sensitive-file fallbacks mandatory.

Use branch-and-bound only for a small optional set; use deterministic greedy marginal gain when the search space is large.

Use LPT scheduling to reduce makespan on one machine. Serialize heavy lanes unless measured capacity proves safe parallelism.
