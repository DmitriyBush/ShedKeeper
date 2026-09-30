"""install.py -- put the hooks of the kit into Claude Code settings, take them out, check them.

  python install.py install   --project DIR | --user
  python install.py uninstall --project DIR | --user
  python install.py check     [--project DIR | --user]

`--project DIR` edits DIR\\.claude\\settings.json, `--user` edits ~\\.claude\\settings.json. Before any
change the settings file is copied next to itself (settings.json.shedkeeper-backup-<time>). The merge only
ADDS the entries of the kit and never touches anyone else's; installing twice does not duplicate;
uninstall removes ONLY the entries of the kit. A settings file that is not valid JSON is never
touched. The timeout of both hooks is 60 seconds: by the hook contract a timeout lets the action
through, so it must be longer than the slowest normal run. The 60 s are a budget, not a guess: the
pre_tool hook spends at most cmd_deadline_s (20 s by default) on copies and at most 10 s on git calls
(two of 5 s), which leaves 30 s of margin for the interpreter start and one slow copy.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time

KIT = os.path.dirname(os.path.abspath(__file__))
TIMEOUT_S = 60
PRE_MATCHER = "Edit|Write|NotebookEdit|MultiEdit|Bash|PowerShell"
# (event, matcher or None, script)
ENTRIES = [("PreToolUse", PRE_MATCHER, "pre_tool.py"),
           ("SessionStart", None, "session.py"),
           ("SessionEnd", None, "session.py")]


def _fwd(s):
    return s.replace("\\", "/").lower()


def hook_command(script):
    return 'python "%s"' % os.path.join(KIT, "hooks", script)


def is_ours(command, script=None):
    """A hook command of THIS kit: it ENDS with the full path of a script of the kit's hooks folder, in
    quotes or not. A substring test would also take `...pre_tool.py.backup` or a wrapper of someone
    else's (found by an independent review)."""
    if not isinstance(command, str):
        return False
    c = os.path.normcase(_fwd(command.strip()))
    names = [script] if script else ["pre_tool.py", "session.py"]
    for n in names:
        p = os.path.normcase(_fwd(os.path.join(KIT, "hooks", n)))
        if c.endswith('"%s"' % p) or c.endswith(" " + p) or c == p:
            return True
    return False


def user_home():
    return os.environ.get("SHEDKEEPER_USER_HOME") or os.path.expanduser("~")


def settings_path(project=None, user=False):
    if user:
        return os.path.join(user_home(), ".claude", "settings.json")
    return os.path.join(os.path.abspath(project), ".claude", "settings.json")


def _load(path):
    """(dict or None, error). A missing file is an empty settings object."""
    if not os.path.exists(path):
        return {}, None
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as e:
        return None, "%s is not readable as JSON (%s) -- not touched" % (path, e)
    if not isinstance(data, dict):
        return None, "%s does not hold a JSON object -- not touched" % path
    return data, None


def _save(path, data, existed):
    if existed:
        backup = "%s.shedkeeper-backup-%s" % (path, time.strftime("%Y%m%d-%H%M%S"))
        n = 0
        while os.path.exists(backup):
            n += 1
            backup = "%s.shedkeeper-backup-%s-%d" % (path, time.strftime("%Y%m%d-%H%M%S"), n)
        shutil.copy2(path, backup)
    else:
        backup = None
        os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.shedkeeper-tmp-%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)
    return backup


def _groups(data, event, create):
    hooks = data.get("hooks")
    if hooks is None:
        if not create:
            return None
        hooks = data["hooks"] = {}
    if not isinstance(hooks, dict):
        raise ValueError('"hooks" in the settings is not an object')
    groups = hooks.get(event)
    if groups is None:
        if not create:
            return None
        groups = hooks[event] = []
    if not isinstance(groups, list):
        raise ValueError('"hooks.%s" in the settings is not a list' % event)
    return groups


def _iter_ours(groups, script):
    for g in groups:
        if isinstance(g, dict) and isinstance(g.get("hooks"), list):
            for h in g["hooks"]:
                if isinstance(h, dict) and is_ours(h.get("command"), script):
                    yield g, h


def install(path):
    data, err = _load(path)
    if err:
        return 1, [err]
    existed = os.path.exists(path)
    out, changed = [], False
    try:
        for event, matcher, script in ENTRIES:
            groups = _groups(data, event, create=True)
            mine = list(_iter_ours(groups, script))
            if mine:
                for g, h in mine:
                    touched = False
                    if h.get("timeout") != TIMEOUT_S or h.get("type") != "command":
                        h["type"], h["timeout"] = "command", TIMEOUT_S
                        changed = touched = True
                        out.append("%s/%s: entry updated (timeout %d)" % (event, script, TIMEOUT_S))
                    if matcher and g.get("matcher") != matcher:
                        if all(is_ours(x.get("command")) for x in g["hooks"] if isinstance(x, dict)):
                            g["matcher"] = matcher
                            changed = touched = True
                            out.append("%s/%s: matcher updated (%s)" % (event, script, matcher))
                        else:
                            out.append("%s/%s: the matcher of a group shared with other hooks differs from "
                                       "%s and was NOT changed" % (event, script, matcher))
                    if not touched:
                        out.append("%s/%s: already installed" % (event, script))
                continue
            group = {"hooks": [{"type": "command", "command": hook_command(script), "timeout": TIMEOUT_S}]}
            if matcher:
                group = dict([("matcher", matcher)] + list(group.items()))
            groups.append(group)
            changed = True
            out.append("%s/%s: added" % (event, script))
    except ValueError as e:
        return 1, ["%s -- not touched" % e]
    if changed:
        backup = _save(path, data, existed)
        out.append("settings written: %s" % path)
        if backup:
            out.append("backup of the old settings: %s" % backup)
    else:
        out.append("nothing to change: %s" % path)
    return 0, out


def uninstall(path):
    if not os.path.exists(path):
        return 0, ["no settings file: %s -- nothing to remove" % path]
    data, err = _load(path)
    if err:
        return 1, [err]
    out, changed = [], False
    try:
        for event in ("PreToolUse", "SessionStart", "SessionEnd"):
            groups = _groups(data, event, create=False)
            if groups is None:
                continue
            keep = []
            for g in groups:
                if isinstance(g, dict) and isinstance(g.get("hooks"), list):
                    rest = [h for h in g["hooks"] if not (isinstance(h, dict) and is_ours(h.get("command")))]
                    if len(rest) != len(g["hooks"]):
                        changed = True
                        out.append("%s: removed %d entr%s of the kit" % (
                            event, len(g["hooks"]) - len(rest), "y" if len(g["hooks"]) - len(rest) == 1 else "ies"))
                        if not rest:
                            continue            # the group held only ours
                        g = dict(g, hooks=rest)
                keep.append(g)
            if keep:
                data["hooks"][event] = keep
            else:
                del data["hooks"][event]
        if isinstance(data.get("hooks"), dict) and not data["hooks"]:
            del data["hooks"]
    except ValueError as e:
        return 1, ["%s -- not touched" % e]
    if changed:
        backup = _save(path, data, True)
        out.append("settings written: %s" % path)
        out.append("backup of the old settings: %s" % backup)
    else:
        out.append("no entries of the kit in %s" % path)
    return 0, out


def installed_hooks(cwd=None, extra_paths=()):
    """Which hooks of the kit are found in the settings files that apply to `cwd` (project settings,
    project local settings, user settings). {'pre_tool': bool, 'session': bool, 'where': [paths]}."""
    paths = list(extra_paths)
    if cwd:
        for name in ("settings.json", "settings.local.json"):
            paths.append(os.path.join(os.path.abspath(cwd), ".claude", name))
    paths.append(os.path.join(user_home(), ".claude", "settings.json"))
    res = {"pre_tool": False, "session": False, "where": []}
    for p in paths:
        data, err = _load(p)
        if not data or err:
            continue
        hooks = data.get("hooks")
        if not isinstance(hooks, dict):
            continue
        for event, key, script in (("PreToolUse", "pre_tool", "pre_tool.py"), ("SessionStart", "session", "session.py")):
            groups = hooks.get(event)
            if isinstance(groups, list) and list(_iter_ours(groups, script)):
                res[key] = True
                if p not in res["where"]:
                    res["where"].append(p)
    return res


def _probe(command):
    """Run the hook command the way Claude Code does (the JSON as UTF-8 bytes on stdin) with a harmless
    Edit of a file that does not exist: the right answer is exit 0 and no output. (ok, what was seen)."""
    payload = json.dumps({"tool_name": "Edit", "tool_input": {
        "file_path": os.path.join(KIT, "_shedkeeper_probe_no_such_file.txt")}, "cwd": KIT,
        "session_id": "shedkeeper-check"}, ensure_ascii=False).encode("utf-8")
    try:
        p = subprocess.run(command, shell=True, input=payload, capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, "%s: %s" % (e.__class__.__name__, e)
    out = p.stdout.decode("utf-8", "replace").strip()
    err = p.stderr.decode("utf-8", "replace").strip().splitlines()
    if p.returncode != 0:
        return False, "exit %d%s" % (p.returncode, (" (%s)" % err[-1][:160]) if err else "")
    if out:
        return False, "unexpected output: %s" % out[:160]
    return True, "exit 0, no output"


def check(path):
    """Print-ready lines and an exit code: 0 all hooks of the kit are in this settings file, are the
    right ones, and the pre_tool command really starts."""
    out = ["settings file: %s (%s)" % (path, "exists" if os.path.exists(path) else "MISSING")]
    data, err = _load(path)
    if err:
        return 1, out + [err]
    allok = True
    if data.get("disableAllHooks") is True:
        out.append("disableAllHooks is TRUE in this file: Claude Code runs NO hooks while it is set")
        allok = False
    probe_cmd = None
    for event, matcher, script in ENTRIES:
        groups = None
        try:
            groups = _groups(data, event, create=False)
        except ValueError as e:
            out.append("%s: %s" % (event, e))
        mine = list(_iter_ours(groups, script)) if groups else []
        script_path = os.path.join(KIT, "hooks", script)
        if not mine:
            allok = False
            out.append("%s/%s: NOT installed" % (event, script))
            continue
        g, h = mine[0]
        bits = ["installed"]
        if h.get("timeout") != TIMEOUT_S:
            bits.append("timeout is %r, expected %d" % (h.get("timeout"), TIMEOUT_S))
            allok = False
        if matcher and g.get("matcher") != matcher:
            bits.append("matcher is %r, expected %r" % (g.get("matcher"), matcher))
            allok = False
        if not os.path.isfile(script_path):
            bits.append("the script file is MISSING")
            allok = False
        out.append("%s/%s: %s" % (event, script, ", ".join(bits)))
        if script == "pre_tool.py" and event == "PreToolUse":
            probe_cmd = h.get("command")
    py = shutil.which("python")
    out.append("python in PATH: %s" % (py or "NOT FOUND -- the hooks run `python`"))
    if not py:
        allok = False
    elif "windowsapps" in py.lower():
        out.append("WARNING: that python is in WindowsApps: it may be the Microsoft Store stub, which "
                   "starts no program (the probe below tells)")
    if probe_cmd:
        ok, seen = _probe(probe_cmd)
        out.append("probe (the pre_tool command run on a harmless Edit): %s -- %s" % ("OK" if ok else "FAILED", seen))
        if not ok:
            allok = False
    return (0 if allok else 1), out


def store_line(cwd):
    sys.path.insert(0, KIT)
    from shedkeeper import config, store
    settings, problems = config.load()
    root = store.store_root(cwd, settings)
    ok, why = store.check_available(root, settings)
    state = "exists" if root and os.path.isdir(root) and store.label_state(root)[0] == "ours" else "not yet created"
    lines = ["store on the volume of %s: %s (%s)%s" % (cwd, root, state, "" if ok else " -- NOT usable: " + why)]
    lines += ["setting: " + p for p in problems]
    return ok, lines


def _tolerant_output():
    """A character the console code page has no letter for (a message of the system in another
    language that the probe brings) must not kill the report: it is printed as '?'."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


def main(argv=None):
    _tolerant_output()
    ap = argparse.ArgumentParser(prog="install.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("action", choices=["install", "uninstall", "check"])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--project", metavar="DIR")
    g.add_argument("--user", action="store_true")
    a = ap.parse_args(argv)
    if a.action != "check" and not (a.project or a.user):
        ap.error("choose --project DIR or --user")
    if a.project and not os.path.isdir(a.project):
        print("the project folder does not exist: %s" % a.project)
        return 2
    if a.action == "check" and not (a.project or a.user):
        targets = [(settings_path(project=os.getcwd()), os.getcwd()), (settings_path(user=True), os.getcwd())]
    else:
        targets = [(settings_path(a.project, a.user), a.project or os.getcwd())]
    code = 0
    for path, cwd in targets:
        if a.action == "install":
            rc, lines = install(path)
        elif a.action == "uninstall":
            rc, lines = uninstall(path)
        else:
            rc, lines = check(path)
            store_ok, more = store_line(cwd)
            lines += more
            rc = rc or (0 if store_ok else 1)
        print("\n".join(lines))
        code = code or rc
    return code


if __name__ == "__main__":
    sys.exit(main())
