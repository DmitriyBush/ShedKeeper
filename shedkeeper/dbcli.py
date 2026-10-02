r"""dbcli.py -- what `shedkeeper.py` does with databases (0.3.0).

  dbcopy(spec, mcp_server, cwd, settings, session)      a verified copy NOW, outside the limits of the hook -> (exit code, text)
  find_copies(text, settings, days, volume)             the copies of databases that fit a text -> (hits, problems), the shape of store.find
  restore_copy(id, settings, to, session, volume, now)  bring a copy back -> (status, text); status as of store.restore
  status_lines(settings, cwd)                           the section "databases" of `status`

A copy is listed in `dbcopies.jsonl` of the personal folder (db.copy_base); its id is `d:<day>:<n>`.
A PostgreSQL copy is brought back ONLY into a NEW database `<name>_restored_<YYYYMMDD_HHMMSS>`: the database that was there is
never touched, and the new one is never dropped by this tool even when the restore reports errors. A SQLite copy is brought back
as a file: the file that is there now (and its -wal / -shm / -journal, which belong to it) goes to the trash of 0.2.0 first.
No password is ever printed: an address is shown with `***`, a password comes from pgpass or from PGPASSWORD of the caller.
"""
import os
import re
import shutil
import sqlite3
import time
from datetime import datetime

from . import db, engines


def _slash(s):
    return str(s).replace("\\", "/").lower()


def _store():
    from . import store
    return store


def _root_for(base, cwd, settings):
    return _store().store_root(getattr(base, "path", "") or cwd, settings)


# ============================================================================================ dbcopy

def dbcopy(spec, mcp_server, cwd, settings, session=""):
    """-> (exit code, text). 0 done and listed; 1 refused / no copy; 3 copy made but NOT listed (the text says where it is)."""
    store = _store()
    use_proofs(cwd, settings)
    base = db.target_from_spec(spec, cwd)
    if isinstance(base, db.Unsupported) and base.adapter:
        return 1, "REFUSED %s" % base.reason
    if isinstance(base, db.Unsupported):
        return 1, ("REFUSED %s -- this version copies PostgreSQL and SQLite only (and an engine whose adapter is proven here). Take the "
                   "copy with the tool of that engine, or build an adapter: README, \"Adding another database engine\" (it points to "
                   "ARCHITECTURE.md)." % base.reason)
    if isinstance(base, db.Unnamed):
        return 1, "REFUSED the database cannot be named from %r: %s" % (db.mask_secrets(spec)[:120], base.reason)
    if base.linked:
        return 1, "REFUSED a hosted project has no address to copy from: give the address of the database"
    root = _root_for(base, cwd, settings)
    ok, note, rep = store.ensure_store_report(root, settings)
    if not ok:
        return 1, "REFUSED the store is not usable, so no copy can be made: %s" % note
    res = db.copy_base(base, root, settings, None, enforce_limits=False, session=session, via="dbcopy",
                       command="dbcopy " + db.mask_secrets(spec), mcp_server=mcp_server or "")
    head = store.creation_report(rep) if rep else ""
    st = res["status"]
    if st == "absent":
        return 1, "%sNOTHING COPIED: %s (%s). No record was written." % ((head + "\n") if head else "", res["msg"], base.url_masked)
    if st != "ok":
        return 1, "%sREFUSED no copy of %s: %s" % ((head + "\n") if head else "", base.url_masked, res["msg"] or st)
    lines = ["OK copy %s of %s: the database is %s, the copy %s%s" % (
        res.get("id") or "(NOT LISTED)", base.url_masked, db._mb(res["size"]), res["path"],
        (" for the MCP server %s" % mcp_server) if mcp_server else ""),
        "verified: %s; sha256 %s" % (_verified_text(base, res), res["sha256"][:16] + "...")]
    if head:
        lines.insert(0, head)
    if not res.get("indexed"):
        lines.append("NOT LISTED in dbcopies.jsonl (%s): the hook will not know this copy -- the file is at %s" % (res["msg"], res["path"]))
        return 3, "\n".join(lines)
    lines.append("A command that destroys this database, given within %d minutes, passes with no new copy." % settings["db_fresh_minutes"])
    return 0, "\n".join(lines)


def use_proofs(cwd, settings):
    """Name the folders in which the proofs of adapters of the user are looked for (before a database is named)."""
    if not engines.load():
        return
    store = _store()
    try:
        engines.use_proofs([store.store_root(cwd, settings)] + list(store.all_roots(settings)))
    except Exception as e:
        engines.use_proofs([], fault="the folders of the store could not be listed (%s: %s)" % (e.__class__.__name__, db.mask_secrets(str(e))[:120]))


def _verified_text(base, res=None):
    if base.engine == "sqlite":
        return "PRAGMA integrity_check = ok"
    if base.engine == "postgres":
        return "pg_restore read the whole dump back: exit 0, no messages"
    return (res or {}).get("verified") or "(the adapter did not say)"


# ============================================================================================ find

def find_copies(text, settings, days=None, volume=None):
    """The copies of databases whose engine, identity, name, address, command or file contain `text` (case and slashes ignored).
    -> (hits, problems); a hit has the keys of store.find (kind 'dbcopy')."""
    store = _store()
    needle = _slash(text or "")
    cutoff = None if days is None else datetime.now().date().toordinal() - days
    hits, problems = [], []
    for root in store.all_roots(settings):
        if volume and os.path.splitdrive(root)[0][:1].upper() != volume[:1].upper() and not store.env_root():
            continue
        path = db.index_path(root)
        if not os.path.isfile(path):
            continue
        try:
            recs, bad = store._read_jsonl(path)
        except OSError as e:
            problems.append("cannot read %s: %s" % (path, e))
            continue
        if bad:
            problems.append("%d unreadable line(s) in %s" % (bad, path))
        restored = {r.get("of"): r for _n, r in recs if r.get("op") == "restore"}
        for n, rec in recs:
            if rec.get("op"):
                continue
            rid = rec.get("id") or "d:?:%d" % n
            try:
                if cutoff is not None and datetime.fromisoformat(rec.get("time", "")).date().toordinal() < cutoff:
                    continue
            except ValueError:
                pass
            blob = " ".join(_slash(rec.get(k, "")) for k in ("engine", "identity", "name", "url", "copy", "command", "mcp_server"))
            if needle in blob:
                hits.append({"kind": "dbcopy", "id": rid, "time": rec.get("time", ""), "src": rec.get("url", ""),
                             "where": rec.get("copy", ""), "present": bool(rec.get("copy")) and os.path.isfile(rec["copy"]),
                             "restored": restored.get(rid),
                             "reason": "copy of a %s database, %s%s" % (rec.get("engine", "?"), db._mb(rec.get("size") or 0),
                                                                        (", MCP server " + rec["mcp_server"]) if rec.get("mcp_server") else ""),
                             "session": rec.get("session", ""), "via": rec.get("via", ""), "root": root})
    return hits, problems


# ============================================================================================ restore

def restore_copy(item_id, settings, to=None, session="", volume=None, now=None):
    """-> (status, text): 'ok' | 'refused' | 'unindexed' | 'blind' (as store.restore)."""
    store = _store()
    now = now or datetime.now()
    found = []
    for root in store.all_roots(settings):
        if volume and os.path.splitdrive(root)[0][:1].upper() != volume[:1].upper() and not store.env_root():
            continue
        path = db.index_path(root)
        if not os.path.isfile(path):
            continue
        try:
            recs, _bad = store._read_jsonl(path)
        except OSError as e:
            return "refused", "REFUSED cannot read %s: %s" % (path, e)
        for _n, r in recs:
            if r.get("id") == item_id and not r.get("op"):
                found.append((root, path, r, recs))
    if not found:
        msg = "REFUSED no readable record %s in any store" % item_id
        lost = store.unsearched_lines(settings)
        return ("blind", msg + "; " + "; ".join(lost)) if lost else ("refused", msg)
    if len(found) > 1:
        return "refused", "REFUSED the id %s exists in several stores (%s) -- name the volume: --volume X:" % (item_id, ", ".join(f[0] for f in found))
    root, idx, rec, recs = found[0]
    copy = rec.get("copy") or ""
    cdir = os.path.join(root, "dbcopies")
    if not copy or not store._within(copy, cdir) or not os.path.isfile(copy) or store._is_link(copy):
        return "refused", "REFUSED the copy is not in the store (cleaned by a person, or the record is damaged): %s" % copy
    try:
        if rec.get("sha256") and db._sha(copy) != rec["sha256"]:
            return "refused", "REFUSED the copy no longer matches its recorded sha256: %s" % copy
    except OSError as e:
        return "refused", "REFUSED cannot read the copy (%s): %s" % (e, copy)
    ad = engines.get(rec.get("engine"))
    if ad is None:
        return "refused", "REFUSED the engine %r of this record is not one this version restores (no adapter for it is loaded)" % rec.get("engine")
    try:
        res = ad.restore(rec, copy, root, settings, now, to, session)
    except Exception as e:             # a fault of an adapter is a refusal: nothing is claimed that was not done
        return "refused", "REFUSED the adapter %s failed while restoring (%s: %s)" % (ad.id, e.__class__.__name__, db.mask_secrets(str(e))[:200])
    if isinstance(res, engines.Refused):
        return "refused", res.text
    if not isinstance(res, engines.Restored):
        return "refused", "REFUSED the adapter %s returned %r, neither Restored nor Refused" % (ad.id, type(res).__name__)
    okrec, where = _record(idx, root, item_id, now, res.to, session, res.extra)
    status, msg = ("ok", res.text) if okrec else ("unindexed", res.unindexed(where))
    if status in ("ok", "unindexed"):
        lost = store.unsearched_lines(settings)
        if lost:
            msg += " | " + "; ".join(lost)
    return status, msg


def _record(idx, root, item_id, now, to, session, extra):
    store = _store()
    orphans = os.path.join(root, "dbcopies_orphans.jsonl")

    def make(k):
        return dict({"op": "restore", "of": item_id, "time": now.isoformat(timespec="seconds"), "to": to, "session": session or ""}, **extra)
    return store._record_restore(idx, orphans, make)


def restore_sqlite(rec, copy, root, settings, now, to, session):
    """The restore of the built-in SQLite adapter -> engines.Restored | engines.Refused."""
    item_id = rec.get("id")
    return _restore_sqlite(item_id, rec, copy, root, None, to, settings, session, now)


def _restore_sqlite(item_id, rec, copy, root, idx, to, settings, session, now):
    store = _store()
    target = os.path.abspath(to) if to else os.path.abspath(rec.get("url") or "")
    if not rec.get("url") and not to:
        return engines.Refused("REFUSED the record holds no path of the database: give --to PATH")
    if not store.DRIVE_RE.match(os.path.splitdrive(target)[0]):
        return engines.Refused("REFUSED the target is not on a drive letter: %s" % target)
    if store._within(target, os.path.dirname(root)):
        return engines.Refused("REFUSED the target is inside the store: %s" % target)
    anc = os.path.dirname(target)
    while not store._lexists(anc) and os.path.dirname(anc) != anc:
        anc = os.path.dirname(anc)
    if not os.path.isdir(anc):
        return engines.Refused("REFUSED a parent of the target is not a folder: %s" % anc)
    if store._lexists(target) and (store._is_link(target) or not os.path.isfile(target)):
        return engines.Refused("REFUSED the target is a folder or a link, not a file: %s" % target)
    part = "%s.shedkeeper-restore-%d.part" % (target, os.getpid())
    if store._lexists(part):
        return engines.Refused("REFUSED temporary name taken: %s" % part)
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy2(copy, part)
    except Exception as e:
        db._drop_own(part)
        return engines.Refused("REFUSED the copy could not be written (%s: %s)" % (e.__class__.__name__, e))
    try:
        ok = db._sha(part) == rec.get("sha256")
        why = "sha256 differs from the copy"
        if ok:
            con = sqlite3.connect("file:%s?mode=ro" % part.replace("\\", "/").replace("?", "%3f").replace("#", "%23"), uri=True)
            try:
                row = con.execute("PRAGMA integrity_check").fetchall()
            finally:
                con.close()
            ok, why = row == [("ok",)], "PRAGMA integrity_check of the restored file: %s" % (row[:2],)
    except (OSError, sqlite3.Error) as e:
        ok, why = False, "%s: %s" % (e.__class__.__name__, e)
    if not ok:
        db._drop_own(part)
        return engines.Refused("REFUSED the restored file does not match the copy (%s); nothing replaced" % why)
    displaced = []                       # [(original name, place in the trash)]
    for suffix in ("", "-wal", "-shm", "-journal"):
        p = target + suffix
        if not store._lexists(p):
            continue
        st, msg, info = store.trash(p, "replaced by restore of %s" % item_id, settings, session, now)
        if st == "refused":
            back = _put_back(displaced)
            db._drop_own(part)
            return engines.Refused("REFUSED the current file %s could not be moved to the trash (%s); nothing replaced%s" % (p, msg, back))
        displaced.append((p, info.get("dst")))
    try:
        os.rename(part, target)
    except OSError as e:
        back = _put_back(displaced)
        return engines.Refused("REFUSED the final rename failed (%s: %s)%s; the verified copy is at %s" % (e.__class__.__name__, e, back, part))
    note = ("; the file that was there is in the trash: %s" % ", ".join(d for _p, d in displaced if d)) if displaced else ""
    return engines.Restored("OK restored the copy %s -> %s%s" % (copy, target, note), target, {"displaced": [d for _p, d in displaced]},
                            lambda where: "RESTORED BUT NOT RECORDED (%s); the file is at %s%s" % (where, target, note))


def _put_back(displaced):
    if not displaced:
        return ""
    bad = []
    for p, dst in displaced:
        try:
            os.rename(dst, p)
        except OSError as e:
            bad.append("%s (%s)" % (dst, e.__class__.__name__))
    return "; what was moved to the trash was put back" if not bad else "; could NOT put back: " + ", ".join(bad)


PG_MAINT = ("postgres", "template1")


def _pg_header(restore_bin, copy, env):
    """The CREATE DATABASE line stored in the archive -> dict of encoding / locale, or {} (not found)."""
    rc, out, _err, _t = db._run([restore_bin, "-s", "-C", "-f", "-", copy], env, 60)
    m = re.search(r"(?m)^CREATE DATABASE\s+(?:\"[^\"]+\"|\S+)\s+WITH\s+([^;]*);", out or "")
    if rc != 0 or not m:
        return {}
    opts = dict((k.upper(), v) for k, v in re.findall(r"(\w+)\s*=\s*('(?:[^']|'')*'|\w+)", m.group(1)))
    return {k: v.strip("'") for k, v in opts.items()}


def _safe_locale(v):
    return bool(v) and re.fullmatch(r"[A-Za-z0-9_.@-]+", v) is not None


def restore_pg(rec, copy, settings, now, to):
    """The restore of the built-in PostgreSQL adapter -> engines.Restored | engines.Refused."""
    if to:
        return engines.Refused("REFUSED --to is not used for PostgreSQL: a copy always comes back into a NEW database, whose name is made by the tool")
    return _restore_pg(rec.get("id"), rec, copy, None, None, settings, "", now)


def _restore_pg(item_id, rec, copy, root, idx, settings, session, now):
    pgdump, ver, major = db.find_pg_dump(settings)
    if not pgdump:
        return engines.Refused("REFUSED pg_dump was not found, so pg_restore, createdb and psql (the tools of the same folder) were not either: %s" % ver)
    tools = {n: db._sibling(pgdump, n) for n in ("pg_restore", "createdb", "psql")}
    missing = [n for n, p in tools.items() if not p]
    if missing:
        return engines.Refused("REFUSED not found next to %s: %s" % (pgdump, ", ".join(missing)))
    base = db.parse_db_url(rec.get("url") or "", os.getcwd())
    if not isinstance(base, db.Base) or base.engine != "postgres" or base.linked:
        return engines.Refused("REFUSED the address of the record cannot be read (%s)" % getattr(base, "reason", "not a PostgreSQL address"))
    env = db._child_env(base, 10)
    if os.environ.get("PGPASSWORD"):
        env["PGPASSWORD"] = os.environ["PGPASSWORD"]            # the caller's own: pgpass is read by the tools themselves
    suffix = "_restored_" + now.strftime("%Y%m%d_%H%M%S")
    stem = base.dbname
    while len((stem + suffix).encode("utf-8")) > 63 and len(stem) > 1:     # PostgreSQL cuts a name at 63 BYTES: cut ours in front of the stamp
        stem = stem[:-1]
    newname = stem + suffix
    lit = newname.replace("'", "''")
    maint_ok, exists = None, None
    for maint in PG_MAINT:
        e2 = dict(env, PGDATABASE=maint)
        rc, out, err, _t = db._run([tools["psql"], "-X", "-w", "-At", "-c", "select count(*) from pg_database where datname = '%s'" % lit], e2, 20)
        if rc == 0:
            maint_ok, exists = maint, out.strip()
            break
        last = db.mask_secrets(err.strip())[:300]
    if maint_ok is None:
        return engines.Refused("REFUSED the server could not be asked whether %s exists (%s)" % (newname, last))
    if exists != "0":
        return engines.Refused("REFUSED a database named %s already exists; nothing was touched" % newname)
    hdr = _pg_header(tools["pg_restore"], copy, env)
    args = [tools["createdb"], "-w", "-T", "template0", "--maintenance-db=" + maint_ok]
    notes = []
    if hdr.get("ENCODING"):
        if _safe_locale(hdr["ENCODING"]):
            args += ["-E", hdr["ENCODING"]]
    else:
        notes.append("the encoding of the original was not found in the archive: the server default is used")
    for key, flag in (("LC_COLLATE", "--lc-collate"), ("LC_CTYPE", "--lc-ctype"), ("LOCALE_PROVIDER", "--locale-provider"),
                      ("LOCALE", "--locale"), ("ICU_LOCALE", "--icu-locale")):
        if _safe_locale(hdr.get(key)):
            args += [flag, hdr[key]]
    args += ["--", newname]               # a name that starts with `-` is a name, not an option
    e3 = dict(env)
    e3.pop("PGDATABASE", None)
    rc, out, err, timed = db._run(args, e3, 60)
    if rc != 0:
        return engines.Refused("REFUSED the new database %s could not be created (%s); nothing was restored" % (
            newname, db.mask_secrets(err.strip())[:300] or ("timeout" if timed else "exit %s" % rc)))
    e4 = dict(env, PGDATABASE=newname)
    rc, out, err, timed = db._run([tools["pg_restore"], "-w", "-d", newname, copy], e4, 3600)
    where = "%s:%d" % (base.host or "localhost", base.port or 5432)
    if timed or rc != 0 or err.strip():
        return engines.Refused("NOT CLEAN: the new database %s on %s was made, but pg_restore reported: %s. It is left as it is (the tool deletes "
                           "nothing); look at it before you use it. The database %s was not touched. Not recorded as a restore."
                           % (newname, where, db.mask_secrets((err or "timeout").strip())[:400], base.dbname))
    how = "psql -h %s -p %d%s -d %s" % (base.host or "localhost", base.port or 5432, (" -U " + base.user) if base.user else "", newname)
    text = ("OK restored the copy %s into the NEW database %s on %s. The database %s was not touched. Look at it: %s . To switch "
            "an application over, change its address to the new name -- that is the person's step.%s"
            % (copy, newname, where, base.dbname, how, (" Notes: " + "; ".join(notes)) if notes else ""))
    return engines.Restored(text, "%s/%s" % (where, newname), {"new_database": newname},
                            lambda rwhere: "RESTORED BUT NOT RECORDED (%s); " % rwhere + text)


# ============================================================================================ status

def _installed_timeouts(cwd):
    """[(file, timeout)] of the PreToolUse entries of the kit in the settings that apply to cwd."""
    out = []
    try:
        import install
        for p in install.applicable_settings(cwd):
            data, err = install._load(p)
            if not data or err:
                continue
            groups = (data.get("hooks") or {}).get("PreToolUse") if isinstance(data.get("hooks"), dict) else None
            if isinstance(groups, list):
                for _g, h in install._iter_ours(groups, "pre_tool.py", install.recorded_commands(p)[0]):
                    out.append((p, h.get("timeout")))
        return out, install.TIMEOUT_S
    except Exception:
        return out, None


def status_lines(settings, cwd=None):
    store = _store()
    L = []
    if not settings.get("db_protection", True):
        L.append("databases: protection is OFF (db_protection = false): commands that destroy a database pass with no copy")
    else:
        L.append("databases: protection is ON (PostgreSQL and SQLite; MCP calls; other engines: %s)" % (
            "pass with a warning (on_db_unsupported = allow)" if settings.get("on_db_unsupported", "allow") == "allow"
            else "refused (on_db_unsupported = deny)"))
    use_proofs(cwd or os.getcwd(), settings)
    for line in engines.status_lines(settings):
        L.append(line)
    pgdump, ver, _major = db.find_pg_dump(settings)
    L.append("pg_dump: %s" % (("%s -- %s" % (pgdump, ver)) if pgdump else "NOT FOUND (%s): PostgreSQL commands that destroy data will be refused" % ver))
    L.append("limits: max_db_mb = %s, db_deadline_s = %s, db_fresh_minutes = %s, on_db_remote = %s, on_db_too_large = %s, on_db_unsupported = %s" % (
        settings["max_db_mb"], settings["db_deadline_s"], settings["db_fresh_minutes"], settings["on_db_remote"],
        settings["on_db_too_large"], settings.get("on_db_unsupported", "allow")))
    if settings.get("mcp_no_db"):
        L.append("MCP servers that hold no database (their calls pass): %s" % ", ".join(settings["mcp_no_db"]))
    spent = db.budget_s(settings)
    installed, default = _installed_timeouts(cwd or os.getcwd())
    L += budget_lines(spent, installed, default)
    try:
        from . import dbmcp
        for why in dbmcp.unreadable_files(cwd or os.getcwd()):
            L.append("MCP settings: %s -- a call of an MCP tool of a server in it cannot be named from it (refused when it holds SQL)" % why)
    except Exception:
        pass
    n, total, where = 0, 0, []
    for root in store.all_roots(settings):
        try:
            recs, _bad = store._read_jsonl(db.index_path(root))
        except OSError:
            continue
        for _k, r in recs:
            if r.get("op"):
                continue
            try:
                if os.path.isfile(r["copy"]):
                    n += 1
                    total += int(r.get("copy_size") or 0)
            except (KeyError, TypeError, ValueError):
                pass
        where.append(root)
    L.append("copies of databases: %d copy(ies), %s on disk%s" % (n, db._mb(total), (" in " + ", ".join(where)) if where else ""))
    return L


def budget_lines(spent, installed, default):
    """[text] -- the budget of the hook against the timeout that is installed. `installed`: [(file, timeout)] of the entries found; `default`: the
    timeout this kit installs (None when it could not be read). An entry WITHOUT an integer timeout is not compared with the kit's own value: the
    runtime then uses its own default, which is not known here -- that is said, not a healthy margin."""
    L = []
    limits = [(p, t) for p, t in installed if isinstance(t, int)]
    for p, t in limits:
        L.append(_budget_line(spent, t, "the installed hook (%s)" % p))
    for p, t in installed:
        if not isinstance(t, int):
            L.append("installed hook (%s): NO readable timeout (%r) -- Claude Code then uses its own default, so the budget of %d s CANNOT be compared "
                     "with it; `python install.py install` sets %s s" % (p, t, spent, default if default else "the timeout of the kit"))
    if not installed:
        if default:
            L.append(_budget_line(spent, default, "the timeout this kit installs (no entry of install.py found in the settings; the plugin carries the same value in hooks/hooks.json)"))
        else:
            L.append("hook budget: %d s in the worst case; the installed timeout could not be read (the settings could not be read), so it was NOT compared" % spent)
    return L


def _budget_line(spent, limit, who):
    margin = limit - spent
    return "hook budget: %d s in the worst case (cmd_deadline_s + git + db_deadline_s + start) against %d s of %s -- margin %d s: %s" % (
        spent, limit, who, margin, "OK" if margin >= 10 else "TOO SMALL (needs >= 10 s): lower db_deadline_s / cmd_deadline_s or raise the timeout (TIMEOUT_S in install.py and install again; for the plugin, `timeout` in its hooks/hooks.json, which an update replaces)")
