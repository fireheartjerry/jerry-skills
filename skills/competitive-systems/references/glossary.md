# Glossary

Terms used in SKILL.md, the cards and the report. In answers to the user, say what the term
means in plain words; the terms themselves belong in the report block.

- precondition: the fact that must be true for a card to apply (e.g. "equality is decided by
  a hashable canonical key"). Checked, not assumed.
- oracle: the obviously-correct slow version used to check the fast one.
- property test: run oracle and fast version on many seeded random inputs and assert equality.
- fail-closed: when the fast path cannot prove it is safe (unmapped file, incomplete graph,
  missing measurement), fall back to the full, slow, safe path.
- direct mapping: a test linked to a changed file by the dependency graph (import, build
  target, or an explicit mapping) rather than by a heuristic.
- sensitive file: a file whose change invalidates the mapping itself (lockfiles, build and CI
  config, dependency manifests, shared fixtures, code generators); changing one forces the
  full run.
- heavy job: a job that needs a large share of a host's resources, so it is pinned to one
  worker or run alone; pinning caps the gain from scheduling.
- makespan: wall time until the last job finishes. worker: a runner, machine, or process slot
  that runs one job at a time; m is the number of workers.
- must-keep set (LLM context): the items kept in the prompt even over budget: direct targets
  (files and symbols named in the task), verification commands (how to run and test), hard
  boundaries (constraints the user stated).
- noise floor: how much two runs of the same unchanged command differ, measured as the
  half-width of the A/A ratio confidence interval.
- minimum effect: the smallest before/after ratio treated as real: the larger of the user's
  threshold and twice the noise floor (`dev.py aa` saves twice the noise floor, at least 2%;
  a larger threshold goes to `dev.py compare --min-effect`, which cannot go below the saved
  floor of the same command).
- A/A test: the same command timed as two interleaved arms; any "difference" it shows is
  noise, so it sets the noise floor before any A/B claim.
- change point: an index in a series of measurements (per commit, per night) where the level
  shifts and stays shifted, as opposed to a single outlier.
- Amdahl cap: the most an end-to-end time can improve when only a share of it gets faster:
  1/(1 - share). Editing a 12% share caps the gain at 1.14x however fast that part becomes.
  The share is measured at a size; a superlinear step's share, and so its cap, grows with n.
  (`bound amdahl --p` takes the share; `p=` in a verdict line is a p-value.)
- reference implementation: the old code, kept callable next to the new code so the oracle
  can compare them on the same inputs.
- differential test: an oracle that runs the reference and the new implementation on the same
  seeded random and edge inputs and asserts equal results.
- metamorphic test: a check of a relation that must hold between outputs when the input is
  transformed (shuffled input gives the same set; running twice changes nothing), useful when
  no reference exists.
- tier: what a technique costs to bring in. free: a standard-library structure (dict, set,
  sort, heap, prefix sums, bisect), allowed even when Gate 2 says stop, with the oracle and no
  speedup claim; cheap: a short known algorithm (union-find, topological sort, BFS); expensive:
  a new dependency or a large algorithm (Aho-Corasick, SCC, bitsets, LSH, big knapsack DP,
  exact subset search). Gate 2's stop refuses cheap and expensive.
- LB (lower bound) for scheduling: no plan on m workers can beat max(total / m, the longest
  job, the total of jobs pinned to one worker); the largest term "binds".
- LPT (longest processing time first): give each job, longest first, to the worker that is
  free soonest; within (4/3 - 1/(3m)) of optimal.
- SCC (strongly connected component): a set of nodes that all reach each other; collapsing
  each one makes a graph acyclic.
- LSH (locality-sensitive hashing): hashing that puts similar items in the same bucket with
  high probability, for near-duplicate search.
- BM25: a standard keyword relevance score that weighs rare words more and long chunks less.
- p50, IQR, CV, MAD: the median; the 25th to 75th percentile range; standard deviation over
  the mean, in percent; the median absolute deviation from the median.
- runs and n: `runs` counts timed repetitions of a command; n is the input size.
