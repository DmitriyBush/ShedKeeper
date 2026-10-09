"""Settings: reads shedkeeper.ini. A value that is not understood becomes the SAFE default and the
reason is returned, so the caller can name it in its output (nothing is ignored silently): the start of a session and `status` do; a decision of a hook
does not (that would be an alarm at every decision), and the safe default it acts on is the strict side.

The product name lives here and nowhere else in the code: PRODUCT and DEFAULT_STORE_DIR.

Where the settings come from (0.3.0):
  - SHEDKEEPER_INI names a file -> ONLY that file is read (and `set` writes to it);
  - otherwise the shipped `shedkeeper.ini` of the kit is read first, then the PERSONAL ini of the user
    (`~/.shedkeeper/shedkeeper.ini`; SHEDKEEPER_USER_HOME replaces `~` for the tests). A plugin update replaces the kit folder -- and
    the shipped ini with it -- but not the personal ini, so what a person set by `shedkeeper.py set` survives an update. `set` writes
    to the personal ini.
  - the shipped ini DESCRIBES each setting for the person (`describe`: the comment above a key). The WEIGHT of a setting -- what a value
    stops protecting -- is decided by the CODE (`WEIGHTS` below), never by a comment of a file an agent can edit; a test compares the two,
    so a difference between the code and the `@weight` lines of the ini is a red test. The code holds also the kind of a value, its
    limits and its default.
"""
import os
import re

PRODUCT = "shedkeeper"
# A switch of the RELEASE, not a setting: nobody (neither the person nor the agent) turns it on from an ini or a command. The code of the databases stays in the kit
# (and keeps being tested with this name replaced by True); in this release the protected list holds files only.
DATABASES_IN_THIS_RELEASE = False
# The timeout the hooks are installed with (install.py reads it too): the check before a step and the accelerators together stay inside it. ONE number for the whole kit.
HOOK_TIMEOUT_S = 90
# The closed list of the tools that only READ: the check before a step lets them pass; every other tool (also one that does not exist yet) is checked.
READ_ONLY_TOOLS = ("Read", "Glob", "Grep", "WebFetch", "WebSearch", "TodoWrite", "TaskList", "TaskGet", "TaskOutput", "ListAgents", "ToolSearch")
# The tools that STOP a process that is running or ASK the person. They change no data and are never refused: an agent that cannot stop a deletion that is running in the background, or
# cannot ask the person what to do, is worse off than one whose check works. A closed list apart from the readers (a reader is let through because it reads; these because they stop or ask).
CONTROL_TOOLS = ("AskUserQuestion", "TaskStop", "KillShell", "ExitPlanMode")
DEFAULT_STORE_DIR = "_shedkeeper"
KIT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_INI = os.path.join(KIT_DIR, "shedkeeper.ini")

TRUE_WORDS = ("true", "1", "yes", "on")
FALSE_WORDS = ("false", "0", "no", "off")

DEFAULTS = {
    "store_dir_name": DEFAULT_STORE_DIR,
    "auto_create_store": True,
    "private_store": True,
    "max_copy_mb": 512,
    "on_too_large": "deny",
    "cmd_max_files": 200,
    "cmd_deadline_s": 20,
    "db_protection": True,
    "pg_dump_path": "",
    "max_db_mb": 200,
    "db_deadline_s": 25,
    "on_db_remote": "deny",
    "on_db_too_large": "deny",
    "on_db_unsupported": "allow",
    "mcp_no_db": (),
    "db_fresh_minutes": 10,
    "db_command": (),
    "step_budget_s": 60,
    "walk_budget_ms": 3000,
    "usn_resolve_max": 2000,
    "lock_wait_s": 30,
    "sqlite_busy_s": 5,
    "max_db_mb_every_step": 50,
    "before_confirm": "accelerators",
}
KEYS = ("store_dir_name", "auto_create_store", "private_store", "max_copy_mb", "on_too_large", "cmd_max_files", "cmd_deadline_s",
        "db_protection", "pg_dump_path", "max_db_mb", "db_deadline_s", "on_db_too_large", "on_db_remote", "on_db_unsupported",
        "db_fresh_minutes", "mcp_no_db", "db_command", "step_budget_s", "walk_budget_ms", "usn_resolve_max", "lock_wait_s", "sqlite_busy_s",
        "max_db_mb_every_step", "before_confirm")
BOOL_KEYS = ("auto_create_store", "private_store", "db_protection")
CHOICE_KEYS = ("on_too_large", "on_db_too_large", "on_db_remote", "on_db_unsupported")
LIST_KEYS = ("mcp_no_db", "db_command")
# a key with a closed set of words: the first word is the safe one (the value used when the text is not understood)
ENUM_KEYS = {"before_confirm": ("accelerators", "provisional")}
# the bool keys whose not understood value is `true` (the safe side); auto_create_store falls to `false`
SAFE_BOOL = {"auto_create_store": False, "private_store": True, "db_protection": True}
SAFE_BOOL_WHY = {"private_store": "the personal folder is closed to other users", "db_protection": "databases are guarded"}
INT_LIMITS = {"max_copy_mb": 10 ** 7, "cmd_max_files": 10 ** 6, "cmd_deadline_s": 3600, "max_db_mb": 10 ** 7,
              "db_deadline_s": 3600, "db_fresh_minutes": 1440, "step_budget_s": 3600, "walk_budget_ms": 600000, "usn_resolve_max": 10 ** 7,
              "lock_wait_s": 3600, "sqlite_busy_s": 3600, "max_db_mb_every_step": 10 ** 7}
# The weight of every setting: (weight, applies, effect). protection = a value can stop protecting something; tuning = a limit or a place,
# protection stays; notice = only what the person is told. `applies`: the values for which the effect holds -- a list of words, `*` (any
# non-empty value), `>default` / `>N` (a number above); an empty list = any value that is not the default. `effect` says what stops being
# protected. The shipped shedkeeper.ini repeats this in its `@weight` comments for the person to read; tests keep the two equal.
WEIGHTS = {
    "store_dir_name": ("protection", (), "the folders of the store under the old name are no longer guarded as the store (the copies and the trash there become ordinary folders an agent may change), and a new, empty store is used from now on"),
    "auto_create_store": ("tuning", (), ""),
    "private_store": ("protection", ("false",), "the copies and the trash of this user are no longer closed to the other users of the machine"),
    "max_copy_mb": ("tuning", (), ""),
    "on_too_large": ("protection", ("allow",), "a change of a file bigger than max_copy_mb goes ahead WITHOUT a copy (a line is written to the log)"),
    "cmd_max_files": ("tuning", (), ""),
    "cmd_deadline_s": ("protection", (">default",), "if the budgets of the hook (cmd_deadline_s + 10 + db_deadline_s + 3) now add up to more than its timeout (90 s), the hook can be cut off and then the action goes ahead UNPROTECTED"),
    "db_protection": ("protection", ("false",), "databases are NOT protected at all: a command that destroys data in a database goes ahead with no copy"),
    "pg_dump_path": ("protection", ("*",), "the copy of a PostgreSQL database and the check of it are made by the program this setting names: a program that is not the real pg_dump (with its pg_restore and psql beside it) can report a copy and a check that are NOT real"),
    "max_db_mb": ("tuning", (), ""),
    "db_deadline_s": ("protection", (">default",), "if the budgets of the hook (cmd_deadline_s + 10 + db_deadline_s + 3) now add up to more than its timeout (90 s), the hook can be cut off and then the action goes ahead UNPROTECTED"),
    "on_db_too_large": ("protection", ("allow",), "a command that destroys a big database (or one that is slow to copy: a copy that did not finish in time) goes ahead WITHOUT a copy (a line is written to the log)"),
    "on_db_remote": ("protection", ("allow",), "a command that destroys data in a database on another machine goes ahead WITHOUT a copy (a line is written to the log)"),
    "on_db_unsupported": ("protection", ("allow",), "a command that destroys data in a database of an engine ShedKeeper cannot copy (MySQL ...) goes ahead WITHOUT a copy, after one warning per project"),
    "db_fresh_minutes": ("protection", (">default",), "an older `dbcopy` copy still counts as fresh, so a command may pass on a copy made before the data last changed"),
    "mcp_no_db": ("protection", ("*",), "the calls of the named servers pass WITHOUT a stop, also the ones that destroy data in a database behind them"),
    "db_command": ("notice", (), ""),
    "step_budget_s": ("protection", (">default",), "the check before a step may now run longer than the hook is allowed to (90 s with the file copies and the database copies of the accelerators); the hook can be cut off and then the step goes ahead UNPROTECTED"),
    "walk_budget_ms": ("tuning", (), ""),
    "usn_resolve_max": ("tuning", (), ""),
    "lock_wait_s": ("tuning", (), ""),
    "sqlite_busy_s": ("tuning", (), ""),
    "max_db_mb_every_step": ("tuning", (), ""),
    "before_confirm": ("tuning", (), ""),
}
_BAD_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


class Settings(dict):
    """The settings the kit reads: a plain dict of the keys of DEFAULTS, and `unconfirmed` -- [(key, value text, effect)] of the weakenings that stand in an ini and ACT, but
    were not made through `set` (no confirmation of the person is in the journal: the file was edited by hand, `_integrity`). The attribute is not a key: code that
    compares or iterates the dict sees only the settings."""
    unconfirmed = ()


def _parse_bool(value):
    v = value.lower()
    if v in TRUE_WORDS:
        return True
    if v in FALSE_WORDS:
        return False
    return None


def _parse_int(value, low, high):
    try:
        n = int(value)
    except ValueError:
        return None
    return n if low <= n <= high else None


# ============================================================================================ one value, one reader

def apply_value(settings, key, value, where):
    """Put `value` (the text after `=`) into `settings[key]` the way a line of the ini does. -> None, or the text of the problem
    (then the SAFE value is in `settings`). The one reader: the ini, `set` and the hook messages all use it."""
    if key in BOOL_KEYS:
        b = _parse_bool(value)
        if b is None:
            settings[key] = SAFE_BOOL[key]
            if key == "auto_create_store":
                return "%s: auto_create_store = %r not understood -> false (safe)" % (where, value)
            return "%s: %s = %r not understood -> true (safe: %s)" % (where, key, value, SAFE_BOOL_WHY[key])
        settings[key] = b
    elif key == "pg_dump_path":
        settings[key] = value
    elif key == "db_command":
        if value.replace("|", " ").split():
            settings[key] = tuple(settings[key]) + (value,)
        else:
            return "%s: db_command is empty, ignored" % where
    elif key == "mcp_no_db":
        names = [x for x in re.split(r"[\s,]+", value) if x]
        settings[key] = tuple(settings[key]) + tuple(names)       # an empty value is "no servers": the default, no problem
    elif key in ENUM_KEYS:
        if value.lower() in ENUM_KEYS[key]:
            settings[key] = value.lower()
        else:
            settings[key] = ENUM_KEYS[key][0]
            return "%s: %s = %r not understood -> %s is used (the words are: %s)" % (where, key, value, ENUM_KEYS[key][0], " | ".join(ENUM_KEYS[key]))
    elif key in CHOICE_KEYS:
        if value.lower() in ("deny", "allow"):
            settings[key] = value.lower()
        else:
            settings[key] = "deny"
            return "%s: %s = %r not understood -> deny (safe)" % (where, key, value)
    elif key == "store_dir_name":
        if value and not _BAD_NAME.search(value) and value not in (".", "..") and value == value.rstrip(". "):
            settings[key] = value
        else:
            settings[key] = DEFAULT_STORE_DIR
            return "%s: store_dir_name = %r is not a plain folder name -> %s" % (where, value, DEFAULT_STORE_DIR)
    else:
        n_val = _parse_int(value, 1, INT_LIMITS[key])
        if n_val is None:
            return "%s: %s = %r not understood -> default %s" % (where, key, value, DEFAULTS[key])
        settings[key] = n_val
    return None


def parse_text(text, label, settings=None, problems=None):
    """(settings, problems) of the text of ONE ini file; `settings`/`problems` given -> the lines are added to them (the layers)."""
    settings = dict(DEFAULTS) if settings is None else settings
    problems = [] if problems is None else problems
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        key, sep, value = (x.strip() for x in line.partition("="))
        where = "%s:%d" % (label, n)
        if not sep or key not in DEFAULTS:
            problems.append("%s: not understood, ignored: %s" % (where, raw.strip()[:80]))
            continue
        pr = apply_value(settings, key, value, where)
        if pr:
            problems.append(pr)
    return settings, problems


# ============================================================================================ where the files are

def user_ini_path():
    return os.path.join(user_dir(), "shedkeeper.ini")


def user_dir():
    """The personal folder of ShedKeeper of this user (`~/.shedkeeper`; SHEDKEEPER_USER_HOME replaces `~`): the personal ini and the adapters
    live here, outside the folder of the kit (an update of the kit does not touch it) and outside the store (a person's folder of copies)."""
    home = os.environ.get("SHEDKEEPER_USER_HOME") or os.path.expanduser("~")
    return os.path.join(home, ".shedkeeper")


def adapters_dir():
    """Where the adapters of the user's engines are (`~/.shedkeeper/adapters`; SHEDKEEPER_ADAPTERS_DIR names another folder, for the tests)."""
    return os.environ.get("SHEDKEEPER_ADAPTERS_DIR") or os.path.join(user_dir(), "adapters")


def ini_files(path=None):
    """The files the kit reads, in the order they are read (only those that exist)."""
    named = path or os.environ.get("SHEDKEEPER_INI")
    cand = [named] if named else [DEFAULT_INI, user_ini_path()]
    return [p for p in cand if os.path.lexists(p)]


def target_ini():
    """The file `set` writes to: the one named by SHEDKEEPER_INI, else the personal ini of the user."""
    return os.environ.get("SHEDKEEPER_INI") or user_ini_path()


def load(path=None, integrity=True):
    """Return (settings dict, problems list). Missing file -> the defaults, no problem. A `path` (or SHEDKEEPER_INI) names the ONE file
    that is read; without either the shipped ini and then the personal ini are read, the later lines winning (the lines of
    mcp_no_db and db_command add up). `integrity=False`: the mark "not through `set`" (`settings.unconfirmed`) is not worked out -- it costs a look
    through the journals of every volume, and only the start of a session and `status` show it; the decisions of the hooks do not."""
    named = path or os.environ.get("SHEDKEEPER_INI")
    files = [named] if named else [DEFAULT_INI, user_ini_path()]
    settings = Settings(DEFAULTS)
    problems = []
    for f in files:
        if not os.path.lexists(f):
            continue
        try:
            with open(f, encoding="utf-8-sig") as fh:
                text = fh.read()
        except (OSError, ValueError) as e:
            settings["auto_create_store"] = False
            problems.append("settings file unreadable (%s: %s): %s -- defaults used, "
                            "store creation switched OFF" % (e.__class__.__name__, e, f))
            continue
        parse_text(text, f, settings, problems)
    if integrity:
        _integrity(settings, problems)
    return settings, problems


def weakening_values(settings):
    """[(key, value text)] -- the settings of weight `protection` that stand at a value that WEAKENS protection (the shipped default never does).
    `mcp_no_db`: the names, joined by one space."""
    out = []
    for key, (weight, _applies, effect) in WEIGHTS.items():
        if weight != "protection" or not effect:
            continue
        val = settings.get(key)
        if key == "mcp_no_db":
            if val:
                out.append((key, " ".join(val)))
            continue
        canon = format_value(val)
        if canon != format_value(DEFAULTS[key]) and effect_for(key, canon):
            out.append((key, canon))
    return out


def _integrity(settings, problems):
    """A weakening of protection that stands in an ini ACTS: the hand of the person is the hand that edits the file. What the journal of the store tells is only a REMINDER:
    a weakening that was not made through `set` (no confirmed `state` line for exactly this key and value, `confirm.approved`) is listed in `settings.unconfirmed`,
    [(key, value text, effect)], and shown with the mark "not through `set`" at the start of a session and in `status` -- it changes nothing in what the kit does.
    A check that cannot be made (the journal cannot be read, the module fails) lists every weakening as not through `set`."""
    settings.unconfirmed = ()
    cands = weakening_values(settings)
    if not cands:
        return
    try:
        from . import confirm
        ok = confirm.approved(cands, settings)
    except Exception:
        ok = {}
    listed = []
    for key, canon in cands:
        allowed = ok.get(key)
        if key == "mcp_no_db":
            gone = tuple(n for n in canon.split() if not (allowed and n in allowed))
            if gone:
                listed.append((key, " ".join(gone)))
        elif allowed is not True:
            listed.append((key, canon))
    settings.unconfirmed = tuple((k, v, WEIGHTS[k][2]) for k, v in listed)


# ============================================================================================ what a value looks like

def format_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (tuple, list)):
        return ", ".join(str(x) for x in v)
    return "" if v is None else str(v)


def allowed_text(key):
    if key in BOOL_KEYS:
        return "true | false"
    if key in CHOICE_KEYS:
        return "deny | allow"
    if key in ENUM_KEYS:
        return " | ".join(ENUM_KEYS[key])
    if key in INT_LIMITS:
        return "a whole number from 1 to %d" % INT_LIMITS[key]
    if key == "mcp_no_db":
        return "names of MCP servers, separated by spaces or commas (empty = none); `set` replaces your list"
    if key == "db_command":
        return "a command: words [| --flag --flag]; `set` ADDS one rule"
    if key == "pg_dump_path":
        return "a file or the folder of pg_dump (empty = look for it)"
    return "a plain folder name (no \\ / : * ? \" < > |)"


def canonical(key, value):
    """The form a value is written in: true/false, lower-case choices, a whole number, the names separated by one space."""
    value = (value or "").strip()
    if key in BOOL_KEYS:
        b = _parse_bool(value)
        return "true" if b else "false"
    if key in CHOICE_KEYS or key in ENUM_KEYS:
        return value.lower()
    if key in INT_LIMITS:
        return str(int(value))
    if key == "mcp_no_db":
        return " ".join(x for x in re.split(r"[\s,]+", value) if x)
    return value


def apply_value_to_tuple(key, canon):
    if key == "mcp_no_db":
        return tuple(x for x in re.split(r"[\s,]+", canon) if x)
    return (canon,)


# ============================================================================================ the meaning of the keys (one source: the ini)

_DESCRIBE_CACHE = {}
_KEYLINE = re.compile(r"^([A-Za-z_]\w*)\s*=")


def _ini_about(path=None):
    """{key: about} -- the comment above each key of the SHIPPED ini, for the person. The weights in its `@weight` lines are read by nobody:
    the code decides (`WEIGHTS`); a test keeps the two equal."""
    path = path or DEFAULT_INI
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    cached = _DESCRIBE_CACHE.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        with open(path, encoding="utf-8-sig") as fh:
            lines = fh.read().splitlines()
    except (OSError, ValueError):
        return {}
    info, block, tags = {}, [], {}

    def flush(key):
        info[key] = "\n".join(block).strip()

    for raw in lines:
        s = raw.strip()
        if s.startswith("#"):
            body = s[1:].strip()
            if body.startswith("@weight"):
                tags["weight"] = True
            elif body.startswith("@key"):
                tags["key"] = body[4:].strip()
            else:
                block.append(s[1:][1:] if s[1:2] == " " else s[1:])
            continue
        if not s:
            if tags.get("key"):
                flush(tags["key"])
            block, tags = [], {}
            continue
        m = _KEYLINE.match(s)
        if m and (block or tags):
            flush(m.group(1))
        block, tags = [], {}
    if tags.get("key"):
        flush(tags["key"])
    # the first block (the header) is attached to no key; a key with no block of its own is simply not described
    _DESCRIBE_CACHE[path] = (mtime, info)
    return info


def describe(path=None):
    """{key: {key, weight, applies, effect, about}}. `weight`, `applies` and `effect` come from the CODE (`WEIGHTS`): they hold when the shipped
    ini is missing, unreadable or edited. `about` is the comment above the key in the shipped ini (empty when the file cannot be read).
    `weight` is protection | tuning | notice; `effect` (protection) says what stops being protected; `applies` is the values for which the
    effect holds (empty = any value that is not the default)."""
    about = _ini_about(path)
    out = {}
    for key, (weight, applies, effect) in WEIGHTS.items():
        out[key] = {"key": key, "weight": weight, "applies": [x.lower() for x in applies], "effect": effect, "about": about.get(key, "")}
    return out


def effect_for(key, value_text):
    """What stops being protected when `key` is set to `value_text`, or '' (the key is not a protection key, or this value is the safe
    one). `value_text` is the canonical text of the new value. Decided by `WEIGHTS` (the code), never by a file."""
    w = WEIGHTS.get(key)
    if not w or w[0] != "protection" or not w[2]:
        return ""
    effect, rules = w[2], [x.lower() for x in w[1]]
    v = (value_text or "").strip().lower()
    default = format_value(DEFAULTS.get(key)).lower()
    if not rules:
        return effect if v != default else ""
    for r in rules:
        if r == "*":
            if v:
                return effect
        elif r.startswith(">"):
            lim = default if r[1:].strip() == "default" else r[1:].strip()
            try:
                if float(v) > float(lim):
                    return effect
            except ValueError:
                pass
        elif v == r:
            return effect
    return ""


# ============================================================================================ writing one key into an ini text

HEADER = ("# ShedKeeper settings of this user. Read AFTER the shedkeeper.ini of the kit; an update of the kit does not touch this file.\n"
          "# Written by `python shedkeeper.py set KEY VALUE`; `python shedkeeper.py settings` lists every key and what it does.\n\n")


def edit_text(text, key, canon, fresh_header=False):
    """The ini text with `key` set to `canon`; every other line (comments too) stays as it is. A scalar key: the LAST line of the key is
    rewritten (the one that wins), the comment after its value is kept; no line -> appended. mcp_no_db: the lines of the key are replaced by
    one. db_command: one line is ADDED (unless it is there already). The line ending of the file is kept."""
    nl = "\r\n" if "\r\n" in text else "\n"
    if fresh_header and not text:
        text = HEADER.replace("\n", nl)
    lines = text.splitlines(keepends=True)
    pat = re.compile(r"^(?P<pre>\s*)(?P<k>%s)(?P<eq>\s*=\s*)(?P<val>[^#\r\n]*?)(?P<tail>\s*#[^\r\n]*)?(?P<nl>\r?\n?)$" % re.escape(key))
    idx = [i for i, ln in enumerate(lines) if pat.match(ln)]
    if key == "db_command":
        want = canon.strip()
        for i in idx:
            if pat.match(lines[i]).group("val").strip() == want:
                return text
        idx = []
    if key == "mcp_no_db" and idx:
        first = idx[0]
        m = pat.match(lines[first])
        lines[first] = "%s%s%s%s%s" % (m.group("pre"), key, m.group("eq"), canon, m.group("tail") or "") + (m.group("nl") or nl)
        for i in reversed(idx[1:]):
            del lines[i]
        return "".join(lines)
    if idx and key != "db_command":
        i = idx[-1]
        m = pat.match(lines[i])
        lines[i] = "%s%s%s%s%s" % (m.group("pre"), key, m.group("eq"), canon, m.group("tail") or "") + (m.group("nl") or nl)
        return "".join(lines)
    body = "".join(lines)
    if body and not body.endswith("\n"):
        body += nl
    return body + "%s = %s%s" % (key, canon, nl)
