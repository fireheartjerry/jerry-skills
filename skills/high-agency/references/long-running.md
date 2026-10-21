# Long-running work: mechanics and handoff

Read this before launching anything in the background, anything that may exceed
the shell tool's foreground timeout, or before promising a later update.

## The pattern

0. Smoke-run. Before a long, paid or unattended launch, run the same command
   with the same environment on a seconds-long input (a tiny dataset, one
   iteration, `--limit 1`) and read its output. A bad argument or missing
   dependency should die here, not at minute one of a two-hour run. Run it
   once with a bad argument too, and add the job's failure prefix to
   `fail_re`, anchored with `^`, regex characters escaped.
1. Detach. Run the job in the background with stdout and stderr going to a log
   file. Record the PID or task id, the log path and the exact command in the
   checklist.
2. Prove the start. Read the first lines of the log and confirm the process is
   alive. A launch command that returns is not a job that is running.
3. Watch with a mechanism that exits on failure as well as success: a
   condition monitor, a polling loop with a done condition, a failure
   condition, a liveness check and a timeout, or a scheduled wake-up. The
   table below says which of these your harness has.
4. Do the next independent step while it runs.
5. Verify the artifact, not the exit code: it exists, is newer than the launch,
   is non-empty, parses, has the expected keys or rows, and the numbers are
   plausible against the last known result.

Before starting anything, check whether the same job is already running and
reuse it. Trust the recorded PID only after confirming its identity: a PID can
be reused by an unrelated process after a reboot or a long gap.

```bash
pid=$(cat run.pid 2>/dev/null)
if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
  ps -p "$pid" -o command=        # compare with the command recorded in run.cmd
fi
# Bracketing the first character keeps pgrep from matching this shell's own argv.
pgrep -f '[b]in/benchmark'                                      # job
# Linux: pgrep -af '[b]in/benchmark' prints the full command line
```

## What each harness offers

Checked against Claude Code 2.1.306 and Codex CLI 0.162.1 on 2026-10-21. If a
name below is missing from your tool list, use the plain-shell column.

| Capability | Claude Code | Codex | Plain shell |
|---|---|---|---|
| Checklist | the todo or task tool | `update_plan` | the plan file (SKILL.md section 3) |
| Detached launch that reports its exit | Bash with `run_in_background: true`; the task re-invokes you when it exits, which also covers a silent death (SIGKILL, OOM) | launch block below | launch block below |
| Condition monitor (wakes you when a predicate holds) | `Monitor` with the until-loop below, plus the tool's timeout | none; poll loop in bounded calls | poll loop |
| Interval re-check across turns | `/loop <interval> <command or slash command>` | none | none |
| Scheduled wake-up for a local job | the in-session scheduled task (cron) tool | none | none |
| Stop a job | the task stop tool, or `kill "$(cat run.pid)"` | `kill "$(cat run.pid)"` | same |
| Headless run (`claude -p`, `codex exec`) | ends with the turn; hand off | ends with the turn; hand off | n/a |

Notes:

- A cloud scheduler (the `schedule` skill, a routine) cannot see a local PID,
  log or checklist. Use it only when the job's state is remotely reachable: a
  CI run, a cloud job, a pushed artifact.
- The Codex shell tool kills a foreground command at its per-call timeout, so
  detach anything that is not quick. To poll, either raise the tool's timeout
  parameter above the poll bound (check your tool list for its name) or poll
  in short calls (a 5-minute deadline each) and re-issue. Progress:
  `tail -n 20 run.log`; liveness: `kill -0 "$(cat run.pid)"`.
- Do not rely on the `timeout` command: stock macOS does not ship it. The
  `deadline` in the poll loop does the same job portably.
- Plain shell detach: `nohup` as in the launch block (macOS and Linux);
  `setsid` in front on Linux when the parent shell may die. tmux instead:
  `tmux new-session -d -s job 'cmd 2>&1 | tee run.log'`, then
  `tmux list-panes -t job -F '#{pane_pid}' > run.pid` so the poll loop has a
  PID, or check liveness with `tmux has-session -t job`. Read with
  `tmux capture-pane -p -t job | tail -n 20`.

Condition-monitor until-loop, one line, with all three exits (done, failed,
dead). When the monitor runs it in a fresh shell, prefix the `fail_re=` line
from the poll block; without it the loop stops with "set fail_re first"
rather than matching every line:

```bash
until [ out/result.json -nt run.pid ] || grep -qE "${fail_re:?set fail_re first}" run.log 2>/dev/null || ! kill -0 "$(cat run.pid 2>/dev/null)" 2>/dev/null; do sleep 30; done
```

## Launch and poll templates (bash 3.2, BSD and GNU)

```bash
# launch
: > run.log
echo 'bin/benchmark --full' > run.cmd                           # job
nohup bin/benchmark --full > run.log 2>&1 &                     # job
echo $! > run.pid
sleep 2; head -n 5 run.log; kill -0 "$(cat run.pid)" && echo STARTED
```

```bash
# Failure signature: what this job prints when it fails. Learn it during the
# smoke run (run once with a bad argument). Anchor it so a count such as
# "0 ERRORS" never matches. A job that dies without logging is caught by the
# liveness exit, not by this grep.
fail_re='Traceback \(most recent call last\)|^(ERROR|FATAL|CRITICAL)\b|^Error:'

# poll: four exits, none of them "hang forever"
deadline=$(( $(date +%s) + 1800 ))   # 30 minutes
while :; do
  if [ out/result.json -nt run.pid ]; then echo DONE; break; fi
  if grep -qE "${fail_re:?set fail_re first}" run.log 2>/dev/null; then echo FAILED; break; fi
  if ! kill -0 "$(cat run.pid 2>/dev/null)" 2>/dev/null; then
    if [ out/result.json -nt run.pid ]; then echo DONE; else echo EXITED_NO_RESULT; fi
    break
  fi
  if [ "$(date +%s)" -ge "$deadline" ]; then echo TIMEOUT; break; fi
  sleep 30
done
```

Why each line is there: `-nt run.pid` ignores an artifact left over from an
earlier run (or the smoke run), since only a file written after this launch is
newer than run.pid; a result-only loop otherwise ends immediately on stale
output. `fail_re` is anchored with `^` and `\b`, so a benign count such as
`found 0 ERRORS` never trips it, and it only catches failures the job logs; a
job that dies silently is caught by the liveness exit instead. That exit covers
a job killed by a signal or OOM, which writes nothing to the log ("Killed" goes
to the parent shell's stderr, not run.log). The re-check inside the liveness
branch closes the race where the job writes the result and exits between two
checks. `grep ... 2>/dev/null` keeps a not-yet-created log from printing an
error on every iteration. If the filesystem has one-second mtime resolution
and the job could finish inside the launch second, move the old artifact aside
before launching and test `-f` instead.

## Handoff template (when nothing can watch)

```
Running: <command> (PID <n> or task <id>), started <time>
Log: <path>
Check: <exact command>
Done when: <condition>      Failed when: <signs in the log or exit>
Expected finish: <ETA if measured, else "unknown">
Next action when done: <what to do with the result>
Nothing is watching this run; the check above is manual.
```

## Do not

- Say "I'll let you know when it finishes" without a mechanism in place.
- Start a second copy because the first looks slow. Read the log.
- Report a job as done because the wrapper exited 0. Open the artifact.
- Leave the job id only in your context. Put it in the checklist.
