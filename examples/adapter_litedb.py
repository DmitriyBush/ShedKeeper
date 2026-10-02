r"""A WORKED, TESTED example of an engine adapter: "litedb", a made-up engine that is one FILE (extension `.litedb`) and, inside, SQLite --
so everything here runs for real with the standard library, and the whole contract can be seen working.

How to use it:  copy this file into the adapters folder (`python shedkeeper.py status` prints its path), then

    python shedkeeper.py prove litedb --target <folder>/shedkeeper_probe_1.litedb        (or no --target: a probe file is made)

Until `prove` has passed for the CURRENT bytes of the file, the engine counts as "not supported": a command that destroys data in it passes
with a warning. Change one byte of the file and the proof is gone (it is bound to the sha256).

An adapter for a real engine (DuckDB, MySQL ...) has the same shape: replace the SQLite calls by the engine's own tools. The contract and what each
method must do and return are in ARCHITECTURE.md, "Adding an engine"; the comments below name the step each method is.
"""
import os
import pathlib
import shutil
import sqlite3

from shedkeeper import db, engines


class LiteDb(engines.Engine):
    id = "litedb"                       # lower case; written in every record of a copy
    title = "LiteDB"                    # for a person
    heads = ("litedb",)                 # the command this adapter reads:  litedb FILE "SQL"
    schemes = ("litedb",)               # an address litedb://<file> names this engine
    exts = (".litedb",)                 # a file with this extension is of this engine (SHEDKEEPER_DB=app.litedb)
    tool_names = ("litedb",)            # the word a project tool (Rails, Prisma ...) would use for it
    ext = ".litecopy"                   # the extension of a copy
    file_based = True                   # the database is a file: Base.path is set, the copy goes on the volume of the file

    # ---- 1 recognise: the destroying actions of ONE command of the shell
    def recognize(self, words, ctx):
        args = words[1:]
        pos = [a for a in args if not a.startswith("-")]
        if not pos:
            return []
        dbfile, sqls = pos[0], pos[1:]
        texts, why = list(sqls), []
        if not sqls:                                    # `litedb FILE < script.sql`, a heredoc, a pipe
            text, problem, has = ctx.stdin_sql(args)
            if problem:
                why.append(problem)
            elif has:
                texts.append(text)
        reasons = ctx.sql_reasons(texts, why)           # the kit's own SQL reader; an unreadable text is a reason too (a copy is taken)
        return [{"reasons": reasons, "target": dbfile, "unnamed": None}] if reasons else []

    # ---- 2 name: which database. Never guess; a password only in Base.conninfo / Base.secrets (a file engine has none)
    def name(self, finding, cwd, env):
        spec = (finding.get("target") or (finding.get("parts") or {}).get("database") or "").strip().strip("\"'")
        if spec.lower().startswith("litedb://"):
            spec = spec[len("litedb://"):]
        if not spec:
            return db.Unnamed("no file is named")
        if "://" in spec:
            return db.Unnamed("%r is an address of another engine, not a litedb file" % db.mask_secrets(spec)[:80])
        if any(c in spec for c in "$%`*?{}"):
            return db.Unnamed("the file name %r comes from a variable or a mask" % spec)
        path = os.path.abspath(spec if os.path.isabs(spec) else os.path.join(cwd or os.getcwd(), spec))
        return db.Base("litedb", path=path, source=finding.get("source", ""), ident="litedb:" + os.path.normcase(path).replace("\\", "/"),
                       label=os.path.basename(path), shown=path.replace("\\", "/"))

    # ---- 3 prepare / 4 size
    def prepare(self, settings):
        return {}, ""                                   # nothing to find: the standard library is enough

    def size(self, base, settings, tools, timeout):
        if not os.path.exists(base.path):
            return "absent", 0, 0                       # the database is not there: nothing to copy
        return "ok", os.path.getsize(base.path), 0

    # ---- 5 copy, then READ IT BACK in full
    def copy(self, base, tmp, tools, left, res):
        src = dst = None
        try:
            src = sqlite3.connect(pathlib.Path(base.path).as_uri() + "?mode=ro", uri=True, timeout=5)
            dst = sqlite3.connect(tmp)
            src.backup(dst)                             # the backup API sees the write-ahead log; a plain file copy would lose it
            dst.commit()
            row = dst.execute("PRAGMA integrity_check").fetchall()      # a full read of the copy, not a look at its head
        except sqlite3.Error as e:
            res["msg"] = "the backup failed (%s: %s)" % (e.__class__.__name__, e)
            return False, ""
        finally:
            for c in (dst, src):
                if c is not None:
                    c.close()
        if row != [("ok",)]:
            res["msg"] = "integrity_check of the copy: %s" % (row[:3],)
            return False, ""
        return True, "PRAGMA integrity_check = ok"

    # ---- 7 restore into a NEW place, never over what is there
    def restore(self, rec, copy, root, settings, now, to, session):
        old = rec.get("url") or ""
        target = to or ("%s.restored_%s%s" % (os.path.splitext(old)[0], now.strftime("%Y%m%d_%H%M%S"), self.exts[0]))
        if not target or os.path.lexists(target):
            return engines.Refused("REFUSED %s exists or is not named; nothing was touched" % (target or "(no place)"))
        part = target + ".part"
        try:
            shutil.copyfile(copy, part)
            con = sqlite3.connect(part)
            try:
                ok = con.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            finally:
                con.close()
            if not ok:
                return engines.Refused("REFUSED the restored file does not pass integrity_check; nothing was put in place")
            os.rename(part, target)                     # NOT os.replace: a file that appeared since the check above must not be overwritten (on Windows rename refuses)
        except FileExistsError:
            return engines.Refused("REFUSED the place %s was taken while the copy was being written; nothing was overwritten, the verified copy is at %s" % (target, part))
        except (OSError, sqlite3.Error) as e:
            return engines.Refused("REFUSED the copy could not be put back (%s: %s)" % (e.__class__.__name__, e))
        return engines.Restored("OK restored the copy %s -> %s" % (copy, target), target, {"new_path": target})

    # ---- for `prove`: where did the restore put it, and four small operations on a probe
    def restored_base(self, base, op):
        return self.name({"target": op.get("to") or ""}, "", {})

    def _con(self, base):
        return sqlite3.connect(base.path, timeout=5)

    def probe_state(self, base, settings, tools):
        if not os.path.exists(base.path) or os.path.getsize(base.path) == 0:
            return True, ""
        con = self._con(base)
        try:
            n = con.execute("select count(*) from sqlite_master").fetchone()[0]
        except sqlite3.Error as e:
            return False, "cannot be read (%s)" % e
        finally:
            con.close()
        return n == 0, ("it holds %d object(s)" % n) if n else ""

    def probe_fill(self, base, settings, tools, rows):
        os.makedirs(os.path.dirname(base.path) or ".", exist_ok=True)
        con = self._con(base)
        try:
            con.execute("create table %s (id integer primary key, txt text not null, num integer)" % engines.PROBE_TABLE)
            con.executemany("insert into %s values (?, ?, ?)" % engines.PROBE_TABLE, rows)
            con.commit()
        except sqlite3.Error as e:
            return str(e)
        finally:
            con.close()
        return ""                                       # '' = done; a text = why not

    def probe_read(self, base, settings, tools):
        con = self._con(base)
        try:
            return [tuple(r) for r in con.execute("select id, txt, num from %s order by id" % engines.PROBE_TABLE)], ""
        except sqlite3.Error as e:
            return None, str(e)                         # None = it cannot be read (after the destroy step: the table is gone)
        finally:
            con.close()

    def probe_destroy(self, base, settings, tools):
        con = self._con(base)
        try:
            con.execute("drop table %s" % engines.PROBE_TABLE)
            con.commit()
        except sqlite3.Error as e:
            return str(e)
        finally:
            con.close()
        return ""


ADAPTER = LiteDb()
