# Long-running work: mechanics and handoff

Read this before launching anything in the background, anything that may exceed
the shell tool's foreground timeout, or before promising a later update.

## The pattern

0. Smoke-run. Before a long, paid or unattended launch, run the same command
   with the same environment on a seconds-long input (a tiny dataset, one
   iteration, `--limit 1`) and read its output. A bad argument or missing
   dependency should die here, not at minute one of a two-hour run. Run it
   once with a bad argument too, and add the job's failure prefix to
   `fail_re`, anchored with `^`, regex characters escaped (the variable is
   set in the poll block below). For paid compute, project the full run's
   cost from the smoke run's.
1. Detach. Move any artifact from an earlier run aside, then run the job in
   the background with stdout and stderr going to a log file. Record the PID
   or task id, the log path and the exact command in the checklist (and in the
   plan file, since a checklist does not outlive the session).
2. Prove the start. Read the first lines of the log and confirm the process is
   alive. A launch command that returns is not a job that is running.
3. Watch with something that exits on failure as well as success (a
   condition monitor, the poll block, a scheduled wake-up); the table below
   says which your harness has.
4. Do the next independent step while it runs.
5. Verify the artifact, not the exit code: it exists, was written by this run,
   is non-empty, parses, has the expected keys or rows, and the numbers are
   plausible against the last known result.

In the blocks below, lines marked `# job` hold the example command and lines
marked `# artifact` the example output file (`out/result.json`); replace both
with your own.

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
| Detached launch that reports its exit | the launch block's first three lines, then its `sh -c` line (without `nohup` and `&`) in Bash with `run_in_background: true`; the task re-invokes you when it exits, also after a silent death (SIGKILL, OOM) | launch block below | launch block below |
| Condition monitor (wakes you when a predicate holds) | `Monitor` running the poll block, its deadline under the tool's timeout; the one line it prints names the exit | none; poll loop in bounded calls | poll loop |
| Interval re-check across turns | `/loop <interval> <command or slash command>` | none | none |
| Scheduled wake-up for a local job | the in-session scheduled task (cron) tool | none | none |
| Notify the human | the push-notification or send-file tool | none; the final message | none |
| Stop a job | the task stop tool, or the stop block below | the stop block below | same |
| Headless run (`claude -p`, `codex exec`) | ends with the turn; hand off | ends with the turn; hand off | n/a |

Notes:

- A cloud scheduler (the `schedule` skill, a routine) cannot see a local PID,
  log or checklist. Use it only when the job's state is remotely reachable: a
  CI run, a cloud job, a pushed artifact.
- The Claude Code shell tool stops a foreground command at its timeout (120 s
  by default, 600 s at most) and refuses a bare `sleep`. Run the poll block
  through `Monitor` or `run_in_background`, not in the foreground.
- The Codex shell tool kills a foreground command at its per-call timeout, so
  detach anything that is not quick. To poll, either raise the tool's timeout
  parameter above the poll bound (check your tool list for its name) or poll
  in short calls (a 5-minute deadline each) and re-issue. Progress:
  `tail -n 20 run.log`; liveness: `kill -0 "$(cat run.pid)"`.
- TIMEOUT means the watcher gave up, not the job: leave the job running and
  write the handoff below.
- Do not rely on the `timeout` command: stock macOS does not ship it; the
  poll loop's `deadline` does that job.
- Plain shell detach: `nohup` as in the launch block; on Linux, `setsid` in
  front survives the parent shell and gives the job its own process group,
  so `kill -- -"$(cat run.pid)"` stops every descendant. tmux instead:
  `tmux new-session -d -s job 'cmd 2>&1 | tee run.log'`, then
  `tmux list-panes -t job -F '#{pane_pid}' > run.pid`; read with
  `tmux capture-pane -p -t job | tail -n 20`.

## Launch, poll and stop templates (bash 3.2, BSD and GNU)

```bash
# launch
mkdir -p out                                                          # artifact
[ ! -e out/result.json ] || mv out/result.json "out/result.json.prev.$(date +%s)"   # artifact
: > run.log; rm -f run.pid; echo 'bin/benchmark --full' > run.cmd    # job
nohup sh -c 'echo $$ > run.pid; exec bin/benchmark --full' > run.log 2>&1 &   # job
sleep 2; head -n 5 run.log
if kill -0 "$(cat run.pid 2>/dev/null)" 2>/dev/null; then echo STARTED; else echo NOT_RUNNING; fi
```

```bash
# Failure signature, learned in the smoke run; anchored so "0 ERRORS" never matches.
fail_re='Traceback \(most recent call last\)|^(ERROR|FATAL|CRITICAL)\b|^Error:'

# poll: prints exactly one line, never hangs
if [ -z "${fail_re:-}" ]; then echo NO_FAIL_RE
elif [ ! -s run.pid ]; then echo NO_PID
elif ! { echo | grep -E "$fail_re" >/dev/null 2>&1; [ $? -le 1 ]; }; then echo BAD_FAIL_RE
else
  deadline=$(( $(date +%s) + 1800 ))   # 30 minutes
  while :; do
    if [ -s out/result.json ]; then echo DONE; break; fi                # artifact
    if [ -f run.log ] && grep -qE "$fail_re" run.log; then echo FAILED; break; fi
    if ! kill -0 "$(cat run.pid)" 2>/dev/null; then
      if [ -s out/result.json ]; then echo DONE; else echo EXITED_NO_RESULT; fi   # artifact
      break
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then echo TIMEOUT; break; fi
    sleep 30
  done
fi
```

```bash
# stop: the job's children first, then the job
pkill -P "$(cat run.pid)"; kill "$(cat run.pid)"
```

Why each line is there: moving the old artifact aside means a non-empty
artifact is this run's; an mtime test such as `-nt run.pid` fails here,
because bash 3.2 and BSD `test` compare whole seconds. The job writes its own
PID before `exec`, so run.pid exists on every launch path. The guards print a
named line instead of reading a missing run.pid as a dead job, or an unset or
malformed `fail_re` as no failure (grep exits 2 on a bad regex). `fail_re` is
anchored, so `found 0 ERRORS` never trips it; a job that dies without logging
(a signal, OOM) is caught by the liveness exit, whose re-check closes the race
where the job writes the result and exits between two checks. A plain `kill`
leaves the job's children running, still able to write the artifact.

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
