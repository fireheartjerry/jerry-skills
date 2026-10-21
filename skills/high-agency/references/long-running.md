# Long-running work: mechanics and handoff

Read this before launching anything in the background, anything that may exceed
the shell tool's foreground timeout, or before promising a later update. Tool
names are current as of late 2026; check your own tool list before relying on
one.

## The pattern

0. Smoke-run. Before a long, paid or unattended launch, run the same command
   with the same environment on a seconds-long input (a tiny dataset, one
   iteration, `--limit 1`) and read its output. A bad argument or missing
   dependency should die here, not at minute one of a two-hour run.
1. Detach. Run the job in the background with stdout and stderr going to a log
   file. Record the PID or task id, the log path and the exact command in the
   checklist.
2. Prove the start. Read the first lines of the log and confirm the process is
   alive. A launch command that returns is not a job that is running.
3. Watch with a mechanism that exits on failure as well as success: a
   background-task monitor, a polling loop with a done condition, a failure
   condition, a liveness check and a timeout, or a scheduled wake-up.
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
pgrep -f '[b]in/benchmark'        # bracket trick: never matches this shell's own argv
# Linux: pgrep -af '[b]in/benchmark' prints the full command line
```

## Harness map

### Claude Code

- Start: the Bash tool with `run_in_background: true`. The result carries a
  task id; the task re-invokes you when it exits, which also covers a silent
  death (SIGKILL, OOM). Read its output with the task output tool or from the
  log file you redirected to. Plain shell also works (launch block below).
- Watch: the `Monitor` tool with an until-loop that has all three exits:
  `until [ out/result.json -nt run.pid ] || grep -qE 'Traceback|ERROR' run.log 2>/dev/null || ! kill -0 "$(cat run.pid 2>/dev/null)" 2>/dev/null; do sleep 30; done`,
  plus the tool's timeout. It wakes you when the condition fires.
- Re-check on an interval across turns: `/loop 5m <command or slash command>`.
- Wake up later for a local job: the in-session scheduled task (cron) tool,
  `/loop`, or Monitor; or hand off. The `schedule` skill runs a cloud agent
  that cannot see a local PID, log or checklist, so use it only when the job's
  state is remotely reachable (a CI run, a cloud job, a pushed artifact).
- Stop a task: the task stop tool, or `kill "$(cat run.pid)"`.
- Headless (`claude -p`): the run ends with the turn and nothing watches
  afterwards. Hand off (template below).

### Codex

- Start: the launch block below in the shell tool. The shell tool kills a
  foreground command at its per-call timeout, so detach anything that is not
  quick.
- Watch: the poll loop template below, in bounded calls. Either raise the shell
  tool's timeout parameter above the poll bound (check your tool list for its
  name) or poll in short calls (a 5-minute deadline each) and re-issue. Do not
  rely on the `timeout` command: stock macOS does not ship it.
- Progress: `tail -n 20 run.log`; liveness: `kill -0 "$(cat run.pid)"`.
- Checklist: `update_plan`.
- There is no scheduled wake-up. A `codex exec` run ends with the turn. Hand off.

### Plain shell, any harness

- Detach: `nohup` as in the launch block (macOS and Linux); `setsid` in front
  on Linux when the parent shell may die.
- tmux instead: `tmux new-session -d -s job 'cmd 2>&1 | tee run.log'`, then
  `tmux list-panes -t job -F '#{pane_pid}' > run.pid` so the poll loop has a
  PID, or check liveness with `tmux has-session -t job`. Read with
  `tmux capture-pane -p -t job | tail -n 20`.

## Launch and poll templates (bash 3.2, BSD and GNU)

```bash
# launch
: > run.log
echo 'bin/benchmark --full' > run.cmd
nohup bin/benchmark --full > run.log 2>&1 &
echo $! > run.pid
sleep 2; head -n 5 run.log; kill -0 "$(cat run.pid)" && echo STARTED

# poll: four exits, none of them "hang forever"
deadline=$(( $(date +%s) + 1800 ))   # 30 minutes
while :; do
  if [ out/result.json -nt run.pid ]; then echo DONE; break; fi
  if grep -qE 'Traceback|ERROR' run.log 2>/dev/null; then echo FAILED; break; fi
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
output. The liveness exit catches a job killed by a signal or OOM, which writes
nothing to the log ("Killed" goes to the parent shell's stderr, not run.log).
The re-check inside the liveness branch closes the race where the job writes
the result and exits between two checks. `grep ... 2>/dev/null` keeps a
not-yet-created log from printing an error on every iteration. If the
filesystem has one-second mtime resolution and the job could finish inside the
launch second, move the old artifact aside before launching and test `-f`
instead.

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
