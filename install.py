"""install.py -- put the hooks of the kit into Claude Code settings, take them out, check them.

  python install.py install   --project DIR | --user
  python install.py uninstall --project DIR | --user
  python install.py check     [--project DIR | --user]

`--project DIR` edits DIR\\.claude\\settings.json, `--user` edits ~\\.claude\\settings.json. Before any
change the settings file is copied next to itself (settings.json.shedkeeper-backup-<time>). The merge only
ADDS the entries of the kit and never touches anyone else's; installing twice does not duplicate;
uninstall removes ONLY the entries of the kit. The command written is a Git Bash one (see hook_command):
when the kit's folder is gone it warns loudly and lets the action pass. `install` also takes out the DEAD
lines of an earlier installation of another folder, `uninstall` takes out those and its own. A line is dead
only when it is PROVEN to be this installer's own and its file is PROVEN absent: its exact command text is in
the record this installer keeps of what it wrote (`installs.jsonl` in the user's personal folder of the store
-- see record_installs), it has exactly a form this installer writes, it names a hook file of the kit inside a
folder `hooks`, and a look at its file answers "not found" (anything else -- no permission, a volume that
cannot be reached -- is "not known": the line stays and is named). Lines of colleagues, of other tools, of an
earlier release that kept no record are never removed. A settings file that is not valid JSON is never
touched. The timeout of the hooks that copy (pre_tool, session) is 90 seconds: by the hook contract a timeout lets the action
through, so it must be longer than the slowest normal run. The hook of the prompt (UserPromptSubmit, prompt.py) is given 30 seconds (TIMEOUTS): it runs
while the person waits for the answer to a message, reads one message and writes one line -- a fraction of a second. The 90 s are a budget, not a guess: the
pre_tool hook spends at most cmd_deadline_s (20 s by default) on copies, at most 10 s on git calls
(two of 5 s) and at most db_deadline_s (25 s by default) on the copies of databases, about 3 s go on the start of the
interpreter -- 58 s in all, which leaves 32 s of margin (it was 60 s before the databases).

TWO DOORS. The same hooks also come as a Claude Code PLUGIN (`.claude-plugin/`, `hooks/hooks.json`; installed
with `/plugin`). The plugin and this installer are two ways in to one set of hooks, and both at once would run
every hook twice. So: `install` refuses while the plugin is PROVEN to be there (enabled by the order of the
settings files AND installed AND its hook files present -- plugin_status; a plugin that is only written as
enabled does not count: install goes on and says so); and the hooks of the plugin (started with `--via-plugin`)
stay silent only when a PROVEN live entry of this installer covers the event, Claude Code gave the hook
CLAUDE_PROJECT_DIR and `disableAllHooks` is on in no file (plugin_yields). `check` says which door is open.

The user's settings are read from CLAUDE_CONFIG_DIR when Claude Code is started with it (user_claude_dir), else from
~/.claude. The plugin registry is read from the plugins root: CLAUDE_CODE_PLUGIN_CACHE_DIR when it is set to an absolute
path, else `plugins` under that folder (plugins_root). The folder a plugin RUNS from is the folder of its marketplace
when that was added from a folder, else the cache copy (plugin_run_folder).
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time

KIT = os.path.dirname(os.path.abspath(__file__))
if KIT not in sys.path:
    sys.path.insert(0, KIT)
try:
    from shedkeeper import config as _kit_config
    TIMEOUT_S = _kit_config.HOOK_TIMEOUT_S      # ONE number for the whole kit (shedkeeper/config.py)
except Exception:                               # a package that does not load must not shut the door the hook names (`install.py uninstall`): the number of the kit stands
    TIMEOUT_S = 90
# `.*`: EVERY tool reaches the hook, also one that does not exist yet (a tool that runs a command, deletes a work tree ...): the closed list of the readers (config.READ_ONLY_TOOLS)
# is cut by the hook itself. Cost, measured (report 55, sect. 4): a call of a tool that the earlier matcher did not send to the hook costs the start of the hook, about 130 ms (Python started
# afresh, the hook run as a subprocess); the extra cost of the `bash` wrapper that the plugin form puts round every call is NOT measured.
PRE_MATCHER = ".*"
# (event, matcher or None, script)
ENTRIES = [("PreToolUse", PRE_MATCHER, "pre_tool.py"),
           ("SessionStart", None, "session.py"),
           ("SessionEnd", None, "session.py"),
           ("UserPromptSubmit", None, "prompt.py")]
# the timeout of a hook, by event. The prompt hook runs while the person waits for the answer to their message and does one regular expression and one small
# file write: 30 s is far more than it needs (measured: well under a second) and far less than the 90 s of the hooks that copy files and databases.
TIMEOUTS = {"UserPromptSubmit": 30}


def timeout_for(event):
    return TIMEOUTS.get(event, TIMEOUT_S)


def _fwd(s):
    return s.replace("\\", "/").lower()


HOOK_FILES = ("pre_tool.py", "session.py", "prompt.py")
EVENTS = ("PreToolUse", "SessionStart", "SessionEnd", "UserPromptSubmit")
PLUGIN = "shedkeeper"                   # `name` in .claude-plugin/plugin.json and in marketplace.json
PLUGIN_ID = "shedkeeper@shedkeeper"     # plugin@marketplace: what /plugin install and /plugin uninstall take
VIA_PLUGIN = "--via-plugin"             # the argument the hooks of the plugin are started with
RECORD_NAME = "installs.jsonl"          # in the personal folder of the store: what this installer wrote where
PLUGIN_FILES = ("hooks/hooks.json", "hooks/pre_tool.py", "hooks/session.py", "hooks/prompt.py")    # what an installed plugin must hold


def _sq(s):
    """A bash single-quoted word: nothing inside is special except the quote itself."""
    return "'" + s.replace("'", "'\\''") + "'"


def _unsq(word):
    return word[1:-1].replace("'\\''", "'")


def lost_messages(hook_path, settings_file):
    """(for the person, for the agent): what a hook whose file is gone says. ASCII except the paths. Paths are
    written with forward slashes and the text holds no double quote: the command goes to Git Bash as an
    argument of `bash -c`, and two backslashes in a row in such an argument arrive as ONE (measured), so a JSON
    that needs an escaped backslash or an escaped quote would be damaged on the way. (A single backslash --
    the `\\u` of a non-ASCII letter in a path -- passes intact.)"""
    hook_path, settings_file = [p.replace("\\", "/") for p in (hook_path, settings_file)]
    person = ("ShedKeeper was not found at %s: its protection is OFF (no copy is saved before an edit, nothing "
              "goes to the trash). If you removed ShedKeeper, ask your agent to run `python install.py uninstall` "
              "(from wherever ShedKeeper is now) or to remove the ShedKeeper lines from %s. If you moved it, run "
              "`python install.py install` from its new folder: it takes out the old lines it wrote itself and "
              "puts in new ones." % (hook_path, settings_file))
    agent = ("ShedKeeper hook file not found at %s: the protection is OFF (the person has the message of their own). You are allowed "
             "to edit %s: if ShedKeeper was removed, delete the entries whose command names that path (or run "
             "python <kit folder>/install.py uninstall --project <dir> (or --user) from a kit that is still there); "
             "if the folder was moved, run python <new folder>/install.py install --project <dir> (or --user): it "
             "takes out the old entries it wrote itself; any it did not write, delete by hand." % (hook_path, settings_file))
    return person, agent


def no_python_messages():
    """(for the person, for the agent): what a hook says when `python` cannot run the hook file (any exit code other than 0 and 2: 126 / 127 --
    bash could not start it; 1 and the rest -- the interpreter itself failed). ASCII, no double quote, no backslash: the same limits as lost_messages."""
    person = ("ShedKeeper cannot run: Python was not found (or `python` is the Microsoft Store placeholder), or Python itself failed on the hook "
              "file -- its protection is OFF: no copy is saved before an edit, nothing goes to the trash. Install or repair Python "
              "(python.org, tick Add python.exe to PATH) and restart Claude Code; `python install.py check` shows the state.")
    agent = ("ShedKeeper cannot run: Python was not found (or `python` is the Microsoft Store placeholder), or Python itself failed on the hook "
             "file -- the protection is OFF: no copy is saved before an edit and nothing goes to the trash (the person has the message of their own).")
    return person, agent


def _warning_json(event, person, agent):
    out = {"systemMessage": person}
    if event != "SessionEnd":
        out["hookSpecificOutput"] = {"hookEventName": event, "additionalContext": agent}
    return _sq(json.dumps(out, ensure_ascii=True))


def _guarded(file_word, run_word, event, lost_json):
    """The one form of every hook command (settings and plugin):

        if [ -f F ]; then python R; rc=$?; if [ $rc -ne 0 ] && [ $rc -ne 2 ]; then printf '%s' <NOPY>; exit 0; fi; exit $rc; else printf '%s' <LOST>; fi

    The file is there: python runs it and its exit code is the exit code of the whole (0 = a pass, 2 = a block still blocks), and
    only those two are the hook's own: the scripts of the kit exit with 0 or 2 and nothing else. Any other code (126 / 127: bash could
    not start `python`; 1: the interpreter itself failed -- a broken PYTHONHOME, a damaged module, a half-updated plugin; 137, 139 ...)
    means the hook did not decide -> a loud warning and exit 0, never a silent pass. The file is gone: the other loud warning, exit 0."""
    person, agent = no_python_messages()
    return ("if [ -f %s ]; then python %s; rc=$?; if [ $rc -ne 0 ] && [ $rc -ne 2 ]; then printf '%%s' %s; exit 0; fi; "
            "exit $rc; else printf '%%s' %s; fi" % (file_word, run_word, _warning_json(event, person, agent), lost_json))


def hook_command(script, event=None, settings_file=None):
    """The command written into the settings. Claude Code runs a hook command in Git Bash on Windows (docs: the
    `shell` field of a command hook is `bash` by default; measured, see README), so the form is a bash one:

        if [ -f 'P' ]; then python 'P'; rc=$?; <126/127 -> warning, exit 0>; exit $rc; else printf '%s' '<JSON>'; fi

    (see _guarded). The hook file is there: python runs it and its exit code is the exit code of the whole (2 = a
    block still blocks); python cannot be started (126 / 127): a loud warning, exit 0. The file is gone: the command
    prints a warning for the person (systemMessage) and the agent (additionalContext) and exits 0 -- the action
    passes, loudly, instead of python's exit 2 blocking everything. P uses forward slashes (what bash and python
    both take)."""
    if event is None:
        event = [e for e, _m, s in ENTRIES if s == script][0]
    return _build(os.path.join(KIT, "hooks", script), event, settings_file)


def _build(hook, event, settings_file=None):
    person, agent = lost_messages(hook, settings_file or "the Claude Code settings file")
    out = {"systemMessage": person}
    if event != "SessionEnd":
        out["hookSpecificOutput"] = {"hookEventName": event, "additionalContext": agent}
    p = _sq(os.path.normpath(hook).replace("\\", "/"))
    return _guarded(p, p, event, _sq(json.dumps(out, ensure_ascii=True)))


_QUOTED = r"'(?:[^']|'\\'')*'"
_GUARDED = re.compile(r"^if \[ -f (%s) \]; then python (%s); else printf '%%s' (%s); fi$" % (_QUOTED, _QUOTED, _QUOTED))   # 0.2.0
_GUARDED_RC = re.compile(
    r"^if \[ -f (%s) \]; then python (%s); rc=\$\?; if \[ \$rc -ne 0 \] && \[ \$rc -ne 2 \]; then printf '%%s' (%s); exit 0; fi; "
    r"exit \$rc; else printf '%%s' (%s); fi$" % (_QUOTED, _QUOTED, _QUOTED, _QUOTED))
_GUARDED_RC126 = re.compile(            # the earlier form of 0.3.0: only 126 / 127 were warned about (an interpreter that exits 1 passed silently)
    r"^if \[ -f (%s) \]; then python (%s); rc=\$\?; if \[ \$rc -eq 126 \] \|\| \[ \$rc -eq 127 \]; then printf '%%s' (%s); exit 0; fi; "
    r"exit \$rc; else printf '%%s' (%s); fi$" % (_QUOTED, _QUOTED, _QUOTED, _QUOTED))
_PLAIN = re.compile(r'^python "([^"]+)"$')


def parse_command(command):
    """(path of the hook file, 'guarded' | 'plain') when the command has EXACTLY a form that this installer writes
    -- the current one (hook_command), the guarded one of 0.2.0 (no 126/127 branch: `install` rewrites it) or the plain
    one of 0.1.0 (`python "<path>"`); otherwise None."""
    if not isinstance(command, str):
        return None
    c = command.strip()
    m = _GUARDED_RC.match(c) or _GUARDED_RC126.match(c)
    lost = 4
    if not m:
        m, lost = _GUARDED.match(c), 3
    if m:
        a, b = _unsq(m.group(1)), _unsq(m.group(2))
        try:
            msg = json.loads(_unsq(m.group(lost))).get("systemMessage")
        except (ValueError, AttributeError):
            return None
        if a == b and isinstance(msg, str) and "ShedKeeper" in msg:
            return a, "guarded"
        return None
    m = _PLAIN.match(c)
    if m:
        return m.group(1), "plain"
    return None


def _same_path(a, b):
    return os.path.normcase(_fwd(a)) == os.path.normcase(_fwd(b))


def is_ours(command, script=None):
    """A hook command of THIS kit: either exactly one of the forms this installer writes with the full path of a
    script of the kit's hooks folder, or (older entries by hand) a command that ENDS with that path, in quotes
    or not. A substring test would also take `...pre_tool.py.backup` or a wrapper of someone else's (found by an
    independent review)."""
    if not isinstance(command, str):
        return False
    names = [script] if script else list(HOOK_FILES)
    parsed = parse_command(command)
    if parsed:
        return any(_same_path(parsed[0], os.path.join(KIT, "hooks", n)) for n in names)
    c = os.path.normcase(_fwd(command.strip()))
    for n in names:
        p = os.path.normcase(_fwd(os.path.join(KIT, "hooks", n)))
        if c.endswith('"%s"' % p) or c.endswith(" " + p) or c == p:
            return True
    return False


def _absence(p):
    """Is there a file at `p`? 'present' | 'absent' | 'offline' | 'unknown: <why>'. 'absent' is said ONLY when the
    system answers "not found" (FileNotFoundError / NotADirectoryError from os.stat). os.path.exists cannot do
    this: it answers False to a refusal of access too (measured, Python 3.x on Windows), so a file of another account would be read as gone. A drive (or share) whose root cannot be looked
    at is 'offline': a disk that is unplugged is not a deleted folder."""
    drive = os.path.splitdrive(p)[0]
    if drive:
        try:
            os.stat(drive + "\\")
        except OSError:
            return "offline"
    try:
        os.stat(p)
    except (FileNotFoundError, NotADirectoryError):
        return "absent"
    except OSError as e:
        return "unknown: %s" % e.__class__.__name__
    return "present"


def classify_dead(command, recorded=None):
    """A line of a PREVIOUS installation whose hook file is gone, judged by PROOF, not by looks.
    -> (state, path, why) or None. `recorded` is the set of command texts this installer recorded for this settings
    file (recorded_commands); None = the record could not be read -> nothing is proven.
      None          the line is not of the form / the file is there / not even a line of ours in form;
      "dead"        recorded by this installer + exactly a form it writes + a hook file of the kit inside a
                    folder `hooks` + the file is PROVEN absent (_absence);
      "offline"     recorded, but the volume cannot be reached now: not decided, kept;
      "undecided"   recorded, but the system did not answer "not found" (no permission ...): not decided, kept;
      "unrecorded"  the form and the name are ours and the file is absent, but this installer did not write the
                    line (a colleague's, another tool's, an earlier release's): never removed, only named.
    A line whose file exists is never touched, however much it looks like ours."""
    parsed = parse_command(command)
    if not parsed:
        return None
    p = parsed[0]
    # a drive or share part is required: a POSIX-style path is `isabs` for Python before 3.13 and would be looked
    # for on the current drive (a live line of Git Bash form would be read as gone)
    if not os.path.isabs(p) or not os.path.splitdrive(p)[0] or os.path.basename(p).lower() not in HOOK_FILES \
            or os.path.basename(os.path.dirname(p)).lower() != "hooks":
        return None
    if is_ours(command):
        return None
    state = _absence(p)
    if state == "present":
        return None
    if recorded is None or command not in recorded:
        return ("unrecorded", p, "") if state == "absent" else None
    if state == "absent":
        return "dead", p, ""
    if state == "offline":
        return "offline", p, ""
    return "undecided", p, state


def user_claude_dir():
    """The folder Claude Code keeps the user's settings.json and plugins in. SHEDKEEPER_USER_HOME (an explicit
    override, and what the tests use) names the home whose `.claude` it is; else CLAUDE_CONFIG_DIR -- Claude Code's
    own variable (docs: with it set, "every ~/.claude path lives under that directory instead"); else ~/.claude."""
    home = os.environ.get("SHEDKEEPER_USER_HOME")
    if home:
        return os.path.join(home, ".claude")
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    if cfg:
        return os.path.abspath(cfg)
    return os.path.join(os.path.expanduser("~"), ".claude")


def settings_path(project=None, user=False):
    if user:
        return os.path.join(user_claude_dir(), "settings.json")
    return os.path.join(os.path.abspath(project), ".claude", "settings.json")


# ---- the record of what this installer wrote (the proof that a dead line is its own) -----------------------

def _kit_mods():
    if KIT not in sys.path:
        sys.path.insert(0, KIT)
    from shedkeeper import config, store
    return config, store


def _norm_settings(p):
    return os.path.normcase(_fwd(os.path.abspath(p)))


def _personal_root(path):
    """(store module, settings dict, personal folder of this user on the volume of `path` or None)."""
    config, store = _kit_mods()
    settings, _problems = config.load(integrity=False)
    return store, settings, store.store_root(path, settings)


def recorded_commands(path):
    """(set of command texts, note): the commands this installer wrote into the settings file `path`, from
    `installs.jsonl` in the user's personal folder on the volume of `path`. No folder / no file yet = an empty set
    (nothing was ever recorded: no line is proven ours). The record cannot be read, or the folder is not this
    user's -> (None, why): nothing is decided dead, and the caller says so."""
    try:
        store, settings, root = _personal_root(path)
        if not root:
            return None, ("no personal folder can be named for %s (no drive letter, or the login name of this user "
                          "cannot be read)" % path)
        if not os.path.isdir(root):
            return set(), ""
        if not store._is_mine(root):
            return None, "%s is not this user's store" % root
        f = os.path.join(root, RECORD_NAME)
        if not os.path.exists(f):
            return set(), ""
        recs, _bad = store._read_jsonl(f)
    except Exception as e:
        return None, "the record of installs could not be read (%s: %s)" % (e.__class__.__name__, e)
    me = _norm_settings(path)
    return {r["command"] for _n, r in recs if isinstance(r.get("command"), str)
            and isinstance(r.get("settings"), str) and _norm_settings(r["settings"]) == me}, ""


def record_installs(path, items):
    """Append to the record the commands now in the settings file `path` that are not recorded yet. `items` =
    [(event, script, command)]. The personal folder is made when it is not there (the same folder the hooks keep
    copies in). -> [lines to print]. A failure is NAMED: the install is done, but a later install / uninstall will
    not be able to take out the dead line of THIS one on its own."""
    try:
        store, settings, root = _personal_root(path)
        if not root:
            raise OSError("no personal folder can be named for %s (no drive letter, or the login name cannot be read)" % path)
        ok, made = store.ensure_store(root, settings)
        if not ok:
            raise OSError(made)
        done, why = recorded_commands(path)
        if done is None:
            raise OSError(why)
        f = os.path.join(root, RECORD_NAME)
        n = 0
        with open(os.path.join(KIT, "VERSION"), encoding="utf-8") as fh:
            version = fh.read().strip()
        for event, script, command in items:
            if command in done:
                continue
            store._append_jsonl(f, lambda _n, e=event, s=script, c=command: {
                "time": time.strftime("%Y-%m-%dT%H:%M:%S"), "settings": os.path.abspath(path), "event": e,
                "script": s, "command": c, "kit": KIT, "version": version})
            n += 1
        out = ["install record: %d new line%s in %s" % (n, "" if n == 1 else "s", f)] if n else []
        if made:
            out.insert(0, made)
        return out
    except Exception as e:
        return ["WARNING: the record of this installation could not be written (%s: %s). The hooks are in; but a "
                "later `install` / `uninstall` cannot prove these lines are its own, so it will NOT take them out if "
                "this folder is moved or removed -- they will have to be removed by hand." % (e.__class__.__name__, e)]


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


def _iter_ours(groups, script, recorded=None):
    """The hook entries of this kit for `script`, by the test of `uninstall` (is_exactly_ours): a command that merely
    ENDS with the path of the hook (`echo note <path>`, a wrapper of a colleague) is not an installation of ours.
    `recorded` is recorded_commands(path) (None = the record is not known: only the exact forms count)."""
    for g in groups:
        if isinstance(g, dict) and isinstance(g.get("hooks"), list):
            for h in g["hooks"]:
                if isinstance(h, dict) and is_exactly_ours(h.get("command"), recorded) and is_ours(h.get("command"), script):
                    yield g, h


def _remove_entries(data, pick):
    """Take out of the three events of the kit every hook entry whose command `pick` names (returns "ours" or
    "dead"). An entry that shares a group with other hooks leaves the group; a group that held only such entries
    goes; an empty event and an empty `hooks` go. -> [(event, kind, command)]."""
    removed = []
    for event in EVENTS:
        groups = _groups(data, event, create=False)
        if groups is None:
            continue
        keep = []
        for g in groups:
            if isinstance(g, dict) and isinstance(g.get("hooks"), list):
                rest = []
                for h in g["hooks"]:
                    kind = pick(h.get("command")) if isinstance(h, dict) else None
                    if kind:
                        removed.append((event, kind, h.get("command")))
                    else:
                        rest.append(h)
                if len(rest) != len(g["hooks"]):
                    if not rest:
                        continue                # the group held only ours
                    g = dict(g, hooks=rest)
            keep.append(g)
        if keep:
            data["hooks"][event] = keep
        else:
            del data["hooks"][event]
    if isinstance(data.get("hooks"), dict) and not data["hooks"]:
        del data["hooks"]
    return removed


def _pick_dead(recorded):
    """The picker of _remove_entries: "dead" for a command that classify_dead PROVES dead (recorded + absent)."""
    def pick(command):
        d = classify_dead(command, recorded)
        return "dead" if d and d[0] == "dead" else None
    return pick


def is_exactly_ours(command, recorded=None):
    """The test of `uninstall`, stricter than is_ours: the command is EXACTLY a form this installer writes for the hook
    files of THIS kit (parse_command, the path compared as a path), or it is a command recorded in `installs.jsonl`
    that is_ours. A command that merely ENDS with the path of a hook (`echo note <path>`, a wrapper of a colleague)
    is not taken out: it is named instead (_lookalike_lines)."""
    if parse_command(command):
        return is_ours(command)
    return bool(recorded) and command in recorded and is_ours(command)


def _pick_ours_or_dead(recorded):
    dead = _pick_dead(recorded)

    def pick(command):
        return "ours" if is_exactly_ours(command, recorded) else dead(command)
    return pick


def _lookalike_lines(data, recorded):
    """Entries that end with the path of a hook of this kit but are not a form this installer writes and are not
    recorded: uninstall leaves them and says so (the person decides)."""
    out = []
    for event in EVENTS:
        try:
            groups = _groups(data, event, create=False)
        except ValueError:
            continue
        for g in groups or []:
            for h in (g.get("hooks") or []) if isinstance(g, dict) and isinstance(g.get("hooks"), list) else []:
                c = h.get("command") if isinstance(h, dict) else None
                if is_ours(c) and not is_exactly_ours(c, recorded):
                    out.append("left a line that ends with the path of a hook of this kit but is not a command this "
                               "installer writes and is not in its record (%s): %r -- if it is yours, remove it by hand"
                               % (event, c))
    return out


def _dead_lines(removed, path):
    return ["removed a line of a previous installation from %s -- that folder no longer exists (%s: %s)"
            % (path, event, (parse_command(cmd) or (cmd,))[0]) for event, kind, cmd in removed if kind == "dead"]


def _kept_lines(data, recorded, plain_blocks=False):
    """Lines in ShedKeeper's form that are NOT removed, each with the reason: the volume cannot be reached; the file
    could not be looked at (no permission ...); the line was not written by this installer. `plain_blocks`: the
    unrecorded lines of the OLD form are left to the caller (check names them as what they are: lines that BLOCK)."""
    out = []
    for event in EVENTS:
        try:
            groups = _groups(data, event, create=False)
        except ValueError:
            continue
        for g in groups or []:
            for h in (g.get("hooks") or []) if isinstance(g, dict) and isinstance(g.get("hooks"), list) else []:
                d = classify_dead(h.get("command"), recorded) if isinstance(h, dict) else None
                if not d:
                    continue
                if d[0] == "offline":
                    out.append("kept a line of another installation (%s: %s): the volume cannot be reached "
                               "now, so it is not known whether the folder is gone" % (event, d[1]))
                elif d[0] == "undecided":
                    out.append("kept a line of another installation (%s: %s): its file could not be looked at (%s), "
                               "so it is not known whether the folder is gone" % (event, d[1], d[2]))
                elif d[0] == "unrecorded" and not (plain_blocks and parse_command(h.get("command"))[1] == "plain"):
                    out.append("left a line in ShedKeeper's form that this installer did not write (%s: %s): its "
                               "file is not there, but only lines this installer recorded are taken out -- if it is "
                               "yours, remove it by hand" % (event, d[1]))
    return out


def _record_problem_lines(recorded, note):
    return [] if recorded is not None else ["WARNING: %s -- no line of an earlier installation is taken out this time" % note]


NO_BASH_TEXT = ("REFUSED: Git Bash was not found. Claude Code on Windows runs every hook command in Git Bash, and the "
                "command this installer writes is a Git Bash command: without Git Bash it would do nothing at all, "
                "with no word (the protection OFF). Nothing was changed. Install Git for Windows "
                "(https://git-scm.com/downloads/win, choose \"Add to PATH\") or, if it is installed, point Claude "
                "Code to it with CLAUDE_CODE_GIT_BASH_PATH in the `env` of settings.json -- see "
                "https://code.claude.com/docs/en/troubleshooting (\"Windows: Claude Code on Windows requires "
                "git-bash\"). Then run this command again.")


def _git_rc(args, cwd):
    """The exit code of `git <args>` run in `cwd`; None when git cannot be run (not installed, too slow)."""
    try:
        return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, timeout=10).returncode
    except (OSError, subprocess.TimeoutExpired):
        return None


def shared_warning(path):
    """A WARNING line when the settings file `path` is shared through git (tracked) or would be as soon as it is
    committed (inside a working tree and not ignored); else None. The entries written by `install --project` name
    the kit folder of THIS machine: in a shared file they are useless to a colleague, who gets the 'not found'
    warning at every start. A line of a colleague that is already in the file is never touched (only recorded
    lines are)."""
    path = os.path.abspath(path)
    d = os.path.dirname(path)
    while d and not os.path.isdir(d):
        d = os.path.dirname(d)
    if not d:
        return None
    rc = _git_rc(["ls-files", "--error-unmatch", "--", path], d)
    if rc is None:
        up, state = d, None
        while up and state is None:
            if os.path.exists(os.path.join(up, ".git")):
                state = "inside a git working tree and git could not be asked (it may be committed)"
            parent = os.path.dirname(up)
            up = parent if parent != up else None
        if state is None:
            return None
    elif rc == 0:
        state = "committed to git (it is shared with everyone who clones the project)"
    elif rc == 1:
        ign = _git_rc(["check-ignore", "-q", "--", path], d)
        if ign != 1:
            return None                             # 0 = ignored (not shared); other = not a repo / git failed
        state = "not ignored by git (it is shared as soon as it is committed)"
    else:
        return None
    local = os.path.join(os.path.dirname(path), "settings.local.json")
    return ("WARNING: %s is %s. The entries written now name THIS machine's kit folder (%s): useless to a colleague, "
            "who then gets the 'not found' warning at every start; and lines of colleagues already in this file are "
            "not touched (only lines this installer recorded are ever taken out). For a personal installation use "
            "--user, or ask your agent to put the entries in %s (a file git does not share)." % (path, state, KIT, local))


def install(path, add_entries=True):
    """Put the hooks of the kit into the settings file `path`. `add_entries=False`: only take out the dead lines
    (the proven ones) and add nothing -- what `install` does when the plugin is the open door."""
    if add_entries and not find_bash():
        return 1, [NO_BASH_TEXT]
    data, err = _load(path)
    if err:
        return 1, [err]
    existed = os.path.exists(path)
    recorded, note = recorded_commands(path)
    out, changed = _record_problem_lines(recorded, note), False
    try:
        dead = _remove_entries(data, _pick_dead(recorded))
        if dead:
            changed = True
            out += _dead_lines(dead, path)
        out += _kept_lines(data, recorded)
        for event, matcher, script in (ENTRIES if add_entries else ()):
            groups = _groups(data, event, create=True)
            mine = list(_iter_ours(groups, script, recorded))
            if mine:
                for g, h in mine:
                    touched = False
                    if h.get("timeout") != timeout_for(event) or h.get("type") != "command":
                        h["type"], h["timeout"] = "command", timeout_for(event)
                        changed = touched = True
                        out.append("%s/%s: entry updated (timeout %d)" % (event, script, timeout_for(event)))
                    want = hook_command(script, event, path)
                    if parse_command(h.get("command")) and h.get("command") != want:
                        h["command"] = want
                        changed = touched = True
                        out.append("%s/%s: entry updated (command: a missing ShedKeeper folder now gives a "
                                   "loud warning and lets the action pass)" % (event, script))
                    if matcher and g.get("matcher") != matcher:
                        if all(is_exactly_ours(x.get("command"), recorded) for x in g["hooks"] if isinstance(x, dict)):
                            g["matcher"] = matcher
                            changed = touched = True
                            out.append("%s/%s: matcher updated (%s)" % (event, script, matcher))
                        else:
                            out.append("%s/%s: the matcher of a group shared with other hooks differs from "
                                       "%s and was NOT changed" % (event, script, matcher))
                    if not touched:
                        out.append("%s/%s: already installed" % (event, script))
                continue
            group = {"hooks": [{"type": "command", "command": hook_command(script, event, path), "timeout": timeout_for(event)}]}
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
    elif add_entries:
        out.append("nothing to change: %s" % path)
    if add_entries:
        # the proof for the next installer that this line is ours: every command of the kit now in the file
        items = [(event, script, h.get("command")) for event, _m, script in ENTRIES
                 for _g, h in _iter_ours(_groups(data, event, create=False) or [], script, recorded) if parse_command(h.get("command"))]
        out += record_installs(path, items)
    out += _lookalike_lines(data, recorded)
    return 0, out


def uninstall(path):
    if not os.path.exists(path):
        return 0, ["no settings file: %s -- nothing to remove" % path]
    data, err = _load(path)
    if err:
        return 1, [err]
    recorded, note = recorded_commands(path)
    out = _record_problem_lines(recorded, note)
    try:
        removed = _remove_entries(data, _pick_ours_or_dead(recorded))
    except ValueError as e:
        return 1, ["%s -- not touched" % e]
    for event in EVENTS:
        n = len([1 for e, k, _c in removed if e == event and k == "ours"])
        if n:
            out.append("%s: removed %d entr%s of the kit" % (event, n, "y" if n == 1 else "ies"))
    out += _dead_lines(removed, path)
    out += _kept_lines(data, recorded)
    out += _lookalike_lines(data, recorded)
    if removed:
        backup = _save(path, data, True)
        out.append("settings written: %s" % path)
        out.append("backup of the old settings: %s" % backup)
    else:
        out.append("no entries of the kit in %s" % path)
    return 0, out


# ---- the second door: the Claude Code plugin ---------------------------------------------------------------

def plugin_lost_messages():
    """(for the person, for the agent): what a plugin hook says when its file is gone. ASCII, no quote of any kind and no
    backslash (the command goes through `bash -c`, see lost_messages)."""
    person = ("ShedKeeper plugin files were not found: its protection is OFF (no copy is saved before an edit, "
              "nothing goes to the trash). Reinstall it with /plugin install %s, or remove it with "
              "/plugin uninstall %s." % (PLUGIN_ID, PLUGIN_ID))
    agent = ("ShedKeeper plugin files were not found: the protection is OFF. The plugin cache is not edited by hand; "
             "the person reinstalls with /plugin install %s or removes it with "
             "/plugin uninstall %s (python <kit folder>/install.py install is the other way in)."
             % (PLUGIN_ID, PLUGIN_ID))
    return person, agent


def plugin_command(script, event):
    """The command of a hook of the PLUGIN, in hooks/hooks.json. The same Git Bash form as hook_command, but the path
    is the plugin root that Claude Code substitutes (and also exports as a variable): a root whose file is gone lets
    the action pass loudly instead of python's exit 2 blocking it (the cache of an older version is removed 7 days
    after an update, while a long session may still hold it). `--via-plugin` tells the hook which door it came by."""
    path = "${CLAUDE_PLUGIN_ROOT}/hooks/" + script
    person, agent = plugin_lost_messages()
    out = {"systemMessage": person}
    if event != "SessionEnd":
        out["hookSpecificOutput"] = {"hookEventName": event, "additionalContext": agent}
    return _guarded('"%s"' % path, '"%s" %s' % (path, VIA_PLUGIN), event, _sq(json.dumps(out, ensure_ascii=True)))


def plugin_hooks():
    """The content of hooks/hooks.json: the same events, matcher, timeout and scripts as ENTRIES."""
    hooks = {}
    for event, matcher, script in ENTRIES:
        group = {"hooks": [{"type": "command", "command": plugin_command(script, event), "timeout": timeout_for(event)}]}
        if matcher:
            group = dict([("matcher", matcher)] + list(group.items()))
        hooks.setdefault(event, []).append(group)
    return {"description": "ShedKeeper: a copy before the changes it recognizes, the trash instead of deletion", "hooks": hooks}


def applicable_settings(project=None):
    """The settings files that apply to a session in `project`, HIGHEST precedence first: project-local, project,
    user (docs, "Settings precedence": local > shared project > user; managed settings, which beat all of these,
    and command-line settings are NOT read here). The user one alone without a project."""
    paths = []
    if project:
        for name in ("settings.local.json", "settings.json"):
            paths.append(os.path.join(os.path.abspath(project), ".claude", name))
    paths.append(os.path.join(user_claude_dir(), "settings.json"))
    return paths


def hooks_disabled(project=None):
    """(effective, where, trues): `disableAllHooks` over the settings that apply. The highest-precedence file that
    sets it to a boolean decides: `effective` is that value (None when no file sets it) and `where` is that file;
    `trues` lists every file that has it true."""
    eff, where, trues = None, None, []
    for p in applicable_settings(project):
        data, err = _load(p)
        v = data.get("disableAllHooks") if data and not err else None
        if isinstance(v, bool):
            if eff is None:
                eff, where = v, p
            if v:
                trues.append(p)
    return eff, where, trues


def plugin_enabled(paths):
    """[(settings path, key)] of the ShedKeeper keys of `enabledPlugins` that are ON (`shedkeeper@<marketplace>`:
    true) after the order of the files: `paths` come HIGHEST precedence first (applicable_settings) and the first
    file that sets a key to true/false decides it, so a `false` in settings.local.json beats a `true` in the shared
    file. This says what the settings WRITE; whether the plugin is really there is plugin_status."""
    seen, found = set(), []
    for p in paths:
        data, err = _load(p)
        ep = data.get("enabledPlugins") if data and not err else None
        if not isinstance(ep, dict):
            continue
        for key, val in ep.items():
            if not isinstance(key, str) or key.split("@")[0] != PLUGIN or key in seen or not isinstance(val, bool):
                continue
            seen.add(key)
            if val:
                found.append((p, key))
    return found


def live_entries(project=None):
    """{event: [settings path]} of the entries of THIS INSTALLER (any kit folder) that are alive: exactly the form it
    writes, a hook file of the kit inside a folder `hooks`, the file THERE -- and, for PreToolUse, the full tool matcher
    (a narrower matcher leaves tools unguarded, so it does not count as covering the event)."""
    res = {}
    for p in applicable_settings(project):
        data, err = _load(p)
        hooks = data.get("hooks") if data and not err else None
        if not isinstance(hooks, dict):
            continue
        for event, matcher, script in ENTRIES:
            groups = hooks.get(event)
            if not isinstance(groups, list):
                continue
            for g in groups:
                if not isinstance(g, dict) or not isinstance(g.get("hooks"), list):
                    continue
                if matcher and g.get("matcher") != matcher:
                    continue
                for h in g["hooks"]:
                    parsed = parse_command(h.get("command")) if isinstance(h, dict) else None
                    if not parsed:
                        continue
                    hp = parsed[0]
                    if os.path.isabs(hp) and os.path.basename(hp).lower() == script \
                            and os.path.basename(os.path.dirname(hp)).lower() == "hooks" and os.path.isfile(hp):
                        if p not in res.setdefault(event, []):
                            res[event].append(p)
    return res


def plugin_yields(event, cwd=None):
    """Does a hook of the plugin stay silent for `event`? Yes (a non-empty list of settings files) ONLY when all of
    these are PROVEN: Claude Code gave the hook CLAUDE_PROJECT_DIR (the project root: what names the settings files
    that apply to this session -- the `cwd` of the hook is not the root of the session and is not used); a LIVE entry
    of install.py (live_entries: a hook file there, the event and the full tool matcher) covers the event in those
    settings; and `disableAllHooks` is on in NO applicable file. Anything less -> [] (the plugin works): a doubled
    hook is untidy, a silent pair is no protection."""
    project = os.environ.get("CLAUDE_PROJECT_DIR")
    if not project:
        return []
    if hooks_disabled(project)[2]:
        return []
    return live_entries(project).get(event, [])


def _absolute(p):
    """An absolute path of the platform: on Windows a drive letter or a UNC share is needed (`\\foo` is rooted, but names no
    drive, and `isabs` of Python <= 3.12 and >= 3.13 disagree about it)."""
    return os.path.isabs(p) and (os.name != "nt" or bool(os.path.splitdrive(p)[0]))


def _plugins_var():
    """(the root named by CLAUDE_CODE_PLUGIN_CACHE_DIR, problem). (None, None): the variable does not apply (unset, empty, or
    SHEDKEEPER_USER_HOME, the explicit override, is set). (None, text): it is set, but where it points is not determined --
    `~` is expanded, and a path that is still not absolute is relative to a folder Claude Code was started in, which this
    process does not know (not measured whether Claude Code ignores it or resolves it)."""
    var = os.environ.get("CLAUDE_CODE_PLUGIN_CACHE_DIR")
    if not var or os.environ.get("SHEDKEEPER_USER_HOME"):
        return None, None
    path = os.path.expanduser(var)
    if not _absolute(path):
        return None, ("CLAUDE_CODE_PLUGIN_CACHE_DIR is set (%s) but is not an absolute path: where Claude Code reads the plugin "
                      "registry from is not determined" % var)
    return os.path.normpath(path), None


def plugins_root():
    """The folder Claude Code keeps its plugin registry (`installed_plugins.json`, `known_marketplaces.json`) and the
    plugin cache in. Docs ("Environment variables"): CLAUDE_CODE_PLUGIN_CACHE_DIR "overrides the plugins root directory;
    despite the name, this sets the parent directory, not the cache itself". Set to an absolute path (`~` expanded) it IS
    the root; else `plugins` in the user's config folder (user_claude_dir). SHEDKEEPER_USER_HOME, the explicit override,
    wins over the variable, as it does over CLAUDE_CONFIG_DIR. A relative value names no root: this returns the default
    folder, but plugins_root_problem() is then set and no registry is read (nothing is proven from it). Every read of the
    registry goes through here."""
    root, _problem = _plugins_var()
    return root or os.path.join(user_claude_dir(), "plugins")


def plugins_root_problem():
    """None, or why the plugins root is not determined (a relative CLAUDE_CODE_PLUGIN_CACHE_DIR): then nothing is read from it."""
    return _plugins_var()[1]


def plugins_root_lines():
    """What `check` and `install` say about where the plugin registry is read from -- always: the installer reads the
    environment of ITS process, and Claude Code reads its own (a variable set in a launcher, in the `env` of settings.json,
    is not seen here)."""
    root, problem = _plugins_var()
    if problem:
        return [problem + "; the plugin is not counted as proven (the default folder %s was NOT read)" % plugins_root()]
    if root:
        return ["the plugin registry is read from CLAUDE_CODE_PLUGIN_CACHE_DIR: %s (the environment of this process; Claude Code "
                "reads its own)" % root]
    return ["the plugin registry is read from %s (CLAUDE_CODE_PLUGIN_CACHE_DIR is not set in the environment of this process; "
            "Claude Code reads its own environment: if it is started with another plugins root, that registry is not this one)"
            % plugins_root()]


def _read_plugin_json(name):
    """(parsed json, problem) for a file of the plugins root; `problem` says why it could not be read."""
    if plugins_root_problem():
        return None, plugins_root_problem()
    p = os.path.join(plugins_root(), name)
    try:
        with open(p, encoding="utf-8-sig") as fh:
            return json.load(fh), None
    except FileNotFoundError:
        return None, "%s does not exist" % p
    except (OSError, ValueError) as e:
        return None, "%s cannot be read (%s: %s)" % (p, e.__class__.__name__, e)


def plugin_registry():
    """({key: [(installPath, version)]}, problem): the plugin registry Claude Code keeps, `installed_plugins.json` in
    the plugins root (plugins_root: CLAUDE_CODE_PLUGIN_CACHE_DIR and CLAUDE_CONFIG_DIR count). `problem` is None, or says
    why the registry could not be read -- an unreadable registry is NOT an empty one."""
    p = os.path.join(plugins_root(), "installed_plugins.json")
    data, why = _read_plugin_json("installed_plugins.json")
    if why:
        return {}, why if plugins_root_problem() else "the plugin registry " + why
    plugins = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(plugins, dict):
        return {}, "the plugin registry %s has no `plugins` object (its format may have changed)" % p
    out = {}
    for k, v in plugins.items():
        if isinstance(k, str) and isinstance(v, list):
            out[k] = [(e.get("installPath"), e.get("version")) for e in v if isinstance(e, dict)]
    return out, None


CACHE_SOURCES = ("github", "git")


def plugin_run_folder(key):
    """(folder, note): the folder Claude Code RUNS the plugin `key` from. Measured on Claude Code 2.1.284: a marketplace
    added from a folder (source `directory` in `known_marketplaces.json`) runs from THAT folder, although the registry's
    `installPath` is a copy in the cache. The cache copy is taken to run only for the kinds `github` and `git` (by the
    documentation, NOT measured here). folder is None when this cannot be decided -- `known_marketplaces.json` missing,
    unreadable, without the marketplace, of a form that is not understood, a `directory` path that is not absolute, or a
    source kind not studied -- and `note` says why; "not determined" is never taken for "the cache". The folder of a
    `directory` marketplace is taken to be the folder of the plugin: true for a plugin whose `source` is `./` (ShedKeeper),
    not for one in a subfolder of the marketplace -- then the files are not found there and the plugin is not proven."""
    market = key.split("@", 1)[1] if "@" in key else None
    data, why = _read_plugin_json("known_marketplaces.json")
    if why:
        return None, why
    entry = data.get(market) if isinstance(data, dict) and market else None
    if not isinstance(entry, dict):
        return None, "known_marketplaces.json does not list the marketplace %r (or its format changed)" % market
    src = entry.get("source")
    kind = src.get("source") if isinstance(src, dict) else None
    if not isinstance(kind, str):
        return None, "the entry of the marketplace %r in known_marketplaces.json has no `source.source` (its format may have changed)" % market
    if kind == "directory":
        path = src.get("path")
        if not isinstance(path, str) or not path:
            return None, "the marketplace %r has the source `directory` but no `source.path`" % market
        if not _absolute(path):
            return None, "the marketplace %r has the source `directory` with a path that is not absolute (%s)" % (market, path)
        return os.path.normpath(path), None
    if kind in CACHE_SOURCES:
        return "", None      # the cache copy runs (documentation, not measured here)
    return None, "the source kind %r of the marketplace %r has not been studied (only %s are taken to run from the cache copy)" % (
        kind, market, ", ".join(CACHE_SOURCES))


def plugin_installed(key):
    """(folder the plugin RUNS from, reasons): the folder of an INTACT installed plugin `key` -- listed in the registry,
    the folder it runs from (plugin_run_folder) is there, and so are hooks/hooks.json and both hook files -- or
    (None, [why not, one per fault]). When the run folder cannot be determined the cache copy is tried and reported, and
    the plugin is NOT proven: nothing shows the hooks run from it."""
    reg, problem = plugin_registry()
    if problem:
        return None, [problem]
    entries = reg.get(key)
    if not entries:
        return None, ["%s is not listed in the plugin registry" % key]
    run, note = plugin_run_folder(key)
    why = []
    for ip, _ver in entries:
        folder = run or ip
        if not isinstance(folder, str) or not os.path.isdir(folder):
            why.append("its %sfolder %s is not there" % ("run " if run else "installed ", folder))
            continue
        missing = [f for f in PLUGIN_FILES if not os.path.isfile(os.path.join(folder, *f.split("/")))]
        if missing:
            why.append("its %sfolder %s lacks %s" % ("run " if run else "installed ", folder, ", ".join(missing)))
            continue
        if run is None:
            why.append("the folder Claude Code runs it from is not determined (%s); the cache copy %s is intact, but nothing "
                       "shows the hooks run from it" % (note, folder))
            continue
        return folder, []
    return None, why


def plugin_folder_lines(key):
    """Lines for `check`: where the plugin runs from, and the registry's copy when it is another folder."""
    run, note = plugin_run_folder(key)
    reg, _problem = plugin_registry()
    cached = sorted({ip for ip, _v in reg.get(key, []) if isinstance(ip, str)})
    if run:
        out = ["the plugin runs from %s (its marketplace is a folder added in place)" % run]
        other = [c for c in cached if os.path.normcase(os.path.normpath(c)) != os.path.normcase(run)]
        if other:
            out.append("the registry's installPath is another folder, %s: that copy in the cache is NOT what runs" % ", ".join(other))
        return out
    if run is None:
        return ["the folder the plugin runs from is not determined (%s); the registry's installPath %s was tried, "
                "and the plugin is not counted as proven" % (note, ", ".join(cached) or "-")]
    return []


def plugin_status(project=None):
    """(proven, unproven): the plugin keys the settings switch ON (plugin_enabled), split by PROOF that the plugin is
    there. proven = [(settings path, key, install folder)]: enabled by the order of the files AND an intact installed
    copy (plugin_installed). unproven = [(settings path, key, [reasons])]: written as enabled, not found installed.
    A line in `enabledPlugins` alone proves nothing: it can be a teammate's committed setting, a leftover of an
    uninstall, a plugin that failed to load."""
    proven, unproven = [], []
    for p, key in plugin_enabled(applicable_settings(project)):
        ip, why = plugin_installed(key)
        if ip:
            proven.append((p, key, ip))
        else:
            unproven.append((p, key, why))
    return proven, unproven


def door_state(project=None):
    """{'mode': 'both' | 'plugin' | 'settings' | 'none', 'plugin': [(path, key)], 'installed': {key: folder},
    'unproven': [(path, key, [reasons])], 'live': {event: [paths]}, 'disabled': the file whose `disableAllHooks` is
    in force (true by precedence) or None}. `plugin` holds only PROVEN plugins; `mode` says what the settings carry,
    `disabled` says that none of it runs."""
    proven, unproven = plugin_status(project)
    plugin = [(p, k) for p, k, _d in proven]
    live = live_entries(project)
    mode = "both" if plugin and live else "plugin" if plugin else "settings" if live else "none"
    eff, where, _trues = hooks_disabled(project)
    return {"mode": mode, "plugin": plugin, "installed": {k: d for _p, k, d in proven}, "unproven": unproven, "live": live,
            "disabled": where if eff is True else None}


def door_lines(state):
    plug = ", ".join("%s in %s" % (k, p) for p, k in state["plugin"])
    where = ", ".join(sorted({p for ps in state["live"].values() for p in ps}))
    if state.get("disabled"):
        # disableAllHooks beats every door: whatever the settings carry, Claude Code runs no hook (found by an independent review)
        out = ["door: none -- disableAllHooks is on in %s: Claude Code runs no hooks, ShedKeeper does not run "
               "(the entries or the plugin that are there stay silent until it is turned off)" % state["disabled"]]
    elif state["mode"] == "both":
        out = ["TWO DOORS: ShedKeeper is on as a plugin (%s) AND put in by install.py (%s). The hooks of the plugin "
               "stay silent while the install.py entries are live and Claude Code gives them CLAUDE_PROJECT_DIR, so "
               "every action is handled once; without that variable both run (harmless, but twice). Keep one: "
               "/plugin uninstall %s, or python install.py uninstall --project <dir> (or --user)." % (plug, where, PLUGIN_ID)]
    elif state["mode"] == "plugin":
        out = ["door: the plugin (%s, installed at %s) -- the hooks come from the plugin, not from install.py"
               % (plug, ", ".join(sorted(set(state["installed"].values()))))]
    elif state["mode"] == "settings":
        out = ["door: install.py (live entries in %s) -- the plugin is not enabled" % where]
    else:
        out = ["door: none -- neither the plugin nor install.py has ShedKeeper in the settings that apply here"]
    for p, key, why in state["unproven"]:
        out.append("plugin door NOT PROVEN: %s is set to true in %s, but %s -- nothing shows that its hooks run "
                   "(/plugin, the Installed tab, says what Claude Code has installed)" % (key, p, "; ".join(why)))
    return out


SUBFOLDER_NOTE = ("NOTE: Claude Code reads the project settings from the .claude folder of the folder it is STARTED in (measured, "
                  "2.1.284): a session started in a subfolder of this project gets no protection from --project; for those use "
                  "--user.")

def project_door_lines(state, project):
    """The SUBFOLDER_NOTE for `check --project`: when the door that covers the session is in the settings of that PROJECT (an entry
    of install.py, or a plugin enabled at the project or local scope) -- those are read only from the folder Claude Code is started in."""
    folder = os.path.normcase(os.path.join(os.path.abspath(project), ".claude")) + os.sep
    paths = [p for ps in state["live"].values() for p in ps] + [p for p, _k in state["plugin"]]
    return [SUBFOLDER_NOTE] if any(os.path.normcase(os.path.abspath(p)).startswith(folder) for p in paths) else []


PROBE_NOTE = " (this proves that the hook starts and answers, not that a copy would be taken)"


def check_plugin(root):
    """The plugin door of `check`, against the folder `root` the plugin RUNS from (plugin_installed: the cache copy, or the
    folder itself when the marketplace was added from a folder) -- read as that folder is, and not as this kit folder is
    taken to be: its hooks.json is compared with the hooks of the installer, its plugin.json with its own VERSION, and the
    commands of its hooks.json start in Git Bash with ${CLAUDE_PLUGIN_ROOT} replaced by it. -> (ok, lines)."""
    out, ok = [], True
    out.append("installed plugin folder: %s" % root)
    try:
        with open(os.path.join(KIT, "VERSION"), encoding="utf-8") as fh:
            this_version = fh.read().strip()
    except OSError:
        this_version = None
    try:
        with open(os.path.join(root, "VERSION"), encoding="utf-8") as fh:
            version = fh.read().strip()
    except OSError:
        version = None
    for rel, want in (("hooks/hooks.json", plugin_hooks()), (".claude-plugin/plugin.json", None)):
        try:
            with open(os.path.join(root, *rel.split("/")), encoding="utf-8-sig") as fh:
                got = json.load(fh)
        except (OSError, ValueError) as e:
            out.append("%s: not readable (%s)" % (rel, e))
            ok = False
            continue
        if not isinstance(got, dict):
            out.append("%s: not understood (the file does not hold a JSON object)" % rel)
            ok = False
            continue
        if want is not None:
            same = got == want
            out.append("%s: %s" % (rel, "the hooks of the plugin are the hooks of the installer (events, matcher, timeout, "
                                        "scripts)" if same else "DIFFERS from what the installer would write"))
            ok = ok and same
        else:
            good = bool(version) and got.get("name") == PLUGIN and got.get("version") == version
            out.append("%s: name %r, version %r (VERSION of the installed copy: %r) -- %s" % (
                rel, got.get("name"), got.get("version"), version, "OK" if good else "MISMATCH"))
            ok = ok and good
    if version and this_version and version != this_version:
        out.append("note: the installed plugin is version %s, the kit this check runs from is %s" % (version, this_version))
    cmd = plugin_command("pre_tool.py", "PreToolUse")
    good, seen = _probe(cmd.replace("${CLAUDE_PLUGIN_ROOT}", root.replace("\\", "/")))
    out.append("probe (the plugin's pre_tool command, root = the installed plugin folder, in Git Bash): %s -- %s"
               % ("OK" if good else "FAILED", seen + (PROBE_NOTE if good else "")))
    ok = ok and good
    rc, o, err = _run_in_bash(cmd.replace("${CLAUDE_PLUGIN_ROOT}", root.replace("\\", "/") + "/_no_such_plugin_root"), _payload("x"))
    try:
        j = json.loads(o)
        good = rc == 0 and "ShedKeeper" in j["systemMessage"] and bool(j["hookSpecificOutput"]["additionalContext"])
    except (ValueError, KeyError, TypeError):
        good = False
    out.append("probe of the fallback (the plugin root without the hook file): %s" % ("OK" if good else "FAILED -- exit %s, %r" % (rc, o[:120])))
    return ok and good, out


def installed_hooks(cwd=None, extra_paths=()):
    """Which hooks of the kit are found in the settings files that apply to `cwd` (project settings,
    project local settings, user settings). {'pre_tool': bool, 'session': bool, 'prompt': bool, 'where': [paths]}."""
    paths = list(extra_paths)
    if cwd:
        for name in ("settings.json", "settings.local.json"):
            paths.append(os.path.join(os.path.abspath(cwd), ".claude", name))
    paths.append(os.path.join(user_claude_dir(), "settings.json"))
    res = {"pre_tool": False, "session": False, "prompt": False, "where": []}
    for p in paths:
        data, err = _load(p)
        if not data or err:
            continue
        hooks = data.get("hooks")
        if not isinstance(hooks, dict):
            continue
        for event, key, script in (("PreToolUse", "pre_tool", "pre_tool.py"), ("SessionStart", "session", "session.py"),
                                   ("UserPromptSubmit", "prompt", "prompt.py")):
            groups = hooks.get(event)
            if isinstance(groups, list) and list(_iter_ours(groups, script, recorded_commands(p)[0])):
                res[key] = True
                if p not in res["where"]:
                    res["where"].append(p)
    return res


def stale_entries(cwd=None, extra_paths=()):
    """[text] -- the entries of the kit in the settings that apply to `cwd` that are of an OLDER FORM than this kit installs: the matcher of PreToolUse
    without the `mcp__.*` of 0.3.0 (every MCP call goes unseen) or a timeout other than TIMEOUT_S (0.2.0 installed 60 s; the database budget of
    0.3.0 is 58 s of it, so a slow copy gets the hook cut off and the action passes). The same comparison `check` makes, so the start of a session
    can say it without running `check`."""
    paths = list(extra_paths)
    if cwd:
        for name in ("settings.json", "settings.local.json"):
            paths.append(os.path.join(os.path.abspath(cwd), ".claude", name))
    paths.append(os.path.join(user_claude_dir(), "settings.json"))
    out = []
    for p in paths:
        data, err = _load(p)
        if err:                 # the file is there and cannot be read: whether the hook stands in it is not known -- said, not skipped
            out.append("%s could not be checked (%s)" % (p, err.replace(" -- not touched", "")))
            continue
        if not data or not isinstance(data.get("hooks"), dict):
            continue
        for event, matcher, script in ENTRIES:
            groups = data["hooks"].get(event)
            if not isinstance(groups, list):
                continue
            for g, h in _iter_ours(groups, script, recorded_commands(p)[0]):
                bits = []
                if h.get("timeout") != timeout_for(event):
                    bits.append("timeout %r, now %d" % (h.get("timeout"), timeout_for(event)))
                if matcher and g.get("matcher") != matcher:
                    bits.append("matcher %r, now %r" % (g.get("matcher"), matcher))
                if bits:
                    out.append("%s %s: %s" % (p, event, "; ".join(bits)))
    return out


def find_bash():
    """The shell Claude Code runs hook commands in on Windows: Git Bash. Claude Code takes it from the
    CLAUDE_CODE_GIT_BASH_PATH variable or finds Git for Windows; the same order here. The `bash.exe` of
    System32 is the launcher of WSL, not Git Bash: never taken."""
    cands = [os.environ.get("CLAUDE_CODE_GIT_BASH_PATH")]
    git = shutil.which("git")
    if git:
        root = os.path.dirname(os.path.dirname(os.path.abspath(git)))
        cands += [os.path.join(root, "bin", "bash.exe"), os.path.join(os.path.dirname(root), "bin", "bash.exe")]
    for var in ("ProgramFiles", "ProgramFiles(x86)", "LocalAppData"):
        base = os.environ.get(var)
        if base:
            cands.append(os.path.join(base, "Git", "bin", "bash.exe"))
            cands.append(os.path.join(base, "Programs", "Git", "bin", "bash.exe"))
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


def _run_in_bash(command, payload):
    """(returncode, stdout, stderr) of the command in Git Bash (`bash -c`, the JSON as UTF-8 bytes on stdin), or an
    error text instead of a code."""
    bash = find_bash()
    if not bash:
        return None, "", "Git Bash was not found (Claude Code on Windows runs hooks in it; set CLAUDE_CODE_GIT_BASH_PATH)"
    try:
        p = subprocess.run([bash, "-c", command], input=payload, capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, "", "%s: %s" % (e.__class__.__name__, e)
    return p.returncode, p.stdout.decode("utf-8", "replace").strip(), p.stderr.decode("utf-8", "replace").strip()


def _payload(file_path):
    return json.dumps({"tool_name": "Edit", "tool_input": {"file_path": file_path}, "cwd": KIT,
                       "session_id": "shedkeeper-check"}, ensure_ascii=False).encode("utf-8")


def _probe(command):
    """Run the hook command the way Claude Code does (in Git Bash, the JSON as UTF-8 bytes on stdin) with a harmless
    Edit of a file that does not exist: the right answer is exit 0 and no output. (ok, what was seen)."""
    rc, out, err = _run_in_bash(command, _payload(os.path.join(KIT, "_shedkeeper_probe_no_such_file.txt")))
    if rc is None:
        return False, err
    if rc != 0:
        return False, "exit %d%s" % (rc, (" (%s)" % err.splitlines()[-1][:160]) if err else "")
    if out:
        return False, "unexpected output: %s" % out[:160]
    return True, "exit 0, no output"


def _probe_lost():
    """The fallback of the command: the same form pointed at a hook file that does not exist must exit 0 and print
    a warning (systemMessage, and additionalContext for the agent). (ok, what was seen)."""
    cmd = _build(os.path.join(KIT, "_shedkeeper_no_such_folder", "hooks", "pre_tool.py"), "PreToolUse", "<settings>")
    rc, out, err = _run_in_bash(cmd, _payload("x"))
    if rc is None:
        return False, err
    try:
        o = json.loads(out)
        ok = rc == 0 and "ShedKeeper" in o["systemMessage"] and o["hookSpecificOutput"]["additionalContext"]
    except (ValueError, KeyError, TypeError):
        ok = False
    return bool(ok), ("exit 0 and a warning" if ok else "exit %s, output %r %s" % (rc, out[:120], err[:120]))


def _disabled_lines(project):
    """([lines], bad): `disableAllHooks` over ALL the settings that apply, by precedence (not one file)."""
    eff, where, trues = hooks_disabled(project)
    if eff is True:
        return ["disableAllHooks is TRUE in %s: Claude Code runs NO hooks while it is set (read by precedence: "
                "project-local, project, user; managed settings are not read here)" % where], True
    if trues:
        return ["disableAllHooks is true in %s, but %s, a file of higher precedence, sets it to false: it has no "
                "effect" % (", ".join(trues), where)], False
    return [], False


def _dead_check(path, data):
    """([lines], bad) for the settings file `path`: lines of a previous installation whose file is gone (proven: they
    are this installer's own -> `install` / `uninstall` take them out), and the lines it leaves alone, named."""
    recorded, note = recorded_commands(path)
    out, bad = _record_problem_lines(recorded, note), False
    for event in EVENTS:
        try:
            groups = _groups(data, event, create=False)
        except ValueError:
            continue
        for g in groups or []:
            for h in (g.get("hooks") or []) if isinstance(g, dict) and isinstance(g.get("hooks"), list) else []:
                d = classify_dead(h.get("command"), recorded) if isinstance(h, dict) else None
                if not d or d[0] not in ("dead", "unrecorded"):
                    continue
                form = parse_command(h.get("command"))[1]
                if d[0] == "dead":
                    out.append("%s: a line of a previous installation points to %s, which does not exist%s: "
                               "`python install.py install` takes it out" % (
                                   event, d[1], " and BLOCKS every guarded tool (old form)" if form == "plain" else
                                   " (it warns on every use and lets the action pass)"))
                    bad = True
                elif form == "plain":
                    out.append("%s: a line of the old form points to %s, which does not exist, and BLOCKS every guarded "
                               "tool; this installer did not write it (it is not in your record), so it will not take "
                               "it out -- remove it by hand" % (event, d[1]))
                    bad = True
    out += _kept_lines(data, recorded, plain_blocks=True)
    return out, bad


def check(path, project=None):
    """Print-ready lines and an exit code: 0 all hooks of the kit are in this settings file, are the
    right ones, and the pre_tool command really starts. `project`: the project whose settings apply (None = the
    user settings alone) -- `disableAllHooks` is read over all of them."""
    out = ["settings file: %s (%s)" % (path, "exists" if os.path.exists(path) else "MISSING")]
    data, err = _load(path)
    if err:
        return 1, out + [err]
    allok = True
    lines, bad = _disabled_lines(project)
    out += lines
    allok = allok and not bad
    if os.environ.get("CLAUDE_CONFIG_DIR") and not os.environ.get("SHEDKEEPER_USER_HOME"):
        out.append("the user settings and plugins are read from CLAUDE_CONFIG_DIR: %s" % user_claude_dir())
    out += plugins_root_lines()
    probe_cmd = None
    recorded = recorded_commands(path)[0]
    for event, matcher, script in ENTRIES:
        groups = None
        try:
            groups = _groups(data, event, create=False)
        except ValueError as e:
            out.append("%s: %s" % (event, e))
        mine = list(_iter_ours(groups, script, recorded)) if groups else []
        script_path = os.path.join(KIT, "hooks", script)
        if not mine:
            allok = False
            out.append("%s/%s: NOT installed" % (event, script))
            continue
        g, h = mine[0]
        bits = ["installed"]
        if h.get("timeout") != timeout_for(event):
            bits.append("timeout is %r, expected %d" % (h.get("timeout"), timeout_for(event)))
            allok = False
        if matcher and g.get("matcher") != matcher:
            bits.append("matcher is %r, expected %r" % (g.get("matcher"), matcher))
            allok = False
        if not os.path.isfile(script_path):
            bits.append("the script file is MISSING")
            allok = False
        parsed = parse_command(h.get("command"))
        if parsed and parsed[1] == "plain":
            bits.append("the command is of the old form (a missing ShedKeeper folder would BLOCK every guarded "
                        "tool): `python install.py install` brings it up to date")
            allok = False
        elif parsed and _GUARDED_RC126.match(h.get("command").strip()):
            bits.append("the command is of the 0.2.0 form (a Python that fails with an exit code other than 126 / 127, 1 for one, lets the "
                        "action pass WITHOUT a warning): `python install.py install` brings it up to date")
            allok = False
        elif parsed and not _GUARDED_RC.match(h.get("command").strip()):
            bits.append("the command is of an earlier form (a Python that cannot start lets the action pass WITHOUT a "
                        "warning): `python install.py install` brings it up to date")
            allok = False
        out.append("%s/%s: %s" % (event, script, ", ".join(bits)))
        if script == "pre_tool.py" and event == "PreToolUse":
            probe_cmd = h.get("command")
    lines, bad = _dead_check(path, data)
    out += lines
    allok = allok and not bad
    py = shutil.which("python")
    out.append("python in PATH: %s" % (py or "NOT FOUND -- the hooks run `python`"))
    if not py:
        allok = False
    elif "windowsapps" in py.lower():
        out.append("WARNING: that python is in WindowsApps: it may be the Microsoft Store stub, which "
                   "starts no program (the probe below tells)")
    if probe_cmd:
        ok, seen = _probe(probe_cmd)
        out.append("probe (the pre_tool command run on a harmless Edit, in Git Bash as Claude Code runs it): %s -- %s"
                   % ("OK" if ok else "FAILED", seen + (PROBE_NOTE if ok else "")))
        if not ok:
            allok = False
        ok, seen = _probe_lost()
        out.append("probe of the fallback (the same form pointed at a hook file that is not there): %s -- %s"
                   % ("OK" if ok else "FAILED", seen))
        if not ok:
            allok = False
    return (0 if allok else 1), out


def store_line(cwd):
    sys.path.insert(0, KIT)
    from shedkeeper import config, store
    settings, problems = config.load()
    root = store.store_root(cwd, settings)
    ok, why = store.check_available(root, settings)
    exists = bool(root and os.path.isdir(root) and store.user_label_state(root)[0] == "ours")
    lines = ["store on the volume of %s: %s (%s)%s" % (cwd, root, "exists" if exists else "not yet created",
                                                     "" if ok else " -- NOT usable: " + why)]
    if exists and ok:
        private, _text = store.private_state(root, settings)
        lines.append(store.describe(root, settings))
        # a store that is meant to be private and is not (an extra entry in its permissions, made by an
        # administrator, a backup program ...): the check fails; the folder stays the store, nothing is changed
        ok = ok and (private or not settings.get("private_store", True))
    lines += store.unsearched_lines(settings)        # copies that find / restore do not look at
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
    project = a.project or (None if a.user else os.getcwd())
    state = None
    if a.action == "install":
        print("\n".join(plugins_root_lines()))
        proven, unproven = plugin_status(project)
        if proven:
            # the second door is open: a second way in would run every hook twice -- refused. The dead lines of an
            # earlier installation of OURS (proven) are taken out first and said; nothing else is touched
            for path, _cwd in targets:
                rc, lines = install(path, add_entries=False)
                if lines:
                    print("\n".join(lines))
            print("REFUSED: ShedKeeper is already on as a plugin (%s). Put in a second way, it would run twice on every "
                  "action (two copies, two refusals). No entry was added. Keep the plugin: there is nothing more to do. "
                  "Or switch to install.py: /plugin uninstall %s inside Claude Code, then run this command again."
                  % (", ".join("%s in %s, installed at %s" % (k, p, d) for p, k, d in proven), PLUGIN_ID))
            return 1
        for p, k, why in unproven:
            print("WARNING: the plugin %s is written as enabled in %s, but not found installed: %s. The plugin is not "
                  "counted as an open door, so install.py puts its entries in; if the plugin does get installed, its hooks "
                  "stay silent while these entries are live (check says which door is open)." % (k, p, "; ".join(why)))
        if a.project:
            for path, _cwd in targets:
                w = shared_warning(path)
                if w:
                    print(w)
    if a.action == "check":
        state = door_state(project)
        if state["mode"] == "plugin":            # the plugin is the only door: the settings carry no hooks of ours
            root = sorted(set(state["installed"].values()))[0]
            ok, lines = check_plugin(root)
            extra = []
            dis, bad = _disabled_lines(project)
            extra += dis
            ok = ok and not bad
            for path, _cwd in targets:
                data, err = _load(path)
                if data is not None and not err:
                    dl, bad = _dead_check(path, data)
                    extra += dl
                    ok = ok and not bad
            store_ok, more = store_line(a.project or os.getcwd())
            folder = [l for k in sorted({k for _p, k in state["plugin"]}) for l in plugin_folder_lines(k)]
            note = project_door_lines(state, a.project) if a.project else []
            print("\n".join(door_lines(state) + plugins_root_lines() + folder + lines + extra + more + note))
            return 0 if ok and store_ok else 1
    code = 0
    for path, cwd in targets:
        if a.action == "install":
            rc, lines = install(path)
        elif a.action == "uninstall":
            rc, lines = uninstall(path)
            found = [(p, k) for p, k, _d in plugin_status(project)[0]]
            if found:
                lines.append("ShedKeeper is still on as a plugin (%s): /plugin uninstall %s takes it out."
                             % (", ".join("%s in %s" % (k, p) for p, k in found), PLUGIN_ID))
        else:
            rc, lines = check(path, project)
            store_ok, more = store_line(cwd)
            lines += more
            rc = rc or (0 if store_ok else 1)
        print("\n".join(lines))
        if a.action == "install" and a.project and rc == 0:
            print(SUBFOLDER_NOTE)
        code = code or rc
    if state:
        print("\n".join(door_lines(state) + (project_door_lines(state, a.project) if a.project else [])))        # the "both" state is safe (the plugin stays silent): exit code unchanged
    return code


if __name__ == "__main__":
    sys.exit(main())
