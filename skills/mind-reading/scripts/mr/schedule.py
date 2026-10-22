"""schedule install|uninstall|status: run `refresh --auto` on a timer.

macOS: a per-user LaunchAgent at ~/Library/LaunchAgents/<label>.plist (label from config `schedule.label`, default
local.mind-reading.refresh) that runs this CLI with `python3 -I ... --home <home> refresh --auto` weekly (config
`schedule.weekday`, 0 = Sunday, `hour`, `minute`; `--daily` for every day), at low priority, with stdout and stderr
appended to <home>/logs/schedule.log. PATH carries the directories of the engine CLIs found at install time (claude,
codex, node), because launchd starts jobs with a minimal PATH. The job does nothing but the refresh; the only network
it reaches is the engine CLI's own. `install` loads it with `launchctl bootstrap`, `uninstall` unloads and deletes it.

Linux and elsewhere: nothing is installed; `install` and `status` print the crontab line to add by hand."""
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from .config import save_config

DEFAULT_PATH = "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def agents_dir():
    return Path(os.environ.get("MIND_READING_LAUNCH_AGENTS_DIR") or os.path.expanduser("~/Library/LaunchAgents"))


def _scfg(cfg, weekday=None, hour=None, minute=None, daily=None, label=None):
    s = dict(cfg.get("schedule") or {})
    for k, v in (("weekday", weekday), ("hour", hour), ("minute", minute), ("daily", daily), ("label", label)):
        if v is not None:
            s[k] = v
    s["weekday"] = int(s.get("weekday", 0)) % 7
    s["hour"] = max(0, min(23, int(s.get("hour", 9))))
    s["minute"] = max(0, min(59, int(s.get("minute", 0))))
    s["daily"] = bool(s.get("daily", False))
    s["label"] = str(s.get("label") or "local.mind-reading.refresh")
    return s


def program(paths):
    script = Path(__file__).resolve().parent.parent / "mind_reading.py"
    return [sys.executable, "-I", str(script), "--home", str(paths.home), "refresh", "--auto"]


def env_path():
    dirs = []
    for tool in ("claude", "codex", "node"):
        found = shutil.which(tool)
        if found:
            d = str(Path(found).resolve().parent)
            for cand in (str(Path(found).parent), d):
                if cand not in dirs:
                    dirs.append(cand)
    for d in DEFAULT_PATH.split(":"):
        if d not in dirs:
            dirs.append(d)
    return ":".join(dirs)


def plist(paths, s):
    log = str(paths.logs / "schedule.log")
    interval = {"Hour": s["hour"], "Minute": s["minute"]}
    if not s["daily"]:
        interval["Weekday"] = s["weekday"]
    return {
        "Label": s["label"],
        "ProgramArguments": program(paths),
        "StartCalendarInterval": interval,
        "StandardOutPath": log,
        "StandardErrorPath": log,
        "EnvironmentVariables": {"PATH": env_path(), "MIND_READING_HOME": str(paths.home)},
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 10,
        "RunAtLoad": False,
    }


def cron_line(paths, s):
    days = "*" if s["daily"] else str(s["weekday"])
    cmd = " ".join(_q(a) for a in program(paths))
    return "%d %d * * %s %s >> %s 2>&1" % (s["minute"], s["hour"], days, cmd, _q(str(paths.logs / "schedule.log")))


def _q(a):
    return a if all(c.isalnum() or c in "/._-~:=+" for c in a) else "'" + a.replace("'", "'\\''") + "'"


def _platform(override=None):
    return override or ("darwin" if sys.platform == "darwin" else "linux")


def _launchctl(args):
    if os.environ.get("MIND_READING_NO_LAUNCHCTL"):
        return 0, "launchctl skipped (MIND_READING_NO_LAUNCHCTL)"
    try:
        p = subprocess.run(["launchctl"] + args, capture_output=True, text=True, timeout=30)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)


def _domain():
    return "gui/%d" % os.getuid()


def describe(s):
    when = "daily" if s["daily"] else "weekly on %s" % ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"][s["weekday"]]
    return "%s at %02d:%02d local time" % (when, s["hour"], s["minute"])


def install(paths, cfg, weekday=None, hour=None, minute=None, daily=None, label=None, dry_run=False, platform=None, log=print):
    s = _scfg(cfg, weekday, hour, minute, daily, label)
    plat = _platform(platform)
    if plat != "darwin":
        line = cron_line(paths, s)
        log("schedule: not macOS, nothing installed. Add this line with `crontab -e` (%s):\n%s" % (describe(s), line))
        return {"platform": plat, "installed": False, "cron": line, "schedule": s}
    data = plist(paths, s)
    target = agents_dir() / (s["label"] + ".plist")
    body = plistlib.dumps(data).decode("utf-8")
    if dry_run:
        log("schedule: would write %s (%s) and load it with launchctl bootstrap %s:\n%s" % (target, describe(s), _domain(), body))
        return {"platform": plat, "installed": False, "plist": str(target), "body": body, "schedule": s}
    paths.logs.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(paths.logs, 0o700)
    except OSError:
        pass
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    _launchctl(["bootout", "%s/%s" % (_domain(), s["label"])])
    code, out = _launchctl(["bootstrap", _domain(), str(target)])
    cfg["schedule"] = s
    if cfg.get("_exists"):
        save_config(paths, cfg)
    if code != 0:
        log("schedule: wrote %s but launchctl bootstrap failed (%s); load it with: launchctl bootstrap %s %s" % (target, out, _domain(), target))
    else:
        log("schedule: installed %s, %s; logs in %s" % (target, describe(s), paths.logs / "schedule.log"))
    return {"platform": plat, "installed": code == 0, "plist": str(target), "schedule": s, "launchctl": out}


def uninstall(paths, cfg, label=None, platform=None, log=print):
    s = _scfg(cfg, label=label)
    plat = _platform(platform)
    if plat != "darwin":
        log("schedule: not macOS; remove the mind-reading line with `crontab -e`")
        return {"platform": plat, "removed": False}
    target = agents_dir() / (s["label"] + ".plist")
    _launchctl(["bootout", "%s/%s" % (_domain(), s["label"])])
    existed = target.exists()
    if existed:
        target.unlink()
    log("schedule: %s" % ("removed %s" % target if existed else "nothing installed at %s" % target))
    return {"platform": plat, "removed": existed, "plist": str(target)}


def status(paths, cfg, platform=None):
    s = _scfg(cfg)
    plat = _platform(platform)
    out = {"platform": plat, "schedule": s, "describe": describe(s)}
    if plat != "darwin":
        out.update(installed=None, cron=cron_line(paths, s))
        return out
    target = agents_dir() / (s["label"] + ".plist")
    out["plist"] = str(target)
    out["installed"] = target.exists()
    if target.exists():
        code, _ = _launchctl(["print", "%s/%s" % (_domain(), s["label"])])
        out["loaded"] = code == 0
        try:
            data = plistlib.loads(target.read_bytes())
            out["program"] = data.get("ProgramArguments")
            out["home_matches"] = str(paths.home) in (data.get("ProgramArguments") or [])
        except Exception:  # an unreadable plist is reported, not raised
            out["program"] = None
    return out


def format_status_line(st):
    if st.get("platform") != "darwin":
        return "schedule: cron (not installed by this tool): %s" % st.get("cron")
    if not st.get("installed"):
        return "schedule: not installed (schedule install runs refresh --auto %s)" % st.get("describe")
    return "schedule: %s, %s%s" % (st["plist"], st["describe"], "" if st.get("loaded", True) else " (written but not loaded)")
