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
  Bash / PowerShell, databases  a command that DESTROYS data in a PostgreSQL or SQLite database (DROP/TRUNCATE/DELETE or UPDATE
                                without WHERE/ALTER ... DROP COLUMN in psql or sqlite3, dropdb, pg_restore --clean, prisma migrate
                                reset, rails db:drop ... -- shedkeeper/db.py) gets a verified dump of that database BEFORE it runs.
                                The database cannot be named, is remote, is bigger than max_db_mb, the copy does not fit
                                db_deadline_s, or no copy could be taken -> refused, with a READY command (`dbcopy`, or
                                SHEDKEEPER_DB=... to name the database); on_db_remote / on_db_too_large = allow let the last
                                two pass and write a line to the log. Refusals (files and databases) come before any copy.
                                An engine this version does not copy (MySQL ... named by Rails, Laravel, Prisma ...): the command passes
                                with a warning to the person (systemMessage) and the agent (additionalContext); on_db_unsupported = deny
                                refuses it.
  MCP tools (mcp__*)            a call that destroys data in a database (SQL in an argument, or a tool name with drop / delete / reset /
                                truncate / wipe / purge -- shedkeeper/dbmcp.py) is decided like a command: the database behind the server is
                                read from its settings; a copy first; a server that cannot be named or is remote -> refused, with a READY
                                `dbcopy ... --mcp-server NAME`; a fresh such copy lets the call pass. Servers without a database: mcp_no_db.
  Every refusal (and the warning about an engine that is not copied) ends with the block for the AGENT of shedkeeper/advice.py: what happened,
  who decides (the user: themselves or through the agent), the options that apply, the exact commands (0.3.0) -- facts and ways on, no order about
  how the agent behaves. A refusal that has no special one gets the generic block in main().
  A command that RUNS `shedkeeper.py set KEY VALUE`, and an Edit / Write / MultiEdit of an ini file the kit reads, give the person a
  `systemMessage`: "ShedKeeper setting KEY: was -> will be" (shedkeeper/settings_cli.py); the action itself is not stopped -- EXCEPT an
  Edit / Write / MultiEdit of an ini file that would WEAKEN protection: refused, with the way through `set` (a weakening is applied only
  after the person types the code `set` gives, which the hook of the user's prompt, hooks/prompt.py, receives; shedkeeper/confirm.py).
  A shell command of the agent that WRITES into a settings file the kit reads (shedkeeper.ini, the personal ini: `>>`, `sed -i`, `tee`, `cp`, a
  python write with a literal path) is refused: settings change through `set`. That guard is a list of forms of commands, good against an agent that
  MISTAKES the way; a write the list does not see (a quote inside a word, a mask, a name made of variables, a program that opens the file itself) is
  not refused, and a weakening that stands in an ini acts (the hand of the person edits the file); the start of a session says so when it was not made
  through `set` (config._integrity). The hook carries no alarm of its own at a decision.
  The answer of the hook is never longer than MAX_OUT (9500) characters and is always valid JSON: a long list of paths is cut (the full list is a
  file in the store), and a last guard in `_emit` shortens the TEXT, never the JSON, keeping the decision.
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
MAX_OUT = 9500          # the runtime cuts the output of a hook at 10000 characters and then reads no JSON: the answer stays under this
PATH_BUDGET = 2400      # the characters (as written in the JSON) a list of paths may take in ONE place of a refusal
# the raw bytes of an input that could not be decoded: does it name a tool we guard?
GUARDED_RAW = re.compile(rb'"tool_name"\s*:\s*"(?:Edit|Write|NotebookEdit|MultiEdit|Bash|PowerShell)"')
# the raw bytes of an input that could not be decoded, naming an MCP tool with a destroying word in the tool name or the text
GUARDED_RAW_MCP = re.compile(rb'"tool_name"\s*:\s*"mcp__', re.I)
ROUGH_MCP = re.compile(r"(?:drop|delete|reset|truncate|wipe|purge|alter|update)", re.I)
# a ROUGH sign that a command destroys or overwrites something -- used only when the recognizers fail
ROUGH = re.compile(
    r"(?<![\w.-])(?:rm|del|erase|rd|rmdir|unlink|shred|srm|remove-item|ri|cp|mv|copy|move|cpi|mi|sc|tee|dd|"
    r"install|truncate|robocopy|xcopy|set-content|out-file|copy-item|move-item|export-csv|tee-object)(?![\w-])"
    r"|>|\bsed\b[^|;&\n]*\s-[a-z]*i|\bperl\b[^|;&\n]*\s-[a-z]*i"
    r"|\bgit\b[^|;&\n]*\b(?:checkout|restore|reset|clean|rm|switch)\b|-delete\b|rmtree"
    r"|\bos\.(?:remove|unlink|rmdir|replace|rename)|\.write_(?:text|bytes)|open\([^)]*['\"](?:[wxa]|r[bt]?\+)"
    r"|\bshutil\.(?:copy\w*|move|rmtree)|\bcurl\b[^|;&\n]*\s(?:-[a-zA-Z]*o\b|--output\b)|\bwget\b[^|;&\n]*\s(?:-[a-zA-Z]*O\b|--output-document\b)"
    r"|(?<![\w.-])(?:iwr|irm|invoke-webrequest|invoke-restmethod)\b[^|;&\n]*\s-outfile\b"
    r"|(?<![\w.-])(?:psql|sqlite3|dropdb|pg_restore|prisma|supabase|rails|rake|alembic|artisan|typeorm[\w-]*|drizzle-kit|"
    r"sequelize[\w-]*|docker|docker-compose|podman|django-admin(?:\.py)?)(?![\w-])|manage\.py|\bdrop\s+(?:database|schema|table|owned)\b|\btruncate\b",
    re.I)


def _fwd(p):
    return os.path.abspath(p).replace("\\", "/")


def _rough_extra(command):
    """The first words of the destroying commands the PERSON added (`db_command` of the settings) and the command names of the adapters of the
    user: the static ROUGH list cannot know them, and the fallback must not pass what the readers would have stopped. Never raises; when it cannot tell
    (the settings or the adapters cannot be loaded) it says True -- it runs after the readers failed, and a command it cannot judge is not passed."""
    try:
        from shedkeeper import config, engines
        words = []
        settings, _problems = config.load(integrity=False)
        for line in settings.get("db_command") or ():
            w = line.replace("|", " ").split()
            if w:
                words.append(re.split(r"[\\/]", w[0])[-1])
        for ad in engines.user_adapters():
            words += [h for h in ad.heads if isinstance(h, str) and h]
        words = [w for w in dict.fromkeys(words) if w]
        if not words:
            return False
        return bool(re.search(r"(?<![\w.-])(?:%s)(?![\w-])" % "|".join(re.escape(w) for w in words), command, re.I))
    except Exception:
        return True             # the settings or the adapters cannot be loaded: what the person added is not known, so the command is judged rough (the strict side)


def rough_destructive(command):
    """Does this command text carry a rough sign of destroying or overwriting something?"""
    return isinstance(command, str) and (bool(ROUGH.search(command)) or _rough_extra(command))


def _q(p, shell):
    """One path quoted for the shell the command came from: single quotes, nothing inside is expanded
    (`$`, a backtick and `"` are harmless in single quotes)."""
    if shell == "powershell":
        return "'" + p.replace("'", "''") + "'"
    return "'" + p.replace("'", "'\\''") + "'"


def _block(what, shell="bash", **kw):
    from shedkeeper import advice
    return advice.block(what, shell=shell, kit=_fwd(os.path.join(KIT, "shedkeeper.py")), quote=_q, **kw)


# What makes a write whose target is not a literal path a write into a guarded place (the settings of the kit, the journals of the store the confirmations are kept in).
# Anywhere in the command (a variable spelled out above the write, a path written round it): the folder and the file of the settings, the variable that names the
# settings, the names of the journals. This guards against an agent that MISTAKES the way, not against one that looks for a way round: a command is a program of a
# language with no end, so what the guards cannot see is a declared limit (README, "Risks"); a weakening in the ini that was not made through `set` is told at the start of a session.
# (A bare `.claude` is NOT a mark: `.claude/hooks`, `.claude/settings.json` are ordinary work, and the kit guards only its own settings.)
GUARD_MARKS_ANYWHERE = re.compile(r"\.shedkeeper\b|shedkeeper\.ini|SHEDKEEPER_INI|confirm\.jsonl|proofs\.jsonl", re.I)


def _guarded_mention(command, opaque):
    """The sign that a write with an unreadable target goes to a guarded place, or ''."""
    m = GUARD_MARKS_ANYWHERE.search(command)
    return m.group(0) if m else ""


def _deny_opaque(opaque, mark, shell="bash"):
    shown = ", ".join(dict.fromkeys(t for _f, t in opaque))[:200]
    return ("shedkeeper: this command writes to a path that is not spelled out (%s), and it points at something ShedKeeper guards (%s: the settings of "
            "ShedKeeper, or its journals). Refused: the guard can only judge a path written out LITERALLY (no variable, no substitution, no "
            "mask). Write the path out literally." % (shown, mark)) + " " + _block(
        "the command writes to a place whose name ShedKeeper cannot read from the text (%s), and the text points at a file ShedKeeper guards (its "
        "settings, or its journals), so it was stopped" % shown, shell,
        agent="write the path out literally in the command (no variable, no substitution, no mask) and run it again; the settings of ShedKeeper are changed "
              "with `python %s set KEY VALUE`, and its journals are written by ShedKeeper only" % _q(_fwd(os.path.join(KIT, "shedkeeper.py")), shell),
        user="make that change themselves, outside the agent, if they mean to write there", no_setting=True)


def _add_notice(state, text):
    """A line for the PERSON (systemMessage of the answer), added to what is there."""
    if text:
        state["notice"] = (state["notice"] + " " + text) if state.get("notice") else text


def _guard_copy(res, path, settings, shell="bash"):
    """None when the action may go on; else the refusal text. The guard of invariant I1: a change
    is not allowed to run when its copy was not taken."""
    st = res.get("status")
    if st in ("ok", "same"):
        return None
    if st == "toolarge":
        if settings["on_too_large"] == "allow":
            return None
        return ("shedkeeper: no copy of %s was taken -- %s. The change is refused. A change of "
                "a big file without a copy is allowed with `set on_too_large allow` (the user's confirmation code), or the ceiling is raised with "
                "`set max_copy_mb N` (now %d)." % (path, res.get("msg", ""), settings["max_copy_mb"])) + " " + _block(
            "the file %s is bigger than the %d MB that ShedKeeper copies before a change, so no copy could be made and the change was "
            "stopped to keep the file safe" % (path, settings["max_copy_mb"]), shell,
            user="make the change themselves outside the agent, or copy the file by hand first and then allow the change with a setting",
            settings=[("on_too_large", "allow")])
    return ("shedkeeper: no copy of %s could be taken (%s). The change is refused so that nothing is "
            "overwritten without a copy." % (path, res.get("msg", "unknown reason"))) + " " + _block(
        "ShedKeeper could not copy the file %s before the change (%s), so it stopped the change to keep the file safe" % (
            path, res.get("msg", "unknown reason")), shell,
        agent="run `python %s status` to see the state of the store" % _q(_fwd(os.path.join(KIT, "shedkeeper.py")), shell),
        user="remove the cause (free space, rights on the folder, a file that is locked), then tell the agent to try again",
        no_setting=True)


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


def _cap_paths(paths, shell="bash", budget=PATH_BUDGET):
    """(the paths that fit `budget` characters as they stand in the JSON of the answer, how many more there are). At least one is shown."""
    shown, used = [], 0
    for p in paths:
        cost = len(json.dumps(_q(p, shell), ensure_ascii=True)) + 1
        if shown and used + cost > budget:
            break
        shown.append(p)
        used += cost
    return shown, len(paths) - len(shown)


def _write_refusal_list(paths, cwd, settings, store, state):
    """The full list of paths of a refusal that had to be cut: a file in the store (`refusals`), its path -- or None when no store can be used."""
    try:
        root = store.store_root(cwd, settings)
        ok, _note, rep = store.ensure_store_report(root, settings)
        if rep:
            state["created"] = state.get("created") or rep
        if not ok:
            return None
        d = os.path.join(root, "refusals")
        os.makedirs(d, exist_ok=True)
        f = os.path.join(d, "paths_%s_%d.txt" % (time.strftime("%Y%m%d_%H%M%S"), os.getpid()))
        with open(f, "w", encoding="utf-8") as fh:
            fh.write("\n".join(paths) + "\n")
        return f
    except Exception:
        return None


def _trash_command(paths, shell="bash", reason="<why>"):
    shedkeeper_py = _fwd(os.path.join(KIT, "shedkeeper.py"))
    quoted = " ".join(_q(p, shell) for p in paths)
    return "python %s trash %s --reason %s" % (_q(shedkeeper_py, shell), quoted, _q(reason, shell))


def _deny_delete(existing, unresolved, shell="bash", listing=None):
    if existing:
        total = len(existing)
        existing, more = _cap_paths(existing, shell)
        text = ("shedkeeper: deleting is not done here -- move to the trash instead; a person can bring "
                "it back. Run: %s" % _trash_command(existing, shell))
        if more:
            text += (" -- the command names %d existing paths; the first %d are in the command above, and %d more (%s). Run the command above, then "
                     "repeat your deletion: what is left is shown the same way." % (
                         total, len(existing), more, ("the full list is the file %s" % listing) if listing else "the command you wrote names them all"))
        if unresolved:
            text += (" -- some paths of your command could not be read from its text (variables, "
                     "masks, piped input); resolve them to literal paths and move those too.")
        return text + " " + _block(
            "the command would DELETE files, and ShedKeeper does not let an agent delete: it moves them to the trash instead, from where "
            "a person can bring them back", shell,
            agent=_trash_command(existing, shell), user="delete the files themselves, outside the agent, if they really want them gone",
            no_setting=True)
    text = ("shedkeeper: deleting is not done here -- move to the trash instead: python %s trash "
            "'<path>' ['<path2>' ...] --reason '<why>' -- the paths of your command could not "
            "be read from its text (variables, masks, piped input); resolve them to literal paths."
            % _q(_fwd(os.path.join(KIT, "shedkeeper.py")), shell))
    return text + " " + _block(
        "the command would DELETE files, and ShedKeeper does not let an agent delete: it moves them to the trash instead, from where a "
        "person can bring them back (the paths of the command could not be read from its text)", shell,
        agent="resolve the paths to literal ones and run `python %s trash '<path>' ['<path2>' ...] --reason '<why>'`" % _q(
            _fwd(os.path.join(KIT, "shedkeeper.py")), shell),
        user="delete the files themselves, outside the agent, if they really want them gone", no_setting=True)


def _deny_store(paths, shell="bash"):
    return ("shedkeeper: the store is changed by a person only -- it holds the copies and the trash, "
            "and nothing here writes into it: %s" % ", ".join(paths[:3])) + " " + _block(
        "the command or edit would change something INSIDE ShedKeeper's store (%s), where the copies and the trash are kept; only a "
        "person changes that folder" % ", ".join(paths[:3]), shell,
        user="change or clean that folder themselves, if they mean to", no_setting=True)


def _deny_settings_write(paths, shell="bash"):
    k = _q(_fwd(os.path.join(KIT, "shedkeeper.py")), shell)
    return ("shedkeeper: a command of an agent does not write into a settings file of ShedKeeper (%s). Change a setting with `python %s set KEY VALUE` "
            "(a weakening needs the person's code)." % (", ".join(paths[:3]), k)) + " " + _block(
        "the command would write straight into a settings file of ShedKeeper (%s), which could weaken the protection without the person's code; "
        "settings change through the `set` command" % ", ".join(paths[:3]), shell,
        agent="run `python %s set KEY VALUE` for the setting (`python %s settings` lists them); if it weakens protection it gives a code for the "
              "person to type" % (k, k),
        user="edit the file themselves, outside the agent, if they want to", no_setting=True)


def _project_root(cwd, fallback, settings, store):
    return store.store_root(cwd or fallback, settings)


def _deny_weakening(path, weak, shell="bash"):
    """An edit of an ini file the kit reads that WEAKENS protection is refused; the way is `set`, which asks the person for a code."""
    what = "; ".join("%s = %s (%s)" % (k, v or "(empty)", eff) for k, v, eff in weak)
    first_key, first_val = weak[0][0], weak[0][1]
    return ("shedkeeper: this edit of %s would weaken protection -- %s. Refused: a weakening is applied only when the person confirms it "
            "with a code, through `set`." % (path, what)) + " " + _block(
        "the edit would weaken what ShedKeeper protects (%s); a weakening is made only with the person's confirmation code, which `set` asks for" % what, shell,
        agent="run `python %s set %s %s`: it gives a code; ask the user to type exactly CONFIRM <code> in their own message; then run the "
              "same set command again" % (_q(_fwd(os.path.join(KIT, "shedkeeper.py")), shell), first_key, first_val or "''"),
        user="type the confirmation code the agent shows (CONFIRM SK-nnnnnn), or edit the ini file themselves, outside the agent",
        no_setting=True)


def _deny_unresolved(path, shell="bash"):
    """An edit of an ini file the kit reads that cannot be worked out here: nothing says it does not weaken protection."""
    return ("shedkeeper: this edit of %s could not be worked out here (the text it replaces is not found in the file as ShedKeeper reads it), so it cannot "
            "be told whether it weakens protection. Refused." % path) + " " + _block(
        "an edit of %s could not be worked out here -- the text it replaces is not found in the file as ShedKeeper reads it -- so nothing says it does "
        "not weaken protection" % path, shell,
        agent="change a setting with `python %s set KEY VALUE` (a weakening needs the user's confirmation code), or make the edit again with the text "
              "exactly as the file has it" % _q(_fwd(os.path.join(KIT, "shedkeeper.py")), shell),
        user="edit the ini file themselves, outside the agent", no_setting=True)


def _ask_to_switch_off(what, canon, cmd_text, cwd, session, settings, shell="bash"):
    """The protection is about to be switched off by an ordinary way (`selfguard`): only the person's confirmation code lets it through (the journal and the
    code are those of `set`, the key is `uninstall`). -> None when the person confirmed (the code is spent), else the text of the refusal."""
    from shedkeeper import confirm, selfguard
    status, text, code = confirm.gate(selfguard.KEY, canon, settings, cwd, session, shell=shell, effect=selfguard.EFFECT, cmd=cmd_text,
                                      what="switches ShedKeeper's protection off (%s)" % what, verb="")
    if status == "apply":
        if confirm.spend(code, cwd, settings):
            return None
        text = "REFUSED: the confirmation code could not be marked as used, so nothing was let through."
    return ("shedkeeper: this would switch the protection of ShedKeeper OFF (%s). Refused unless the person confirms it. %s" % (what, text)) + " " + _block(
        "this would switch ShedKeeper's protection off (%s); the person decides, and their confirmation code is the way on" % what, shell,
        user="type the confirmation code (CONFIRM SK-nnnnnn) in their own message, or do it themselves outside the agent (`python install.py uninstall`, "
             "`/plugin uninstall %s` inside Claude Code, or edit the settings file)" % "shedkeeper@shedkeeper", no_setting=True)


def _decide_edit(tool, tool_input, cwd, session, settings, store, state):
    path = tool_input.get(EDIT_FIELDS[tool])
    if not isinstance(path, str) or not path:
        return "allow", "", {"targets": 0}
    ap = os.path.abspath(os.path.join(cwd, path) if cwd and not os.path.isabs(path) else path)
    if store.is_store_path(ap, settings):
        return "deny", _deny_store([ap]), {"targets": 1, "zone": "store"}
    try:
        from shedkeeper import selfguard
        off = selfguard.edit_removes_protection(ap, tool, tool_input)
    except Exception as e:                  # a settings file of Claude Code that cannot be checked: the strict side; any other file: nothing here to say
        off = ""
        try:
            from shedkeeper import selfguard as _sg
            if _sg.is_claude_settings(ap):
                off = "the edit could not be checked (%s: %s)" % (e.__class__.__name__, e)
        except Exception:
            pass
    if off:
        why = _ask_to_switch_off("this edit of %s %s" % (ap, off), "edit of %s: %s" % (ap.replace("\\", "/"), off), "the same edit of %s" % ap, cwd, session, settings)
        if why:
            return "deny", why, {"targets": 1, "zone": "claude-settings"}
    try:
        from shedkeeper import config, settings_cli
        read = {os.path.normcase(os.path.abspath(f)) for f in config.ini_files()} | {
            os.path.normcase(os.path.abspath(config.target_ini()))}
        if os.path.normcase(ap) in read:
            weak = settings_cli.edit_weakens(ap, tool, tool_input, settings)
            if weak:
                return "deny", _deny_weakening(ap, weak), {"targets": 1, "zone": "ini"}
            if settings_cli.edit_unresolved(ap, tool, tool_input):
                return "deny", _deny_unresolved(ap), {"targets": 1, "zone": "ini"}
            try:
                msg = settings_cli.edit_message(ap, tool, tool_input, settings)
                if msg:
                    state["setting_notice"] = msg
            except Exception:               # the words for the person are a convenience; the check above is the guard
                pass
    except Exception as e:                  # cannot tell whether this is a settings file, or whether the edit weakens -> the strict side
        return "deny", ("shedkeeper: this edit of %s could not be checked against the settings files of ShedKeeper (%s: %s). Refused. Change a "
                        "setting with `python %s set KEY VALUE`, or edit the file outside the agent."
                        % (ap, e.__class__.__name__, e, _q(_fwd(os.path.join(KIT, "shedkeeper.py")), "bash"))), {"targets": 1, "zone": "ini"}
    if not store.isfile(ap):
        return "allow", "", {"targets": 0}
    root = _project_root(cwd, ap, settings, store)
    ok, note, state["created"] = store.ensure_store_report(root, settings)
    if not ok:
        return "deny", ("shedkeeper: the store is not usable, so no copy can be made before this change: "
                        "%s. The change is refused. (To remove the hook: python install.py uninstall.)"
                        % note) + " " + _block(
            "the folder where ShedKeeper keeps its copies cannot be used (%s), so no copy of %s can be made and the change was stopped" % (
                note, ap), "bash", agent="run `python %s status` to see the state of the store" % _q(_fwd(os.path.join(KIT, "shedkeeper.py")), "bash"),
            user="repair the folder (see `status`), or remove the hook if they do not want ShedKeeper (python install.py uninstall)",
            no_setting=True), {"targets": 1, "root": root}
    res = store.copy_version(ap, root, settings, session, tool)
    why = _guard_copy(res, ap, settings)
    meta = {"targets": 1, "root": root, "copy": res.get("status")}
    if why:
        return "deny", why, meta
    if res.get("status") == "toolarge":
        store.log(root, {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": "no_copy_allowed",
                         "path": ap, "msg": res.get("msg")})
        from shedkeeper import db as _db
        _add_notice(state, _db.no_copy_notice(ap, "on_too_large", res.get("msg")))
        return "context", "shedkeeper: %s -- no copy (on_too_large = allow)" % res.get("msg"), meta
    if res.get("status") == "ok":
        return "context", "shedkeeper: copy saved (%s)" % (
            res.get("id") if res.get("indexed") else "NOT INDEXED: " + res.get("msg", "")), meta
    return "allow", "", meta


def _db_root(base, cwd, settings, store):
    """The personal folder a copy of this database is written into: the volume of the project (cwd); a SQLite file -- the
    volume of the file itself."""
    return store.store_root(getattr(base, "path", "") or cwd, settings)


def _roots_for(cwd, settings, store):
    """roots_for(base) of db.plan: where a copy of this database may be listed. None (an MCP server: no database named yet) -> every
    personal folder of the user, and the one of the project."""
    def roots_for(base):
        if base is None:
            try:
                extra = store.all_roots(settings)
            except Exception:
                extra = []
            return [r for r in dict.fromkeys([store.store_root(cwd, settings)] + list(extra)) if r]
        return [r for r in dict.fromkeys([store.store_root(cwd, settings), _db_root(base, cwd, settings, store)]) if r]
    return roots_for


def _db_plan(command, cwd, shell, settings, store, state):
    """The plan for the databases a command destroys (shedkeeper/db.py), or None: nothing to do (no such action, or
    db_protection = off). Marks the command as recognized: a fault after this point is a refusal."""
    if not settings.get("db_protection", True):
        return None
    from shedkeeper import db
    roots_for = _roots_for(cwd, settings, store)
    db.load_proofs(roots_for)             # the proofs of the adapters of the user BEFORE a command is read: a fault of a proven adapter is a refusal
    actions = db.db_actions(command, cwd, shell, settings.get("db_command", ()))
    if not actions:
        return None
    state["recognized"] = True
    return db.plan(actions, cwd, settings, roots_for, quote=_q, kit_py=_fwd(os.path.join(KIT, "shedkeeper.py")), shell=shell)


def _decide_mcp(data, tool, tool_input, cwd, session, settings, store, state):
    """A call of an MCP tool: the same decision as for a command, over the database behind the server."""
    if not settings.get("db_protection", True):
        return "allow", "", {"targets": 0}
    from shedkeeper import db, dbmcp
    cwd = cwd or os.getcwd()
    roots_for = _roots_for(cwd, settings, store)
    db.load_proofs(roots_for)
    dbmcp.take_notes()                    # (a hook lives for one call; a test runs many in one process)
    acts = dbmcp.actions_for(tool, tool_input, data.get("mcp_server"), cwd, settings)
    _journal_mcp_notes(cwd, settings, store, state, tool)
    if not acts:
        return "allow", "", {"targets": 0}
    state["recognized"] = True
    kit_py = _fwd(os.path.join(KIT, "shedkeeper.py"))
    pl = db.plan(acts, cwd, settings, roots_for, quote=_q, kit_py=kit_py, shell="bash")
    if pl.refusal:
        return "deny", pl.refusal, {"targets": len(acts)}
    text_in = "MCP call %s: %s" % (tool, ", ".join(dict.fromkeys(r for a in acts for r in a.reasons))[:300])
    refusal, text, created, dbmeta = db.execute(
        pl, cwd, session, settings, store, text_in, lambda b: _db_root(b, cwd, settings, store), quote=_q, kit_py=kit_py, shell="bash")
    state["created"] = state.get("created") or created
    _add_notice(state, dbmeta.get("notice"))
    state["warn_marks"] = dbmeta.get("warn_marks") or []
    meta = {"targets": len(pl.items)}
    if dbmeta.get("unsupported"):
        meta["unsupported"] = dbmeta["unsupported"]
    if refusal:
        return "deny", refusal, meta
    return "context", text, meta


def _journal_mcp_notes(cwd, settings, store, state, tool):
    """What the reader of an MCP call could not do (a settings file that cannot be read, the 400 strings / 5 MB limits) is a line in the journal of
    the store: a part that was not read is not silence. The store is made when it must hold such a line."""
    try:
        from shedkeeper import dbmcp
        notes = dbmcp.take_notes()
        if not notes:
            return
        root = store.store_root(cwd, settings)
        ok, _note, rep = store.ensure_store_report(root, settings)
        if rep:
            state["created"] = state.get("created") or rep
        if ok:
            for ev, why in notes:
                store.log(root, {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": ev, "tool": tool, "why": why})
    except Exception:
        pass


def _db_root_of(items, cwd, settings, store):
    """The folder the log line of a database decision goes to: the one of the first item whose base is a real database (an item of an
    unsupported engine or of an MCP server with no copy has none: the folder of the project)."""
    for it in items:
        b = it.base
        if b is not None and hasattr(b, "conninfo") and hasattr(b, "path"):        # a real database (db.Base), not an unsupported engine
            return _db_root(b, cwd, settings, store)
    return store.store_root(cwd, settings)


def _decide_cmd(tool, tool_input, cwd, session, settings, store, recognize, state):
    command = tool_input.get("command")
    if not isinstance(command, str) or not command.strip():
        return "allow", "", {"targets": 0}
    shell = "powershell" if tool == "PowerShell" else "bash"
    cwd = cwd if isinstance(cwd, str) and cwd else os.getcwd()
    try:
        from shedkeeper import settings_cli
        msg = settings_cli.hook_message(command, cwd, shell, settings)
        if msg:
            state["setting_notice"] = msg   # the person is shown the change of a setting; the command itself is not stopped
    except Exception:
        pass
    # --- the protection switched off by an ordinary command (`install.py uninstall`, `claude plugin uninstall|disable shedkeeper`)
    from shedkeeper import selfguard
    off = selfguard.protection_off_commands(command, cwd, shell)
    if off:
        state["recognized"] = True
        why = _ask_to_switch_off("; ".join(off), "command " + off[0], "the same command", cwd, session, settings, shell)
        if why:
            return "deny", why, {"targets": 0, "zone": "claude-settings"}
    # --- deletion: refuse, with the ready trash command
    dele = recognize.delete_targets(command, cwd, shell)
    if dele.found:
        state["recognized"] = True
        inside = [p for p in dele.paths if store.is_store_path(p, settings)]
        if inside:
            return "deny", ("shedkeeper: the store is cleaned by a person only -- it holds the copies and the "
                            "trash: %s" % ", ".join(inside[:3])) + " " + _block(
                "the command would delete something inside ShedKeeper's store (%s), where the copies and the trash are kept; only a person "
                "cleans that folder" % ", ".join(inside[:3]), shell,
                user="clean that folder themselves, if they mean to", no_setting=True), {"targets": len(dele.paths)}
        existing = [p for p in dele.paths if store.lexists(p)]
        if existing or dele.unresolved:
            listing = None
            if existing and _cap_paths(existing, shell)[1]:
                listing = _write_refusal_list(existing, cwd, settings, store, state)
            return "deny", _deny_delete(existing, dele.unresolved, shell, listing), {"targets": len(existing)}
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
        return "deny", _deny_store(inside, shell), {"targets": len(inside)}
    from shedkeeper import settings_cli          # (a fault in the scan below goes up to main(): refused when the text carries a rough sign)
    appends = recognize.append_targets(command, cwd, shell)      # (an append overwrites nothing, so it is not copied -- but it is no less a write)
    inside_app = [p for p in appends if store.is_store_path(p, settings)]
    if inside_app:                              # the store (the journals in it too) is changed by a person or by ShedKeeper, as for `>` and a python rewrite
        return "deny", _deny_store(inside_app, shell), {"targets": len(inside_app)}
    guarded = paths + appends
    ini_hit = [p for p in guarded if settings_cli.is_settings_file(p)]
    if ini_hit:
        return "deny", _deny_settings_write(ini_hit, shell), {"targets": len(ini_hit), "zone": "ini"}
    opaque = recognize.unreadable_writes(command, cwd, shell)          # a write whose target is not a literal path (`>> $F`, `tee $(...)`, `cp x ${DEST}`)
    if opaque:
        mark = _guarded_mention(command, opaque)
        if mark:
            state["recognized"] = True
            return "deny", _deny_opaque(opaque, mark, shell), {"targets": len(opaque), "zone": "opaque"}
    if notes:
        return "deny", ("shedkeeper: this command names targets that cannot be listed in full (%s). Refused: "
                        "what is not named cannot be copied first. Name the files with literal paths, "
                        "or split the command." % "; ".join(notes[:3])) + " " + _block(
            "the command overwrites files that ShedKeeper cannot list in full (%s), and what is not listed cannot be copied first, so it "
            "was stopped" % "; ".join(notes[:3]), shell,
            agent="run the command again naming the files with literal paths, or split it into smaller commands",
            user="run the command themselves, outside the agent", no_setting=True), {"targets": len(paths)}
    budget = [GIT_TOTAL_S]
    for gdir in rollbacks:
        files, err = _git_modified(gdir, budget)
        if err:
            return "deny", ("shedkeeper: this command would overwrite files with git, and the list of them "
                            "could not be made (%s). Refused: nothing can be copied first." % err) + " " + _block(
                "the command would overwrite files with git, and ShedKeeper could not list which files (%s), so nothing can be copied first "
                "and the command was stopped" % err, shell,
                agent="run `git status` and `git diff --stat` to see what would be overwritten; the user decides",
                user="run the git command themselves, outside the agent, if they mean to overwrite the files", no_setting=True), {"targets": 0}
        paths += files
    seen, todo = set(), []
    for p in paths:
        ap = os.path.abspath(p)
        key = os.path.normcase(ap)
        if key in seen or store.is_store_path(ap, settings) or not store.isfile(ap):
            continue
        seen.add(key)
        todo.append(ap)
    root = store.store_root(cwd, settings) if todo else None    # only now: a command with nothing to copy never needs the store
    if todo and len(todo) > settings["cmd_max_files"]:
        return "deny", ("shedkeeper: this command overwrites %d files; the ceiling is cmd_max_files = %d. "
                        "Refused, not skipped. Split the command, or raise the ceiling with `set cmd_max_files N`."
                        % (len(todo), settings["cmd_max_files"])) + " " + _block(
            "the command would overwrite %d files, more than the %d that ShedKeeper copies for one command, so it was stopped" % (
                len(todo), settings["cmd_max_files"]), shell,
            agent="split the command into parts of fewer files",
            user="run the command themselves, outside the agent",
            settings=[("cmd_max_files", len(todo))]), {"targets": len(todo), "root": root}
    # --- databases: every refusal (here, after the file refusals above) comes before any copy
    dbplan = _db_plan(command, cwd, shell, settings, store, state)
    if dbplan is not None and dbplan.refusal:
        return "deny", dbplan.refusal, {"targets": len(todo), "root": root}
    dbitems = dbplan.items if dbplan is not None else []
    if not todo and not dbitems:
        return "allow", "", {"targets": 0}
    dbtext = ""
    if dbitems:
        from shedkeeper import db
        refusal, dbtext, created, _dbmeta = db.execute(
            dbplan, cwd, session, settings, store, command, lambda b: _db_root(b, cwd, settings, store),
            quote=_q, kit_py=_fwd(os.path.join(KIT, "shedkeeper.py")), shell=shell)
        state["created"] = state.get("created") or created
        _add_notice(state, _dbmeta.get("notice"))
        state["warn_marks"] = _dbmeta.get("warn_marks") or []
        if refusal:
            return "deny", refusal, {"targets": len(todo) + len(dbitems), "root": root}
        if not todo:
            return "context", dbtext, dict({"targets": len(dbitems), "root": _db_root_of(dbitems, cwd, settings, store)},
                                           **({"unsupported": _dbmeta["unsupported"]} if _dbmeta.get("unsupported") else {}))
    ok, note, rep = store.ensure_store_report(root, settings)
    state["created"] = state.get("created") or rep
    if not ok:
        return "deny", ("shedkeeper: the store is not usable, so no copy can be made before this command: "
                        "%s. The command is refused." % note) + " " + _block(
            "the folder where ShedKeeper keeps its copies cannot be used (%s), so no copy can be made before the command and it was stopped" % note,
            shell, agent="run `python %s status` to see the state of the store" % _q(_fwd(os.path.join(KIT, "shedkeeper.py")), shell),
            user="repair the folder (see `status`), or remove the hook if they do not want ShedKeeper (python install.py uninstall)",
            no_setting=True), {"targets": len(todo), "root": root}
    t0 = time.monotonic()
    made, notes = 0, []
    for i, ap in enumerate(todo):
        if time.monotonic() - t0 > settings["cmd_deadline_s"]:
            left = len(todo) - i
            return "deny", ("shedkeeper: this command overwrites %d files; %d are left without a copy after "
                            "the ceilings (cmd_max_files = %d, cmd_deadline_s = %d). Refused, not skipped. "
                            "Split the command, or raise the ceilings with `set cmd_max_files N` and `set cmd_deadline_s N` (the second asks for the user's code)."
                            % (len(todo), left, settings["cmd_max_files"], settings["cmd_deadline_s"])) + " " + _block(
                "the copies of the %d files this command overwrites did not fit the time ShedKeeper has (%d s), so it was stopped" % (
                    len(todo), settings["cmd_deadline_s"]), shell,
                agent="split the command into parts of fewer files", user="run the command themselves, outside the agent",
                settings=[("cmd_deadline_s", settings["cmd_deadline_s"] + 10)]), \
                   {"targets": len(todo), "root": root}
        res = store.copy_version(ap, root, settings, session, tool)
        why = _guard_copy(res, ap, settings, shell)
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
            from shedkeeper import db as _db
            _add_notice(state, _db.no_copy_notice(ap, "on_too_large", res.get("msg", "")))
    text = "shedkeeper: %d cop%s saved before this command" % (made, "y" if made == 1 else "ies")
    if notes:
        text += "; " + "; ".join(notes[:3])
    if dbtext:
        text += "; " + dbtext[len("shedkeeper: "):]
    return "context", text, dict({"targets": len(todo) + len(dbitems), "root": root, "copies": made},
                                 **({"unsupported": _dbmeta["unsupported"]} if dbitems and _dbmeta.get("unsupported") else {}))


def decide(data, state=None):
    """(decision, text, meta) for one hook input; decision is 'allow' | 'deny' | 'context'."""
    state = state if state is not None else {}
    from shedkeeper import config, recognize, store
    tool = data.get("tool_name")
    if isinstance(tool, str) and tool.startswith("mcp__"):
        ti = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
        sid = data.get("session_id") if isinstance(data.get("session_id"), str) else ""
        wd = data.get("cwd") if isinstance(data.get("cwd"), str) else ""
        settings, _problems = config.load(integrity=False)
        state["settings"] = settings
        return _decide_mcp(data, tool, ti, wd, sid, settings, store, state)
    if not isinstance(tool, str) or (tool not in EDIT_FIELDS and tool not in CMD_TOOLS):
        return "allow", "", {}
    tool_input = data.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    session = data.get("session_id") if isinstance(data.get("session_id"), str) else ""
    cwd = data.get("cwd") if isinstance(data.get("cwd"), str) else ""
    settings, _problems = config.load(integrity=False)
    state["settings"] = settings
    if tool in EDIT_FIELDS:
        return _decide_edit(tool, tool_input, cwd, session, settings, store, state)
    return _decide_cmd(tool, tool_input, cwd, session, settings, store, recognize, state)


def _safe_block(what, **kw):
    """_block that cannot raise: a fault in building the instruction must never turn a refusal into a pass or into a crash."""
    try:
        return _block(what, **kw)
    except Exception:
        return ""


def _with_block(decision, text):
    """A refusal that carries no instruction to the agent gets the generic one (advice.generic): the person decides, nothing is invented."""
    if decision != "deny" or not isinstance(text, str):
        return text
    try:
        from shedkeeper import advice
        if advice.has_block(text):
            return text
        return text + " " + advice.generic(text, kit=_fwd(os.path.join(KIT, "shedkeeper.py")), quote=_q)
    except Exception:
        return text


SHORTENED = " [... shortened: a hook answer may not be longer than 10000 characters ...] "


def _build(decision, text, message):
    if decision == "deny":
        out = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": text}}
    elif decision == "context" and text:
        out = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": text}}
    elif message:
        out = {}
    else:
        return None
    if message:
        out["systemMessage"] = message
    return out


def _cut(text, n):
    """`text` kept to n characters: the beginning and the end (the instruction to the agent stands at the end), the middle named as shortened."""
    if len(text) <= n:
        return text
    head = int(n * 0.6)
    return text[:head] + SHORTENED + text[len(text) - max(0, n - head):]


def _fit(decision, text, message):
    """The JSON string of the answer, at most MAX_OUT characters, valid, with the decision unchanged: the TEXT is shortened, never the JSON."""
    out = _build(decision, text, message)
    if out is None:
        return None
    s = json.dumps(out, ensure_ascii=True)
    if len(s) <= MAX_OUT:
        return s
    if len(message) > 600:
        message = message[:600] + " ..."
        s = json.dumps(_build(decision, text, message), ensure_ascii=True)
        if len(s) <= MAX_OUT:
            return s
    lo, hi = 0, len(text)
    while lo < hi:                       # the longest cut of the text that still fits
        mid = (lo + hi + 1) // 2
        if len(json.dumps(_build(decision, _cut(text, mid), message), ensure_ascii=True)) <= MAX_OUT:
            lo = mid
        else:
            hi = mid - 1
    return json.dumps(_build(decision, _cut(text, lo), message), ensure_ascii=True)


def _emit(decision, text, message=""):
    """One JSON object on one line, under MAX_OUT characters. `message` (the report of a store made just now) goes to the person as the
    top-level `systemMessage`, beside the decision when there is one -- a refusal carries it too."""
    try:
        from shedkeeper import advice
        text = advice.polish(text)
    except Exception:
        pass
    s = _fit(decision, text, message)
    if s is None:
        return
    sys.stdout.write(s)
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


def _guarded_input(data, raw):
    """Is this input one of a tool the kit guards (or one that cannot be told)? `data`: the parsed input or None; `raw`: the bytes or None. Not known -> True."""
    if isinstance(data, dict):
        tool = data.get("tool_name")
        return isinstance(tool, str) and (tool in EDIT_FIELDS or tool in CMD_TOOLS or tool.startswith("mcp__"))
    if isinstance(raw, (bytes, bytearray)):
        return bool(GUARDED_RAW.search(raw) or GUARDED_RAW_MCP.search(raw))
    return True


def _fail_closed(data, raw, exc):
    """The last resort of the hook: something went wrong that no inner guard caught (the output, json.dumps, stdout, the reading of the input). A hook that dies
    with an ordinary error code (1) lets the command run -- Claude Code counts it a non-blocking error; exit code 2 of a PreToolUse hook BLOCKS the call and
    gives stderr to the agent (Claude Code, hooks: "Exit 2 means a blocking error ... PreToolUse blocks the tool call"; stdout and JSON are ignored then).
    -> the exit code: 2 for a guarded tool (the reason is written to stderr), 0 for one we do not guard."""
    if not _guarded_input(data, raw):
        return 0
    try:                        # the words of the fault are made INSIDE the guard: a text that cannot be formed must not turn the stop into a crash
        name = exc.__class__.__name__
        try:
            from shedkeeper import db as _db
            err = _db.mask_secrets("%s: %s" % (name, exc))
        except Exception:       # the mask cannot be loaded: the text of the fault may hold a password -- only the NAME of the fault is shown
            err = "%s (the text is not shown: the mask for secrets could not be loaded)" % name
    except Exception:
        err = "an error (it cannot be named)"
    try:
        sys.stderr.write("shedkeeper: the hook failed after it read the input (%s), so no copy can be trusted before this action and it is STOPPED (exit code 2). "
                         "`python %s status` shows the state; the user removes the hook with python install.py uninstall.\n" % (
                             err[:300], _fwd(os.path.join(KIT, "shedkeeper.py"))))
        sys.stderr.flush()
    except Exception:
        pass
    return 2


def main():
    holder = {}
    try:
        return _main(holder)
    except Exception as e:          # (Exception, not BaseException: an exit or an interrupt is not a fault of ours)
        return _fail_closed(holder.get("data"), holder.get("raw"), e)


def _main(holder):
    raw = _read_input()
    holder["raw"] = raw
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as e:
        if GUARDED_RAW.search(raw) or (GUARDED_RAW_MCP.search(raw) and ROUGH_MCP.search(raw.decode("utf-8", "replace"))):
            _emit("deny", "shedkeeper: the hook input for a guarded tool could not be read as UTF-8 JSON (%s: %s), "
                          "so nothing can be copied first. Refused." % (e.__class__.__name__, e) + " " + _safe_block(
                "the input ShedKeeper got for this action could not be read, so nothing can be copied first and the action was stopped",
                user="try the action again; if it keeps failing, look at `python %s status`" % _q(_fwd(os.path.join(KIT, "shedkeeper.py")), "bash")))
        return 0
    holder["data"] = data
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
        try:
            from shedkeeper import db as _db
            err = _db.mask_secrets(err)             # an address with a password may stand in the text of an error
        except Exception:
            pass
        ti = data.get("tool_input")
        command = ti.get("command") if isinstance(ti, dict) else None
        mcp_rough = isinstance(tool, str) and tool.startswith("mcp__") and bool(
            ROUGH_MCP.search(tool) or ROUGH_MCP.search(json.dumps(ti, ensure_ascii=False, default=str)[:20000]))
        if tool in EDIT_FIELDS or state.get("recognized") or mcp_rough or (tool in CMD_TOOLS and rough_destructive(command)):
            decision, text, meta = "deny", ("shedkeeper: the hook failed (%s), so no copy can be trusted "
                                            "before this action. Refused. (To remove the hook: python "
                                            "install.py uninstall.)" % err) + " " + _safe_block(
                "ShedKeeper itself failed while checking this action (%s), so no copy can be trusted before it and the action was stopped" % err,
                agent="run `python %s status` to see what is wrong" % _q(_fwd(os.path.join(KIT, "shedkeeper.py")), "bash"),
                user="repair or remove ShedKeeper (python install.py uninstall) if the fault stays"), {"error": err}
        else:
            decision, text, meta = "allow", "", {"error": err}
    try:
        from shedkeeper import config, store
        settings, _p = config.load(integrity=False)
        cwd = data.get("cwd") if isinstance(data.get("cwd"), str) else ""
        rec = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "tool": tool, "decision": decision,
               "targets": meta.get("targets", 0), "ms": int((time.monotonic() - t0) * 1000)}
        if meta.get("error"):
            rec["error"] = meta["error"]
        if meta.get("unsupported"):
            rec["unsupported_engine"] = meta["unsupported"]
        if decision != "allow" or meta.get("targets") or meta.get("error"):       # (a pass BECAUSE of a fault is never silent)
            root = meta.get("root") or store.store_root(cwd, settings)
            store.log(root, rec)
    except Exception:
        pass                    # a failing log never changes the decision
    # a refused command did not run: what it would have set is not announced as done
    message = " ".join(x for x in ((state.get("setting_notice") if decision != "deny" else ""), (state.get("notice") if decision != "deny" else "")) if x)
    try:
        from shedkeeper import store
        report = store.creation_report(state.get("created"))
        if report:
            message = report + (" " + message if message else "")
    except Exception as e:      # a failing report never changes the decision -- but it leaves a trace in the log
        try:
            from shedkeeper import config, store
            settings, _p = config.load(integrity=False)
            cwd = data.get("cwd") if isinstance(data.get("cwd"), str) else ""
            store.log(store.store_root(cwd, settings), {"time": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                                        "event": "report_error", "error": "%s: %s" % (e.__class__.__name__, e)})
        except Exception:
            pass
    text = _with_block(decision, text)
    _emit(decision, text, message)
    if decision != "deny" and state.get("warn_marks"):
        try:                    # the once-per-project warning is spent only now that it has been delivered in a decision that lets the command through
            from shedkeeper import survey
            for root_, cwd_, engine_ in state["warn_marks"]:
                survey.mark_unsupported_warned(root_, cwd_, engine_)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    _rc = main()
    for _stream in (sys.stderr, sys.stdout):
        try:
            _stream.flush()
        except Exception:       # a broken pipe: the answer is lost, but the exit code must not become the one the interpreter gives when it flushes again at exit (120)
            pass
    os._exit(_rc)
