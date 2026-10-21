"""devkit: the measurement and repository toolkit behind scripts/dev.py.

Every module is standard-library Python 3.8+. `stats` holds pure functions, `measure` the
timing engine and the evidence commands, `bound` the arithmetic bounds, `common` shared
helpers (repository root, git facts, changed files, globs, tools/dev.json, errors,
formatting). Repository intelligence: `detect` (languages, tools, command resolution),
`graph` (import graphs and the card algorithms), `affected` (fail-closed test selection),
`checks` (check, lint, typecheck, test with history records).
"""
VERSION = "3.0.0"
