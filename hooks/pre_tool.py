"""PreToolUse hook: a copy before every change, the trash instead of deletion.

Input: the hook JSON on stdin, read as BYTES and decoded as UTF-8 (Claude Code sends UTF-8; the console
code page of the machine must not decide what a path is). Fields: tool_name, tool_input, cwd, session_id.
Output follows the Claude Code hook contract: exit 0 always; a refusal is a JSON object on stdout
  {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                          "permissionDecisionReason": "<text>"}}
and an informational line is {"hookSpecificOutput": {"hookEventName": "PreToolUse",
"additionalContext": "<text>"}}. (Form taken from a hook already in production use; compare with the
documentation of PreToolUse decision control when you change it.) When the personal store was made in THIS
call, the same JSON (a refusal too) carries a top-level "systemMessage": one paragraph for the person (where,
closed to whom, why the number, how to switch off); with no decision to give it is the only key.

What it does
  Edit / Write / NotebookEdit / MultiEdit
                                the file exists -> a verified copy of it is made BEFORE the tool runs.
                                No copy (error, mismatch, over the size limit with on_too_large = deny)
                                -> the change is refused. A file that does not exist -> nothing to copy.
                                A path inside a store is refused: the store is changed by a person only.
  Bash / PowerShell, deletion   refused, with a READY command that moves the items to the trash.
  Bash / PowerShell, writing    every existing file the command names as a target (redirect, tee, sed -i,
                                cp/mv, Set-Content ..., python writes with a literal path, the files a
                                recursive copy would overwrite, and the files a git rollback would
                                overwrite) is copied BEFORE the command. No copy -> refused. Ceilings:
                                cmd_max_files, cmd_deadline_s -> refused, not skipped. A target inside a
                                store, or a target list that could not be named in full (a mask cut at the
                                ceiling, a recursive copy from an unreadable source) -> refused.
  Two doors: started with `--via-plugin` (the plugin's hooks.json) it does nothing while a live entry of install.py
  covers PreToolUse (install.plugin_yields); started without the argument (install.py's entries) it always works.
  An exception inside the hook is a refusal when the tool is Edit/Write/NotebookEdit/MultiEdit, when a
  deletion/write was already recognized, or when the command text carries a rough sign of destruction
  (ROUGH: a deleting word, `>`, sed -i, cp, mv, Set-Content, git checkout ...); otherwise the action
  passes. Input that cannot be decoded is refused when its raw bytes name a guarded tool. Not covered
  (declared): an interpreter that fails before this file runs (Claude Code then lets the action through),
  writes/deletions inside a program the command runs, paths from variables.
"""
import json
import os
import re
import subprocess
import sys
import time

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if KIT not in sys.path:
    sys.path.insert(0, KIT)

EDIT_FIELDS = {"Edit": "file_path", "Write": "file_path", "NotebookEdit": "notebook_path",
               "MultiEdit": "file_path"}
CMD_TOOLS = ("Bash", "PowerShell")
GIT_TIMEOUT_S = 5       # one git call
GIT_TOTAL_S = 10        # all git calls of one hook run together (so the installed timeout has a budget)
# the raw bytes of an input that could not be decoded: does it name a tool we guard?
GUARDED_RAW = re.compile(rb'"tool_name"\s*:\s*"(?:Edit|Write|NotebookEdit|MultiEdit|Bash|PowerShell)"')
# a ROUGH sign that a command destroys or overwrites something -- used only when the recognizers fail
ROUGH = re.compile(
    r"(?<![\w.-])(?:rm|del|erase|rd|rmdir|unlink|shred|srm|remove-item|ri|cp|mv|copy|move|cpi|mi|sc|tee|dd|"
    r"install|truncate|robocopy|xcopy|set-content|out-file|copy-item|move-item|export-csv|tee-object)(?![\w-])"
    r"|>|\bsed\b[^|;&\n]*\s-[a-z]*i|\bperl\b[^|;&\n]*\s-[a-z]*i"
    r"|\bgit\b[^|;&\n]*\b(?:checkout|restore|reset|clean|rm|switch)\b|-delete\b|rmtree"
    r"|\bos\.(?:remove|unlink|rmdir|replace|rename)|\.write_(?:text|bytes)|open\([^)]*['\"][wxa]",
    re.I)


def _fwd(p):
    return os.path.abspath(p).replace("\\", "/")


def rough_destructive(command):
    """Does this command text carry a rough sign of destroying or overwriting something?"""
    return isinstance(command, str) and bool(ROUGH.search(command))


def _q(p, shell):
    """One path quoted for the shell the command came from: single quotes, nothing inside is expanded
    (`$`, a backtick and `"` are harmless in single quotes)."""
    if shell == "powershell":
        return "'" + p.replace("'", "''") + "'"
    return "'" + p.replace("'", "'\\''") + "'"


def _guard_copy(res, path, settings):
    """None when the action may go on; else the refusal text. The guard of invariant I1: a change
    is not allowed to run when its copy was not taken."""
    st = res.get("status")
    if st in ("ok", "same"):
        return None
    if st == "toolarge":
        if settings["on_too_large"] == "allow":
            return None
        return ("shedkeeper: no copy of %s was taken -- %s. The change is refused. To allow changes of "
                "big files without a copy set on_too_large = allow in shedkeeper.ini (or raise "
                "max_copy_mb, now %d)." % (path, res.get("msg", ""), settings["max_copy_mb"]))
    return ("shedkeeper: no copy of %s could be taken (%s). The change is refused so that nothing is "
            "overwritten without a copy." % (path, res.get("msg", "unknown reason")))


def _git(args, budget):
    """One git call within the shared budget (a one-element list of seconds left)."""
    left = min(GIT_TIMEOUT_S, budget[0])
    if left <= 0:
        raise subprocess.TimeoutExpired("git", 0)
    t0 = time.monotonic()
    try:
        return subprocess.run(["git"] + args, capture_output=True, timeout=left)
    finally:
        budget[0] -= time.monotonic() - t0


def _names(out):
    return [n for n in out.decode("utf-8", "replace").split("\0") if n]


def _git_modified(gdir, budget=None):
    """(files a git rollback in gdir would overwrite, error). Not a repository -> ([], None): the git
    command itself will fail. A hang, a missing git, or a diff that fails in a repository -> ([], text):
    the caller refuses. The list is the union of the working tree against the index and, when HEAD
    exists, against HEAD (a repository without a commit has no HEAD, and a rollback still overwrites)."""
    budget = budget if budget is not None else [GIT_TOTAL_S]
    try:
        top = _git(["-C", gdir, "rev-parse", "--show-toplevel"], budget)
        if top.returncode != 0:
            return [], None
        root = top.stdout.decode("utf-8", "replace").strip()
        names = []
        d1 = _git(["-C", root, "diff", "--name-only", "-z"], budget)
        if d1.returncode != 0:
            return [], "git diff failed (exit %d)" % d1.returncode
        names += _names(d1.stdout)
        head = _git(["-C", root, "rev-parse", "--verify", "-q", "HEAD"], budget)
        if head.returncode == 0:
            d2 = _git(["-C", root, "diff", "--name-only", "-z", "HEAD"], budget)
            if d2.returncode != 0:
                return [], "git diff HEAD failed (exit %d)" % d2.returncode
            names += _names(d2.stdout)
        elif head.returncode != 1:
            return [], "git rev-parse HEAD failed (exit %d)" % head.returncode
        uniq = list(dict.fromkeys(names))
        return [os.path.join(root, n) for n in uniq], None
    except subprocess.TimeoutExpired:
        return [], "git did not answer in time"
    except OSError as e:
        return [], "git could not be run (%s)" % e


def _trash_command(paths, shell="bash", reason="<why>"):
    shedkeeper_py = _fwd(os.path.join(KIT, "shedkeeper.py"))
    quoted = " ".join(_q(p, shell) for p in paths)
    return "python %s trash %s --reason %s" % (_q(shedkeeper_py, shell), quoted, _q(reason, shell))


def _deny_delete(existing, unresolved, shell="bash"):
    if existing:
        text = ("shedkeeper: deleting is not done here -- move to the trash instead; a person can bring "
                "it back. Run: %s" % _trash_command(existing, shell))
        if unresolved:
            text += (" -- some paths of your command could not be read from its text (variables, "
                     "masks, piped input); resolve them to literal paths and move those too.")
        return text
    return ("shedkeeper: deleting is not done here -- move to the trash instead: python %s trash "
            "'<path>' ['<path2>' ...] --reason '<why>' -- the paths of your command could not "
            "be read from its text (variables, masks, piped input); resolve them to literal paths."
            % _q(_fwd(os.path.join(KIT, "shedkeeper.py")), shell))


def _deny_store(paths):
    return ("shedkeeper: the store is changed by a person only -- it holds the copies and the trash, "
            "and nothing here writes into it: %s" % ", ".join(paths[:3]))


def _project_root(cwd, fallback, settings, store):
    return store.store_root(cwd or fallback, settings)


def _decide_edit(tool, tool_input, cwd, session, settings, store, state):
    path = tool_input.get(EDIT_FIELDS[tool])
    if not isinstance(path, str) or not path:
        return "allow", "", {"targets": 0}
    ap = os.path.abspath(os.path.join(cwd, path) if cwd and not os.path.isabs(path) else path)
    if store.is_store_path(ap, settings):
        return "deny", _deny_store([ap]), {"targets": 1, "zone": "store"}
    if not store.isfile(ap):
        return "allow", "", {"targets": 0}
    root = _project_root(cwd, ap, settings, store)
    ok, note, state["created"] = store.ensure_store_report(root, settings)
    if not ok:
        return "deny", ("shedkeeper: the store is not usable, so no copy can be made before this change: "
                        "%s. The change is refused. (To remove the hook: python install.py uninstall.)"
                        % note), {"targets": 1, "root": root}
    res = store.copy_version(ap, root, settings, session, tool)
    why = _guard_copy(res, ap, settings)
    meta = {"targets": 1, "root": root, "copy": res.get("status")}
    if why:
        return "deny", why, meta
    if res.get("status") == "toolarge":
        store.log(root, {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": "no_copy_allowed",
                         "path": ap, "msg": res.get("msg")})
        return "context", "shedkeeper: %s -- no copy (on_too_large = allow)" % res.get("msg"), meta
    if res.get("status") == "ok":
        return "context", "shedkeeper: copy saved (%s)" % (
            res.get("id") if res.get("indexed") else "NOT INDEXED: " + res.get("msg", "")), meta
    return "allow", "", meta


def _decide_cmd(tool, tool_input, cwd, session, settings, store, recognize, state):
    command = tool_input.get("command")
    if not isinstance(command, str) or not command.strip():
        return "allow", "", {"targets": 0}
    shell = "powershell" if tool == "PowerShell" else "bash"
    cwd = cwd if isinstance(cwd, str) and cwd else os.getcwd()
    # --- deletion: refuse, with the ready trash command
    dele = recognize.delete_targets(command, cwd, shell)
    if dele.found:
        state["recognized"] = True
        inside = [p for p in dele.paths if store.is_store_path(p, settings)]
        if inside:
            return "deny", ("shedkeeper: the store is cleaned by a person only -- it holds the copies and the "
                            "trash: %s" % ", ".join(inside[:3])), {"targets": len(dele.paths)}
        existing = [p for p in dele.paths if store.lexists(p)]
        if existing or dele.unresolved:
            return "deny", _deny_delete(existing, dele.unresolved, shell), {"targets": len(existing)}
        # a deletion of paths that do not exist removes nothing
    # --- writing: a copy before the command
    notes = []
    found, rollbacks = recognize.write_targets(command, cwd, shell, max_files=settings["cmd_max_files"],
                                               notes=notes)
    paths = [p for _form, p in found]
    if paths or rollbacks or notes:
        state["recognized"] = True
    inside = [p for p in paths if store.is_store_path(p, settings)]
    if inside:
        return "deny", _deny_store(inside), {"targets": len(inside)}
    if notes:
        return "deny", ("shedkeeper: this command names targets that cannot be listed in full (%s). Refused: "
                        "what is not named cannot be copied first. Name the files with literal paths, "
                        "or split the command." % "; ".join(notes[:3])), {"targets": len(paths)}
    budget = [GIT_TOTAL_S]
    for gdir in rollbacks:
        files, err = _git_modified(gdir, budget)
        if err:
            return "deny", ("shedkeeper: this command would overwrite files with git, and the list of them "
                            "could not be made (%s). Refused: nothing can be copied first." % err), {"targets": 0}
        paths += files
    seen, todo = set(), []
    for p in paths:
        ap = os.path.abspath(p)
        key = os.path.normcase(ap)
        if key in seen or store.is_store_path(ap, settings) or not store.isfile(ap):
            continue
        seen.add(key)
        todo.append(ap)
    if not todo:
        return "allow", "", {"targets": 0}
    root = store.store_root(cwd, settings)          # only now: a command with nothing to copy never needs the store
    if len(todo) > settings["cmd_max_files"]:
        return "deny", ("shedkeeper: this command overwrites %d files; the ceiling is cmd_max_files = %d. "
                        "Refused, not skipped. Split the command, or raise the ceiling in shedkeeper.ini."
                        % (len(todo), settings["cmd_max_files"])), {"targets": len(todo), "root": root}
    ok, note, state["created"] = store.ensure_store_report(root, settings)
    if not ok:
        return "deny", ("shedkeeper: the store is not usable, so no copy can be made before this command: "
                        "%s. The command is refused." % note), {"targets": len(todo), "root": root}
    t0 = time.monotonic()
    made, notes = 0, []
    for i, ap in enumerate(todo):
        if time.monotonic() - t0 > settings["cmd_deadline_s"]:
            left = len(todo) - i
            return "deny", ("shedkeeper: this command overwrites %d files; %d are left without a copy after "
                            "the ceilings (cmd_max_files = %d, cmd_deadline_s = %d). Refused, not skipped. "
                            "Split the command, or raise the ceilings in shedkeeper.ini."
                            % (len(todo), left, settings["cmd_max_files"], settings["cmd_deadline_s"])), \
                   {"targets": len(todo), "root": root}
        res = store.copy_version(ap, root, settings, session, tool)
        why = _guard_copy(res, ap, settings)
        if why:
            return "deny", why, {"targets": len(todo), "root": root}
        if res.get("status") == "ok":
            made += 1
            if not res.get("indexed"):
                notes.append("NOT INDEXED: " + res.get("msg", ""))
        elif res.get("status") == "toolarge":
            store.log(root, {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": "no_copy_allowed",
                             "path": ap, "msg": res.get("msg")})
            notes.append("no copy (on_too_large = allow): " + res.get("msg", ""))
    text = "shedkeeper: %d cop%s saved before this command" % (made, "y" if made == 1 else "ies")
    if notes:
        text += "; " + "; ".join(notes[:3])
    return "context", text, {"targets": len(todo), "root": root, "copies": made}


def decide(data, state=None):
    """(decision, text, meta) for one hook input; decision is 'allow' | 'deny' | 'context'."""
    state = state if state is not None else {}
    from shedkeeper import config, recognize, store
    tool = data.get("tool_name")
    if not isinstance(tool, str) or (tool not in EDIT_FIELDS and tool not in CMD_TOOLS):
        return "allow", "", {}
    tool_input = data.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    session = data.get("session_id") if isinstance(data.get("session_id"), str) else ""
    cwd = data.get("cwd") if isinstance(data.get("cwd"), str) else ""
    settings, _problems = config.load()
    if tool in EDIT_FIELDS:
        return _decide_edit(tool, tool_input, cwd, session, settings, store, state)
    return _decide_cmd(tool, tool_input, cwd, session, settings, store, recognize, state)


def _emit(decision, text, message=""):
    """One JSON object on one line. `message` (the report of a store made just now) goes to the person as the
    top-level `systemMessage`, beside the decision when there is one -- a refusal carries it too."""
    if decision == "deny":
        out = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                      "permissionDecisionReason": text}}
    elif decision == "context" and text:
        out = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": text}}
    elif message:
        out = {}
    else:
        return
    if message:
        out["systemMessage"] = message
    sys.stdout.write(json.dumps(out, ensure_ascii=True))
    sys.stdout.flush()


def _read_input():
    """The raw bytes of stdin. (A test may hand a text stream without .buffer.)"""
    buf = getattr(sys.stdin, "buffer", None)
    return buf.read() if buf is not None else sys.stdin.read().encode("utf-8")


def _yields_to_install(data):
    """Two doors, one handling: started by the PLUGIN (`--via-plugin`) while a live entry of install.py covers PreToolUse
    in the settings of this session, this copy stays silent -- the other one does the work. The proof is install.plugin_yields:
    CLAUDE_PROJECT_DIR given, the entry live, disableAllHooks on nowhere. Any doubt (the check fails, nothing is found,
    no variable) -> False: a doubled hook is untidy, a silent pair is no protection."""
    if "--via-plugin" not in sys.argv[1:]:
        return False
    try:
        import install
        cwd = data.get("cwd") if isinstance(data.get("cwd"), str) else None
        return bool(install.plugin_yields("PreToolUse", cwd))
    except Exception:
        return False


def main():
    raw = _read_input()
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as e:
        if GUARDED_RAW.search(raw):
            _emit("deny", "shedkeeper: the hook input for a guarded tool could not be read as UTF-8 JSON (%s: %s), "
                          "so nothing can be copied first. Refused." % (e.__class__.__name__, e))
        return 0
    if not isinstance(data, dict):
        return 0
    if _yields_to_install(data):
        return 0
    tool = data.get("tool_name")
    t0 = time.monotonic()
    state = {}
    try:
        decision, text, meta = decide(data, state)
    except Exception as e:      # the hook catches EVERYTHING
        err = "%s: %s" % (e.__class__.__name__, e)
        ti = data.get("tool_input")
        command = ti.get("command") if isinstance(ti, dict) else None
        if tool in EDIT_FIELDS or state.get("recognized") or (tool in CMD_TOOLS and rough_destructive(command)):
            decision, text, meta = "deny", ("shedkeeper: the hook failed (%s), so no copy can be trusted "
                                            "before this action. Refused. (To remove the hook: python "
                                            "install.py uninstall.)" % err), {"error": err}
        else:
            decision, text, meta = "allow", "", {"error": err}
    try:
        from shedkeeper import config, store
        settings, _p = config.load()
        cwd = data.get("cwd") if isinstance(data.get("cwd"), str) else ""
        rec = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "tool": tool, "decision": decision,
               "targets": meta.get("targets", 0), "ms": int((time.monotonic() - t0) * 1000)}
        if meta.get("error"):
            rec["error"] = meta["error"]
        if decision != "allow" or meta.get("targets"):
            root = meta.get("root") or store.store_root(cwd, settings)
            store.log(root, rec)
    except Exception:
        pass                    # a failing log never changes the decision
    message = ""
    try:
        from shedkeeper import store
        message = store.creation_report(state.get("created"))
    except Exception as e:      # a failing report never changes the decision -- but it leaves a trace in the log
        try:
            from shedkeeper import config, store
            settings, _p = config.load()
            cwd = data.get("cwd") if isinstance(data.get("cwd"), str) else ""
            store.log(store.store_root(cwd, settings), {"time": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                                        "event": "report_error", "error": "%s: %s" % (e.__class__.__name__, e)})
        except Exception:
            pass
    _emit(decision, text, message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
