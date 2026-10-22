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
  track       the correction tracker (real invocations, the next typed message classified)
  prefer      blind A/B pages and their scoring (the primary human metric)
  auto        refresh --auto (capped, gated, unattended) and refresh --rollback
  incremental pinned chunks, so a refresh mines only new material
  schedule    the LaunchAgent or cron line that runs refresh --auto
  stats, budget  Wilson intervals and the sign test; per-call cost estimates under a cap
"""

__version__ = "2.0.0"
