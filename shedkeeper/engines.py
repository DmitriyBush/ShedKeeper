r"""engines.py -- the engine adapter: the contract, the two built-in adapters, the adapters of the user, the proofs (0.3.0).

An ENGINE ADAPTER is the one place where ShedKeeper knows a database engine. Everything else -- the decision of the hook, the store, the
list of copies, `dbcopy`, `restore`, `find`, `status` -- is the same for every engine and talks to the engine only through the contract
below. PostgreSQL and SQLite are two adapters of this contract (`PostgresEngine`, `SqliteEngine`); an adapter of the user is a python file
in the folder `config.adapters_dir()` that defines `ADAPTER` -- see ARCHITECTURE.md ("Adding an engine").

THE CONTRACT  (every method is called by the framework; the adapter never writes the list of copies or the log itself)

  class attributes
    id            the engine, lower case, [a-z][a-z0-9_]{1,20}: written in every record of a copy, the prefix of a copy's file name
    title         the name for a person ("MySQL")
    heads         names of commands the adapter READS (`mysql`, `mysqldump`): `recognize` is called for them
    schemes       address schemes the engine owns (`mysql`, `mariadb`): an address with this scheme is named by `name`
    exts          file extensions that mean this engine when a file is named (`.duckdb`): for a file-based engine
    tool_names    the words project tools use for the engine (Rails `mysql2`, Laravel `mysql`, Prisma `mysql`)
    ext           the extension of the copy file (`.dump`); file_based  True when the database is one file (Base.path)

  1 recognise   recognize(words, ctx)                 -> [finding]       the destroying actions of this engine in ONE command of the shell
  2 name        name(finding, cwd, env)                -> Base | Unnamed   which database; the password only in Base.conninfo / Base.secrets
  3 prepare     prepare(settings)                      -> (tools, why)     find what the engine's own tools need (None + why: no copy possible)
  4 size        size(base, settings, tools, timeout)   -> ("ok", bytes, extra) | ("absent", 0, 0) | ("error", 0, why)
                check(base, tools, extra)              -> "" | why         optional: this tool cannot copy that server
  5 copy        copy(base, tmp, tools, left, res)      -> (ok, how_verified)   write ONE file `tmp`, then READ IT BACK IN FULL (not a listing)
  6 record      done by the framework: id, engine, identity, address WITHOUT password, size, sha256, how verified, command (masked), time
  7 restore     restore(rec, copy, root, settings, now, to, session) -> Restored | Refused   into a NEW place; never over a live database
  prove         restored_base(base, op) -> Base;  probe_state / probe_fill / probe_read / probe_destroy  (see prove.py)

A finding is {"reasons": ["DROP TABLE"], "target": "<address or file or None>", "unnamed": "<why not nameable> or None", "parts": {...}}.
The framework masks every printable text and REFUSES to write a record that carries a secret of the connection (Base.secrets).

AN ADAPTER OF THE USER counts as protection only while a proof for the CURRENT sha256 of its file exists (`prove`); otherwise the engine
is treated as one that is not supported (the command passes with a warning). A file that cannot be loaded is a loud line in `status` and at the
start of a session; the kit goes on without it. The loader executes the very bytes it hashed (one read: no gap between the hash and the code).
"""
import hashlib
import os
import re
import sys
import time

from . import config

CONTRACT_VERSION = 1
BUILTIN_IDS = ("postgres", "sqlite")
BUILTIN_HEADS = ("psql", "dropdb", "pg_restore", "sqlite3")
ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,20}$")
PROBE_PREFIX = "shedkeeper_probe"
PROBE_TABLE = "shedkeeper_probe_t"
PROOFS = "proofs.jsonl"
REQUIRED = ("name", "size", "copy", "restore", "restored_base", "probe_state", "probe_fill", "probe_read", "probe_destroy")


class Restored(object):
    """`restore` succeeded. `to`: where it went (text for the record), `extra`: more fields of the record, `unindexed(where)`: the text to
    give when the record of the restore could not be written."""

    def __init__(self, text, to, extra=None, unindexed=None):
        self.text, self.to, self.extra = text, to, dict(extra or {})
        self.unindexed = unindexed or (lambda where: "RESTORED BUT NOT RECORDED (%s); %s" % (where, text))


class Refused(object):
    def __init__(self, text):
        self.text = text


class Engine(object):
    """The base of an adapter: defaults for what an adapter may leave out. See the module text for the contract."""
    id = ""
    title = ""
    heads = ()
    schemes = ()
    exts = ()
    tool_names = ()
    ext = ".dump"
    file_based = False
    builtin = False
    prefix = ""

    def recognize(self, words, ctx):
        return []

    def check(self, base, tools, extra):
        return ""


# ============================================================================================ what an adapter of the user may use

class RecognizeCtx(object):
    """What `recognize` gets besides the words: the folder, the variables assigned in the command, and the readers the built-in adapters
    use too (a SQL file, standard input, the SQL class of the kit, option parsing)."""

    def __init__(self, raw, env):
        from . import db
        self._db = db
        self.raw = raw
        self.cwd = raw.get("cwd") or os.getcwd()
        self.env = dict(env or {})
        self.piece = raw.get("piece", "")

    def read_file(self, word):
        """(text, why not) of a SQL file given as a literal path."""
        return self._db._read_sql_file(word, self.cwd)

    def stdin_sql(self, args):
        """(text, why not, has_stdin): what the client reads from a here-string, a heredoc, `< file` or a pipe."""
        return self._db._stdin_sql(self.raw, list(args))

    def sql_reasons(self, texts, why=()):
        """The destroying-SQL reasons ["DROP TABLE", ...] of these texts; a text that cannot be read to the end adds one more reason (the strict
        side: a copy is taken)."""
        return self._db.sql_reasons(list(texts), list(why))[0]

    def getopt(self, args, short_with_arg, long_with_arg):
        """GNU-like ([(name, value or None)], [positional])."""
        return self._db._getopt(list(args), set(short_with_arg), set(long_with_arg))


def run(cmd, env, timeout, data=None):
    """(returncode, stdout, stderr, timed_out) of a child that never reads the keyboard; killed on a timeout. `data` (bytes) goes to its stdin."""
    from . import db
    return db._run_input(cmd, env, timeout, data) if data is not None else db._run(cmd, env, timeout)


def file_sha256(path):
    from . import db
    return db._sha(path)


# ============================================================================================ the built-in adapters

class PostgresEngine(Engine):
    id, title, builtin, prefix = "postgres", "PostgreSQL", True, "pg"
    heads, schemes, ext = ("psql", "dropdb", "pg_restore"), ("postgres", "postgresql", "pgsql", "postgis"), ".pgdump"

    # recognition of psql / dropdb / pg_restore lives in db.py (_Acc): it needs the scanner of the shell (heredocs, pipes, `< file`)
    def name(self, finding, cwd, env):
        from . import db
        return db.parse_db_url(finding["target"], cwd)

    def prepare(self, settings):
        from . import db
        pgdump, ver, major = db.find_pg_dump(settings)
        if not pgdump:
            return None, ver
        return {"pgdump": pgdump, "ver": ver, "major": major}, ""

    def size(self, base, settings, tools, timeout):
        from . import db
        return db.measure_size(base, settings, tools["pgdump"], timeout)

    def check(self, base, tools, extra):
        if extra and tools["major"] and tools["major"] < extra:
            return ("%s is older than the server (PostgreSQL %d); a dump of a newer server needs a newer pg_dump -- set pg_dump_path in "
                    "shedkeeper.ini" % (tools["ver"], extra))
        return ""

    def copy(self, base, tmp, tools, left, res):
        from . import db
        return db._dump_pg(base, tools["pgdump"], tmp, left, res)

    def restore(self, rec, copy, root, settings, now, to, session):
        from . import dbcli
        return dbcli.restore_pg(rec, copy, settings, now, to)

    def restored_base(self, base, op):
        import copy as _copy
        b = _copy.copy(base)
        b.conninfo = dict(base.conninfo)
        b.dbname = op.get("new_database") or base.dbname
        b.conninfo["PGDATABASE"] = b.dbname
        return b

    # ---- the proof (prove.py): a table of known rows in a probe database
    def _psql(self, base, tools, settings, args, data=None, timeout=60):
        from . import db
        psql = db._sibling(tools["pgdump"], "psql")
        if not psql:
            return None, "psql was not found next to %s" % tools["pgdump"]
        env = db._child_env(base, timeout)
        env["PGCLIENTENCODING"] = "UTF8"
        rc, out, err, timed = run([psql, "-X", "-w", "-v", "ON_ERROR_STOP=1"] + args, env, timeout, data)
        if timed:
            return None, "psql did not finish in time"
        if rc != 0:
            return None, "psql failed (exit %s): %s" % (rc, db.mask_secrets(err.strip())[:300])
        return out, ""

    def probe_state(self, base, settings, tools):
        q = ("select (select count(*) from pg_class c join pg_namespace n on n.oid = c.relnamespace where n.nspname not in "
             "('pg_catalog','information_schema') and n.nspname not like 'pg_toast%' and n.nspname not like 'pg_temp%' and c.relkind in "
             "('r','p','v','m','S','f')) + (select count(*) from pg_namespace where nspname not in ('pg_catalog','information_schema','public') "
             "and nspname not like 'pg_%')")
        out, why = self._psql(base, tools, settings, ["-At", "-c", q])
        if out is None:
            return False, why
        try:
            n = int(out.strip())
        except ValueError:
            return False, "unexpected answer of psql: %r" % out[:60]
        return n == 0, ("the database holds %d table(s), view(s), sequence(s) or schema(s)" % n) if n else ""

    def probe_fill(self, base, settings, tools, rows):
        parts = ["create table %s (id integer primary key, txt text not null, num integer);" % PROBE_TABLE]
        for i, txt, num in rows:
            parts.append("insert into %s values (%d, '%s', %s);" % (PROBE_TABLE, i, txt.replace("'", "''"), "null" if num is None else int(num)))
        out, why = self._psql(base, tools, settings, ["-q", "-f", "-"], data="\n".join(parts).encode("utf-8"))
        return why or ""

    def probe_read(self, base, settings, tools):
        import csv
        import io
        out, why = self._psql(base, tools, settings, ["--csv", "-t", "-c",
                                                      "select id, txt, coalesce(num::text, '<NULL>') from %s order by id" % PROBE_TABLE])
        if out is None:
            return None, why
        rows = []
        # psql on Windows writes its standard output in text mode: every line break comes out as CR LF. The rows of the proof hold no CR, so the
        # break is put back to LF before the csv is read (a CR that WAS in the data would be lost here: the proof's data has none).
        for r in csv.reader(io.StringIO(out.replace("\r\n", "\n"), newline="")):
            if r:
                rows.append((int(r[0]), r[1], None if r[2] == "<NULL>" else int(r[2])))
        return rows, ""

    def probe_destroy(self, base, settings, tools):
        out, why = self._psql(base, tools, settings, ["-q", "-c", "drop table %s" % PROBE_TABLE])
        return why or ""


class SqliteEngine(Engine):
    id, title, builtin, prefix, file_based = "sqlite", "SQLite", True, "sqlite", True
    heads, schemes, exts, ext = ("sqlite3",), ("sqlite", "sqlite3", "file"), (".db", ".sqlite", ".sqlite3", ".db3"), ".sqlite"

    def name(self, finding, cwd, env):
        from . import db
        return db.parse_db_url(finding["target"], cwd)

    def prepare(self, settings):
        return {}, ""

    def size(self, base, settings, tools, timeout):
        from . import db
        return db.measure_size(base, settings, None, timeout)

    def copy(self, base, tmp, tools, left, res):
        from . import db
        return db._dump_sqlite(base, tmp, left, res)

    def restore(self, rec, copy, root, settings, now, to, session):
        from . import dbcli
        return dbcli.restore_sqlite(rec, copy, root, settings, now, to, session)

    def restored_base(self, base, op):
        from . import db
        return db.Base("sqlite", path=op.get("to") or base.path, source="restore")

    # ---- the proof
    def _con(self, base):
        import sqlite3
        return sqlite3.connect(base.path, timeout=5)

    def probe_state(self, base, settings, tools):
        import sqlite3
        if not os.path.exists(base.path):
            return True, ""
        if not os.path.isfile(base.path):
            return False, "%s is not a file" % base.path
        if os.path.getsize(base.path) == 0:
            return True, ""
        con = None
        try:
            con = self._con(base)
            n = con.execute("select count(*) from sqlite_master").fetchone()[0]
        except sqlite3.Error as e:
            return False, "not an SQLite database that can be read (%s)" % e
        finally:
            if con is not None:
                con.close()
        return n == 0, ("the file holds %d object(s)" % n) if n else ""

    def probe_fill(self, base, settings, tools, rows):
        import sqlite3
        os.makedirs(os.path.dirname(base.path) or ".", exist_ok=True)
        con = None
        try:
            con = self._con(base)
            con.execute("create table %s (id integer primary key, txt text not null, num integer)" % PROBE_TABLE)
            con.executemany("insert into %s values (?, ?, ?)" % PROBE_TABLE, rows)
            con.commit()
        except sqlite3.Error as e:
            return "%s: %s" % (e.__class__.__name__, e)
        finally:
            if con is not None:
                con.close()
        return ""

    def probe_read(self, base, settings, tools):
        import sqlite3
        con = None
        try:
            con = self._con(base)
            return [tuple(r) for r in con.execute("select id, txt, num from %s order by id" % PROBE_TABLE).fetchall()], ""
        except sqlite3.Error as e:
            return None, "%s: %s" % (e.__class__.__name__, e)
        finally:
            if con is not None:
                con.close()

    def probe_destroy(self, base, settings, tools):
        import sqlite3
        con = None
        try:
            con = self._con(base)
            con.execute("drop table %s" % PROBE_TABLE)
            con.commit()
        except sqlite3.Error as e:
            return "%s: %s" % (e.__class__.__name__, e)
        finally:
            if con is not None:
                con.close()
        return ""


BUILTIN = {"postgres": PostgresEngine(), "sqlite": SqliteEngine()}


# ============================================================================================ the adapters of the user

class Loaded(object):
    """One file of the adapters folder: what was found. `adapter` is the object when it loaded, else `error` says why not."""

    def __init__(self, path, sha, adapter=None, error=None):
        self.path = path
        self.stem = os.path.splitext(os.path.basename(path))[0].lower()
        self.sha = sha
        self.adapter = adapter
        self.error = error

    @property
    def engine_id(self):
        return self.adapter.id if self.adapter is not None else self.stem


_STATE = {"loaded": None, "dir": None, "proof_roots": None}


def adapters_dir():
    return config.adapters_dir()


def reset():
    """Forget what was loaded (the folder is read again at the next use) and the roots proofs are looked for in."""
    _STATE["loaded"] = None
    _STATE["dir"] = None
    _STATE["proof_roots"] = None


def _validate(ad):
    """None when the adapter object is usable, else why not."""
    eid = getattr(ad, "id", None)
    if not isinstance(eid, str) or not ID_RE.match(eid):
        return "`id` must be a lower-case word [a-z][a-z0-9_]{1,20}, got %r" % (eid,)
    if eid in BUILTIN_IDS:
        return "`id` %r is a built-in engine" % eid
    for attr in ("heads", "schemes", "exts", "tool_names"):
        v = getattr(ad, attr, ())
        if not isinstance(v, (tuple, list)) or not all(isinstance(x, str) and x for x in v):
            return "`%s` must be a tuple of non-empty strings" % attr
    from . import db
    taken = sorted({h.lower() for h in ad.heads if h.lower() in BUILTIN_HEADS or db.is_kit_head(h)})
    if taken:
        return ("`heads` names a command of a built-in engine or one the kit reads itself (%s): an adapter takes only a command the kit does "
                "not know" % ", ".join(taken))
    ext = getattr(ad, "ext", ".dump")
    if not isinstance(ext, str) or not re.match(r"^\.[A-Za-z0-9_]{1,12}$", ext):
        return "`ext` must look like `.dump`"
    missing = [m for m in REQUIRED if not callable(getattr(ad, m, None))]
    if missing:
        return "missing method(s): %s" % ", ".join(missing)
    return None


def _load_file(path):
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as e:
        return Loaded(path, "", error="cannot be read (%s: %s)" % (e.__class__.__name__, e))
    sha = hashlib.sha256(raw).hexdigest()
    mod = {"__name__": "shedkeeper_adapter_" + re.sub(r"\W", "_", os.path.splitext(os.path.basename(path))[0]), "__file__": path,
           "__builtins__": __builtins__}
    try:
        code = compile(raw, path, "exec")
        exec(code, mod)                       # the bytes that were hashed are the bytes that run
    except BaseException as e:               # SystemExit and a syntax error too: a broken adapter must not stop the kit
        if isinstance(e, KeyboardInterrupt):
            raise
        return Loaded(path, sha, error="failed to load (%s: %s)" % (e.__class__.__name__, str(e)[:200]))
    ad = mod.get("ADAPTER")
    if ad is None:
        return Loaded(path, sha, error="the file defines no ADAPTER")
    try:
        if isinstance(ad, type):
            ad = ad()
        why = _validate(ad)
    except BaseException as e:
        if isinstance(e, KeyboardInterrupt):
            raise
        return Loaded(path, sha, error="ADAPTER could not be made (%s: %s)" % (e.__class__.__name__, str(e)[:200]))
    if why:
        return Loaded(path, sha, error="ADAPTER is not valid: " + why)
    ad.builtin = False
    ad.sha = sha
    ad.path = path
    if not getattr(ad, "prefix", ""):
        ad.prefix = ad.id
    return Loaded(path, sha, adapter=ad)


def load(force=False):
    """[Loaded] of the files of the adapters folder (sorted by name; a name that starts with `_` is not an adapter). Cached for the life of
    the process; never raises: a folder that cannot be listed is an empty list."""
    folder = adapters_dir()
    if not force and _STATE["loaded"] is not None and _STATE["dir"] == folder:
        return _STATE["loaded"]
    out = []
    try:
        names = sorted(n for n in os.listdir(folder) if n.lower().endswith(".py") and not n.startswith("_"))
    except OSError:
        names = []
    seen = {}
    for n in names:
        item = _load_file(os.path.join(folder, n))
        if item.adapter is not None:
            ad = item.adapter
            claims = [("id", ad.id)] + [("head", h.lower()) for h in ad.heads] + [("scheme", s.lower()) for s in ad.schemes] + \
                     [("ext", e.lower()) for e in ad.exts] + [("tool name", t.lower()) for t in ad.tool_names]
            clash = next(((k, v, seen[(k, v)]) for k, v in claims if (k, v) in seen), None)
            clash = clash or next(((k, v, "a built-in engine") for k, v in claims if (k, v) in
                                   {("scheme", s) for s in PostgresEngine.schemes + SqliteEngine.schemes} |
                                   {("ext", e) for e in SqliteEngine.exts}), None)
            if clash:
                item = Loaded(item.path, item.sha, error="its %s %r is already claimed by %s" % clash)
            else:
                for c in claims:
                    seen[c] = os.path.basename(item.path)
        out.append(item)
    _STATE["loaded"], _STATE["dir"] = out, folder
    return out


def user_adapters():
    return [x.adapter for x in load() if x.adapter is not None]


def broken():
    return [x for x in load() if x.adapter is None]


def get(engine_id):
    """The adapter of an engine id (built-in or of the user), or None."""
    if engine_id in BUILTIN:
        return BUILTIN[engine_id]
    for ad in user_adapters():
        if ad.id == engine_id:
            return ad
    return None


def title(engine_id):
    ad = get(engine_id)
    return (ad.title or ad.id) if ad is not None else str(engine_id)


def for_head(head):
    """The adapter of the USER that reads this command name, or None (the built-in heads are read by db.py)."""
    h = (head or "").lower()
    for ad in user_adapters():
        if h in [x.lower() for x in ad.heads]:
            return ad
    return None


def for_scheme(scheme):
    s = (scheme or "").lower()
    for ad in user_adapters():
        if s in [x.lower() for x in ad.schemes]:
            return ad
    return None


def for_ext(ext):
    e = (ext or "").lower()
    for ad in user_adapters():
        if e in [x.lower() for x in ad.exts]:
            return ad
    return None


def for_tool_name(word):
    w = (word or "").lower()
    for ad in user_adapters():
        if w in [x.lower() for x in ad.tool_names]:
            return ad
    return None


def broken_claims(word):
    """The file names of broken adapters whose file NAME is this word (an adapter that did not load is known only by its file name)."""
    w = (word or "").lower()
    return [x for x in broken() if x.stem == w]


# ============================================================================================ proofs

def use_proofs(roots, fault=""):
    """Name the folders of the store in which a proof of an adapter is looked for from now on (the hook and `dbcopy` call this before they
    name a database). Not called -> no proof is known -> every adapter of the user counts as NOT proven (the safe default). `fault`: the folders could not
    be listed -- a text; the proofs then are UNREADABLE (a loud state), not 'none'."""
    _STATE["proof_roots"] = [r for r in dict.fromkeys(roots or []) if r]
    _STATE["proof_fault"] = fault or ""


def _proof_file(root):
    return os.path.join(root, PROOFS)


_NAMED_ENGINE = re.compile(r'"engine"\s*:\s*"([^"]*)"')
_NAMED_SHA = re.compile(r'"adapter_sha"\s*:\s*"([0-9a-f]{8,64})')


def _line_names(text):
    """(engine ids, sha256 beginnings) that a DAMAGED line of proofs.jsonl still shows: a line cut off by a crash often keeps its first fields."""
    return _NAMED_ENGINE.findall(text), _NAMED_SHA.findall(text)


def _line_concerns(ad, text):
    """Does a damaged line concern this adapter? It does when it names its engine id or the beginning of its sha256; a line that names neither may be about any."""
    ids, shas = _line_names(text)
    if not ids and not shas:
        return True
    return ad.id in ids or any(str(getattr(ad, "sha", "")).startswith(s) for s in shas)


def proofs_problem(roots=None, ad=None):
    """'' when every proofs.jsonl in these folders (default: the ones named by use_proofs) can be read whole -- or does not exist; else a text: which file and why
    (cannot be opened; holds damaged lines; the folders could not be listed). A proof that cannot be read is NOT 'no proof': the adapter that stood behind it
    was protecting, and a quiet 'not proven' would let a destroying command through with a warning. `ad` given: a damaged line counts only when it concerns
    that adapter (it names its id or sha256, or names none -- see _line_concerns): a bad line stops the adapter it names, not every adapter of the user. A file
    that cannot be opened at all, and folders that could not be listed, concern every adapter. The way out is `quarantine` (engines.quarantine_proofs)."""
    from . import store
    own = roots is None
    roots = _STATE["proof_roots"] if own else roots
    notes = []
    if own and _STATE.get("proof_fault"):
        notes.append(_STATE["proof_fault"])
    for root in roots or ():
        pf = _proof_file(root)
        try:
            damaged = store._damaged_jsonl(pf)
        except OSError as e:
            notes.append("%s cannot be read (%s: %s)" % (pf, e.__class__.__name__, e))
            continue
        for n, text in damaged:
            if ad is not None and not _line_concerns(ad, text):
                continue
            ids, shas = _line_names(text)
            notes.append("%s line %d is damaged (%s)" % (pf, n, ("it names the engine %s" % ", ".join(dict.fromkeys(ids))) if ids else
                                                       ("it names the sha256 %s..." % shas[0][:12]) if shas else "it names no adapter, so it may be about any"))
    return "; ".join(notes)


def quarantine_proofs(roots):
    """The way out of a damaged proofs.jsonl: the damaged lines go to `proofs.jsonl.bad` (nothing is deleted); every adapter of the user that a damaged line may concern
    gets a line `prove_failed` (step `quarantine`), because the line that was lost may have been the one that took its proof away -- it must be proved and activated
    again. -> [text lines]."""
    from . import store
    out = []
    for root in roots or ():
        pf = _proof_file(root)
        try:
            damaged = store._damaged_jsonl(pf)
        except OSError as e:
            out.append("%s cannot be read (%s: %s) -- left as it is" % (pf, e.__class__.__name__, e))
            continue
        if not damaged:
            continue
        adapters = [x.adapter for x in load() if x.adapter is not None]
        hit = [ad for ad in adapters if any(_line_concerns(ad, text) for _n, text in damaged)]
        now = time.strftime("%Y-%m-%dT%H:%M:%S")

        def extra(_recs, hit=hit, now=now):
            return [{"event": "prove_failed", "engine": ad.id, "adapter_sha": getattr(ad, "sha", ""), "contract": CONTRACT_VERSION, "time": now,
                     "step": "quarantine", "why": "a damaged line of proofs.jsonl was moved to proofs.jsonl.bad; prove and activate again"} for ad in hit]
        try:
            n = store.quarantine_jsonl(pf, damaged, extra)
        except Exception as e:
            out.append("%s could not be repaired (%s: %s) -- left as it is" % (pf, e.__class__.__name__, e))
            continue
        out.append("%s: %d damaged line(s) moved to %s.bad; %d adapter(s) must be proved and activated again%s" % (
            pf, len(damaged), PROOFS, n, (": " + ", ".join(ad.id for ad in hit)) if hit else ""))
    return out


def _records_for(ad, roots, events):
    """[((time, root index, line number), record)] of the records of these events for THIS adapter file (id, sha256, contract version), oldest first."""
    from . import store
    roots = _STATE["proof_roots"] if roots is None else roots
    out = []
    for i, root in enumerate(roots or ()):
        try:
            recs, _bad = store._read_jsonl(_proof_file(root))
        except OSError:
            continue
        for n, r in recs:
            if (r.get("event") in events and r.get("engine") == ad.id and r.get("adapter_sha") == getattr(ad, "sha", None)
                    and r.get("contract") == CONTRACT_VERSION):
                out.append(((r.get("time", ""), i, n), r))
    out.sort(key=lambda x: x[0])
    return out


def proof_state(ad, roots=None):
    """(state, record) of an adapter of the user for its CURRENT bytes: 'none' (no proof), 'failed' (the newest word is a failed run: it took an
    earlier proof away), 'proved' (the proof passed, the PERSON has not confirmed it: it does NOT protect), 'active' (proved and confirmed by the
    person with a code for these bytes), 'unreadable' (the file of proofs cannot be read whole -- see proofs_problem: nothing is known, and it is not 'none'). A `proved` after an activation keeps it (the person confirmed THESE bytes); a failed run takes it away."""
    if proofs_problem(roots, ad):
        return "unreadable", None
    state, rec = "none", None
    for _k, r in _records_for(ad, roots, ("proved", "prove_failed", "activated")):
        ev = r["event"]
        if ev == "prove_failed":
            state, rec = "failed", r
        elif ev == "proved":
            state, rec = ("active" if state == "active" else "proved"), r
        elif ev == "activated" and state in ("proved", "active"):
            state = "active"
    return state, rec


def proof_for(ad, roots=None):
    """The newest proof record of this adapter file (engine, sha256, contract version), or None."""
    from . import store
    roots = _STATE["proof_roots"] if roots is None else roots
    best, best_key = None, None
    for i, root in enumerate(roots or ()):
        try:
            recs, _bad = store._read_jsonl(_proof_file(root))
        except OSError:
            continue
        for n, r in recs:
            if (r.get("event") in ("proved", "prove_failed") and r.get("engine") == ad.id and r.get("adapter_sha") == getattr(ad, "sha", None)
                    and r.get("contract") == CONTRACT_VERSION):
                key = (r.get("time", ""), i, n)
                if best_key is None or key > best_key:         # the newest word wins: a failed run takes an earlier proof away
                    best, best_key = r, key
    return best if best is not None and best.get("event") == "proved" else None


def is_proven(ad, roots=None):
    """Built-in adapters: yes (the tests of the kit run the same proof). An adapter of the user: only with a proof for its CURRENT sha256 that the
    PERSON confirmed with a code (`activate`): the agent may build and prove an adapter, only the person makes it count as protection."""
    if getattr(ad, "builtin", False):
        return True
    return proof_state(ad, roots)[0] == "active"


def write_activation(root, ad, rec):
    """The person confirmed the proof for the CURRENT bytes of the adapter file: from now on it protects."""
    from . import store
    full = dict(rec, event="activated", engine=ad.id, adapter_sha=getattr(ad, "sha", "builtin"), contract=CONTRACT_VERSION,
                time=time.strftime("%Y-%m-%dT%H:%M:%S"))
    store._append_jsonl(_proof_file(root), lambda n: full)
    return full


def write_proof(root, ad, rec):
    from . import store
    full = dict(rec, event="proved", engine=ad.id, adapter_sha=getattr(ad, "sha", "builtin"), contract=CONTRACT_VERSION,
                time=time.strftime("%Y-%m-%dT%H:%M:%S"))
    store._append_jsonl(_proof_file(root), lambda n: full)
    return full


def _unproven_reason(ad):
    k = os.path.join(config.KIT_DIR, "shedkeeper.py").replace("\\", "/")
    if proof_state(ad)[0] == "proved":
        return ("the adapter %s is proved but NOT ACTIVATED: only the person makes a proof count. Ask the user to type the code `prove` printed "
                "(CONFIRM SK-nnnnnn, in their own message), then run `python %s activate %s`" % (os.path.basename(ad.path), k, ad.id))
    return ("the adapter %s is installed but NOT proven on this machine (no proof for the current sha256 of its file): run `python %s "
            "prove %s --target <a probe database>`, then the person confirms it with `activate`" % (os.path.basename(ad.path), k, ad.id))


def name_for(engine_id, finding, cwd, env):
    """The database an adapter names -> Base | Unnamed | Unsupported. An adapter that is not proven names nothing (Unsupported: the command
    passes with a warning); a fault inside a proven adapter is an Unnamed (the hook refuses: the strict side)."""
    from . import db
    ad = get(engine_id)
    if ad is None:
        broke = broken_claims(engine_id)
        return db.Unsupported(engine_id, "the adapter file %s failed to load: %s" % (os.path.basename(broke[0].path), broke[0].error)
                              if broke else "")
    if not getattr(ad, "builtin", False) and proofs_problem(ad=ad):
        return db.Unnamed("the proofs of the adapters cannot be read (%s), so it is not known whether the adapter %s protects: the command is refused until that is "
                          "repaired -- `python %s quarantine` moves the damaged lines away (nothing is deleted), then `prove` and `activate` again; `status` shows the state" % (
                              proofs_problem(ad=ad), ad.id, os.path.join(config.KIT_DIR, "shedkeeper.py").replace("\\", "/")))
    if not is_proven(ad):
        u = db.Unsupported(ad.id, _unproven_reason(ad))
        u.adapter = ad.id
        return u
    try:
        b = ad.name(finding, cwd, env)
    except Exception as e:
        return db.Unnamed("the adapter %s could not name the database (%s: %s)" % (ad.id, e.__class__.__name__, db.mask_secrets(str(e))[:160]))
    if isinstance(b, db.Base):
        if b.engine != ad.id:
            return db.Unnamed("the adapter %s returned a database of the engine %r" % (ad.id, b.engine))
        return b
    if isinstance(b, db.Unnamed):
        return b
    return db.Unnamed("the adapter %s returned %r, neither a database nor a reason" % (ad.id, type(b).__name__))


def status_lines(settings=None, roots=None):
    """The lines of `status` about the engines: the built-in ones, every adapter of the user (proven or not), every file that did not load."""
    lines = ["engines: postgres, sqlite (built in; the tests of the kit run the same proof `prove` runs)"]
    for x in load():
        if x.adapter is None:
            lines.append("ADAPTER BROKEN: %s -- %s. The engine %r is treated as NOT supported (commands pass with a warning); fix the file or "
                         "move it away." % (x.path, x.error, x.stem))
            continue
        ad = x.adapter
        state, pr = proof_state(ad, roots)
        if state == "unreadable":
            lines.append("adapter %s (%s, sha256 %s): PROOFS CANNOT BE READ -- %s. Whether it protects is NOT known, so a command of its engine is REFUSED until the "
                         "file is repaired: `python %s quarantine` moves the damaged lines away (nothing is deleted), then `prove` and `activate` again" % (
                             ad.id, os.path.basename(x.path), x.sha[:12], proofs_problem(roots, ad), os.path.join(config.KIT_DIR, "shedkeeper.py").replace("\\", "/")))
        elif state == "active":
            lines.append("adapter %s (%s, sha256 %s): PROVED %s on %s and ACTIVATED by the person" % (
                ad.id, os.path.basename(x.path), x.sha[:12], pr.get("time", "?") if pr else "?", pr.get("probe", "a probe") if pr else "a probe"))
        elif state == "proved":
            lines.append("adapter %s (%s, sha256 %s): PROVED %s but NOT ACTIVATED -- it does not protect until the person confirms it (the code `prove` "
                         "printed, typed in their own message); then `python %s activate %s`" % (
                             ad.id, os.path.basename(x.path), x.sha[:12], pr.get("time", "?") if pr else "?",
                             os.path.join(config.KIT_DIR, "shedkeeper.py").replace("\\", "/"), ad.id))
        else:
            lines.append("adapter %s (%s, sha256 %s): NOT PROVED -- treated as an engine that is not supported (commands pass with a warning); run "
                         "`python %s prove %s --target <a probe database>`" % (ad.id, os.path.basename(x.path), x.sha[:12],
                                                                              os.path.join(config.KIT_DIR, "shedkeeper.py").replace("\\", "/"), ad.id))
    return lines


def session_notes(roots=None):
    """One sentence for the start of a session about adapters that failed to load, and about proofs that cannot be read (empty when none): protection is quietly
    lower. `roots`: the folders of the store to look for the proofs in (the start of a session names them; None: the ones named by use_proofs)."""
    parts = ["the adapter %s failed to load (%s): engine %s is treated as NOT supported" % (os.path.basename(x.path), x.error, x.stem) for x in broken()]
    k = os.path.join(config.KIT_DIR, "shedkeeper.py").replace("\\", "/")
    try:
        if load():
            prob = proofs_problem(roots)
            if prob:
                hit = [x.adapter.id for x in load() if x.adapter is not None and proofs_problem(roots, x.adapter)]
                parts.append(("the proofs of the adapters %s cannot be read (%s): whether they protect is not known, and a command of such an engine is refused until "
                              "`python %s quarantine` has moved the damaged lines away (nothing is deleted); then `prove` and `activate` again" % (", ".join(hit), prob, k))
                             if hit else ("a damaged line of proofs.jsonl (%s) names no adapter that is installed now: nothing is stopped by it; `python %s quarantine` moves it away" % (prob, k)))
    except Exception as e:      # the check itself failed: that is said, not skipped -- whether the proofs hold is not known
        parts.append("proofs: could not be checked (%s: %s)" % (e.__class__.__name__, str(e)[:200]))
    return "; ".join(parts)
