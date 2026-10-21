"""Stub claude and codex CLIs for the deterministic test suite.

Behaviour is driven by directives embedded anywhere in the prompt, matched by
`@@name` or `@@name:value` (value has no whitespace; lists use commas):

  @@sleep:N        sleep N seconds before answering
  @@slow:N         emit N progress events, one every 0.5 s, before answering
  @@quiet-events:N emit N events the parent does not render (claude stream_event,
                   codex item.started only), one every 0.5 s, before answering
  @@exit:N         exit with code N after emitting whatever it emitted
  @@noanswer       claude: no `result` event; codex: no agent_message and no -o file
  @@hang           sleep 3600 (dies on SIGTERM)
  @@hang-hard      ignore SIGTERM, sleep 3600 (only SIGKILL works)
  @@big:N          answer is N bytes of deterministic text
  @@stderr:TEXT    write TEXT to stderr before answering (underscores become spaces)
  @@is-error       claude: result has is_error true, subtype error_during_execution, exit 1
  @@no-thread-event  codex: omit thread.started (rollout file still written)
  @@no-rollout     codex: write no rollout file either
  @@depth          answer is "depth=N name=X" from AGENT_SESSION_DEPTH / AGENT_SESSION_NAME
  @@spawn:A,B,C    run `agent-session spawn A B C` from inside the engine; answer with
                   its exit code, stdout and stderr (tests nesting)

Default answer: "reply#<k>: <first 80 chars of the prompt>", k counting prompts in
the conversation so far (including this one). Conversations persist under
$FAKE_ENGINE_HOME/sessions/. Every invocation appends one line to
$FAKE_ENGINE_HOME/calls.jsonl.
"""

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
VERSION = {"claude": "9.9.9 (fake Claude Code)", "codex": "codex-cli 9.9.9-fake"}


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def err(text):
    sys.stderr.write(text + "\n")
    sys.stderr.flush()


def directives(text):
    found = {}
    for m in DIRECTIVE.finditer(text or ""):
        found[m.group(1)] = m.group(2) if m.group(2) is not None else True
    return found


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


def answer_for(prompt, d, k):
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


def pre_answer(d):
    if "stderr" in d and d["stderr"] is not True:
        err(str(d["stderr"]).replace("_", " "))
    if "hang-hard" in d:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    if "hang" in d or "hang-hard" in d:
        deadline = time.time() + 3600
        while time.time() < deadline:
            time.sleep(1)
        sys.exit(0)
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
            "model": None, "flags": [], "permission_mode": None, "tools": None}
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
    d = directives(prompt)
    pre_answer(d)
    answer = answer_for(prompt, d, len(prompts))
    is_error = "is-error" in d
    result = {"type": "result", "subtype": "error_during_execution" if is_error else "success",
              "is_error": is_error, "result": answer, "session_id": cid, "total_cost_usd": 0.001,
              "num_turns": 1, "duration_ms": 5}
    if opts["format"] == "stream-json":
        emit({"type": "system", "subtype": "init", "session_id": cid, "model": opts["model"] or "default",
              "tools": [], "cwd": os.getcwd()})
        slow = int(d["slow"]) if "slow" in d and d["slow"] is not True else 0
        for k in range(slow):
            emit({"type": "assistant", "message": {"role": "assistant",
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
        if "noanswer" not in d:
            emit(result)
    elif opts["format"] == "json":
        if "noanswer" not in d:
            emit(result)
    else:
        if "noanswer" not in d:
            sys.stdout.write(answer + "\n")
            sys.stdout.flush()
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
    if not argv or argv[0] != "exec":
        err("fake codex: only `exec` is supported")
        return 2
    rest = argv[1:]
    resume = False
    if rest and rest[0] == "resume":
        resume = True
        rest = rest[1:]
    opts = {"json": False, "out": None, "model": None, "skip_git": False, "bypass": False,
            "sandbox": None, "last": False, "config": []}
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
    d = directives(prompt)
    if "no-rollout" not in d:
        write_rollout(cid, os.getcwd())
    pre_answer(d)
    answer = answer_for(prompt, d, len(prompts))
    if opts["json"]:
        if "no-thread-event" not in d:
            emit({"type": "thread.started", "thread_id": cid})
        emit({"type": "turn.started"})
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
        emit({"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5}})
    elif "noanswer" not in d:
        sys.stdout.write(answer + "\n")
        sys.stdout.flush()
    if opts["out"] and "noanswer" not in d:
        with open(opts["out"], "w", encoding="utf-8") as fh:
            fh.write(answer)
    err("tokens used: 15")
    return exit_code(d, 0)


def main(engine):
    argv = sys.argv[1:]
    if engine == "claude":
        return claude_main(argv)
    return codex_main(argv)
