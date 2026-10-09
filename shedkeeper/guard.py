r"""guard.py -- the CHECK before every step (0.3.0, section 4 of the design): what is in the confirmed list is compared with the state of the last copy; what changed
(or what cannot be told) gets a verified copy BEFORE the step, and the step goes only then.

  check(data, settings, ...)     the hook: one call per step of the agent (any tool that is not in the closed list of readers)
  snapshot(cwd, settings, ...)   the first copy and any copy by hand, OUTSIDE Claude Code (no budget of the hook; progress is printed)
  restore_step(...) / find_text(...)   the way back (a NEW place) and the list of steps

Order (M4.3), for every entry: (1) read the signal; (2) changed / same / unknown; (3) changed or unknown -> a copy, read back in full; (4) the new base of the signal
is the value read in (1) -- written ONLY after the copy was read back, together with the manifest of the step, atomically (M4.4a, C4.1: after a crash the next check sees
`changed` again and copies again). Any exception inside the check -> the step does not go (M4.4b). A copy that does not fit the time (`step_budget_s`) -> the step does
not go and the lawful way is `snapshot` in the person's own terminal (M4.4c). Nothing here relies on a recogniser of commands (C6.1).

Where it keeps things, in the personal folder of the store `<root>`:
  guard/<project>/state.db      the bases of the signals: per entry a JSON state, and the signatures of the files of the last copy (one transaction per commit)
  guard/<project>/steps.jsonl   the manifests of the steps (one line per step that copied something): the line number is the step number
  guard/<project>/lock          the lock of the check (one check at a time per project: parallel calls, sub-agents, a second Claude Code: M4.2)
  objects/<ab>/<sha256>         the content of every file copy, once (by content), read back
  dbcopies/...                  the copies of databases (db.copy_base, as for `dbcopy`)
"""
import hashlib
import json
import os
import pathlib
import re
import shutil
import sqlite3
import sys
import time

from . import advice, config, db, protect, signals, store

READ_ONLY_TOOLS = config.READ_ONLY_TOOLS
CONTROL_TOOLS = config.CONTROL_TOOLS                    # the tools that stop a process or ask the person: never refused (config.py)
HOOK_TIMEOUT_S = config.HOOK_TIMEOUT_S
DEGRADED_STATUS_DAYS = 7                                # `status` shows a degradation with the time it was LAST seen, and forgets one not seen for this many days
SQLITE_COMPANIONS = ("-wal", "-shm", "-journal")
COPY_CHUNK = 1 << 20
JOURNAL_ADVANCE_BYTES = 1 << 20          # the position of the change journal is written back when it moved by this much (or when anything else changed)
FREE_MARGIN_BYTES = 64 << 20           # the copies must leave at least this much free on the volume of the store


class GuardDeny(Exception):
    """The step does not go: the text is for the agent and the person."""

    def __init__(self, text):
        Exception.__init__(self, text)
        self.text = text


def _deny(what, agent=None, user=None, settings=(), no_setting=False):
    return "shedkeeper: %s. The step is refused. " % what.strip().rstrip(".") + advice.block(what, agent=agent, user=user, settings=settings, no_setting=no_setting)


def _snapshot_hint():
    return "python %s snapshot" % advice.kit_py()


# ============================================================================================ the lock

class _Lock(object):
    """One check at a time per project (M4.2). A byte lock the operating system drops when the process dies (no stale lock). Waits at most `wait_s`."""

    def __init__(self, path, wait_s):
        self.path, self.wait_s, self.fd = path, wait_s, None

    def __enter__(self):
        os.makedirs(store._lp(os.path.dirname(self.path)), exist_ok=True)
        self.fd = os.open(store._lp(self.path), os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0))
        end = time.monotonic() + self.wait_s
        while True:
            try:
                os.lseek(self.fd, 0, os.SEEK_SET)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.lockf(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 1, 0)
                return self
            except OSError:
                if time.monotonic() >= end:
                    os.close(self.fd)
                    self.fd = None
                    raise GuardDeny(_deny("another check of this project holds the lock for more than %d s (lock_wait_s)" % self.wait_s,
                                          agent="run the same step again", no_setting=True))
                time.sleep(0.05)

    def __exit__(self, *a):
        if self.fd is not None:
            try:
                os.lseek(self.fd, 0, os.SEEK_SET)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.lockf(self.fd, fcntl.LOCK_UN, 1, 0)
            except OSError:
                pass
            os.close(self.fd)
            self.fd = None


# ============================================================================================ the state (bases of the signals)

def _s64(n):
    return n - (1 << 64) if n >= (1 << 63) else n


def _u64(n):
    return n + (1 << 64) if n < 0 else n


class Table(object):
    """The signatures of the files of the last copy of one entry (the view `signals` reads)."""

    def __init__(self, con, entry):
        self.con, self.entry, self._rows = con, entry, None

    def rows(self):
        if self._rows is None:
            self._rows = {r[0]: (r[1], r[2], r[3], _u64(r[4])) for r in self.con.execute("select rel,size,mtime,ctime,frn from files where entry=?", (self.entry,))}
        return self._rows

    def row(self, rel):
        r = self.con.execute("select size,mtime,ctime,frn from files where entry=? and rel=?", (self.entry, rel)).fetchone()
        return (r[0], r[1], r[2], _u64(r[3])) if r else None

    def rows_has(self, rel):
        return self.con.execute("select 1 from files where entry=? and rel=?", (self.entry, rel)).fetchone() is not None

    def by_frn(self, frn):
        r = self.con.execute("select rel from files where entry=? and frn=? limit 1", (self.entry, _s64(frn))).fetchone()
        return r[0] if r else None

    def under(self, prefix):
        like = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "/%"
        return [r[0] for r in self.con.execute("select rel from files where entry=? and (rel=? or rel like ? escape '\\')", (self.entry, prefix, like))]


class StateDB(object):
    def __init__(self, path):
        self.path = path
        self.con = None
        self.notes = []
        self._open()

    def _open(self):
        os.makedirs(store._lp(os.path.dirname(self.path)), exist_ok=True)
        try:
            self.con = self._connect()
            self.con.execute("select count(*) from kv").fetchone()
        except sqlite3.DatabaseError as e:
            # a state that does not read is NOT deleted: it is moved aside, and every entry starts without a base (so everything is copied again: the safe side)
            try:
                if self.con is not None:
                    self.con.close()
            except Exception:
                pass
            self.con = None
            bad = self.path + ".bad.%s" % time.strftime("%Y%m%d%H%M%S")
            try:
                os.replace(self.path, bad)
                self.notes.append("the state of the signals did not read (%s: %s); moved to %s, everything is copied again" % (e.__class__.__name__, e, bad))
            except OSError as e2:
                raise GuardDeny(_deny("the state of the signals cannot be read or moved aside (%s: %s)" % (e.__class__.__name__, e2), user="look at %s" % self.path, no_setting=True))
            self.con = self._connect()

    def _connect(self):
        con = sqlite3.connect(store._lp(self.path), timeout=10, isolation_level=None)
        con.execute("create table if not exists kv(entry text primary key, st text not null)")
        con.execute("create table if not exists files(entry text not null, rel text not null, size integer, mtime integer, ctime integer, frn integer, primary key(entry, rel))")
        con.execute("create index if not exists files_frn on files(entry, frn)")
        return con

    def get(self, entry):
        r = self.con.execute("select st from kv where entry=?", (entry,)).fetchone()
        if not r:
            return {}
        try:
            return json.loads(r[0])
        except ValueError:
            return {}

    def table(self, entry):
        return Table(self.con, entry)

    def commit(self, entry, new_state, upserts=None, deleted=None, replace_all=False):
        """ONE transaction: the signatures and the state of the entry. Called only after the copy of the entry was read back and its manifest written."""
        c = self.con
        c.execute("begin immediate")
        try:
            if replace_all:
                c.execute("delete from files where entry=?", (entry,))
            elif deleted:
                c.executemany("delete from files where entry=? and rel=?", [(entry, d) for d in deleted])
            if upserts:
                c.executemany("insert or replace into files(entry,rel,size,mtime,ctime,frn) values(?,?,?,?,?,?)",
                              [(entry, rel, s[0], s[1], s[2], _s64(s[3])) for rel, s in upserts.items()])
            c.execute("insert or replace into kv(entry, st) values(?,?)", (entry, json.dumps(new_state)))
            c.execute("commit")
        except Exception:
            try:
                c.execute("rollback")
            except Exception:
                pass
            raise

    def close(self):
        try:
            self.con.close()
        except Exception:
            pass


# ============================================================================================ files: objects by content

def _obj_path(root, sha):
    return os.path.join(root, "objects", sha[:2], sha)


def _stream_copy(src, dst_tmp, deadline):
    """Copies `src` into `dst_tmp` and hashes what it read. -> (sha256, size). OSError on a fault, TimeoutError past the deadline."""
    h = hashlib.sha256()
    n = 0
    with open(store._lp(src), "rb") as fi, open(store._lp(dst_tmp), "wb") as fo:
        while True:
            if deadline and time.monotonic() > deadline:
                raise TimeoutError("the copy of %s did not finish in time" % src)
            b = fi.read(COPY_CHUNK)
            if not b:
                break
            h.update(b)
            fo.write(b)
            n += len(b)
        fo.flush()
        os.fsync(fo.fileno())
    return h.hexdigest(), n


def put_object(root, src, deadline, limit_bytes=None):
    """A verified copy of the file `src` by content: -> (sha256, size, how). The copy is READ BACK (size + sha256) before it counts (M5.1, C5.2). The same content is kept once.
    An object that does not read back the way it was written does not count: ValueError. (A file that changes WHILE it is copied is not detected here: the base keeps the signature read
    BEFORE the copy, so the next check sees the difference and copies again.)"""
    tmpdir = os.path.join(root, "objects", "_tmp")
    os.makedirs(store._lp(tmpdir), exist_ok=True)
    tmp = os.path.join(tmpdir, "%d_%d.part" % (os.getpid(), int(time.time() * 1000) % 10 ** 9))
    try:
        sha, size = _stream_copy(src, tmp, deadline)
        final = _obj_path(root, sha)
        if os.path.isfile(store._lp(final)):
            good = os.path.getsize(store._lp(final)) == size and store._sha256(final) == sha
            if good:
                return sha, size, "same"
            os.replace(store._lp(final), store._lp(final + ".damaged.%d" % int(time.time())))       # a damaged object is moved aside, never overwritten in place
        os.makedirs(store._lp(os.path.dirname(final)), exist_ok=True)
        os.replace(store._lp(tmp), store._lp(final))
        if os.path.getsize(store._lp(final)) != size or store._sha256(final) != sha:
            raise ValueError("the copy of %s does not read back the way it was written" % src)
        return sha, size, "new"
    finally:
        db._drop_own(tmp)


def put_sqlite_object(root, src, deadline, settings):
    """A SQLite file copied by the backup API (never raw: C5.1), checked with PRAGMA integrity_check, then stored by content. -> (sha256, size)."""
    tmpdir = os.path.join(root, "objects", "_tmp")
    os.makedirs(store._lp(tmpdir), exist_ok=True)
    tmp = os.path.join(tmpdir, "sqlite_%d_%d.part" % (os.getpid(), int(time.time() * 1000) % 10 ** 9))

    class _B(object):
        path = src

    res = {"msg": "", "status": "error"}
    busy_s = min(float(settings.get("sqlite_busy_s", 5)), max(0.0, deadline - time.monotonic()))          # a key of the settings never makes the wait longer than the step has
    busy_end = time.monotonic() + busy_s
    free, why = sqlite_readable(src, busy_s)
    if not free:
        raise GuardDeny(_deny("the SQLite file %s is held by a writer for more than sqlite_busy_s = %s s (%s), so it cannot be copied" % (src, settings.get("sqlite_busy_s", 5), why),
                              user="wait until the program that writes to it is done (or close it), then tell the agent to try again", no_setting=True))
    try:
        while True:
            ok, _how = db._dump_sqlite(_B, tmp, lambda: deadline - time.monotonic(), res)
            if ok:
                break
            if "lock" in (res.get("msg") or "").lower() or "busy" in (res.get("msg") or "").lower():
                if time.monotonic() < busy_end:
                    db._drop_own(tmp)
                    time.sleep(0.2)
                    continue
            raise OSError("the SQLite file %s could not be copied: %s" % (src, res.get("msg") or res.get("status")))
        sha, size = store._sha256(tmp), os.path.getsize(store._lp(tmp))
        final = _obj_path(root, sha)
        if os.path.isfile(store._lp(final)):
            if os.path.getsize(store._lp(final)) == size and store._sha256(final) == sha:
                return sha, size
            os.replace(store._lp(final), store._lp(final + ".damaged.%d" % int(time.time())))       # a damaged object is moved aside, never overwritten in place (as put_object does)
        os.makedirs(store._lp(os.path.dirname(final)), exist_ok=True)
        os.replace(store._lp(tmp), store._lp(final))
        if os.path.getsize(store._lp(final)) != size or store._sha256(final) != sha:
            raise ValueError("the copy of %s does not read back" % src)
        return sha, size
    finally:
        db._drop_own(tmp)


# ============================================================================================ one run of the check

class Run(object):
    """The data of one check or snapshot."""

    def __init__(self, cwd, settings, session, tool="", hook=True, out=None):
        self.cwd, self.settings, self.session, self.tool, self.hook, self.out = cwd, settings, session, tool, hook, out
        self.degraded = []                     # the notes that mean the promise is lower than usual (the person is told once per session per cause)
        self.degraded_marks = []               # [(file, key)] spent only when the answer that carried the words was DELIVERED (`mark_said`)
        self.state = None
        if not hook:                                  # `snapshot` is the person's own run outside the hook: no time of the hook, no walk budget
            self.settings = dict(settings, walk_budget_ms=600000)
        self.t0 = time.monotonic()
        budget = float(settings.get("step_budget_s", 60))
        self.deadline = self.t0 + budget if hook else None
        self.notes = []                        # for the log
        self.loud = []                         # for the person (systemMessage)
        self.manifest = []
        self.copied = 0
        self.root = None
        self.pkey = ""
        self._pgdump = None

    def left(self):
        return (self.deadline - time.monotonic()) if self.deadline else 3600.0

    def cap(self, seconds):
        """`seconds`, cut to what the step has left (the hook runs in a fixed time; a key of the settings never makes a wait or a call longer than that)."""
        return min(float(seconds), max(1.0, self.left())) if self.hook else float(seconds)

    def busy_s(self):
        """The wait for a writer of a SQLite file (`sqlite_busy_s`), cut to the step."""
        v = float(self.settings.get("sqlite_busy_s", 5))
        return min(v, max(0.0, self.left())) if self.hook else v

    def add_notes(self, eid, notes):
        for n in notes:
            if n.startswith(signals.DEGRADED):
                self.degraded.append("%s: %s" % (eid, n[len(signals.DEGRADED):]))
            self.notes.append("%s: %s" % (eid, n))

    def say(self, text):
        if self.out is not None:
            self.out(text)

    def pg_tools(self):
        if self._pgdump is None:
            path, why, _major = db.find_pg_dump(self.settings)
            self._pgdump = (path, why, db._sibling(path, "psql") if path else None)
        return self._pgdump


def _entry_name(e):
    return e["id"]


def _skip_paths(root):
    out = []
    if root:
        out.append(root)
        out.append(os.path.dirname(root))                      # the whole `<X>:\_shedkeeper`
    return out


def _too_large(settings, rel_or_path, size, what="file"):
    """None | a refusal text | 'allow' (it goes without a copy, loudly)."""
    if size <= int(settings["max_copy_mb"]) * 1048576:
        return None
    if settings["on_too_large"] == "allow":
        return "allow"
    return _deny("the %s %s is %.1f MB, bigger than max_copy_mb = %d: no copy can be taken" % (what, rel_or_path, size / 1048576.0, settings["max_copy_mb"]),
                 user="copy it by hand first, or exclude it from the list, or raise the limit", settings=[("on_too_large", "allow")])


def _process_tree(run, root, e, st_db):
    """Kind D. -> manifest entry or None (nothing changed)."""
    sig = signals.tree_signal(e, st_db.get(e["id"]), st_db.table(e["id"]), run.settings, skip=_skip_paths(run.root), now=None, left=run.left if run.hook else None)
    run.add_notes(e["id"], sig.notes)
    budget_ms = int(run.settings.get("walk_budget_ms", 3000))
    mode = sig.info.get("mode") or ""
    if (mode == "timeout" or (mode.startswith("walk") and sig.info.get("ms", 0) > budget_ms)) and run.hook:
        raise GuardDeny(_deny("the check of the tree %s became EXPENSIVE: the walk took %s ms, more than walk_budget_ms = %d (a tree that grew, an install that filled it ...)" % (
            e["root"], sig.info.get("ms", "more than the whole time"), budget_ms),
            user="exclude the heavy folder with `protect exclude %s NAME` (it needs your code), or take the copy in your own terminal (`%s`), then tell the agent to go on" % (e["id"], _snapshot_hint()),
            no_setting=True))
    if sig.verdict == signals.SAME:
        # the base moves forward (the position of the journal): safe, because nothing under the root changed since the last copy
        before = st_db.get(e["id"])
        keep = {k: v for k, v in before.items() if k != "usn"}
        now = {k: v for k, v in sig.new_state.items() if k != "usn"}
        if now != keep or sig.upserts or sig.deleted or int(sig.new_state.get("usn", 0)) - int(before.get("usn", 0)) > JOURNAL_ADVANCE_BYTES:
            st_db.commit(e["id"], sig.new_state, sig.upserts, sig.deleted)         # (a position that moved only a little is not worth a write at every step)
        return None
    todo = list(sig.changed)
    if sig.verdict == signals.UNKNOWN and not sig.changed and not sig.full:
        raise GuardDeny(_deny("the state of the tree %s cannot be told (%s)" % (e["root"], sig.why), user="look at the folder (does it exist? can it be read?) and tell the agent to try again", no_setting=True))
    need = sum(sig.upserts[r][0] for r in todo if r in sig.upserts)
    try:
        free = shutil.disk_usage(store._lp(run.root)).free
    except OSError:
        free = None
    if free is not None and need and free < need + FREE_MARGIN_BYTES:
        raise GuardDeny(_deny("there is not enough free space for the copies: %.1f MB to copy, %.1f MB free on the volume of the store %s" % (need / 1048576.0, free / 1048576.0, run.root),
                              user="free space on that volume (or move the store with `store_dir_name` / adopt another volume), then tell the agent to try again", no_setting=True))

    def companion_of(rel):
        for c in SQLITE_COMPANIONS:
            if rel.endswith(c):
                return rel[:-len(c)]
        return None

    main = {}                                              # SQLite files of the tree that need a backup because a companion (-wal ...) changed
    for rel in list(todo):
        base_rel = companion_of(rel)
        if base_rel is not None:
            p = os.path.join(root, base_rel.replace("/", os.sep))
            if os.path.isfile(p) and signals.is_sqlite_file(p):
                main[base_rel] = True
                if base_rel not in todo:
                    todo.append(base_rel)
    files = []
    skipped_large = []
    sqlite_seen = []
    n_done = 0
    total = len(todo)
    last_say = time.monotonic()
    for rel in todo:
        if companion_of(rel) in main:
            continue                                      # a companion is read through the backup of its main file
        p = os.path.join(root, rel.replace("/", os.sep))
        sg = sig.upserts.get(rel) or (0, 0, 0, 0)
        if run.deadline and time.monotonic() > run.deadline:
            raise GuardDeny(_deny("the copy of %s did not fit in step_budget_s = %s s (%d of %d files done)" % (e["root"], run.settings.get("step_budget_s"), n_done, total),
                                  user="take the copy in your own terminal, outside Claude Code: `%s`, then tell the agent to try again" % _snapshot_hint(), no_setting=True))
        try:
            size = os.path.getsize(store._lp(p))
        except FileNotFoundError:
            run.notes.append("%s: %s was gone before it could be copied (its earlier state is in the copies already taken)" % (e["id"], rel))
            continue
        except OSError as ex:
            raise GuardDeny(_deny("the file %s cannot be read (%s: %s), so it cannot be copied" % (p, ex.__class__.__name__, ex),
                                  user="close the program that holds it, or exclude it with `protect exclude %s NAME`, then tell the agent to try again" % e["id"], no_setting=True))
        is_sql = (rel in main) or (companion_of(rel) is None and size >= 512 and signals.is_sqlite_file(p))
        big = _too_large(run.settings, p, size)
        if big == "allow":
            skipped_large.append(rel)
            run.loud.append("the file %s is bigger than max_copy_mb and goes WITHOUT a copy (on_too_large = allow)" % p)
            continue
        if big:
            raise GuardDeny(big)
        try:
            if is_sql:
                sha, size2 = put_sqlite_object(run.root, p, run.deadline or (time.monotonic() + 3600), run.settings)
                kind = "sqlite"
                sqlite_seen.append(os.path.normcase(os.path.abspath(p)))
            else:
                sha, size2, _how = put_object(run.root, p, run.deadline)
                kind = "file"
        except FileNotFoundError:
            run.notes.append("%s: %s was gone before it could be copied" % (e["id"], rel))
            continue
        except TimeoutError:
            raise GuardDeny(_deny("the copy of %s did not fit in step_budget_s = %s s" % (p, run.settings.get("step_budget_s")),
                                  user="take the copy in your own terminal, outside Claude Code: `%s`, then tell the agent to try again" % _snapshot_hint(), no_setting=True))
        except (OSError, ValueError) as ex:
            raise GuardDeny(_deny("no verified copy of %s could be taken (%s: %s)" % (p, ex.__class__.__name__, ex),
                                  user="remove the cause (the file is locked, no free space, no rights), then tell the agent to try again", no_setting=True))
        files.append({"rel": rel, "sha": sha, "size": size2, "mtime_ns": ((sg[1] - signals.FILETIME_EPOCH_NS) * 100) if sg[1] else 0, "kind": kind})
        n_done += 1
        if run.out and time.monotonic() - last_say > 2:
            run.say("  %s: %d / %d files" % (e["id"], n_done, total))
            last_say = time.monotonic()
    _announce_sqlite(run, sqlite_seen)
    entry = {"entry": e["id"], "kind": "D", "files": files, "deleted": list(sig.deleted), "skipped_large": skipped_large, "mode": sig.info.get("mode"), "full": bool(sig.full)}
    run.notes.append("%s: %s" % (e["id"], sig.why))
    # the state is written AFTER the manifest line (the caller) -- here only what is needed to write it
    entry["_commit"] = (e["id"], sig.new_state, sig.upserts, sig.deleted, bool(sig.full))
    return entry


def _announce_sqlite(run, paths):
    """A SQLite file that appeared inside a tree and is not an entry of its own (3.14): copied with the backup program already (stronger, never weaker); said ONCE, loudly, at
    the first step that sees it; the start of the next sessions repeats it while it is not decided."""
    if not paths or not run.pkey or protect.databases_off():
        return                                             # (a release without databases cannot add the file as an entry: nothing is offered that would be refused)
    st = run.state or protect.state(run.cwd, run.settings)
    have = {protect.entry_key(x) for x in st.entries if x["kind"] == "S"}
    done = set(protect.announced(run.root, run.pkey)) | set(protect.declined(run.root, run.pkey)) | set((st.mark or {}).get("seen") or ())
    for p in paths:
        if p in have or p in done:
            continue
        run.loud.append("a SQLite file %s is inside a guarded folder and is not an entry of its own: it is copied with the backup program; decide: `python %s protect add sqlite <file>` "
                        "or `protect decline %s`" % (p, advice.kit_py(), p))
        try:
            protect.mark_announced(run.root, run.pkey, p)
        except OSError:
            pass


def _db_base(e):
    return db.pg_base(e["host"], e["port"], e["dbname"], e.get("user", ""), "", source="protected list")


def sqlite_readable(path, busy_s):
    """(True, '') when a reader can get at the SQLite file within `busy_s` seconds; (False, why) when a writer keeps it locked that long (a rollback-journal database held by an exclusive
    writer blocks readers; the backup would otherwise retry until the whole time of the copy is spent). Any other fault is not decided here: the copy reports it."""
    end = time.monotonic() + max(0.0, float(busy_s))
    uri = pathlib.Path(os.path.abspath(path)).as_uri() + "?mode=ro"
    while True:
        con = None
        try:
            con = sqlite3.connect(uri, uri=True, timeout=0.2)
            con.execute("select count(*) from sqlite_master").fetchone()
            return True, ""
        except sqlite3.OperationalError as e:
            msg = str(e)
            if "locked" not in msg.lower() and "busy" not in msg.lower():
                return True, ""
            if time.monotonic() >= end:
                return False, msg
            time.sleep(0.1)
        except sqlite3.Error:
            return True, ""
        finally:
            if con is not None:
                try:
                    con.close()
                except Exception:
                    pass


def _copy_db(run, base, e):
    """A verified copy of a database through db.copy_base -> manifest facts, or raises GuardDeny."""
    s = run.settings
    dl = min(float(s["db_deadline_s"]), max(1.0, run.left())) if run.hook else float(s["db_deadline_s"])
    if base.engine == "sqlite" and os.path.isfile(base.path):
        free, why = sqlite_readable(base.path, run.busy_s())
        if not free:
            raise GuardDeny(_deny("the SQLite file %s is held by a writer for more than sqlite_busy_s = %s s (%s), so it cannot be copied" % (base.path, s.get("sqlite_busy_s", 5), why),
                                  user="wait until the program that writes to it is done (or close it), then tell the agent to try again", no_setting=True))
    res = db.copy_base(base, run.root, s, dl if run.hook else 0, enforce_limits=True, session=run.session, via="guard", command="(guard before a step: %s)" % run.tool)
    if base.engine == "sqlite" and res["status"] == "error" and ("lock" in (res["msg"] or "").lower() or "busy" in (res["msg"] or "").lower()):
        busy_end = time.monotonic() + run.busy_s()
        while time.monotonic() < busy_end and res["status"] == "error":
            time.sleep(0.2)
            res = db.copy_base(base, run.root, s, dl if run.hook else 0, enforce_limits=True, session=run.session, via="guard", command="(guard before a step: %s)" % run.tool)
    st = res["status"]
    if st == "ok":
        return {"copy_id": res.get("id"), "copy": res.get("path"), "sha256": res.get("sha256"), "size": res.get("size"), "verified": res.get("verified", "")}
    if st == "absent":
        return {"absent": True}
    if st == "toolarge":
        if s["on_db_too_large"] == "allow":
            run.loud.append("the database %s is bigger than max_db_mb and goes WITHOUT a copy (on_db_too_large = allow)" % base.url_masked)
            return {"skipped_large": True}
        raise GuardDeny(_deny("the database %s is too big for the check before a step: %s" % (base.url_masked, res["msg"]),
                              user="take the copy in your own terminal: `%s`, or raise max_db_mb" % _snapshot_hint(), settings=[("on_db_too_large", "allow")]))
    if st == "timeout" and s["on_db_too_large"] == "allow":
        run.loud.append("the copy of the database %s did not finish in time and the step goes WITHOUT it (on_db_too_large = allow)" % base.url_masked)
        return {"skipped_large": True}
    raise GuardDeny(_deny("no verified copy of the database %s could be taken (%s)" % (base.url_masked, res.get("msg") or st),
                          user=("take the copy in your own terminal, outside Claude Code: `%s`" % _snapshot_hint()) if st == "timeout" else "remove the cause (the server, the rights, pg_dump), then tell the agent to try again",
                          no_setting=True))


def _process_sqlite(run, e, st_db):
    sig = signals.sqlite_signal(e, st_db.get(e["id"]))
    if sig.verdict == signals.SAME:
        return None
    base = db.Base("sqlite", path=e["path"])
    facts = _copy_db(run, base, e)
    run.notes.append("%s: %s" % (e["id"], sig.why))
    return {"entry": e["id"], "kind": "S", "path": e["path"], **facts, "_commit": (e["id"], sig.new_state, None, None, False)}


def _process_pg(run, e, st_db):
    base = _db_base(e)
    if isinstance(base, db.Unnamed):
        raise GuardDeny(_deny("the database of the entry %s cannot be named (%s)" % (e["id"], base.reason), no_setting=True,
                              user="correct the entry with `protect remove %s` and `protect add ...`" % e["id"]))
    pgdump, why, psql = run.pg_tools()
    if not pgdump:
        raise GuardDeny(_deny("pg_dump was not found (%s): no copy of the database %s can be taken" % (why, base.url_masked),
                              user="install the PostgreSQL client tools, or name pg_dump with `set pg_dump_path`", settings=[("pg_dump_path", "<folder of pg_dump>")]))
    sig = signals.pg_signal(e, st_db.get(e["id"]), run.settings, base, psql, timeout=run.cap(15.0))
    if sig.verdict == signals.SAME:
        return None
    if sig.dead and sig.info.get("track_counts"):
        # the counters do not move: a copy at every step -- when it is cheap (M4.15)
        kind, size, extra = db.measure_size(base, run.settings, pgdump, run.cap(15.0))
        if kind == "ok" and size > int(run.settings.get("max_db_mb_every_step", 50)) * 1048576:
            raise GuardDeny(_deny("track_counts = off on the server: the counters of %s do not move, so it would need a copy before EVERY step, and it is %.0f MB (more than max_db_mb_every_step = %s)" % (
                base.url_masked, size / 1048576.0, run.settings.get("max_db_mb_every_step", 50)),
                user="switch track_counts on in the server, or take the copy in your own terminal (`%s`), or take the database out of the list" % _snapshot_hint(), no_setting=True))
    facts = _copy_db(run, base, e)
    run.notes.append("%s: %s" % (e["id"], sig.why))
    ns = dict(sig.new_state)
    if sig.dead:
        ns["tup"] = None                                   # no base is recorded for a dead signal: the next check is `unknown` again, on purpose
    return {"entry": e["id"], "kind": e["kind"], "db": base.url_masked, **facts, "_commit": (e["id"], ns, None, None, False)}


def measure_remote(root, e, settings):
    """The price of an entry of kind R (M4.18): a verified copy over the network, timed. -> (seconds, text for the person)."""
    base = _db_base(e)
    if isinstance(base, db.Unnamed):
        return None, "the database cannot be named (%s)" % base.reason
    t0 = time.monotonic()
    res = db.copy_base(base, root, settings, 0, enforce_limits=True, session="", via="measure", command="(measure of a remote entry)")
    secs = time.monotonic() - t0
    if res["status"] == "ok":
        return secs, ("a verified copy of %s over the network took %.1f s: that is what EVERY step adds while the database changes or its counters cannot be read (usually the case on a hosted service)" % (base.url_masked, secs))
    return None, "no copy could be taken now (%s): the step would be refused until the database can be copied" % (res.get("msg") or res["status"])


def _entries_to_check(st, settings, cwd):
    """(entries, why-not). Active list: its entries. No confirmed list: nothing (variant A: only the accelerators), or the provisional list of the survey
    when before_confirm = provisional (variant B: in the code, off by default, waits for the person's word)."""
    if st.kind == "active":
        return [e for e in st.entries if e["kind"] in protect.live_kinds()], ""
    if st.kind in ("none", "draft") and settings.get("before_confirm", "accelerators") == "provisional":
        from . import survey
        try:
            cand = survey.candidates(cwd, settings)
        except Exception as e:
            raise GuardDeny(_deny("the provisional list could not be made (%s: %s)" % (e.__class__.__name__, e), no_setting=True))
        return [c["entry"] for c in cand if c.get("entry") and c["entry"]["kind"] in protect.live_kinds() and c["entry"]["kind"] != "R"], "provisional"
    return [], ""


def _log(root, rec):
    try:
        store.log(root, rec)
    except Exception:
        pass


def _guard_dir(root, pkey):
    return os.path.join(root, "guard", pkey)


def _cause(text):
    return re.sub(r"\d+", "N", text)[:90]


def _say_degraded(run, gdir):
    """The notes that mean the promise of this check is LOWER than usual (the walk without ChangeTime, a cut look at the files, a history of the journal that could not be read, a state
    that did not read): said to the person once per session per cause (`run.loud`), and kept in `degraded.json` for `status` (with the time it was last seen). The cause is spent for the
    session only when the answer that carries the words is DELIVERED (`mark_said`, called by the hook after it wrote an answer that lets the step through): a step that is refused, or
    cut by the watchdog, drops the notice and the words come again. A fault in this never changes the check."""
    if not run.degraded:
        return
    try:
        seen_path = os.path.join(gdir, "degraded_said.json")
        said = set(protect._json_list(seen_path))
        fresh = []
        for d in run.degraded:
            key = "%s|%s" % (run.session or "-", _cause(d))
            if key not in said:
                said.add(key)
                fresh.append(d)
                run.degraded_marks.append((seen_path, key))
        if fresh:
            run.loud.append("ShedKeeper: the promise of this check is LOWER now: " + "; ".join(x[:200] for x in fresh[:3]))
        info_path = os.path.join(gdir, "degraded.json")
        try:
            with open(store._lp(info_path), encoding="utf-8") as fh:
                info = {k: v for k, v in json.load(fh).items() if isinstance(v, dict)}
        except (OSError, ValueError, AttributeError):
            info = {}
        info.update({_cause(d): {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "text": d[:300]} for d in run.degraded})
        tmp = os.path.join(gdir, "degraded.json.%d.tmp" % os.getpid())
        with open(store._lp(tmp), "w", encoding="utf-8") as fh:
            json.dump(info, fh)
        os.replace(store._lp(tmp), store._lp(info_path))
    except Exception:
        pass


def mark_said(result):
    """The answer of the hook that carried `result["notice"]` was DELIVERED: only now the causes it named are spent for the session. Never raises."""
    for path, key in (result or {}).get("marks") or []:
        try:
            protect._json_list_add(path, key)
        except Exception:
            pass


def run_check(run, st):
    """The body of the check, under the lock. -> the manifest line number or None."""
    run.degraded = []                                      # (the notes of an earlier list of the same step are not this list's)
    entries, mode = _entries_to_check(st, run.settings, run.cwd)
    if not entries:
        return None
    root = st.root
    run.root = root
    run.pkey = st.pkey
    run.state = st
    ok, note, rep = store.ensure_store_report(root, run.settings)
    if not ok:
        raise GuardDeny(_deny("the store cannot be used (%s), so no copy can be taken" % note, user="repair the store (`python %s status`)" % advice.kit_py(), no_setting=True))
    run.created = rep
    gdir = _guard_dir(root, st.pkey)
    with _Lock(os.path.join(gdir, "lock"), run.cap(run.settings.get("lock_wait_s", 30)) if run.hook else float(run.settings.get("lock_wait_s", 30))):
        sdb = StateDB(os.path.join(gdir, "state.db"))
        try:
            run.notes += sdb.notes
            if sdb.notes:
                run.degraded += sdb.notes
            parts = []
            order = {"S": 0, "P": 1, "R": 2, "D": 3}
            for e in sorted(entries, key=lambda x: order.get(x["kind"], 9)):       # the cheap signals first (M4.4)
                if run.deadline and time.monotonic() > run.deadline:
                    raise GuardDeny(_deny("the check did not finish in step_budget_s = %s s" % run.settings.get("step_budget_s"),
                                          user="take the copy in your own terminal: `%s`" % _snapshot_hint(), no_setting=True))
                if e["kind"] == "D":
                    r = _process_tree(run, e["root"], e, sdb)
                elif e["kind"] == "S":
                    r = _process_sqlite(run, e, sdb)
                else:
                    r = _process_pg(run, e, sdb)
                if r is not None:
                    parts.append(r)
            _say_degraded(run, gdir)
            if not parts:
                return None
            step_no = None
            holder = {}

            def make(n):
                holder["n"] = n
                return {"step": n, "time": time.strftime("%Y-%m-%dT%H:%M:%S"), "session": run.session or "", "tool": run.tool,
                        "entries": [{k: v for k, v in p.items() if not k.startswith("_")} for p in parts]}
            try:
                store._append_jsonl(os.path.join(gdir, "steps.jsonl"), make)
            except Exception as ex:
                raise GuardDeny(_deny("the manifest of the step could not be written (%s: %s): the copies are made but the bases of the signals stay as they were" % (ex.__class__.__name__, ex),
                                      user="repair the store (`python %s status`)" % advice.kit_py(), no_setting=True))
            step_no = holder["n"]
            for p in parts:                               # only now (M4.4a): the manifest is written, the copies are read back
                eid, new_state, ups, dels, full = p["_commit"]
                try:
                    sdb.commit(eid, new_state, ups, dels, replace_all=full)
                except Exception as ex:
                    raise GuardDeny(_deny("the base of the signal of %s could not be written (%s: %s)" % (eid, ex.__class__.__name__, ex),
                                          user="repair the store (`python %s status`)" % advice.kit_py(), no_setting=True))
            run.copied = len(parts)
            run.manifest = parts
            return step_no
        finally:
            sdb.close()


def _below_broken_text(st, cwd=""):
    """The refusal of a step while a list UNDER the folder of the step (or of the session) cannot be used (order 12, v6, variant A): the list does not tell what it guards (the mark keeps hashes
    only, and an entry may name a tree outside its project), so no step can be proven not to reach it."""
    try:
        under = store._within(st.project, cwd or os.getcwd()) or store._within(os.path.realpath(st.project), os.path.realpath(cwd or os.getcwd()))
    except Exception:
        under = False
    where = "a project under the folder of this step" if under else "a project under the folder of this session (CLAUDE_PROJECT_DIR)"
    return _deny("the protected list of %s (%s) cannot be used now: %s. Every step that changes data is refused while that list cannot be used" % (st.project, where, st.why),
                 user="repair that list (the copies of the store are not touched), or rebuild it with `protect add ...`, which needs your code; or open the session in the folder of the project "
                      "you work on (then that list is not under the folder of the step)", no_setting=True)


def check(data, settings, out=None):
    """The hook. -> None when the step may go on and nothing is to be said, else a dict {decision, text, notice, meta}. Never raises: an exception is a refusal (M4.4b).
    Every list that stands over the folder of the step (its own and the ones above it) is checked."""
    tool = data.get("tool_name")
    if isinstance(tool, str) and (tool in READ_ONLY_TOOLS or tool in CONTROL_TOOLS):
        return None
    cwd = data.get("cwd") if isinstance(data.get("cwd"), str) else ""
    session = data.get("session_id") if isinstance(data.get("session_id"), str) else ""
    if not cwd:
        cwd = os.getcwd()                                  # the runtime always sends one; without it the folder of this process stands in (a step is not passed unchecked for want of a name)
        try:
            _log(store.store_root(cwd, settings), {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": "no_cwd", "tool": tool, "msg": "the hook input had no cwd: the folder of the hook process %s was used" % cwd})
        except Exception:
            pass
    run = Run(cwd, settings, session, tool or "", hook=True)
    try:
        if settings.get("before_confirm", "accelerators") != "provisional" and not protect.maybe_list(cwd, settings):
            return None                                    # no list of this project anywhere: nothing to check, and no volume was looked at beyond the project's own
        steps, copied, roots, created, loud = [], 0, [], None, []
        for st in protect.states(cwd, settings):
            if st.kind == "broken":
                if st.below and st.root is not None:
                    raise GuardDeny(_below_broken_text(st, cwd))
                if st.root is None:
                    raise GuardDeny(_deny("a protected list of %s may be there, but the store cannot be named: %s" % (st.project, st.why),
                                          user="repair the cause (the account, the volume), then tell the agent to try again", no_setting=True))
                lead = "the protected list was confirmed but it cannot be used now" if st.mark else "the protected list cannot be used now"
                raise GuardDeny(_deny("%s (the list of %s): %s" % (lead, st.project, st.why),
                                      user="repair the list (the copies of the store are not touched), or rebuild it with `protect add ...`, which needs your code", no_setting=True))
            if st.kind in ("none", "draft") and settings.get("before_confirm", "accelerators") != "provisional":
                continue
            if not st.root:
                if st.kind == "none":
                    continue
                raise GuardDeny(_deny("no store folder can be named for this folder: %s" % st.why, no_setting=True))
            run.copied = 0
            step = run_check(run, st)
            copied += run.copied
            roots.append(st.root)
            if step is not None:
                steps.append(step)
            if getattr(run, "created", None) and not created:
                created = run.created
        if not steps and not run.loud:
            return None
        res = {"decision": "allow", "text": "", "notice": " ".join(run.loud), "meta": {"guard_ms": int((time.monotonic() - run.t0) * 1000), "root": roots[0] if roots else None,
                                                                                        "guard_copied": copied, "guard_notes": run.notes[:20], "guard_step": steps[0] if steps else None}, "created": created,
               "marks": list(run.degraded_marks)}
        return res
    except GuardDeny as d:
        return {"decision": "deny", "text": d.text, "notice": "", "meta": {"guard_ms": int((time.monotonic() - run.t0) * 1000), "error": "guard denied"}}
    except Exception as e:        # M4.4b: everything that goes wrong inside the check refuses the step
        try:
            err = db.mask_secrets("%s: %s" % (e.__class__.__name__, e))
        except Exception:
            err = e.__class__.__name__
        return {"decision": "deny", "text": _deny("the check before the step failed (%s), so no copy can be trusted" % err[:300],
                                                  agent="run `python %s status` to see what is wrong" % advice.kit_py(),
                                                  user="repair or remove ShedKeeper (python install.py uninstall) if the fault stays", no_setting=True),
                "notice": "", "meta": {"error": err[:300]}}


# ============================================================================================ snapshot (outside the hook)

def snapshot(cwd, settings, session="", out=print):
    """The copy by hand: every entry of EVERY list the check before a step looks at (the folder's own, the ones above it, the ones under it) is checked and copied as the hook would, WITHOUT
    the budget of the hook, with progress. -> (exit code, text): 0 done; 3 done but some entry has NO copy; 1 refused (a list that cannot be used, a fault)."""
    todo = [s for s in protect.states(cwd, settings) if s.kind != "none" and (s.entries or s.kind == "broken")]
    if not todo:
        return 1, "REFUSED: there is no list to copy (`protect add ...`, then `protect confirm`)"
    codes, texts = [], []
    for st in todo:
        if len(todo) > 1:
            out("list of %s:" % st.project)
        code, text = _snapshot_one(st, cwd, settings, session, out)
        codes.append(code)
        texts.append(("list of %s:\n%s" % (st.project, text)) if len(todo) > 1 else text)
    return (1 if 1 in codes else (3 if 3 in codes else 0)), "\n".join(texts)


def _snapshot_one(st, cwd, settings, session, out):
    """The copy by hand of ONE list. -> (exit code, text)."""
    if st.kind == "broken":
        return 1, "REFUSED: the list of %s cannot be used: %s" % (st.project, st.why)
    run = Run(cwd, settings, session, "snapshot", hook=False, out=out)
    lines = []
    nocopy = []
    t0 = time.monotonic()
    try:
        entries = [e for e in st.entries if e["kind"] in protect.live_kinds()]
        out("snapshot: %d entries" % len(entries))
        if protect.db_unprotected_text(st.entries):
            out("WARNING: %s. %s" % (protect.db_unprotected_text(st.entries), protect.db_off_words(st.entries)))
            for e in protect.db_unprotected(st.entries):
                out("  %s: NO copy (databases are not part of this release)" % e["id"])
        nocopy.extend(e["id"] for e in protect.db_unprotected(st.entries))
        run.root = st.root
        run.pkey = st.pkey
        ok, note, _rep = store.ensure_store_report(st.root, settings)
        if not ok:
            return 1, "REFUSED: the store cannot be used (%s)" % note
        gdir = _guard_dir(st.root, st.pkey)
        with _Lock(os.path.join(gdir, "lock"), float(settings.get("lock_wait_s", 30))):
            sdb = StateDB(os.path.join(gdir, "state.db"))
            try:
                parts = []
                for e in entries:
                    t1 = time.monotonic()
                    out("  %s ..." % protect.describe_entry(e))
                    if e["kind"] == "D":
                        r = _process_tree(run, e["root"], e, sdb)
                    elif e["kind"] == "S":
                        r = _process_sqlite(run, e, sdb)
                    else:
                        r = _process_pg(run, e, sdb)
                    word, missing = _copy_word(r)
                    out("  %s: %s (%.1f s)" % (e["id"], word, time.monotonic() - t1))
                    if missing:
                        nocopy.append(e["id"])
                    if r is not None:
                        parts.append(r)
                if parts:
                    holder = {}

                    def make(n):
                        holder["n"] = n
                        return {"step": n, "time": time.strftime("%Y-%m-%dT%H:%M:%S"), "session": session or "", "tool": "snapshot",
                                "entries": [{k: v for k, v in p.items() if not k.startswith("_")} for p in parts]}
                    store._append_jsonl(os.path.join(gdir, "steps.jsonl"), make)
                    for p in parts:
                        eid, ns, ups, dels, full = p["_commit"]
                        sdb.commit(eid, ns, ups, dels, replace_all=full)
                    lines.append("manifest: step %d" % holder["n"])
            finally:
                sdb.close()
    except GuardDeny as d:
        return 1, d.text
    except Exception as ex:
        return 1, "REFUSED: the snapshot failed (%s: %s)" % (ex.__class__.__name__, ex)
    lines.append("snapshot done in %.1f s" % (time.monotonic() - t0))
    if nocopy:
        lines.append("%d entr%s %s NO copy (%s): the data in %s is NOT protected by a copy" % (len(nocopy), "y" if len(nocopy) == 1 else "ies", "has" if len(nocopy) == 1 else "have",
                                                                                          ", ".join(nocopy), "it" if len(nocopy) == 1 else "them"))
    return (3 if nocopy else 0), "\n".join(lines + run.loud)


def _copy_word(r):
    """(the word for the person, whether something got NO copy) for the result of one entry of a check: 'copied' is said only when a copy was taken."""
    if r is None:
        return "unchanged", False
    if r.get("absent"):
        return "NO copy (the database is not there)", True
    if r.get("skipped_large") is True:
        return "NO copy (too big or too slow to copy; you allowed the step to go without one)", True
    if r.get("skipped_large"):
        return "copied, but %d file(s) are too big and have NO copy (you allowed it)" % len(r["skipped_large"]), True
    return "copied", False


# ============================================================================================ steps: find and restore

def _steps(root, pkey):
    p = os.path.join(_guard_dir(root, pkey), "steps.jsonl")
    try:
        recs, bad = store._read_jsonl(p)
    except FileNotFoundError:
        return [], 0
    return [r for _n, r in recs if isinstance(r.get("step"), int)], bad


def find_text(cwd, settings, limit=30):
    """The steps with copies of every list the check looks at (the folder's own, above it, under it); with more than one list, each block names its project."""
    sts = [s for s in protect.states(cwd, settings) if s.root and s.kind != "none"] or [protect.state(cwd, settings)]
    if not sts[0].root:
        return 1, "no store for this project"
    if len(sts) == 1:
        return _find_one(sts[0], limit)
    blocks = []
    for st in sts:
        code, text = _find_one(st, limit)
        blocks.append("list of %s:\n%s" % (st.project, text))
    return 0, "\n".join(blocks)


def _find_one(st, limit):
    steps, bad = _steps(st.root, st.pkey)
    if not steps:
        return 0, "no steps with copies yet for %s" % st.project
    lines = ["steps with copies (the number is the one `restore --step N` takes):"]
    for r in steps[-limit:]:
        what = []
        for en in r["entries"]:
            if en["kind"] == "D":
                what.append("%s: %d file(s)%s" % (en["entry"], len(en.get("files") or []), (", %d deleted" % len(en["deleted"])) if en.get("deleted") else ""))
            else:
                what.append("%s: database copy %s" % (en["entry"], en.get("copy_id") or ("absent" if en.get("absent") else "skipped")))
        lines.append("  step %d  %s  tool=%s  session=%s  | %s" % (r["step"], r["time"], r.get("tool", ""), (r.get("session") or "")[:8], "; ".join(what)))
    if bad:
        lines.append("(%d damaged line(s) in the manifest were skipped)" % bad)
    return 0, "\n".join(lines)


def restore_step(cwd, settings, step, to, session=""):
    """The state of the list as it was copied up to step `step` (inclusive), into the NEW place `to` (M7.1). -> (exit code, text)."""
    sts = [s for s in protect.states(cwd, settings) if s.root and s.entries]
    if not sts:
        return 1, "REFUSED: there is no list for this project"
    found, bad_all = [], 0
    for s in sts:                                          # the step is looked for in every list over this folder: the nearest may have no copy at all while the one above has
        steps_s, bad_s = _steps(s.root, s.pkey)
        bad_all += bad_s
        upto = [r for r in steps_s if r["step"] <= step]
        if upto:
            found.append((s, upto, bad_s))
    if not found:
        return 1, "REFUSED: no step up to %d has a copy" % (step) + ((" (looked in the lists of: %s)" % "; ".join(s.project for s in sts)) if len(sts) > 1 else "") + (
            (" (%d damaged line(s) in the manifest were skipped: the step may be among them)" % bad_all) if bad_all else "")
    st, steps, bad = found[0]
    to = os.path.abspath(to)
    if store._lexists(to):
        return 1, "REFUSED: %s exists already: a restore goes to a NEW place and overwrites nothing" % to
    for e in st.entries:
        roots = [e["root"]] if e["kind"] == "D" else ([os.path.dirname(e["path"])] if e["kind"] == "S" else [])
        for r_ in roots:
            if store._within(to, r_):
                return 1, "REFUSED: %s lies inside %s, the root of the entry %s: restore to a place OUTSIDE the guarded data" % (to, r_, e["id"])
    if store._within(to, st.root):
        return 1, "REFUSED: %s lies inside the store" % to
    state_by_entry, db_by_entry, big_by_entry = {}, {}, {}
    for r in steps:
        for en in r["entries"]:
            if en["kind"] == "D":
                m = state_by_entry.setdefault(en["entry"], {})
                bg = big_by_entry.setdefault(en["entry"], {})       # {rel: words}: the files that have NO copy at the last step that spoke of them
                if en.get("full"):
                    m.clear()
                    bg.clear()
                for f in en.get("files") or []:
                    m[f["rel"]] = f
                    bg.pop(f["rel"], None)                          # a later step copied it: it is restored, not 'NOT restored'
                for rel in en.get("deleted") or []:
                    m.pop(rel, None)
                    bg.pop(rel, None)
                for rel in en.get("skipped_large") or []:
                    m.pop(rel, None)
                    bg[rel] = "%s/%s: no copy was taken (bigger than the limit; you allowed it)" % (en["entry"], rel)
            else:
                db_by_entry[en["entry"]] = en
    big = [words for bg in big_by_entry.values() for _rel, words in sorted(bg.items())]
    restored, missing = [], []
    os.makedirs(to)
    for eid, m in state_by_entry.items():
        for rel, f in sorted(m.items()):
            src = _obj_path(st.root, f["sha"])
            dst = os.path.join(to, eid, rel.replace("/", os.sep))
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copyfile(store._lp(src), store._lp(dst))
                if store._sha256(dst) != f["sha"]:
                    raise ValueError("sha256 differs after the restore")
                if f.get("mtime_ns"):
                    try:
                        os.utime(dst, ns=(f["mtime_ns"], f["mtime_ns"]))
                    except OSError:
                        pass
                restored.append("%s/%s" % (eid, rel))
            except (OSError, ValueError) as ex:
                missing.append("%s/%s (%s: %s)" % (eid, rel, ex.__class__.__name__, ex))
    notes = []
    for eid, en in db_by_entry.items():
        if en.get("copy") and os.path.isfile(en["copy"]):
            dst = os.path.join(to, eid, os.path.basename(en["copy"]))
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copyfile(en["copy"], dst)
                restored.append("%s/%s" % (eid, os.path.basename(en["copy"])))
                if en["kind"] in ("P", "R"):
                    notes.append("%s is a PostgreSQL dump: load it into a NEW database with pg_restore -d NEWDB %s" % (eid, dst))
            except OSError as ex:
                missing.append("%s (%s)" % (eid, ex))
        elif en.get("skipped_large") or en.get("absent"):
            missing.append("%s: no copy was taken (%s)" % (eid, "bigger than the limit" if en.get("skipped_large") else "the database did not exist"))
        else:
            missing.append("%s: the copy file is not there (%s)" % (eid, en.get("copy")))
    missing = missing + big                                # the failures come FIRST: a long list of files that never had a copy must not push them out of the lines shown
    text = ["restored %d item(s) into %s (step %d and the steps before it)%s" % (len(restored), to, step, (" from the list of %s" % st.project) if len(sts) > 1 else "")]
    if len(found) > 1:
        text.append("other lists above this folder also have a step up to %d: %s -- to restore one of them, run the command from its own folder" % (step, "; ".join(s.project for s, _u, _b in found[1:])))
    if missing:
        text.append("NOT restored (%d): %s" % (len(missing), "; ".join(missing[:20])))
    if bad:
        text.append("THE RESTORE MAY BE INCOMPLETE: %d damaged line(s) in the manifest of the steps were skipped, and the steps they held are not in this restore" % bad)
    text += notes
    return (0 if not (missing or bad) else 1), "\n".join(text)


def _link_counts(st):
    """{entry id: number of links skipped at the last check} for the trees of the list, read from the state of the signals; {} when there is none. Never writes."""
    path = os.path.join(_guard_dir(st.root, st.pkey), "state.db")
    if not os.path.isfile(store._lp(path)):
        return {}
    out = {}
    con = None
    try:
        con = sqlite3.connect(pathlib.Path(os.path.abspath(path)).as_uri() + "?mode=ro", uri=True, timeout=1)
        for eid, js in con.execute("select entry, st from kv"):
            try:
                n = int(json.loads(js).get("links") or 0)
            except (ValueError, AttributeError):
                n = 0
            if n:
                out[eid] = n
    except sqlite3.Error:
        return {}
    finally:
        if con is not None:
            con.close()
    return out


def _degraded_age_ok(stamp):
    """Is a degradation seen at `stamp` (local time, as written) still young enough to show? Not readable -> show it."""
    try:
        return time.time() - time.mktime(time.strptime(stamp, "%Y-%m-%dT%H:%M:%S")) <= DEGRADED_STATUS_DAYS * 86400
    except (ValueError, TypeError, OverflowError):
        return True


def status_lines(cwd, settings):
    """[lines] for `status`: every list the check before a step looks at (the nearest first); with more than one, each block names its project."""
    sts = [s for s in protect.states(cwd, settings) if s.kind != "none"] or [protect.state(cwd, settings)]
    here = protect._project(cwd or os.getcwd())
    named = len(sts) > 1 or store._norm(sts[0].project) != store._norm(here)
    out = []
    for st in sts:
        out += _status_block(st, named)
    out.append("before the list is confirmed: %s" % settings.get("before_confirm", "accelerators"))
    return out


def _status_block(st, named):
    out = ["protected list%s: %s (%d entries)%s" % ((" of %s" % st.project) if named else "", st.kind, len(st.entries), (" -- " + st.why) if st.why else "")]
    dbt = protect.db_unprotected_text(st.entries)
    if dbt:
        out.append("WARNING: %s. %s" % (dbt, protect.db_off_words(st.entries)))
    if st.root:
        steps, bad = _steps(st.root, st.pkey)
        out.append("steps with copies: %d%s" % (len(steps), (", last at %s" % steps[-1]["time"]) if steps else ""))
        if bad:
            out.append("WARNING: %d damaged line(s) in the manifest of the steps: `restore --step` may be incomplete" % bad)
        dpath = os.path.join(_guard_dir(st.root, st.pkey), "degraded.json")
        try:
            with open(store._lp(dpath), encoding="utf-8") as fh:
                for cause, v in sorted(json.load(fh).items()):
                    if isinstance(v, dict) and _degraded_age_ok(v.get("time")):
                        out.append("DEGRADED (last at %s): %s" % (v.get("time", "?"), v.get("text", cause)))
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError) as e:
            out.append("WARNING: the notes of degradations cannot be read (%s: %s), so a lowered promise may not be shown here" % (e.__class__.__name__, e))
        links = _link_counts(st)
        if links:
            out.append("folders reached through a link (junction, symbolic link) are not entered (limit 9): %s" % "; ".join("%s: %d" % (k, v) for k, v in sorted(links.items())))
    return out
