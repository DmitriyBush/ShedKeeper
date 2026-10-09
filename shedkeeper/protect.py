r"""protect.py -- the LIST of what ShedKeeper protects (0.3.0, section 3 of the design).

  python shedkeeper.py protect show                       the whole list, what is NOT in it, the limits
  python shedkeeper.py protect add tree PATH              a folder (a tree); the default exclusions of M4.10a are written into the entry
  python shedkeeper.py protect add sqlite FILE            a SQLite file (found by its header)
  python shedkeeper.py protect add pg HOST:PORT/DB[?user=NAME]      a PostgreSQL database on THIS machine (no password here, never)
  python shedkeeper.py protect add remote HOST:PORT/DB[?user=NAME]  a database on ANOTHER machine -- only by the person's own word (18.1)
  python shedkeeper.py protect remove ID                  (needs the person's code once the list is confirmed)
  python shedkeeper.py protect exclude ID NAME            an exclusion (a name, a pattern, a path under the root); needs the code once confirmed
  python shedkeeper.py protect unexclude ID NAME          takes an exclusion back (more protection: no code)
  python shedkeeper.py protect decline KEY                a found candidate the person does not want (it is not offered again)
  python shedkeeper.py protect confirm                    the person confirms the list (the code `SK-nnnnnn` through the hook of the prompt)

The list lives in the personal folder of the store (`<store>/protect/<project>/protected.json`); the mark "the list is confirmed" lives in the journal of
confirmations (`confirm.jsonl`, op `plist`), SEPARATELY from the list (M3.12a): a mark with a list that cannot be read is NOT "nothing was confirmed" -- the
guard refuses the step (state `broken`). Adding an entry needs no code (M3.13); removing one, adding an exclusion or anything else that lets data go without a
copy needs the person's code (M3.12, the same mechanism as `set`: confirm.gate). Nothing here writes a password.
"""
import hashlib
import json
import os
import re
import time

from . import config, confirm, store

FILE = "protected.json"
DB_KINDS = ("S", "P", "R")                # the kinds of the databases: not protected in a release whose `config.DATABASES_IN_THIS_RELEASE` is False
DB_OFF_TEXT = "Database protection is built and under review; it is not part of this release. Files are protected."
DB_FILE_HINT = "A SQLite file is a file: to protect it, add the folder as a tree (`protect add tree FOLDER`); the file in it is copied with the backup program."
KINDS = {"D": "tree", "S": "SQLite file", "P": "PostgreSQL database on this machine", "R": "PostgreSQL database on another machine",
         "X": "database of another engine (an adapter of the user)"}

# the limits of the design, in plain words: shown with the list, at the survey and in `status` (M2.3)
LIMITS = (
    "Remote databases are not in the list unless you added them yourself (limit 1).",
    "A folder outside the project is guarded only if you added it (limit 2).",
    "What a background process does and undoes between two steps of the agent is not seen (limit 3); what one command creates and destroys inside itself is not seen (limit 7).",
    "A tree on a volume without a change journal is walked at every step: when the walk is too dear the step is refused until you choose (limit 4).",
    "A file or database bigger than the size limit gets no copy: the step is refused unless you allowed it to go without (limit 5).",
    "A file that another program keeps locked cannot be read: the step is refused until it is closed or excluded (limit 10).",
    "Other database engines are guarded only through an adapter that has passed the proof (limit 6).",
    "Folders reached through a link (junction, symbolic link) are not entered (limit 9).",
    "PostgreSQL: a superuser who switches the counters off in their own session changes a database unseen (limit 16); parallel steps of sub-agents are ordered at the check, not at the work (limit 12).",
    "A tree on a volume without ChangeTime (FAT, exFAT): a file rewritten with the same size and the same modification time is not seen (limit 8).",
    "File times move with the clock of the system (a tick of about 16 ms): two rewrites of a file with the same size inside ONE tick, with a check between them, are not told apart by their times (a step of an agent is never that fast; a program writing in a tight loop can be).",
)


def live_kinds():
    """The kinds of entries the check before a step looks at in this release."""
    return ("D",) if databases_off() else ("D", "S", "P", "R")


def databases_off():
    """True in a release that does not protect databases (the switch is a constant of the release, read at every call: the tests replace it)."""
    return not config.DATABASES_IN_THIS_RELEASE


def db_unprotected(entries):
    """[entry] of the list that are databases while this release does not protect them (they are in the list from an earlier build): said LOUDLY, never a `deny` (a declared limit)."""
    return [e for e in entries if e.get("kind") in DB_KINDS] if databases_off() else []


def db_unprotected_text(entries):
    n = len(db_unprotected(entries))
    if not n:
        return ""
    return "%d database entr%s in your list %s NOT protected in this release" % (n, "y" if n == 1 else "ies", "is" if n == 1 else "are")


def db_hint(entries):
    """The way out for a SQLite entry of an old list (the file is a file: its folder as a tree is copied), else ''."""
    return DB_FILE_HINT if any(e.get("kind") == "S" for e in db_unprotected(entries)) else ""


def db_off_words(entries):
    """DB_OFF_TEXT, and the way out when one of the entries is a SQLite file."""
    return DB_OFF_TEXT + ((" " + db_hint(entries)) if db_hint(entries) else "")


class State(object):
    """The state of the list for one project. `kind`: none (no list) | draft (a list, not confirmed) | active | broken (a mark of confirmation but the list does not read)."""

    def __init__(self, kind, root=None, pkey="", project="", data=None, why="", mark=None, below=False):
        self.kind = kind
        self.below = below                 # True: the list was found UNDER the folder of the step (`_lists_below`), not the step's own and not above it
        self.root = root                   # the personal folder of the store (None: it cannot be named)
        self.pkey = pkey
        self.project = project
        self.data = data or {"entries": []}
        self.why = why
        self.mark = mark

    @property
    def entries(self):
        return list(self.data.get("entries") or [])


def _project(cwd):
    from . import survey
    return survey.project_root(cwd)


def project_candidates(cwd):
    """[folder] -- every folder that may be THE project of this step, the nearest first: the folder of the step and its ancestors (at most 20 levels), the root of the repository and
    CLAUDE_PROJECT_DIR. The `cwd` of a step is where the shell of the agent stands (a `cd` lives between calls), not where the project is: a list is looked for at every one of them,
    and an empty `cwd` is the folder of this process."""
    seen, out = set(), []

    def add(p):
        if not p:
            return
        p = os.path.abspath(p)
        k = os.path.normcase(p)
        if k not in seen:
            seen.add(k)
            out.append(p)
    d = os.path.abspath(cwd or os.getcwd())
    for _ in range(20):
        add(d)
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    add(_project(cwd or os.getcwd()))
    add(os.environ.get("CLAUDE_PROJECT_DIR"))
    return out


def _personal_dirs(vol):
    """[folder] of the volume folder `vol` that may be the personal folder of this user (one listing of `vol`); with the login name unreadable: every folder in it (a sign of a list in ANY of them counts)."""
    try:
        names = os.listdir(store._lp(vol))
    except (FileNotFoundError, NotADirectoryError):
        return []                                          # no folder there (or a file where the folder should be): no personal folder, so no list (a proof, not a fault)
    login = store.login_name()
    if login:
        want = {n.lower() for n in store.personal_names(login)}
        names = [n for n in names if n.lower() in want]
    return [os.path.join(vol, n) for n in names[:200]]


def project_id(project):
    key = os.path.normcase(project).replace("\\", "/").rstrip("/")
    base = re.sub(r"[^A-Za-z0-9_.-]", "_", os.path.basename(key) or "root")[:40]
    return "%s_%s" % (base, hashlib.sha256(key.encode("utf-8", "replace")).hexdigest()[:10])


def list_dir(root, pkey):
    return os.path.join(root, "protect", pkey)


def list_path(root, pkey):
    return os.path.join(list_dir(root, pkey), FILE)


def _digest(entries):
    return hashlib.sha256(json.dumps(entries, sort_keys=True).encode("utf-8")).hexdigest()


def read_list(root, pkey):
    """(data | None, 'absent' | 'ok' | 'unreadable', why)."""
    p = list_path(root, pkey)
    try:
        with open(store._lp(p), "rb") as fh:
            raw = fh.read()
    except FileNotFoundError:
        return None, "absent", ""
    except OSError as e:
        return None, "unreadable", "%s cannot be read (%s: %s)" % (p, e.__class__.__name__, e)
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError) as e:
        return None, "unreadable", "%s is damaged (%s)" % (p, e)
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list) or not all(isinstance(x, dict) and x.get("kind") in KINDS for x in data["entries"]):
        return None, "unreadable", "%s is not a list in the form of this version" % p
    return data, "ok", ""


def write_list(root, pkey, data):
    """Atomic: a temporary file, flushed to the disk, then renamed over the list (a crash leaves the old list or the new one, never half)."""
    d = list_dir(root, pkey)
    os.makedirs(store._lp(d), exist_ok=True)
    final = os.path.join(d, FILE)
    tmp = final + ".%d.tmp" % os.getpid()
    with open(store._lp(tmp), "wb") as fh:
        fh.write(json.dumps(data, indent=1, sort_keys=True).encode("utf-8"))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(store._lp(tmp), store._lp(final))


def read_mark(root, pkey):
    """('yes', record) | ('no', None) | ('unreadable', why): the last mark of confirmation of the list of this project in the journal of confirmations."""
    p = os.path.join(root, confirm.FILE)
    try:
        recs, _bad = store._read_jsonl(p)
    except FileNotFoundError:
        return "no", None
    except OSError as e:
        return "unreadable", "%s cannot be read (%s: %s)" % (p, e.__class__.__name__, e)
    marks = [r for _n, r in recs if r.get("op") == "plist" and r.get("project") == pkey]
    if marks:
        return "yes", marks[-1]
    if _bad:
        # a torn or garbled line may have BEEN the mark: with no mark to read, "never confirmed" is not known (H1) -- the list is `broken`, not `draft`. The journal is shared by every
        # project of the volume, though: only a damaged line that says `plist` or carries the key of this project may have been ITS mark
        try:
            mine = [n for n, line in store._damaged_jsonl(p) if _may_be_mark(line, pkey)]
        except OSError as e:
            return "unreadable", "%s cannot be read (%s: %s)" % (p, e.__class__.__name__, e)
        if mine:
            return "unreadable", ("%d damaged line(s) in %s may be the mark of this list, and no mark of it can be read from the rest. `python %s quarantine` moves the damaged lines "
                                  "aside (nothing is deleted); a list whose mark was lost is then rebuilt with `protect add ...`, which needs your code" % (len(mine), p, _kit_py()))
    return "no", None


def _may_be_mark(line, pkey):
    """Could this damaged line of the shared journal have been the mark of confirmation of the list `pkey`? Only if it says `plist` or carries the key of the project."""
    return "plist" in line or (bool(pkey) and pkey in line)


def _fingerprint(e):
    """What the entry GUARDS (kind and target), as a short hash: a change of it by a rewrite of the list file is a different thing guarded."""
    ident = {k: e.get(k) for k in ("kind", "root", "path", "host", "port", "dbname", "user", "engine", "target")}
    return hashlib.sha256(json.dumps(ident, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def confirmed_shape(entries):
    """{id: {f: fingerprint, x: exclusions}} of the entries as the person confirmed them: written into the mark, compared with the list at every look (`_departure`)."""
    return {e["id"]: {"f": _fingerprint(e), "x": sorted(str(x) for x in (e.get("exclude") or ()))} for e in entries}


def _departure(mark, entries):
    """'' or the words: where the list differs from what the person confirmed in a way that lets data go without a copy AND without their code (an entry taken out, its target
    changed, an exclusion added -- by a rewrite of the file). Added entries and taken-back exclusions are more protection: they are not a departure."""
    by_id = {e["id"]: e for e in entries}
    shape = mark.get("ent") if isinstance(mark.get("ent"), dict) else {}
    for i in list(mark.get("ids") or []) + list(mark.get("added_ids") or []):         # (`added_ids`: entries added after the confirmation, through the command)
        e = by_id.get(i)
        if e is None:
            return "the entry %s that you confirmed or added is not in the list (it was taken out without your code)" % i
        c = shape.get(i)
        if not isinstance(c, dict):
            continue
        if c.get("f") != _fingerprint(e):
            return "the entry %s is not the one you confirmed (what it guards was changed without your code)" % i
        extra = [str(x) for x in (e.get("exclude") or ()) if str(x) not in (c.get("x") or ())]
        if extra:
            return "the entry %s has exclusions you did not confirm: %s" % (i, ", ".join(extra))
    return ""


def _remark(st, ids, session, code, why, exclude=None, unexclude=None, drop=None, add=None, reset=False):
    """A change of the list made through the command (the person's code took an entry out, added an exclusion, rebuilt it; or, with no code, an entry was added or an exclusion taken
    back -- more protection): a new mark follows it, so that the change is not read as a departure from what was confirmed, and the undoing of it by a rewrite of the file is.
    It keeps the old `seen`; `ids` is what is left of the confirmed entries; `exclude` = (id, name) puts the exclusion into the confirmed shape of that entry, `unexclude` takes it
    out; `drop` = an id taken out of the entries added since; `add` = an entry added since (it is kept in `added_ids`, apart from `ids`, so that the start of a session still announces
    it as added since the confirmation); `reset` forgets the old shapes (a rebuilt list). Raises on a fault."""
    rec = dict(st.mark or {})
    added = [] if reset else [i for i in (rec.get("added_ids") or []) if i != drop]
    if add is not None and add["id"] not in added:
        added.append(add["id"])
    live = set(ids) | set(added)
    shape = {} if reset else {i: dict(v) for i, v in (rec.get("ent") or {}).items() if i in live}
    if add is not None:
        shape[add["id"]] = confirmed_shape([add])[add["id"]]
    if exclude and exclude[0] in shape:
        shape[exclude[0]]["x"] = sorted(set(shape[exclude[0]].get("x") or ()) | {str(exclude[1])})
    if unexclude and unexclude[0] in shape:
        shape[unexclude[0]]["x"] = sorted(x for x in (shape[unexclude[0]].get("x") or ()) if x != str(unexclude[1]))
    rec.update({"op": "plist", "t": time.time(), "project": st.pkey, "ids": list(ids), "added_ids": added, "ent": shape, "code": code or "", "session": session or "", "why": why})
    confirm._add(st.root, rec)


def _remark_add(st, ent, session):
    """An entry was added to a CONFIRMED list (no code: more protection): the mark follows it."""
    _remark(st, list((st.mark or {}).get("ids") or []), session, "", "add", add=ent)


def _remark_unexclude(st, ent_id, name, session):
    """An exclusion of a CONFIRMED list was taken back (no code: more protection): the mark follows it."""
    _remark(st, list((st.mark or {}).get("ids") or []), session, "", "unexclude", unexclude=(ent_id, name))


def _witness(data, digest, ids):
    """The second witness of a confirmation, inside the list itself: a list that says it was confirmed while the journal has no mark is `broken`, not a draft."""
    data["confirmed"] = {"digest": digest, "ids": list(ids), "time": _now()}

def _list_project(personal, pkey):
    """The project folder a list names in itself (`project` in protected.json), or None (it cannot be read, or it names none that has this key)."""
    try:
        with open(store._lp(list_path(personal, pkey)), "rb") as fh:
            data = json.loads(fh.read().decode("utf-8-sig"))
        p = data.get("project")
        return p if isinstance(p, str) and p and project_id(p) == pkey else None
    except (OSError, ValueError, AttributeError):
        return None


def _lists_below(cwd, settings, by_vol, seen):
    """[project folder] of the lists that stand UNDER the folder of the step (or under CLAUDE_PROJECT_DIR): the session was opened in a folder over the projects, or in the folder of one
    project that holds another with a list of its own. One listing of `protect/` in each personal folder of the volume, and the project each list names in itself (read at every step: the
    lists of a volume are tens, not thousands). A list that names no project, or whose file cannot be read, cannot be told to stand under this folder: it is not found here (a limit,
    ARCHITECTURE: Limits). Raises when it cannot look."""
    out, bases = [], []
    for b in (cwd or os.getcwd(), os.environ.get("CLAUDE_PROJECT_DIR")):
        if b and os.path.normcase(os.path.abspath(b)) not in [os.path.normcase(x) for x in bases]:
            bases.append(os.path.abspath(b))
    for base in bases:
        vol = store.volume_root(base, settings)
        if not vol:
            continue
        if vol not in by_vol:
            by_vol[vol] = _personal_dirs(vol)
        for personal in by_vol[vol]:
            try:
                names = os.listdir(store._lp(os.path.join(personal, "protect")))
            except (FileNotFoundError, NotADirectoryError):
                continue
            for pkey in names:                         # no cap: a list left out silently would be a step let through unchecked; the cost grows with the count (measured in 60_OTCHET)
                if pkey in seen:
                    continue
                project = _list_project(personal, pkey)
                if project and store._within(project, base):
                    seen.add(pkey)
                    out.append(project)
    return out


def _with_sign(cwd, settings, below=True, below_out=None):
    """[project folder] (nearest first) for which a folder of a list is seen in a personal folder of this user, on the volume of that project: the step's own folder and the ones above
    it, then (`below`) the projects whose lists stand UNDER the folder of the step (those are also appended to `below_out`, when given). Raises when it cannot look."""
    by_vol, out, seen = {}, [], set()
    for project in project_candidates(cwd):
        pkey = project_id(project)
        seen.add(pkey)
        vol = store.volume_root(project, settings)
        if not vol:
            continue                                       # a folder with no drive letter (a network path): no store can be named for it, so no list can be there
        if vol not in by_vol:
            by_vol[vol] = _personal_dirs(vol)
        if any(os.path.isdir(os.path.join(p, "protect", pkey)) for p in by_vol[vol]):
            out.append(project)
    if below:
        lower = _lists_below(cwd, settings, by_vol, seen)
        out += lower
        if below_out is not None:
            below_out.extend(lower)
    return out


def maybe_list(cwd, settings):
    """The CHEAP question the hook asks at every step first: is there any sign of a list of this project -- or of a folder above the step -- in a personal folder of this user? It looks at
    the folders of the store of the project's own volume only (one listing, no label, no permission reading, no other volume): when there is none, the step has nothing to be checked
    against. True means `maybe`: `state` then judges. What it cannot decide is `maybe`, never `no`."""
    try:
        return bool(_with_sign(cwd, settings))
    except Exception:
        return True                                        # a fault in this look is not `no list`: the full look decides


def _state_of(project, settings):
    """The state of the list of ONE project folder. Reads two files; never writes. A store that cannot be named is not `no list`: it is said (`broken`) when a list may be there."""
    pkey = project_id(project)
    try:
        root = store.store_root(project, settings)
    except Exception as e:
        return State("broken", None, pkey, project, why="the store cannot be named (%s: %s), so it is not known whether a protected list is there" % (e.__class__.__name__, e))
    if not root:
        vol = store.volume_root(project, settings)
        if not vol:
            return State("none", None, pkey, project, why="no list is possible here: this folder has no drive letter (a network path)")
        try:
            sign = any(os.path.isdir(os.path.join(p, "protect", pkey)) for p in _personal_dirs(vol))
        except Exception:
            sign = True
        if sign:
            return State("broken", None, pkey, project, why="the store cannot be named (the login name of this user cannot be read) and a protected list of this project is there")
        return State("none", None, pkey, project, why="the store cannot be named (the login name cannot be read); no list of this project was seen")
    if not os.path.isdir(root):
        return State("none", root, pkey, project)
    mark_state, mark = read_mark(root, pkey)
    data, ls, why = read_list(root, pkey)
    if mark_state == "unreadable":
        return State("broken", root, pkey, project, data, "the journal of confirmations cannot be read, so it is not known whether the list was confirmed: " + str(mark))
    if mark_state == "yes":
        if ls != "ok":
            return State("broken", root, pkey, project, None, ("the list was confirmed, but it is not there" if ls == "absent" else why), mark)
        gone = _departure(mark, data["entries"])
        if gone:
            return State("broken", root, pkey, project, data, "the list is not the one you confirmed: " + gone, mark)
        return State("active", root, pkey, project, data, mark=mark)
    if ls == "ok" and isinstance(data.get("confirmed"), dict):
        # the list itself says it was confirmed, and the journal has no mark: the journal was damaged or taken away -- not "never confirmed" (the cheapest way to switch the guard off)
        return State("broken", root, pkey, project, data, "the list says it was confirmed on %s, but the journal of confirmations has no mark of it (the journal was damaged or removed)" % data["confirmed"].get("time", "?"))
    if ls == "unreadable":
        return State("draft", root, pkey, project, None, why)       # not confirmed, and damaged: nothing to guard yet
    if ls == "absent":
        return State("none", root, pkey, project)
    return State("draft", root, pkey, project, data)


def _fault_state(cwd, e):
    """The look for lists failed: that is not 'no list' (and not 'only the root of the project'): `broken`, with the reason."""
    project = _project(cwd or os.getcwd())
    return State("broken", None, project_id(project), project,
                 why="the folders of the lists cannot be looked at (%s: %s), so it is not known whether a protected list stands over this folder" % (e.__class__.__name__, e))


def states(cwd, settings, below=True):
    """[State] -- one for every folder that has a list and that the check before a step looks at: the step's own and the ones above it (nearest first), then (`below`) the projects whose
    lists stand under the folder of the step; with none, the state of the project root (kind `none` or what it is). A fault of the look is a `broken` state, never a smaller answer."""
    lower = []
    try:
        projects = _with_sign(cwd, settings, below, lower)
    except Exception as e:
        return [_fault_state(cwd, e)]               # a fault of the look is never `below`: it refuses every step
    if not projects:
        projects = [_project(cwd or os.getcwd())]
    out = []
    for p in projects:
        st = _state_of(p, settings)
        st.below = p in lower
        out.append(st)
    return out


def state(cwd, settings):
    """The state of the list the person works with from `cwd`: the nearest folder (the step's own, then upward) that has one, else the project root. (The lists UNDER the folder are not
    this one: `protect add` in a folder over projects begins a list of that folder.)"""
    return states(cwd, settings, below=False)[0]


# ============================================================================================ entries

def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _next_id(entries, kind):
    letter = kind.lower()
    nums = [int(e["id"][1:]) for e in entries if re.match(r"^[a-z]\d+$", str(e.get("id", ""))) and e["id"][0] == letter]
    return "%s%d" % (letter, (max(nums) + 1) if nums else 1)


def _parse_pg(spec):
    """HOST:PORT/DB[?user=NAME] -> (host, port, dbname, user) or raises ValueError. No password is accepted anywhere in it."""
    m = re.match(r"^(?:(?P<host>\[[^\]]+\]|[^:/?@\s]*)(?::(?P<port>\d+))?)?/(?P<db>[^?\s]+)(?:\?user=(?P<user>[^&\s]+))?$", spec.strip())
    if not m or "@" in spec:
        raise ValueError("the form is HOST:PORT/DB or HOST:PORT/DB?user=NAME (no password: it comes from PGPASSWORD / pgpass)")
    host = (m.group("host") or "localhost").strip("[]")
    return host, int(m.group("port") or 5432), m.group("db"), m.group("user") or ""


def make_entry(kind, value, cwd, settings, existing):
    """-> (entry, notes). Raises ValueError with the reason when the entry cannot be made."""
    from . import db, signals
    notes = []
    if kind == "tree":
        root = os.path.abspath(value)
        if not os.path.isdir(root):
            raise ValueError("%s is not a folder" % root)
        exclude = list(signals.DEFAULT_EXCLUDE)
        try:
            sroot = store.store_root(root, settings)
        except Exception:
            sroot = None
        if sroot and store._within(sroot, root):
            shared = os.path.dirname(sroot)                    # the shared folder of the volume (labels of the store) goes with it
            top = shared if store._within(shared, root) and store._norm(shared) != store._norm(root) else sroot
            rel = os.path.relpath(top, root).replace("\\", "/")
            exclude.append(rel)
            notes.append("the store (%s) lies inside this folder: it is excluded (%s) so that the copies are not copied" % (sroot, rel))
        if sroot and store._within(root, sroot):
            raise ValueError("the folder is inside the store %s" % sroot)
        if not store._within(root, _project(cwd)):
            notes.append("this folder is outside the project (limit 2): it is guarded because you added it")
        if existing and any(e["kind"] == "D" and store._norm(e["root"]) == store._norm(root) for e in existing):
            raise ValueError("this folder is already in the list")
        ent = {"kind": "D", "root": root, "exclude": exclude}
    elif kind == "sqlite":
        path = os.path.abspath(value)
        if not os.path.isfile(path):
            raise ValueError("%s is not a file" % path)
        if not signals.is_sqlite_file(path):
            raise ValueError("%s has no SQLite header (`SQLite format 3`): it is not a SQLite file" % path)
        if any(e["kind"] == "S" and store._norm(e["path"]) == store._norm(path) for e in existing):
            raise ValueError("this file is already in the list")
        ent = {"kind": "S", "path": path}
    elif kind in ("pg", "remote"):
        host, port, dbname, user = _parse_pg(value)
        local = db.is_local_host(host)
        if kind == "pg" and not local:
            raise ValueError("host %r is not on this machine: a database on another machine is added with `protect add remote ...` (it costs a copy at every step)" % host)
        if kind == "remote" and local:
            raise ValueError("host %r is on this machine: use `protect add pg ...`" % host)
        ent = {"kind": "P" if kind == "pg" else "R", "host": host, "port": port, "dbname": dbname, "user": user}
        if any(e["kind"] in ("P", "R") and e["host"].lower() == host.lower() and e["port"] == port and e["dbname"] == dbname for e in existing):
            raise ValueError("this database is already in the list")
    else:
        raise ValueError("the kinds are: tree, sqlite, pg, remote")
    ent["id"] = _next_id(existing, ent["kind"])
    ent["added"] = _now()
    return ent, notes


def describe_entry(e):
    base = _describe_entry(e)
    return base + "  [NOT protected in this release]" if db_unprotected([e]) else base


def _describe_entry(e):
    k = e["kind"]
    if k == "D":
        return "%s  tree %s  (excluded: %s)" % (e["id"], e["root"], ", ".join(e.get("exclude") or ()) or "nothing")
    if k == "S":
        return "%s  SQLite file %s" % (e["id"], e["path"])
    if k in ("P", "R"):
        return "%s  PostgreSQL %s:%s/%s%s  (%s)" % (e["id"], e["host"], e["port"], e["dbname"], ("  user=" + e["user"]) if e.get("user") else "",
                                                      "this machine" if k == "P" else "ANOTHER machine: a copy at every step")
    return "%s  %s %s  (NOT protected until the adapter is proven)" % (e["id"], e.get("engine", "?"), e.get("target", ""))


def show_text(st, settings, cwd, with_candidates=True):
    lines = []
    head = {"none": "NO LIST: nothing is guarded by copies before a step; only the recognised commands are (accelerators).",
            "draft": "LIST NOT CONFIRMED by you: the promise does NOT apply yet; only the recognised commands are guarded (accelerators).",
            "active": "LIST CONFIRMED: every entry below is checked before every step of the agent that can change something.",
            "broken": "LIST BROKEN: " + st.why}[st.kind]
    lines.append(head)
    if db_unprotected_text(st.entries):
        lines.append("WARNING: " + db_unprotected_text(st.entries) + ". " + db_off_words(st.entries))
    lines.append("project: %s" % st.project)
    if st.entries:
        lines.append("entries:")
        lines += ["  " + describe_entry(e) for e in st.entries]
    else:
        lines.append("entries: none")
    if with_candidates:
        try:
            from . import survey
            cand = survey.candidates(cwd, settings)
            have = {os.path.normcase(e.get("root") or e.get("path") or "") for e in st.entries}
            have |= {"%s:%s/%s" % (e.get("host"), e.get("port"), e.get("dbname")) for e in st.entries if e["kind"] in ("P", "R")}
            rest = [c for c in cand if c.get("key") not in have]
            if rest:
                lines.append("found, NOT in the list (not guarded):")
                lines += ["  - " + c["text"] for c in rest]
        except Exception as e:
            lines.append("found but not in the list: (the survey failed: %s: %s)" % (e.__class__.__name__, str(e)[:120]))
    lines.append("limits (not guarded):")
    lines += ["  - " + x for x in LIMITS]
    if st.kind == "draft":
        lines.append("To confirm this list: `python %s protect confirm`, then the user types the code it gives." % _kit_py())
    return "\n".join(lines)


def _kit_py():
    from . import advice
    return advice.kit_py()


# ============================================================================================ candidates that are not decided (3.14) and the line at the start of a session

def _json_list(path):
    try:
        with open(store._lp(path), encoding="utf-8") as fh:
            v = json.load(fh)
        return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []
    except (OSError, ValueError):
        return []


def _json_list_add(path, key):
    cur = _json_list(path)
    if key in cur:
        return
    cur.append(key)
    os.makedirs(store._lp(os.path.dirname(path)), exist_ok=True)
    tmp = path + ".%d.tmp" % os.getpid()
    with open(store._lp(tmp), "w", encoding="utf-8") as fh:
        json.dump(cur, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(store._lp(tmp), store._lp(path))


def declined(root, pkey):
    return _json_list(os.path.join(list_dir(root, pkey), "declined.json"))


def announced(root, pkey):
    return _json_list(os.path.join(list_dir(root, pkey), "announced.json"))


def mark_announced(root, pkey, key):
    _json_list_add(os.path.join(list_dir(root, pkey), "announced.json"), key)


def entry_key(e):
    if e["kind"] == "D":
        return os.path.normcase(os.path.abspath(e["root"]))
    if e["kind"] == "S":
        return os.path.normcase(os.path.abspath(e["path"]))
    return "%s:%s/%s" % ("localhost" if _is_local(e["host"]) else e["host"], e["port"], e["dbname"])


def _is_local(host):
    from . import db
    return db.is_local_host(host)


def pending_candidates(st, settings, cwd):
    """[candidate] -- what the survey finds now that is neither in the list, nor shown to the person when they confirmed it, nor declined by them (3.14)."""
    if st.kind != "active":
        return []
    from . import survey
    have = {entry_key(e) for e in st.entries}
    seen = set((st.mark or {}).get("seen") or ()) | set(declined(st.root, st.pkey))
    out = []
    for c in survey.candidates(cwd, settings):
        if c.get("entry") is None or c["key"] in have or c["key"] in seen:
            continue
        out.append(c)
    return out


def session_line(cwd, settings, kit):
    """(for the agent, for the person) -- the state of the list at the start of a session (3.5, 3.13, 3.14, limits). Two short lines; empty strings when it cannot be told. Every list the
    check before a step looks at is told (the nearest first); with more than one, each line names its project."""
    sts = states(cwd, settings)
    lists = [s for s in sts if s.kind != "none"]
    if not lists:
        return _line_for(sts[0], cwd, settings, kit)
    here = _project(cwd or os.getcwd())
    agent, person = [], []
    for i, st in enumerate(lists):
        a, p = _line_for(st, cwd, settings, kit, first=(i == 0))
        tag = "" if (len(lists) == 1 and store._norm(st.project) == store._norm(here)) else "[list of %s] " % st.project
        if a:
            agent.append(tag + a)
        if p:
            person.append(tag + p)
    return " ".join(agent), " ".join(person)


def _line_for(st, cwd, settings, kit, first=True):
    """The two lines of one list. `first`: the nearest list (the found-and-not-decided data and the limits are told once, with it)."""
    cmd = "python %s" % kit
    if st.kind == "none" and st.root is None and st.why.startswith("no list is possible here"):
        return ("ShedKeeper: a protected list is NOT possible for this folder (%s); only the recognised destroying commands are guarded." % st.why.split(": ", 1)[-1],
                "ShedKeeper: no protected list is possible for this folder (a network path); only the recognised commands are guarded before a step.")
    if st.kind == "none":
        try:                                               # a person who has not looked at the new principle yet gets the short start of 0.3.0: the survey (offered once) tells it
            from . import survey
            if not (st.root and survey.has(st.root, "survey_done", survey.project_key(cwd))):
                return "", ""
        except Exception:
            return "", ""
        return ("ShedKeeper: NO protected list is confirmed -- until one is, only the recognised destroying commands are guarded (the promise of the list does not apply yet). "
                "Confirm one: `%s protect show`, `protect add ...`, `protect confirm`." % cmd,
                "ShedKeeper: no protected list is confirmed yet; only the recognised commands are guarded before a step.")
    if st.kind == "draft":
        dbd = db_unprotected_text(st.entries)
        return ("ShedKeeper: the protected list (%d entries) is NOT confirmed -- only the recognised destroying commands are guarded; confirm it with `%s protect confirm` "
                "(the user types the code)." % (len(st.entries), cmd) + ((" WARNING: %s. %s" % (dbd, db_off_words(st.entries))) if dbd else ""),
                "ShedKeeper: the protected list is not confirmed yet; only the recognised commands are guarded before a step." + ((" " + dbd[0].upper() + dbd[1:] + ".") if dbd else "")
                + ((" " + db_hint(st.entries)) if db_hint(st.entries) else ""))
    if st.kind == "broken":
        lead = "the protected list was confirmed but cannot be used" if st.mark else "the protected list cannot be used"
        return ("ShedKeeper WARNING: %s (%s): every step is REFUSED until it is repaired. `%s protect show`." % (lead, st.why, cmd),
                "ShedKeeper: the protected list cannot be used (%s): the agent's steps are refused until it is repaired." % st.why)
    live = [e for e in st.entries if e["kind"] in live_kinds()]
    notyet = [e for e in st.entries if e["kind"] not in ("D", "S", "P", "R")]
    dbtext = db_unprotected_text(st.entries)
    parts = ["ShedKeeper: the confirmed list guards %d entr%s before every step (%s)." % (len(live), "y" if len(live) == 1 else "ies", "; ".join(e["id"] for e in live))]
    if notyet:
        parts.append("NOT guarded (no signal for their kind, no proof yet): %s." % ", ".join(e["id"] for e in notyet))
    if dbtext:
        parts.append("WARNING: %s. %s" % (dbtext, db_off_words(st.entries)))
    new = new_since_confirmation(st)
    person = ["ShedKeeper guards %d entr%s before every step." % (len(live), "y" if len(live) == 1 else "ies")]
    if notyet:
        person.append("%d entr%s of a kind it cannot check yet %s NOT guarded." % (len(notyet), "y" if len(notyet) == 1 else "ies", "is" if len(notyet) == 1 else "are"))
    if dbtext:
        person.append(dbtext[0].upper() + dbtext[1:] + ".")
        if db_hint(st.entries):
            person.append(db_hint(st.entries))
    if new:
        parts.append("Added since you confirmed: %s." % ", ".join(describe_entry(e) for e in new))
        person.append("Added since you confirmed the list: %s." % ", ".join(e["id"] for e in new))
    try:
        pend = pending_candidates(st, settings, cwd) if first else []
    except Exception:
        pend = []
    if pend:
        parts.append("NEW, not in the list and not decided: %s (add with `protect add ...` or `protect decline KEY`)." % "; ".join(c["text"][:120] for c in pend[:3]))
        person.append("New data found that is not in your list: %s." % "; ".join(c["text"][:80] for c in pend[:3]))
    if first:
        parts.append("Not guarded: " + "; ".join(x.split(" (limit")[0].rstrip(".") for x in LIMITS[:4]) + ". `%s protect show` has all of it." % cmd)
    return " ".join(parts), " ".join(person)


# ============================================================================================ CLI

def _need_store(st, settings):
    if not st.root:
        return "REFUSED: no store folder can be named for this project (%s)" % (st.why or "unknown reason")
    ok, note, _rep = store.ensure_store_report(st.root, settings)
    if not ok:
        return "REFUSED: the store cannot be used (%s)" % note
    return ""


def _weaken(op, canon, effect, cmdline, cwd, settings, session, st):
    """Weakening (3.4): the person's code once the list is confirmed. -> (status, text, code): free | apply | ask | refuse."""
    if st.kind not in ("active", "broken"):
        return "free", "", None
    return confirm.gate("protect:" + op, canon, settings, cwd, session, effect=effect, cmd=cmdline, what="would take something out of the protected list", verb="protect")


def cli(argv, settings, cwd, session=""):
    """`protect ...` -> (exit code, text). 0 done; 1 refused (nothing written); 4 not applied -- the person's code is needed."""
    if not argv or argv[0] in ("show", "list"):
        st0 = state(cwd, settings)
        text = show_text(st0, settings, cwd)
        others = [s for s in states(cwd, settings) if s.pkey != st0.pkey and s.kind != "none"]
        if others:
            text += "\nother lists the check before a step looks at as well: " + "; ".join("%s (%s, %d entries)" % (s.project, s.kind, len(s.entries)) for s in others)
        return 0, text
    verb, rest = argv[0], argv[1:]
    if verb == "add" and rest and rest[0] in ("sqlite", "pg", "remote") and databases_off():
        return 2, DB_OFF_TEXT
    st = state(cwd, settings)
    if st.kind == "broken":
        # the list was confirmed and now does not read: only the person's code lets a NEW list stand in its place (an agent must not shrink a guard by breaking its list)
        if verb != "add":
            return 1, "REFUSED: " + st.why + ". `protect show` shows the state; a new list is begun with `protect add ...`, which needs the user's code."
        status, text, code = confirm.gate("protect:rebuild", "rebuild", settings, cwd, session, cmd="python %s protect %s" % (_kit_py(), " ".join(argv)),
                                          effect="the damaged or missing list is replaced by a new one: what the old list guarded is no longer guaranteed to be in it",
                                          what="replaces a confirmed list that cannot be read", verb="protect")
        if status == "ask":
            return confirm.EXIT_ASK, "The list cannot be read (%s). %s" % (st.why, text)
        if status == "refuse":
            return 1, text
        if status == "apply" and not confirm.spend(code, cwd, settings):
            return 1, "REFUSED: the confirmation code %s could not be marked as used, so nothing was changed" % code
        st.data = {"entries": []}
        rebuilt = code or ""
    else:
        rebuilt = None
    if verb == "add":
        if len(rest) < 2:
            return 1, "usage: protect add tree|sqlite|pg|remote VALUE"
        bad = _need_store(st, settings)
        if bad:
            return 1, bad
        if rest[0] == "remote":
            pass                                                         # the person's own word: the command is run on their word (18.1 p.4)
        data = st.data if st.kind in ("draft", "active", "broken") else {"entries": []}
        try:
            ent, notes = make_entry(rest[0], " ".join(rest[1:]), cwd, settings, data["entries"])
        except ValueError as e:
            return 1, "REFUSED: %s" % e
        data.setdefault("version", 1)
        data["project"] = st.project
        data["entries"].append(ent)
        try:
            if rebuilt is not None:                                       # the person's code replaced the list: the mark follows (nothing of the old list is confirmed any more)
                _remark(st, [], session, rebuilt, "rebuilt", reset=True, add=ent)
                _witness(data, "", [])
            write_list(st.root, st.pkey, data)
        except (OSError, ValueError) as e:
            return 1, "REFUSED: the list could not be written (%s: %s)" % (e.__class__.__name__, e)
        out = ["added: " + describe_entry(ent)] + notes
        if rebuilt is None and st.kind == "active":
            try:                                                          # more protection, no code: the mark follows it (the list first: a fault between the two leaves the old mark)
                _remark_add(st, ent, session)
            except Exception as e:
                out.append("the list holds the new entry, but the mark of the confirmed list could not be moved (%s: %s): until you confirm the list again, taking this entry out of the file "
                           "by hand is not noticed" % (e.__class__.__name__, e))
        if ent["kind"] == "R":
            try:
                from . import guard
                secs, text_r = guard.measure_remote(st.root, ent, settings)
            except Exception as e:
                secs, text_r = None, "the price could not be measured (%s: %s)" % (e.__class__.__name__, e)
            out.append("A database on another machine is copied at EVERY step (the counters of the server are usually closed to you there). " + text_r)
        out.append("The list is %s." % ("confirmed: the new entry is guarded from the next step; you see it in the line at the start of the next session"
                                         if st.kind == "active" else "not confirmed yet: `protect confirm`"))
        return 0, "\n".join(out)
    if verb in ("remove", "exclude", "unexclude"):
        if st.kind not in ("draft", "active") or not st.entries:
            return 1, "REFUSED: there is no list to change"
        ident = rest[0] if rest else ""
        ent = next((e for e in st.entries if e["id"] == ident), None)
        if ent is None:
            return 1, "REFUSED: no entry %r (ids: %s)" % (ident, ", ".join(e["id"] for e in st.entries))
        if verb == "remove":
            effect = "the entry %s will no longer be guarded: what is in it goes without a copy" % describe_entry(ent)
            canon = "remove %s" % ent["id"]
            cmdline = "python %s protect remove %s" % (_kit_py(), ent["id"])
        elif verb == "exclude":
            if ent["kind"] != "D" or len(rest) < 2:
                return 1, "usage: protect exclude ID NAME  (only trees have exclusions)"
            effect = "%s inside the entry %s will no longer be guarded: it goes without a copy" % (rest[1], ent["id"])
            canon = "exclude %s %s" % (ent["id"], rest[1])
            cmdline = "python %s protect exclude %s %s" % (_kit_py(), ent["id"], rest[1])
        else:
            if ent["kind"] != "D" or len(rest) < 2:
                return 1, "usage: protect unexclude ID NAME"
            effect = None
            canon = ""
            cmdline = ""
        if effect:
            status, text, code = _weaken(verb, canon, effect, cmdline, cwd, settings, session, st)
            if status == "ask":
                return confirm.EXIT_ASK, text
            if status == "refuse":
                return 1, text
            if status == "apply" and not confirm.spend(code, cwd, settings):
                return 1, "REFUSED: the confirmation code %s could not be marked as used in the store, so nothing was changed" % code
        data = st.data
        try:
            if effect and st.kind == "active":                            # the person's code was given: the mark of the confirmed list follows it, then the list
                keep = [i for i in (st.mark or {}).get("ids") or [] if not (verb == "remove" and i == ent["id"])]
                _remark(st, keep, session, "", verb, exclude=(ent["id"], rest[1]) if verb == "exclude" else None, drop=ent["id"] if verb == "remove" else None)
        except (OSError, ValueError) as e:
            return 1, "REFUSED: the mark of the confirmed list could not be changed (%s: %s); nothing was changed" % (e.__class__.__name__, e)
        if verb == "remove":
            data["entries"] = [e for e in data["entries"] if e["id"] != ent["id"]]
        elif verb == "exclude":
            if rest[1] not in ent["exclude"]:
                ent["exclude"].append(rest[1])
        else:
            ent["exclude"] = [x for x in ent["exclude"] if x != rest[1]]
        try:
            write_list(st.root, st.pkey, data)
        except OSError as e:
            return 1, "REFUSED: the list could not be written (%s: %s)" % (e.__class__.__name__, e)
        if verb == "unexclude" and st.kind == "active":
            try:                                                          # more protection, no code: the mark follows it, so that putting the exclusion back by hand is noticed
                _remark_unexclude(st, ent["id"], rest[1], session)
            except Exception as e:
                return 0, "done: %s %s (the mark of the confirmed list could not be moved: %s: %s; putting the exclusion back by hand is not noticed until you confirm the list again)" % (
                    verb, " ".join(rest), e.__class__.__name__, e)
        return 0, "done: %s %s" % (verb, " ".join(rest))
    if verb == "decline":
        if st.kind != "active" or len(rest) < 1:
            return 1, "usage: protect decline KEY  (the list must be confirmed; KEY is the key shown by `protect show`)"
        key = " ".join(rest)
        status, text, code = _weaken("decline", "decline " + key, "the notice about %s (found data that is not in your list) will not be shown to you again" % key,
                                     "python %s protect decline %s" % (_kit_py(), key), cwd, settings, session, st)
        if status == "ask":
            return confirm.EXIT_ASK, text
        if status == "refuse":
            return 1, text
        if status == "apply" and not confirm.spend(code, cwd, settings):
            return 1, "REFUSED: the confirmation code %s could not be marked as used, so nothing was changed" % code
        try:
            _json_list_add(os.path.join(list_dir(st.root, st.pkey), "declined.json"), key)
        except OSError as e:
            return 1, "REFUSED: not written (%s: %s)" % (e.__class__.__name__, e)
        return 0, "noted: %s is not wanted in the list; it will not be offered again" % " ".join(rest)
    if verb == "confirm":
        if st.kind not in ("draft", "active"):
            return 1, "REFUSED: there is no list to confirm (`protect add ...` first)"
        if not st.entries:
            return 1, "REFUSED: the list is empty"
        digest = _digest(st.entries)
        effect = "the list below becomes the one ShedKeeper guards before every step: " + "; ".join(describe_entry(e) for e in st.entries)
        status, text, code = confirm.gate("protect:confirm", digest, settings, cwd, session, effect=effect, cmd="python %s protect confirm" % _kit_py(),
                                          what="is the list of what ShedKeeper protects", verb="protect")
        if status == "ask":
            return confirm.EXIT_ASK, show_text(st, settings, cwd, with_candidates=True) + "\n\n" + text
        if status == "refuse":
            return 1, text
        if status == "apply" and not confirm.spend(code, cwd, settings):
            return 1, "REFUSED: the code %s could not be marked as used, so the list was NOT confirmed" % code
        seen = []
        try:
            from . import survey
            seen = [c["key"] for c in survey.candidates(cwd, settings) if c.get("entry") is not None]
        except Exception:
            pass
        try:
            confirm._add(st.root, {"op": "plist", "t": time.time(), "project": st.pkey, "digest": digest, "ids": [e["id"] for e in st.entries], "ent": confirmed_shape(st.entries),
                                   "seen": seen, "code": code or "", "session": session or ""})
        except Exception as e:
            return 1, "REFUSED: the mark of confirmation could not be written (%s: %s); the list is not confirmed" % (e.__class__.__name__, e)
        try:
            _witness(st.data, digest, [e["id"] for e in st.entries])
            write_list(st.root, st.pkey, st.data)
        except (OSError, ValueError) as e:
            return 0, "the list is confirmed, but its own second witness could not be written (%s: %s): if the journal of confirmations is ever damaged, the list will read as not confirmed" % (e.__class__.__name__, e)
        dbt = db_unprotected_text(st.entries)
        checked = ("From the next step every file entry is checked before it; %s. %s" % (dbt, db_off_words(st.entries))) if dbt else "From the next step every entry is checked before it."
        return 0, "the list is confirmed (%d entries). %s Take the first copy with `python %s snapshot` in your own terminal (it can be big)." % (len(st.entries), checked, _kit_py())
    return 1, "usage: protect show | add KIND VALUE | remove ID | exclude ID NAME | unexclude ID NAME | decline KEY | confirm"


def new_since_confirmation(st):
    """[entry] added to the list after the person confirmed it (M3.13): shown in the line at the start of a session."""
    if st.kind != "active" or not st.mark:
        return []
    ids = set(st.mark.get("ids") or [])
    return [e for e in st.entries if e["id"] not in ids]
