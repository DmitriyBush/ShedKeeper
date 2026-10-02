r"""settings_cli.py -- the menu of settings and the change of a setting by a conversation (0.3.0).

  python shedkeeper.py settings [--json]     every setting: key, now, default, allowed, weight, what it does in plain words
  python shedkeeper.py set KEY VALUE         change one (the value is checked by the same reader as the ini; the change is logged)

The agent shows the person the menu and changes a setting ONLY after the person agrees. The person sees every change without asking:
the hook turns a `set` command, and an edit of an ini file, into a `systemMessage` (`hook_message`, `edit_message`).

Where the setting is written: the file `config.target_ini()` -- the one named by SHEDKEEPER_INI when that is set (the kit then reads only
it), else the PERSONAL ini of the user (`~/.shedkeeper/shedkeeper.ini`), which the kit reads AFTER its own shipped shedkeeper.ini and
which a plugin update does not overwrite. Comments and other lines of the file are kept; the write is a temporary file and one
os.replace; the version before is kept as a copy of the store when the store can be used; the change is a line in the log of the store.
"""
import json
import os
import re
import time

from . import advice, config, confirm, store

I1_NOTE = ("No setting here is a silent switch. A copy that was TRIED and FAILED is never let through by a setting (a failed copy stops "
           "the action; the only way past is to switch the guard of databases off as a whole, db_protection = false, which `status` "
           "shows) -- with ONE exception, named: on_db_too_large = allow also lets through a database copy that did not finish in time (the "
           "dump, or the check of it, was cut off by db_deadline_s): too slow to copy counts as too big to copy. The settings of weight "
           "PROTECTION let some actions through WITHOUT a copy -- loudly, with a line in the log -- or switch a guard off: changing them "
           "takes the user's word (a confirmation code).")


# ============================================================================================ the menu

def menu(settings):
    """[dict] one per key, in the order of the shipped ini: key, now, default, allowed, weight, effect, about."""
    info = config.describe()
    out = []
    for key in config.KEYS:
        d = info.get(key) or {}
        out.append({"key": key, "now": config.format_value(settings.get(key)), "default": config.format_value(config.DEFAULTS[key]),
                    "allowed": config.allowed_text(key), "weight": d.get("weight") or "unknown", "effect": d.get("effect") or "",
                    "about": d.get("about") or "(no description: the shipped shedkeeper.ini is missing or has no comment for this key)"})
    return out


def render_menu(settings):
    files = config.ini_files()
    L = ["ShedKeeper settings. Read from: %s. `set` writes to: %s." % (", ".join(files) or "(no file: the defaults)", config.target_ini()),
         I1_NOTE,
         "Change one with: python %s set KEY VALUE   (weight: protection = a value can stop protecting something; tuning = a limit or a "
         "place, protection stays; notice = only what you are told)" % advice.kit_py(), ""]
    for m in menu(settings):
        L.append("%s = %s    [default %s; allowed: %s; weight: %s]" % (m["key"], m["now"] or "(empty)", m["default"] or "(empty)", m["allowed"], m["weight"]))
        L.append("    " + m["about"].replace("\n", "\n    "))
        if m["weight"] == "protection" and m["effect"]:
            L.append("    If it is not the default: " + m["effect"])
    return "\n".join(L)


def settings_json(settings):
    return json.dumps({"files": config.ini_files(), "writes_to": config.target_ini(), "note": I1_NOTE, "settings": menu(settings)},
                      indent=2, ensure_ascii=False)


# ============================================================================================ the change

def _parse(key, value):
    """(canonical text of the value, problem). The same reader as the ini: config.apply_value; a value it does not understand is a problem
    and nothing is written."""
    if re.search(r"[#\r\n]", value):
        return None, "a '#' (it starts a comment in the ini) or a line break cannot be part of a value"
    probe = dict(config.DEFAULTS)
    problem = config.apply_value(probe, key, value, "set")
    if problem:
        return None, problem
    return config.canonical(key, value), None


def effective(key):
    """The value of `key` the kit reads NOW (from the files), as text."""
    s, _p = config.load(integrity=False)
    return config.format_value(s.get(key))


def _read_text(path):
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except FileNotFoundError:
        return None, False
    bom = raw.startswith(b"\xef\xbb\xbf")
    return raw.decode("utf-8-sig"), bom


def _write_atomic(path, text, bom):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "wb") as fh:
        fh.write((b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8"))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _journal(settings, cwd, rec):
    """(written, where-or-why). Into the log of the store of the project; the store is made when it does not exist yet (the same thing
    the hook does on first use) unless auto_create_store is off."""
    try:
        root = store.store_root(cwd, settings)
        if not root:
            return False, "no store folder can be named for this folder"
        ok, note, _created = store.ensure_store_report(root, settings)
        if not ok:
            return False, note
        store.log(root, rec)
        return True, os.path.join(root, "log.jsonl")
    except Exception as e:
        return False, "%s: %s" % (e.__class__.__name__, e)


def _in_force(key, settings, canon):
    """Is `canon` the value the kit reads for `key` already (a value put in by hand acts, so `set` of it changes nothing and says nothing of approval)? A list of names
    (`mcp_no_db`) is the same list whatever the order and the separator: `config.format_value` writes `pg, prod`, `canonical` writes `pg prod`."""
    if key == "mcp_no_db":
        return set(config.apply_value_to_tuple(key, canon)) == set(settings.get(key) or ())
    return config.format_value(settings.get(key)) == canon


def set_setting(key, value, settings, cwd, session=""):
    """(exit code, text). 0 changed and in effect; 1 refused (nothing written); 3 written but NOT in effect (the text says why)."""
    key = (key or "").strip().lower()
    if key not in config.DEFAULTS:
        return 1, "REFUSED: %r is not a setting of ShedKeeper. The settings: %s." % (key, ", ".join(config.KEYS))
    canon, problem = _parse(key, value)
    if problem:
        return 1, "REFUSED: %r is not accepted for %s -- nothing was written. Allowed: %s." % (value, key, config.allowed_text(key))
    status, gate_text, ccode = confirm.gate(key, canon, settings, cwd, session)       # a weakening needs the person's code
    if status == "ask":
        return confirm.EXIT_ASK, gate_text
    if status == "refuse":
        return 1, gate_text
    if status == "apply" and not confirm.spend(ccode, cwd, settings):
        return 1, ("REFUSED: the confirmation code %s could not be marked as used in the store, so the weakening was NOT written (a code that is not "
                   "spent could be used again). Nothing was changed; fix the store (`status`) and ask the user for a new code." % ccode)
    path = config.target_ini()
    text, bom = _read_text(path)
    was = config.format_value(settings.get(key))
    new_text = config.edit_text(text or "", key, canon, fresh_header=text is None)
    notes = []
    if text is not None:
        try:
            root = store.store_root(cwd, settings)
            ok, note, _c = store.ensure_store_report(root, settings)
            if ok:
                res = store.copy_version(path, root, settings, session, "set")
                if res.get("status") == "ok" and res.get("id"):
                    notes.append("the file before the change is kept (%s)" % res.get("id"))
                elif res.get("status") == "same":
                    notes.append("the file before the change is kept (an identical copy is in the store already)")
                else:
                    notes.append("NO copy of the file before the change was kept (%s: %s)" % (res.get("status"), res.get("msg") or "no reason given"))
            else:
                notes.append("NO copy of the file before the change was kept (%s)" % note)
        except Exception as e:
            notes.append("NO copy of the file before the change was kept (%s: %s)" % (e.__class__.__name__, e))
    try:
        _write_atomic(path, new_text, bom)
    except OSError as e:
        return 1, "REFUSED: %s cannot be written (%s) -- nothing was changed." % (path, e)
    # what `set` made is written to the journal AFTER the ini: without this line the start of a session marks the value "not through `set`" (config._integrity)
    state_why = ""
    if (config.WEIGHTS.get(key) or ("",))[0] == "protection" and not (
            status == "free" and config.effect_for(key, canon) and _in_force(key, settings, canon)):
        st_ok, st_why = confirm.record_state(key, canon, "confirmed" if status == "apply" else "free", ccode, cwd, settings, session)
        if not st_ok:
            state_why = st_why
    now = effective(key)
    lines = ["setting %s: %s -> %s" % (key, was or "(empty)", now or "(empty)")]
    info = config.describe().get(key) or {}
    if info.get("weight") == "protection" and info.get("effect") and now != config.format_value(config.DEFAULTS[key]):
        lines.append("PROTECTION: " + info["effect"])
    elif now == config.format_value(config.DEFAULTS[key]):
        lines.append("this is the default: protection as designed")
    lines.append("written to: " + path)
    lines += notes
    code = 0
    if state_why:
        lines.append("NOTE: the value is in effect, but the record that `set` made it could not be written to the journal of the store (%s): the start of a session will "
                     "mark it \"not through `set`\". The store is checked with `python %s status`." % (state_why, advice.kit_py()))
    if not _in_effect(key, canon, now):
        code = 3
        lines.append("NOT IN EFFECT: the file was written but the kit reads %r now, not %r -- another file read later overrides it "
                     "(see `python %s settings`)" % (now, canon, advice.kit_py()))
    ok, where = _journal(settings, cwd, {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": "setting_changed", "key": key, "was": was,
                                          "now": now, "session": session or "", "file": path})
    lines.append(("journal: " + where) if ok else "journal: NOT written (%s)" % where)
    if status == "apply":
        lines.append("confirmed by the person with %s; the code is used up (it was marked BEFORE the value was written)" % ccode)
    return code, "\n".join(lines)


def _in_effect(key, canon, now):
    """Is what the kit reads for `key` (`now`, the text read back from the files after the write) the value that was asked for? A list
    key: every asked name is among the effective ones."""
    if key in config.LIST_KEYS:
        have = [x.strip() for x in now.split(",")]
        return all(w in have for w in config.apply_value_to_tuple(key, canon))
    probe = dict(config.DEFAULTS)
    config.apply_value(probe, key, canon, "set")
    return now == config.format_value(probe[key])


# ============================================================================================ what the person is told by the hook

def settings_files():
    """Every file the kit reads its settings from, or would write them to -- whether it exists yet or not (the personal ini is created by
    the first `set`): the normalised paths."""
    out = []
    for f in list(config.ini_files()) + [config.target_ini(), config.DEFAULT_INI, config.user_ini_path()]:
        if f:
            out.append(os.path.normcase(os.path.abspath(f)))
    return set(out)


def is_settings_file(path):
    try:
        return os.path.normcase(os.path.abspath(path)) in settings_files()
    except Exception:
        return True             # a path that cannot be judged is not let through: a guard that fails open is no guard


def _shown(key, value_text):
    probe = dict(config.DEFAULTS)
    problem = config.apply_value(probe, key, value_text, "hook")
    return probe[key], problem


def weakened(settings):
    """[(key, value text, default text, effect)] -- the settings of weight `protection` that stand NOW at a value that weakens protection.
    The shipped default is never a weakening (`on_db_unsupported = allow` is the default and says so in its own warning)."""
    out = []
    for key in config.KEYS:
        d = config.describe().get(key)
        if not d or d["weight"] != "protection":
            continue
        val = config.format_value(settings.get(key))
        default = config.format_value(config.DEFAULTS[key])
        if val == default:
            continue
        eff = config.effect_for(key, val.replace(", ", " ") if key == "mcp_no_db" else val)
        if eff:
            out.append((key, val, default, eff))
    return out


# Settings that name a PROGRAM or a PLACE, not a switch: a value other than the default is a choice (the ini itself recommends `pg_dump_path` when pg_dump is
# not on the PATH), so the reminder says so, with what it means, and gives no advice to blank it -- a blank value would break the copies.
PATH_KEYS = ("pg_dump_path", "store_dir_name")
NOT_THROUGH_SET = " (not through `set`)"
PATH_MEANING = {
    "pg_dump_path": "the copies of PostgreSQL databases, and the check of them, are made by the program you named (with its pg_restore and psql beside it); if it is "
                    "not the real pg_dump, a copy and a check can be unreal",
    "store_dir_name": "the store lives under the name you chose; folders of the old name are no longer guarded as the store",
}


def reminder_lines(settings, kit=None, compact=False):
    """(text for the person, text for the agent) of the start-of-session reminder, or ('', '') when nothing differs. A switch that weakens protection is
    reminded as WEAKENED with the way back; one that was not made through `set` (the settings file was edited by hand, `config._integrity`) carries the mark
    "not through `set`" -- it is in force all the same. A setting that names a program or a place (PATH_KEYS) is reminded as a choice that differs from the
    default, with what it means and no advice to blank it. `compact=True`: the keys and values only, with the two commands that give the rest (what each one
    switches off, the way back) -- for a start whose answer would not fit the ceiling of the runtime."""
    k = kit or advice.kit_py()
    w = weakened(settings)
    if not w:
        return "", ""
    by_hand = {key for key, _v, _e in (getattr(settings, "unconfirmed", ()) or ())}
    sw = [x for x in w if x[0] not in PATH_KEYS]
    ch = [x for x in w if x[0] in PATH_KEYS]
    if compact:
        names = ", ".join("%s = %s%s" % (key, val or "(empty)", NOT_THROUGH_SET if key in by_hand else "") for key, val, _d, _e in w)
        more = "`python %s settings` lists what each one switches off, `python %s status` the state; `python %s set KEY DEFAULT` puts one back" % (k, k, k)
        return ("ShedKeeper: protection is WEAKENED by your settings -- %s (the full text did not fit; %s; strengthening needs no code)." % (names, more),
                "ShedKeeper reminder: protection is weakened by the settings in force -- %s (the full text did not fit; %s; strengthening needs no confirmation)." % (names, more))
    person, agent = [], []
    if sw:
        what = "; ".join("%s = %s (default %s): %s%s" % (key, val or "(empty)", default or "(empty)", eff, NOT_THROUGH_SET if key in by_hand else "")
                         for key, val, default, eff in sw)
        back = " ".join("`python %s set %s %s`" % (k, key, default if default else "''") for key, _v, default, _e in sw)
        person.append("ShedKeeper: protection is WEAKENED by your settings -- %s. To put it back: %s (it needs no code)." % (what, back))
        agent.append("ShedKeeper reminder: protection is weakened by the settings in force -- %s. To put it back: %s (strengthening needs no confirmation)." % (what, back))
        if any(x[0] in by_hand for x in sw):
            person.append("A line marked \"%s\" stands in your settings file with no record that `set` wrote it; it is in force." % NOT_THROUGH_SET.strip(" ()"))
            agent.append("A line marked \"%s\" stands in the settings file with no record that `set` wrote it; it is in force. The user was told at the start of the session."
                         % NOT_THROUGH_SET.strip(" ()"))
    if ch:
        what = "; ".join("%s = %s differs from the default (%s): %s" % (key, val or "(empty)", default or "(empty)", PATH_MEANING.get(key, eff))
                         for key, val, default, eff in ch)
        person.append("ShedKeeper note: %s. Nothing to do if you chose it." % what)
        agent.append("ShedKeeper note: the user chose settings that differ from the default -- %s." % what)
    return " ".join(person), " ".join(agent)


def change_message(key, was, new_display, valid=True):
    info = config.describe().get(key) or {}
    text = "ShedKeeper setting %s: %s -> %s" % (key, was or "(empty)", new_display or "(empty)")
    if not valid:
        text += " (the value is not valid: the command will be refused)"
    elif info.get("weight") == "protection" and info.get("effect") and new_display != config.format_value(config.DEFAULTS[key]):
        text += ". " + info["effect"]
    return text


SET_RE = re.compile(r"^set$", re.I)


def kit_calls(command, cwd, shell="bash"):
    """[[word after shedkeeper.py, ...]] for every `... shedkeeper.py WORD ...` the command RUNS (a mention in `echo`/`grep` is not it: the
    scanner of recognize.py tells the places where a command is executed)."""
    from . import recognize
    found = []

    class Obs(recognize.Deletion):
        def visit(self, head, args, pre, ctx):
            if not recognize.PYTHONS.match(head):         # python, python3, python3.12, pythonw, py ...
                return
            a = [x for x in args if not (x.startswith("-") and len(x) > 1 and x[1].isalpha() and not x.endswith(".py"))]
            for i, x in enumerate(a):
                if re.split(r"[\\/]", x)[-1].lower() == "shedkeeper.py":
                    found.append(a[i + 1:])
                    return

    if not isinstance(command, str) or "shedkeeper" not in command.lower():
        return []
    try:
        recognize._walk(command, "PowerShell" if shell == "powershell" else "Bash", 0, Obs(), cwd)
    except Exception:
        return []
    return found


def set_commands(command, cwd, shell="bash"):
    """[(key, value)] for every `... shedkeeper.py set KEY VALUE` the command RUNS."""
    return [(rest[1], " ".join(rest[2:])) for rest in kit_calls(command, cwd, shell) if len(rest) >= 2 and SET_RE.match(rest[0])]


def activate_commands(command, cwd, shell="bash"):
    """[engine id] for every `... shedkeeper.py activate ENGINE` the command RUNS."""
    return [rest[1] for rest in kit_calls(command, cwd, shell) if len(rest) >= 2 and rest[0].lower() == "activate"]


def hook_message(command, cwd, shell, settings):
    """The systemMessage for a command that runs `shedkeeper.py set`: '' when there is none."""
    msgs = []
    for key, value in set_commands(command, cwd, shell):
        key = key.lower()
        if key not in config.DEFAULTS:
            msgs.append("ShedKeeper: the agent runs `set %s ...` -- that is not a setting; it will be refused." % key)
            continue
        shown, problem = _shown(key, value)
        was = config.format_value(settings.get(key))
        if key == "db_command":                 # `set` ADDS a rule: say what is added, not a list that looks like a replacement
            msgs.append("ShedKeeper setting db_command: adds the rule %r" % value if problem is None else
                        "ShedKeeper setting db_command: %r is not valid: the command will be refused" % value)
            continue
        msg = change_message(key, was, config.format_value(shown), valid=problem is None)
        if problem is None:
            canon, bad = _parse(key, value)
            if canon is not None and confirm.weakens(key, canon, settings):
                msg += " [not applied until you type the confirmation code the agent will show you]"
        msgs.append(msg)
    for engine in activate_commands(command, cwd, shell):
        msgs.append("ShedKeeper: the agent runs `activate %s`: the adapter counts as protection only if you type the confirmation code the agent shows you" % engine)
    return " | ".join(msgs)


def _replace_once(text, old_s, new_s, every):
    """`text` with `old_s` replaced by `new_s` (every occurrence or the first), or None when `old_s` is not in it. The Edit tool of Claude Code finds the text
    of a file that has CRLF line ends with the LF it was given, and writes the file back with its own line ends: so a text that is not found as it stands is
    looked for once more with its line ends turned into CRLF (and its new text the same way)."""
    if not isinstance(old_s, str) or not isinstance(new_s, str) or not old_s:
        return None
    if old_s not in text and "\r\n" in text:
        crlf = lambda s: s.replace("\r\n", "\n").replace("\n", "\r\n")  # noqa: E731
        if crlf(old_s) in text:
            old_s, new_s = crlf(old_s), crlf(new_s)
    if old_s not in text:
        return None
    return text.replace(old_s, new_s) if every else text.replace(old_s, new_s, 1)


def _edited_text(path, tool, tool_input):
    """(text now, text after the edit or None when the edit cannot be computed) of an ini file for Edit / Write / MultiEdit."""
    text, _bom = _read_text(path)
    if text is None:
        text = ""
    new = None
    if tool == "Write":
        c = tool_input.get("content")
        new = c if isinstance(c, str) else None
    elif tool == "Edit":
        new = _replace_once(text, tool_input.get("old_string"), tool_input.get("new_string"), tool_input.get("replace_all"))
    elif tool == "MultiEdit":
        edits = tool_input.get("edits")
        cur = text
        if isinstance(edits, list):
            for e in edits:
                cur = _replace_once(cur, e.get("old_string") if isinstance(e, dict) else None, e.get("new_string") if isinstance(e, dict) else None,
                                    e.get("replace_all") if isinstance(e, dict) else None)
                if cur is None:
                    break
            new = cur
    return text, new


def edit_unresolved(path, tool, tool_input):
    """True when an edit of an ini file the kit reads cannot be worked out here (the text it replaces is not found in the file, the input is not one
    this module reads): then it cannot be told whether the edit weakens protection, and the hook takes the strict side."""
    return _edited_text(path, tool, tool_input)[1] is None


def edit_weakens(path, tool, tool_input, settings):
    """[(key, new value text, effect)] of what an edit of an ini file the kit reads would WEAKEN (the hook refuses it and points at
    `set`, which asks the person for a code). [] when nothing is weakened or the edit cannot be computed."""
    text, new = _edited_text(path, tool, tool_input)
    if new is None:
        return []
    before, _p1 = config.parse_text(text, path)
    after, _p2 = config.parse_text(new, path)
    out = []
    for key in config.KEYS:
        if before.get(key) == after.get(key):
            continue
        canon = config.format_value(after.get(key))
        if key in config.LIST_KEYS:
            if key == "mcp_no_db" and set(after.get(key) or ()) <= set(before.get(key) or ()):
                continue
            canon = " ".join(after.get(key) or ()) if key == "mcp_no_db" else canon
        eff = config.effect_for(key, canon)
        if eff:
            out.append((key, canon, eff))
    return out


def edit_message(path, tool, tool_input, settings):
    """The systemMessage for an edit of an ini file the kit reads: what changes compared with the file as it is now ('' when nothing
    that the kit reads changes, or the edit cannot be computed)."""
    text, new = _edited_text(path, tool, tool_input)
    if new is None:
        return ""
    before, _p1 = config.parse_text(text, path)
    after, _p2 = config.parse_text(new, path)
    msgs = []
    for key in config.KEYS:
        if before.get(key) != after.get(key):
            msgs.append(change_message(key, config.format_value(before.get(key)), config.format_value(after.get(key))))
    return " | ".join(msgs)
