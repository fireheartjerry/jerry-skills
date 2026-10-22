"""Stub claude, codex and gemini CLIs for the deterministic test suite.

Behaviour is driven by directives embedded anywhere in the prompt, matched by
`@@name` or `@@name:value` (value has no whitespace; lists use commas):

  @@sleep:N        sleep N seconds before answering
  @@slow:N         emit N progress events, one every 0.5 s, before answering
  @@quiet-events:N emit N events the parent does not render (claude stream_event,
                   codex item.started only), one every 0.5 s, before answering
  @@exit:N         exit with code N after emitting whatever it emitted
                   (gemini: 42, 53 and 55 also suppress the result event, like the
                   documented input-error, turn-limit and untrusted-folder exits)
  @@noanswer       claude: no `result` event; codex: no agent_message and no -o file;
                   gemini: no `result` event
  @@hang           sleep 3600 (dies on SIGTERM)
  @@gate:PATH      wait (up to an hour) until PATH, relative to the cwd, exists, then go on
  @@hang-hard      ignore SIGTERM (from before init) and sleep 3600 (only SIGKILL works)
  @@big:N          answer is N bytes of deterministic text
  @@stderr:TEXT    write TEXT to stderr before answering (underscores become spaces)
  @@is-error       claude: result has is_error true, subtype error_during_execution, exit 1;
                   gemini: an `error` event, exit 1
  @@no-thread-event  codex: omit thread.started (rollout file still written)
  @@no-rollout     codex: write no rollout file either
  @@depth          answer is "depth=N name=X" from AGENT_SESSION_DEPTH / AGENT_SESSION_NAME
  @@spawn:A,B,C    run `agent-session spawn A B C` from inside the engine; answer with
                   its exit code, stdout and stderr (tests nesting)
  @@tokens:IN,OUT  usage counters in the terminal event (cached 0); default 10,5
  @@cost:X         claude: total_cost_usd (default 0.001)
  @@denied:N       claude: N system/permission_denied events and permission_denials in the result
  @@answer-file:PATH  the answer is the file's content; claude adds structured_output when
                   --json-schema was given and the content parses as JSON (unless @@no-structured)
  @@no-structured  claude: never add structured_output
  @@stall:N        emit the init event, then nothing for N seconds, then continue
  @@linger:N       emit the terminal event, then stay alive N seconds
  @@bg-task:N      claude: start a background task, emit a first result ("background task started"),
                   wait N seconds with no output, emit the task events, a new init segment and the
                   real result (cost cumulative, like claude -p)
  @@bg-exit        claude: with @@bg-task, exit 0 right after the first result, task still pending
  @@empty-lead:N   claude: right after init, an empty placeholder result (num_turns 0, result ""),
                   then N seconds of silence before the real turn (claude --resume does this)
  @@tool-calls:N   emit N tool calls quickly before the rest of the turn
  @@rate-limit     claude: two api_retry events (rate_limit, retry_delay_ms 1000), then an is_error
                   result "API Error: 429", exit 1; codex: turn.failed "... 429 Too Many Requests", exit 1;
                   gemini: an error event and stderr "429 RESOURCE_EXHAUSTED", exit 1
  @@usage-limit:N  codex: turn.failed "You've hit your usage limit. Try again in N seconds";
                   claude: an is_error result with the same text; gemini: an error event with it
  @@auth-fail      stderr "Not logged in" (codex) / "Invalid API key" (claude) / "Authentication failed"
                   (gemini), exit 1, no terminal event
  @@crash          exit 1 with no terminal event
  @@crash-after-edit  claude: apply @@edit directives, then exit 1 with no terminal event
  @@fail-once:CLASS  the first call carrying this prompt fails as CLASS (rate-limit, crash, stall,
                   crash-after-edit);
                   later calls succeed (counter in $FAKE_ENGINE_HOME/once/)
  @@edit:PATH=TEXT write TEXT (underscores become spaces) to PATH under the cwd; may repeat
  @@commit:MSG     git add -A and commit with message MSG in the cwd
  @@on-turn:K:<directive>   apply <directive> only when this is prompt number K of the conversation
  @@when:<suffix>:<directive>  apply <directive> only when AGENT_SESSION_NAME ends with <suffix>

`@@on-turn` and `@@when` are read from every prompt of the conversation, so a
directive planted in turn 1 can shape a later fixup turn. FAKE_ENGINE_DIRECTIVES
(environment) is read as if it were part of every prompt. Other directives
apply to the prompt that carries them. FAKE_ENGINE_ANSWER_FILE (environment)
acts like @@answer-file when no directive names one.

Default answer: "reply#<k>: <first 80 chars of the prompt>", k counting prompts in
the conversation so far (including this one). Conversations persist under
$FAKE_ENGINE_HOME/sessions/. Every invocation appends one line to
$FAKE_ENGINE_HOME/calls.jsonl, and every SIGINT or SIGTERM it receives one line
to $FAKE_ENGINE_HOME/signals.jsonl. `codex login status` exits 0 ("Logged in")
unless FAKE_ENGINE_CODEX_LOGGED_OUT is set. `gemini --help` lists the flags the
adapter uses, minus any named in FAKE_ENGINE_GEMINI_HELP_DROP.
"""

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid

HERE = os.path.dirname(os.path.realpath(__file__))
AGENT_SESSION = os.path.normpath(os.path.join(HERE, "..", "..", "scripts", "agent-session"))
HOME = os.environ.get("FAKE_ENGINE_HOME") or os.path.join(os.environ.get("TMPDIR", "/tmp"), "fake-engine")
DIRECTIVE = re.compile(r"@@([a-z-]+)(?::([^\s]+))?")
VERSION = {"claude": "9.9.9 (fake Claude Code)", "codex": "codex-cli 9.9.9-fake", "gemini": "0.9.9 (fake gemini)"}


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def err(text):
    sys.stderr.write(text + "\n")
    sys.stderr.flush()


MULTI = ("edit",)


def put(found, name, value):
    if name in MULTI:
        found.setdefault(name, []).append(value)
    else:
        found[name] = value


def directives(text):
    """Unconditional directives of one prompt (`edit` accumulates a list)."""
    found = {}
    for m in DIRECTIVE.finditer(text or ""):
        name = m.group(1)
        if name in ("on-turn", "when"):
            continue
        put(found, name, m.group(2) if m.group(2) is not None else True)
    return found


def conditional_directives(prompts, k):
    """@@on-turn:K:... and @@when:SUFFIX:... over the whole conversation."""
    found = {}
    me = os.environ.get("AGENT_SESSION_NAME") or ""
    for prompt in prompts:
        for m in DIRECTIVE.finditer(prompt or ""):
            name, value = m.group(1), m.group(2) or ""
            if name not in ("on-turn", "when"):
                continue
            cond, _, rest = value.partition(":")
            if name == "on-turn" and cond != str(k):
                continue
            if name == "when" and not me.endswith(cond):
                continue
            dname, _, dval = rest.partition(":")
            if dname:
                put(found, dname, dval if dval else True)
    return found


def all_directives(prompt, prompts):
    # FAKE_ENGINE_DIRECTIVES adds directives to every prompt (for children whose
    # prompt the test does not write, such as judges).
    extra = os.environ.get("FAKE_ENGINE_DIRECTIVES") or ""
    d = directives(prompt)
    for key, value in directives(extra).items():
        if key in MULTI:
            d.setdefault(key, []).extend(value)
        else:
            d.setdefault(key, value)
    for key, value in conditional_directives(prompts + [extra], len(prompts)).items():
        if key in MULTI:
            d.setdefault(key, []).extend(value)
        else:
            d[key] = value
    return fail_once(d, prompt)


def fail_once(d, prompt):
    """@@fail-once:CLASS turns into the CLASS directive on the first call only."""
    cls = d.get("fail-once")
    if not cls or cls is True:
        return d
    folder = os.path.join(HOME, "once")
    os.makedirs(folder, exist_ok=True)
    # A retried first turn may carry the runner's note about the failed attempt
    # in front of the original prompt; it is still the same prompt.
    key = prompt or ""
    if key.startswith("Note from the runner:") and "\n\n" in key:
        key = key.split("\n\n", 1)[1]
    marker = os.path.join(folder, hashlib.sha256(key.encode("utf-8")).hexdigest())
    if os.path.exists(marker):
        return d
    with open(marker, "w") as fh:
        fh.write("1\n")
    if cls == "stall":
        d["stall"] = "3600"
    else:
        d[cls] = True
    return d


def install_signal_log(d):
    def handler(signum, _frame):
        try:
            os.makedirs(HOME, exist_ok=True)
            with open(os.path.join(HOME, "signals.jsonl"), "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"signal": signal.Signals(signum).name, "pid": os.getpid(),
                                     "name": os.environ.get("AGENT_SESSION_NAME"), "ts": time.time()}) + "\n")
        except OSError:
            pass
        try:
            sys.stdout.flush()
        except (OSError, ValueError):
            pass
        os._exit(128 + signum)
    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)
    if "hang-hard" in d:
        # Before init, so a test that has seen init knows SIGTERM is ignored.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)


def apply_edits(d):
    for spec in d.get("edit") or []:
        if spec is True or "=" not in spec:
            continue
        rel, text = spec.split("=", 1)
        path = os.path.join(os.getcwd(), rel)
        os.makedirs(os.path.dirname(path) or os.getcwd(), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text.replace("_", " ") + "\n")
    msg = d.get("commit")
    if msg and msg is not True:
        base = ["git", "-c", "user.name=fake", "-c", "user.email=fake@example.com", "-c", "commit.gpgsign=false"]
        subprocess.run(base + ["add", "-A"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(base + ["commit", "-q", "-m", msg.replace("_", " ")], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)


def stall(d):
    if "stall" in d and d["stall"] is not True:
        time.sleep(float(d["stall"]))


def linger(d):
    if "linger" in d and d["linger"] is not True:
        time.sleep(float(d["linger"]))


def record(engine, argv, stdin, extra=None):
    os.makedirs(HOME, exist_ok=True)
    entry = {"engine": engine, "argv": argv, "stdin": stdin, "cwd": os.getcwd(), "pid": os.getpid(),
             "pgid": os.getpgid(0), "ts": time.time(),
             "env": {"AGENT_SESSION_DEPTH": os.environ.get("AGENT_SESSION_DEPTH"),
                     "AGENT_SESSION_NAME": os.environ.get("AGENT_SESSION_NAME"),
                     "AGENT_SESSION_STARTED": os.environ.get("AGENT_SESSION_STARTED"),
                     "FAKE_ENGINE_PROBE": os.environ.get("FAKE_ENGINE_PROBE"),
                     "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY"),
                     "HTTPS_PROXY": os.environ.get("HTTPS_PROXY"),
                     "AWS_PROFILE": os.environ.get("AWS_PROFILE"),
                     "UNRELATED_SECRET": os.environ.get("UNRELATED_SECRET"),
                     "LC_ALL": os.environ.get("LC_ALL")}}
    if extra:
        entry.update(extra)
    with open(os.path.join(HOME, "calls.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def conv_path(engine, cid):
    return os.path.join(HOME, "sessions", "%s-%s.json" % (engine, cid))


def load_conv(engine, cid):
    try:
        with open(conv_path(engine, cid), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def save_conv(engine, cid, prompts):
    os.makedirs(os.path.dirname(conv_path(engine, cid)), exist_ok=True)
    with open(conv_path(engine, cid), "w", encoding="utf-8") as fh:
        json.dump(prompts, fh)


def answer_file(d):
    path = d.get("answer-file") if d.get("answer-file") not in (None, True) else None
    return path or os.environ.get("FAKE_ENGINE_ANSWER_FILE") or None


def answer_for(prompt, d, k):
    path = answer_file(d)
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read()
        except OSError as exc:
            return "fake engine: cannot read answer file %s: %s" % (path, exc)
    if "big" in d:
        n = int(d["big"])
        return ("0123456789abcdef" * (n // 16 + 1))[:n]
    if "depth" in d:
        return "depth=%s name=%s" % (os.environ.get("AGENT_SESSION_DEPTH"), os.environ.get("AGENT_SESSION_NAME"))
    if "spawn" in d:
        args = [a for a in str(d["spawn"]).split(",") if a]
        cp = subprocess.run([sys.executable, AGENT_SESSION, "spawn"] + args, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, universal_newlines=True)
        return "spawn exit=%d stdout=%s stderr=%s" % (cp.returncode, cp.stdout.strip(), cp.stderr.strip())
    return "reply#%d: %s" % (k, prompt[:80])


def tokens_of(d):
    raw = d.get("tokens")
    if raw and raw is not True and "," in str(raw):
        a, b = str(raw).split(",", 1)
        return int(a), int(b)
    return 10, 5


def cost_of(d):
    raw = d.get("cost")
    if raw and raw is not True:
        return float(raw)
    return 0.001


def count_of(d, name):
    raw = d.get(name)
    if raw and raw is not True:
        return int(raw)
    return 0


def pre_answer(d):
    if "stderr" in d and d["stderr"] is not True:
        err(str(d["stderr"]).replace("_", " "))
    if "hang" in d or "hang-hard" in d:
        deadline = time.time() + 3600
        while time.time() < deadline:
            time.sleep(1)
        sys.exit(0)
    gate = d.get("gate")
    if gate and gate is not True:
        path = os.path.join(os.getcwd(), gate)
        deadline = time.time() + 3600
        while not os.path.exists(path) and time.time() < deadline:
            time.sleep(0.05)
    if "sleep" in d and d["sleep"] is not True:
        time.sleep(float(d["sleep"]))


def read_stdin():
    try:
        return sys.stdin.read()
    except (OSError, ValueError):
        return ""


def exit_code(d, default=0):
    if "exit" in d and d["exit"] is not True:
        return int(d["exit"])
    return default


# --------------------------------------------------------------------------- claude

def claude_main(argv):
    if argv == ["--version"] or argv == ["-v"]:
        print(VERSION["claude"])
        return 0
    opts = {"print": False, "format": "text", "verbose": False, "session_id": None, "resume": None,
            "model": None, "flags": [], "permission_mode": None, "tools": None, "json_schema": None,
            "max_budget_usd": None, "max_turns": None}
    positional = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-p", "--print"):
            opts["print"] = True
        elif a == "--output-format":
            i += 1
            opts["format"] = argv[i]
        elif a == "--verbose":
            opts["verbose"] = True
        elif a == "--session-id":
            i += 1
            opts["session_id"] = argv[i]
        elif a in ("--resume", "-r"):
            i += 1
            opts["resume"] = argv[i]
        elif a == "--model":
            i += 1
            opts["model"] = argv[i]
        elif a == "--permission-mode":
            i += 1
            opts["permission_mode"] = argv[i]
        elif a in ("--tools", "--allowedTools", "--allowed-tools", "--disallowedTools"):
            i += 1
            opts["tools"] = argv[i]
        elif a == "--json-schema":
            i += 1
            opts["json_schema"] = argv[i]
        elif a == "--max-budget-usd":
            i += 1
            opts["max_budget_usd"] = argv[i]
        elif a == "--max-turns":
            i += 1
            opts["max_turns"] = argv[i]
        elif a in ("--dangerously-skip-permissions", "--allow-dangerously-skip-permissions", "--strict-mcp-config"):
            opts["flags"].append(a)
        elif a.startswith("-"):
            err("fake claude: unknown option %s" % a)
            return 2
        else:
            positional = a
        i += 1
    stdin = None if positional is not None else read_stdin()
    prompt = positional if positional is not None else stdin
    record("claude", argv, stdin, {"positional": positional})

    if opts["json_schema"] is not None:
        try:
            json.loads(opts["json_schema"])
        except ValueError:
            err("Error: --json-schema must be valid JSON")
            return 1
    if opts["session_id"]:
        try:
            uuid.UUID(opts["session_id"])
        except ValueError:
            err("Invalid session ID. Must be a valid UUID.")
            return 1
        if load_conv("claude", opts["session_id"]) is not None:
            err("Session ID %s is already in use." % opts["session_id"])
            return 1
        cid = opts["session_id"]
    elif opts["resume"]:
        if load_conv("claude", opts["resume"]) is None:
            err("No conversation found with session ID: %s" % opts["resume"])
            return 1
        cid = opts["resume"]
    else:
        cid = str(uuid.uuid4())
    prompts = (load_conv("claude", cid) or []) + [prompt]
    save_conv("claude", cid, prompts)
    d = all_directives(prompt, prompts)
    install_signal_log(d)
    if "auth-fail" in d:
        err("Invalid API key · Please run /login")
        return 1
    stream = opts["format"] == "stream-json"
    if stream:
        emit({"type": "system", "subtype": "init", "session_id": cid, "model": opts["model"] or "default",
              "tools": [], "cwd": os.getcwd()})
    if stream and "empty-lead" in d:
        emit({"type": "result", "subtype": "success", "is_error": False, "result": "", "session_id": cid,
              "total_cost_usd": 0.0, "num_turns": 0, "duration_ms": 0, "usage": {"input_tokens": 0,
                                                                                  "output_tokens": 0}})
        if d["empty-lead"] is not True:
            time.sleep(float(d["empty-lead"]))
    stall(d)
    for k in range(count_of(d, "tool-calls")):
        if stream:
            emit({"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_t%d" % k, "name": "Bash", "input": {"command": "echo %d" % k}}]},
                  "session_id": cid})
    pre_answer(d)
    if "crash" in d:
        return 1
    if "crash-after-edit" in d:
        apply_edits(d)
        return 1
    if "rate-limit" in d or "usage-limit" in d:
        if "rate-limit" in d:
            text = "API Error: 429 {\"type\":\"error\",\"error\":{\"type\":\"rate_limit_error\"}}"
            for attempt in (1, 2):
                if stream:
                    emit({"type": "system", "subtype": "api_retry", "attempt": attempt, "max_retries": 2,
                          "retry_delay_ms": 1000, "error_status": 429, "error": "rate_limit", "session_id": cid})
        else:
            text = "You've hit your usage limit. Try again in %s seconds" % d["usage-limit"]
        if stream:
            emit({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": text,
                  "session_id": cid, "total_cost_usd": 0.0, "num_turns": 0, "duration_ms": 5,
                  "usage": {"input_tokens": 0, "output_tokens": 0}})
        return 1
    apply_edits(d)
    answer = answer_for(prompt, d, len(prompts))
    is_error = "is-error" in d
    tok_in, tok_out = tokens_of(d)
    denied = count_of(d, "denied")
    result = {"type": "result", "subtype": "error_during_execution" if is_error else "success",
              "is_error": is_error, "result": answer, "session_id": cid, "total_cost_usd": cost_of(d),
              "num_turns": 1, "duration_ms": 5,
              "usage": {"input_tokens": tok_in, "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0, "output_tokens": tok_out}}
    if denied:
        result["permission_denials"] = [{"tool_name": "Read", "tool_use_id": "toolu_d%d" % k,
                                         "tool_input": {"file_path": "/tmp/denied-%d.txt" % k}} for k in range(denied)]
    if opts["json_schema"] is not None and "no-structured" not in d:
        try:
            parsed = json.loads(answer)
        except ValueError:
            parsed = None
        if isinstance(parsed, (dict, list)):
            result["structured_output"] = parsed
    if stream:
        slow = int(d["slow"]) if "slow" in d and d["slow"] is not True else 0
        for k in range(slow):
            emit({"type": "assistant", "message": {"id": "msg_slow%d" % k, "role": "assistant",
                                                   "usage": {"input_tokens": 10, "output_tokens": 5},
                                                   "content": [{"type": "text", "text": "working %d/%d" % (k + 1, slow)}]},
                  "session_id": cid})
            time.sleep(0.5)
        quiet = int(d["quiet-events"]) if "quiet-events" in d and d["quiet-events"] is not True else 0
        for k in range(quiet):
            emit({"type": "stream_event", "event": {"type": "content_block_delta", "index": k}, "session_id": cid})
            time.sleep(0.5)
        emit({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "true"}}]}, "session_id": cid})
        emit({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": [{"type": "text", "text": ""}]}]}, "session_id": cid})
        for k in range(denied):
            emit({"type": "system", "subtype": "permission_denied", "session_id": cid, "tool_name": "Read",
                  "tool_use_id": "toolu_d%d" % k, "tool_input": {"file_path": "/tmp/denied-%d.txt" % k}})
        if "bg-task" in d:
            task = {"task_id": "bfake1", "task_type": "local_bash", "description": "fake background sleep"}
            emit({"type": "system", "subtype": "background_tasks_changed", "tasks": [task], "session_id": cid})
            emit({"type": "system", "subtype": "task_started", "task_id": "bfake1",
                  "description": task["description"], "session_id": cid})
            first = dict(result, result="background task started", total_cost_usd=round(cost_of(d) / 2, 6))
            emit(first)
            if "bg-exit" in d:
                return 0
            if d["bg-task"] is not True:
                time.sleep(float(d["bg-task"]))
            emit({"type": "system", "subtype": "task_updated", "task_id": "bfake1",
                  "patch": {"status": "completed"}, "session_id": cid})
            emit({"type": "system", "subtype": "task_notification", "task_id": "bfake1", "status": "completed",
                  "summary": "Background command completed (exit code 0)", "session_id": cid})
            emit({"type": "system", "subtype": "background_tasks_changed", "tasks": [], "session_id": cid})
            emit({"type": "system", "subtype": "init", "session_id": cid, "model": opts["model"] or "default",
                  "tools": [], "cwd": os.getcwd()})
            emit({"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "the background task finished"}]}, "session_id": cid})
        if "noanswer" not in d:
            emit(result)
    elif opts["format"] == "json":
        if "noanswer" not in d:
            emit(result)
    else:
        if "noanswer" not in d:
            sys.stdout.write(answer + "\n")
            sys.stdout.flush()
    linger(d)
    return exit_code(d, 1 if is_error else 0)


# --------------------------------------------------------------------------- codex

def inside_git_repo(path):
    while True:
        if os.path.exists(os.path.join(path, ".git")):
            return True
        parent = os.path.dirname(path)
        if parent == path:
            return False
        path = parent


def write_rollout(cid, cwd):
    home = os.environ.get("CODEX_HOME")
    if not home:
        return
    now = time.localtime()
    folder = os.path.join(home, "sessions", time.strftime("%Y", now), time.strftime("%m", now), time.strftime("%d", now))
    os.makedirs(folder, exist_ok=True)
    stamp = time.strftime("%Y-%m-%dT%H-%M-%S", now)
    path = os.path.join(folder, "rollout-%s-%s.jsonl" % (stamp, cid))
    iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".000Z"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"timestamp": iso, "type": "session_meta",
                             "payload": {"id": cid, "cwd": cwd, "timestamp": iso, "originator": "fake_codex"}}) + "\n")
        fh.write(json.dumps({"timestamp": iso, "type": "turn_context", "payload": {"cwd": cwd}}) + "\n")


def codex_main(argv):
    if argv == ["--version"] or argv == ["-V"]:
        print(VERSION["codex"])
        return 0
    if argv[:2] == ["login", "status"]:
        if os.environ.get("FAKE_ENGINE_CODEX_LOGGED_OUT"):
            err("Not logged in")
            return 1
        print("Logged in using ChatGPT")
        return 0
    if not argv or argv[0] != "exec":
        err("fake codex: only `exec` and `login status` are supported")
        return 2
    rest = argv[1:]
    resume = False
    if rest and rest[0] == "resume":
        resume = True
        rest = rest[1:]
    opts = {"json": False, "out": None, "model": None, "skip_git": False, "bypass": False,
            "sandbox": None, "last": False, "config": [], "output_schema": None}
    session = None
    positional = None
    i = 0
    while i < len(rest):
        a = rest[i]
        if a == "--json":
            opts["json"] = True
        elif a in ("-o", "--output-last-message"):
            i += 1
            opts["out"] = rest[i]
        elif a in ("-m", "--model"):
            i += 1
            opts["model"] = rest[i]
        elif a == "--output-schema":
            i += 1
            opts["output_schema"] = rest[i]
        elif a == "--skip-git-repo-check":
            opts["skip_git"] = True
        elif a == "--dangerously-bypass-approvals-and-sandbox":
            opts["bypass"] = True
        elif a in ("-s", "--sandbox"):
            if resume:
                # Mirrors codex-cli 0.162: `exec resume` has no --sandbox option.
                err("error: unexpected argument '%s' found\n\n  tip: to pass '%s' as a value, use '-- %s'" % (a, a, a))
                return 2
            i += 1
            opts["sandbox"] = rest[i]
        elif a in ("-c", "--config"):
            i += 1
            opts["config"].append(rest[i])
            key, _, value = rest[i].partition("=")
            if key.strip() == "sandbox_mode":
                opts["sandbox"] = value.strip().strip('"')
        elif a == "--last":
            opts["last"] = True
        elif a.startswith("-") and a != "-":
            err("fake codex: unknown option %s" % a)
            return 2
        elif resume and session is None and a != "-":
            session = a
        else:
            positional = a
        i += 1
    stdin = read_stdin() if positional in (None, "-") else None
    prompt = stdin if positional in (None, "-") else positional
    record("codex", argv, stdin, {"positional": positional})
    err("codex banner: model=%s, sandbox=%s" % (opts["model"] or "default",
                                                "danger-full-access" if opts["bypass"] else (opts["sandbox"] or "workspace-write")))
    if opts["output_schema"] and not os.path.exists(opts["output_schema"]):
        err("error: output schema file not found: %s" % opts["output_schema"])
        return 1
    if resume and opts["last"]:
        err("fake codex: --last is forbidden")
        return 1
    if not opts["skip_git"] and not inside_git_repo(os.getcwd()):
        err("Not inside a trusted directory and --skip-git-repo-check was not specified.")
        return 1
    if resume:
        if not session or load_conv("codex", session) is None:
            err("no session found: %s" % session)
            return 1
        cid = session
    else:
        cid = str(uuid.uuid4())
    prompts = (load_conv("codex", cid) or []) + [prompt]
    save_conv("codex", cid, prompts)
    d = all_directives(prompt, prompts)
    install_signal_log(d)
    if "no-rollout" not in d:
        write_rollout(cid, os.getcwd())
    if "auth-fail" in d:
        err("Not logged in. Run `codex login` first.")
        return 1
    if opts["json"]:
        if "no-thread-event" not in d:
            emit({"type": "thread.started", "thread_id": cid})
        emit({"type": "turn.started"})
    stall(d)
    for k in range(count_of(d, "tool-calls")):
        if opts["json"]:
            emit({"type": "item.completed", "item": {"id": "tc_%d" % k, "type": "command_execution",
                                                      "command": "echo %d" % k, "aggregated_output": "",
                                                      "exit_code": 0, "status": "completed"}})
    pre_answer(d)
    if "crash" in d:
        return 1
    if "rate-limit" in d or "usage-limit" in d:
        if "rate-limit" in d:
            text = "exceeded retry limit, last status: 429 Too Many Requests"
        else:
            text = "You've hit your usage limit. Try again in %s seconds" % d["usage-limit"]
        if opts["json"]:
            emit({"type": "error", "message": text})
            emit({"type": "turn.failed", "error": {"message": text}})
        err(text)
        return 1
    apply_edits(d)
    answer = answer_for(prompt, d, len(prompts))
    tok_in, tok_out = tokens_of(d)
    if opts["json"]:
        slow = int(d["slow"]) if "slow" in d and d["slow"] is not True else 0
        for k in range(slow):
            emit({"type": "item.started", "item": {"id": "item_%d" % k, "type": "command_execution",
                                                    "command": "echo %d" % k, "status": "in_progress"}})
            emit({"type": "item.completed", "item": {"id": "item_%d" % k, "type": "command_execution",
                                                      "command": "echo %d" % k, "aggregated_output": "%d\n" % k,
                                                      "exit_code": 0, "status": "completed"}})
            time.sleep(0.5)
        quiet = int(d["quiet-events"]) if "quiet-events" in d and d["quiet-events"] is not True else 0
        for k in range(quiet):
            emit({"type": "item.started", "item": {"id": "long_%d" % k, "type": "command_execution",
                                                    "command": "make test", "status": "in_progress"}})
            time.sleep(0.5)
        if "noanswer" not in d:
            emit({"type": "item.completed", "item": {"id": "item_msg", "type": "agent_message", "text": answer}})
        emit({"type": "turn.completed", "usage": {"input_tokens": tok_in, "cached_input_tokens": 0,
                                                 "output_tokens": tok_out}})
    elif "noanswer" not in d:
        sys.stdout.write(answer + "\n")
        sys.stdout.flush()
    if opts["out"] and "noanswer" not in d:
        with open(opts["out"], "w", encoding="utf-8") as fh:
            fh.write(answer)
    err("tokens used: %d" % (tok_in + tok_out))
    linger(d)
    return exit_code(d, 0)


# --------------------------------------------------------------------------- gemini

GEMINI_EXITS = {42: "Error: invalid input (fake gemini exit 42)",
                53: "Error: reached the maximum number of session turns (fake gemini exit 53)",
                55: "Error: this folder is not trusted; run gemini interactively to trust it (fake gemini exit 55)"}


GEMINI_HELP_FLAGS = ("--output-format", "--approval-mode", "--skip-trust", "--model", "--resume", "--prompt")


def gemini_main(argv):
    if argv == ["--version"] or argv == ["-v"]:
        print(VERSION["gemini"])
        return 0
    if argv == ["--help"]:
        # FAKE_ENGINE_GEMINI_HELP_DROP=--skip-trust,... leaves flags out, like an older CLI.
        drop = set(os.environ.get("FAKE_ENGINE_GEMINI_HELP_DROP", "").split(","))
        print("Usage: gemini [options]\n")
        for flag in GEMINI_HELP_FLAGS:
            if flag not in drop:
                print("  %s" % flag)
        return 0
    opts = {"format": "text", "approval": None, "skip_trust": False, "model": None, "resume": None, "prompt": None}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--output-format":
            i += 1
            opts["format"] = argv[i]
        elif a == "--approval-mode":
            i += 1
            opts["approval"] = argv[i]
        elif a == "--skip-trust":
            opts["skip_trust"] = True
        elif a in ("--model", "-m"):
            i += 1
            opts["model"] = argv[i]
        elif a == "--resume":
            i += 1
            opts["resume"] = argv[i]
        elif a in ("-p", "--prompt"):
            i += 1
            opts["prompt"] = argv[i]
        elif a.startswith("-"):
            err("fake gemini: unknown option %s" % a)
            return 2
        else:
            opts["prompt"] = a
        i += 1
    stdin = read_stdin() if opts["prompt"] is None else None
    prompt = opts["prompt"] if opts["prompt"] is not None else stdin
    record("gemini", argv, stdin, {"positional": opts["prompt"]})
    if opts["resume"]:
        if load_conv("gemini", opts["resume"]) is None:
            err("Error: no session found with id %s" % opts["resume"])
            return 1
        cid = opts["resume"]
    else:
        cid = str(uuid.uuid4())
    prompts = (load_conv("gemini", cid) or []) + [prompt]
    save_conv("gemini", cid, prompts)
    d = all_directives(prompt, prompts)
    install_signal_log(d)
    if "auth-fail" in d:
        err("Authentication failed: please set an auth method")
        return 1
    rc = exit_code(d, 1 if "is-error" in d else 0)
    tok_in, tok_out = tokens_of(d)
    stream = opts["format"] == "stream-json"
    if stream:
        emit({"type": "init", "session_id": cid, "model": opts["model"] or "default",
              "approval_mode": opts["approval"] or "default"})
    stall(d)
    for k in range(count_of(d, "tool-calls")):
        if stream:
            emit({"type": "tool_use", "tool_name": "run_shell_command", "tool_id": "tc%d" % k,
                  "parameters": {"command": "echo %d" % k}})
    pre_answer(d)
    if "crash" in d:
        return 1
    if "rate-limit" in d or "usage-limit" in d:
        text = ("429 RESOURCE_EXHAUSTED" if "rate-limit" in d
                else "You've hit your usage limit. Try again in %s seconds" % d["usage-limit"])
        if stream:
            emit({"type": "error", "message": text})
        err(text)
        return 1
    if rc in GEMINI_EXITS:
        err(GEMINI_EXITS[rc])
        return rc
    apply_edits(d)
    answer = answer_for(prompt, d, len(prompts))
    if stream:
        slow = int(d["slow"]) if "slow" in d and d["slow"] is not True else 0
        for k in range(slow):
            emit({"type": "message", "role": "assistant", "content": "working %d/%d" % (k + 1, slow)})
            time.sleep(0.5)
        emit({"type": "tool_use", "tool_name": "run_shell_command", "tool_id": "t1", "parameters": {"command": "true"}})
        emit({"type": "tool_result", "tool_name": "run_shell_command", "tool_id": "t1", "status": "success", "output": ""})
        if "is-error" in d:
            emit({"type": "error", "message": "fake gemini error"})
        elif "noanswer" not in d:
            emit({"type": "result", "status": "success", "response": answer,
                  "stats": {"input_tokens": tok_in, "output_tokens": tok_out, "cached": 0,
                            "total_tokens": tok_in + tok_out, "tool_calls": 1, "duration_ms": 5}})
    elif "noanswer" not in d:
        sys.stdout.write(answer + "\n")
        sys.stdout.flush()
    linger(d)
    return rc


def main(engine):
    argv = sys.argv[1:]
    if engine == "claude":
        return claude_main(argv)
    if engine == "gemini":
        return gemini_main(argv)
    return codex_main(argv)
