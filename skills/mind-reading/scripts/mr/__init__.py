"""mind-reading pipeline package. Stdlib only, python3 -I safe.

Modules:
  config      config.json load/save, defaults, path resolution
  filters     skip-prefix list, tag stripping, origin classification
  scrub       secret/PII scrub
  extract_*   transcript readers (Claude Code, Codex)
  corpus      append-only corpus, manifest, keys
  prep        normalize, dedup, twin detection, split, chunk
  schema      minimal JSON Schema validator
  engine      claude / codex / print / fake runners
  mine, synthesize, select, evaluate, refresh, status, lint
"""

__version__ = "2.0.0"
