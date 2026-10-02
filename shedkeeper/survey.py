r"""survey.py -- getting to know the machine and the project (0.3.0), and the once-only notes.

  python shedkeeper.py survey [--json] [--project PATH]     READ ONLY: what is here, what ShedKeeper would do, what to set
  python shedkeeper.py survey --done                        only records that the survey was done for the project

The survey reads and prints; it writes nothing except ONE line in the personal folder of the store (`guide.jsonl`): that the survey was
done for this project. It never connects to a database (a size of a PostgreSQL database is not asked) and never prints a password: a
database is shown only through Base.url_masked / its engine.

The same file `guide.jsonl` keeps the once-only facts, so nothing is said twice:
  offered      the SessionStart hook offered the survey to the agent for this project (set at once, answered or not);
  done         the survey was run (or marked done) for this project;
  unsupported  the warning about an engine ShedKeeper does not copy was given for (project, engine); after it the engine passes
               quietly, with a line in the log of the store.
  rule_offered the facts and the recommendation about the rule in the user's own system were given to the agent (ONCE per user: the key is the
               user, not a project; looked up in every store of the user).
The project is the root of the git repository the folder is in; a folder with no repository is its own project.
"""
import json
import os
import re
import shutil
import sys
import time

from . import advice, config, db, dbmcp, engines, store

STATE_NAME = "guide.jsonl"
SIZE_ASK_MB_FACTOR = 1.25


# ============================================================================================ the project and the once-only record

def project_root(cwd):
    """The root of the project: the first folder upward (at most 20 levels) with `.git`; no repository -> the folder itself."""
    start = os.path.abspath(cwd or os.getcwd())
    d = start
    for _ in range(20):
        if os.path.exists(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return start


def project_key(cwd):
    return os.path.normcase(project_root(cwd)).replace("\\", "/").rstrip("/")


def _state_file(root):
    return os.path.join(root, STATE_NAME)


def state_read(root):
    """[records] of guide.jsonl; [] when there is none or it cannot be read (the callers then treat nothing as said)."""
    if not root:
        return []
    try:
        recs, _bad = store._read_jsonl(_state_file(root))
        return [r for _n, r in recs]
    except OSError:
        return []


def state_add(root, rec):
    """True when the line was written. Only into a personal folder that is ours (the same rule as the log of the store)."""
    try:
        if not (root and os.path.isdir(root) and store._is_mine(root)):
            return False
        rec = dict(rec, time=time.strftime("%Y-%m-%dT%H:%M:%S"))
        store._append_jsonl(_state_file(root), lambda n: rec)
        return True
    except Exception:
        return False


def has(root, event, project, engine=None):
    for r in state_read(root):
        if r.get("event") == event and r.get("project") == project and (engine is None or r.get("engine") == engine):
            return True
    return False


def offer_text(kit=None):
    k = kit or advice.kit_py()
    return ("ShedKeeper setup check for this project: `python %s survey`. It only reads (no changes, no passwords shown) and lists what ShedKeeper "
            "found here, what it would do and which settings could fit; a setting is changed only with `python %s set <key> <value>`, and one that weakens "
            "protection needs the user's confirmation code. The check is offered to the user once, at a natural moment, in their language." % (k, k))


def offer_once(root, cwd):
    """The text to put into the context of the session start, or '' (already offered, done, or the record cannot be written -- then
    nothing is offered: an offer that cannot be remembered would be repeated every session)."""
    key = project_key(cwd)
    if has(root, "survey_offered", key) or has(root, "survey_done", key):
        return ""
    if not state_add(root, {"event": "survey_offered", "project": key}):
        return ""
    return offer_text()


USER_KEY = "(user)"          # the key of the once-only facts that belong to the USER, not to a project


def rule_offer_text(kit=None):
    """The facts and the recommendation of ShedKeeper about the rule of the user's own system (README, "A rule for your agent"). English, for the agent; the
    user hears it in the language of the conversation. ShedKeeper does not write, read or check the file of instructions of the user (the rule is theirs)."""
    k = kit or advice.kit_py()
    return ("ShedKeeper facts, given once to each user: ShedKeeper is installed here (K=\"%s\"). It guards the user's data: before an edit or a deletion in a project "
            "or outside it, it saves a copy or moves the file to a trash; it also guards its own settings, so it stops a command that writes to them (a weakening of "
            "protection is made with `python %s set <key> <value>` and the user's confirmation code). ShedKeeper's recommendation to the user: ask your agent to write, "
            "in your own system, in the file of instructions your agent reads (for Claude Code: `CLAUDE.md`), a short rule about working with ShedKeeper, in your own "
            "words. Its principles: (a) ShedKeeper exists to protect your data, from the agent's own mistakes too; (b) when ShedKeeper stops an action, the way on "
            "is the lawful one it names, or a question to you -- not a way round it; (c) only you weaken the protection. A model wording is in README.md of the kit, "
            "section \"A rule for your agent\". ShedKeeper itself does not write that file, does not read it and does not check that the rule is there. This note "
            "is given once; at a natural moment, pass the recommendation to the user in the language of your conversation." % (kit_dir(k), k))


def kit_dir(kit_py):
    """The folder of the kit from the path of its `shedkeeper.py`."""
    return os.path.dirname(kit_py).replace("\\", "/")


def rule_offer_once(root, roots=()):
    """The text to put into the context of the session start, or '' (given before -- in this store or in any other store of the user -- or the record cannot be
    written: then nothing is given, a note that cannot be remembered would be repeated every session). One record per USER (USER_KEY), written at once."""
    for r in list(roots) + [root]:
        if has(r, "rule_offered", USER_KEY):
            return ""
    if not state_add(root, {"event": "rule_offered", "project": USER_KEY}):
        return ""
    return rule_offer_text()


def unsupported_pending(root, cwd, engine):
    """True when the warning about this engine in this project was NOT given yet. Nothing is recorded: the hook records it
    (`mark_unsupported_warned`) only after the warning was delivered in a decision that lets the command through."""
    return not has(root, "unsupported_warned", project_key(cwd), engine)


def mark_unsupported_warned(root, cwd, engine):
    """The warning was delivered: record it (once per project and engine). True when the line is there."""
    key = project_key(cwd)
    return has(root, "unsupported_warned", key, engine) or state_add(root, {"event": "unsupported_warned", "project": key, "engine": engine})


def unsupported_first_time(root, cwd, engine):
    """True when the warning about this engine in this project was NOT given yet, and the fact is recorded at once. For a caller that delivers
    the warning itself in the same breath (the hook does not: it uses unsupported_pending and mark_unsupported_warned, so a refused command
    does not spend the warning)."""
    if not unsupported_pending(root, cwd, engine):
        return False
    mark_unsupported_warned(root, cwd, engine)
    return True


# ============================================================================================ what is here

def _fake_action(words, cwd):
    return db.DbAction("tool", words[0], ["survey"], cwd, {}, words=list(words), tool="survey")


def _describe_base(b, source):
    """One database of the project, as a dict that carries no secret (url_masked, never the conninfo)."""
    if isinstance(b, db.Unsupported):
        return {"source": source, "engine": b.engine, "where": "unknown", "supported": False, "name": b.url_masked,
                "note": (b.reason if b.adapter else "this version of ShedKeeper copies PostgreSQL and SQLite only")}
    if isinstance(b, db.Unnamed):
        return {"source": source, "engine": "?", "where": "unknown", "supported": None, "name": "", "note": b.reason}
    out = {"source": source, "engine": engines.title(b.engine), "supported": True,
           "where": "remote" if b.remote else "local", "name": b.url_masked, "note": ""}
    if b.engine == "sqlite" and b.path and os.path.isfile(b.path):
        try:
            out["size_mb"] = round(os.path.getsize(b.path) / 1048576.0, 1)
        except OSError:
            pass
    return out


def project_databases(cwd, env=None):
    """[dict] -- the databases the files of the project name, found the way db.py finds them for a command (no command here). A `.env` that
    exists and cannot be read is a line of its own (it is not `no database`)."""
    env = os.environ if env is None else env
    action0 = _fake_action(["survey"], cwd)
    _eff, look = db._lookup_factory(action0, env)
    found, seen = [], set()
    for d in db._ancestors(cwd):
        envf = os.path.join(d, ".env")
        if os.path.isfile(envf):
            try:
                with open(envf, "rb") as fh:
                    fh.read(1)
            except OSError as e:
                found.append({"source": ".env", "engine": "?", "where": "unknown", "supported": None, "name": "",
                              "note": "%s exists and cannot be read (%s): the database it names is not known" % (envf, e.__class__.__name__)})
            break

    def add(b, source):
        if b is None:
            return
        d = _describe_base(b, source)
        key = (d["engine"], d["name"], d["source"])
        if key not in seen:
            seen.add(key)
            found.append(d)

    def tool(words, fn, rel_checks):
        if any(db._find_up(cwd, *r) for r in rel_checks):
            try:
                add(fn(_fake_action(words, cwd), look, cwd), "/".join(rel_checks[0]))
            except Exception as e:
                add(db.Unnamed("could not be read (%s)" % e.__class__.__name__), "/".join(rel_checks[0]))

    tool(["prisma"], db._from_prisma, [("prisma", "schema.prisma"), ("schema.prisma",)])
    tool(["supabase", "db", "reset"], db._from_supabase, [("supabase", "config.toml")])
    tool(["alembic"], db._from_alembic, [("alembic.ini",)])
    tool(["rails"], db._from_rails, [("config", "database.yml")])
    if db._find_up(cwd, "artisan"):
        try:
            add(db._from_laravel(_fake_action(["artisan"], cwd), look, cwd), "artisan + .env")
        except Exception as e:
            add(db.Unnamed("could not be read (%s)" % e.__class__.__name__), "artisan + .env")
    try:
        add(db._from_database_url(_fake_action(["survey"], cwd), look, cwd), ".env / DATABASE_URL")
    except Exception as e:
        add(db.Unnamed("DATABASE_URL could not be read (%s)" % e.__class__.__name__), ".env / DATABASE_URL")
    return found


def mcp_servers(cwd):
    """[dict] -- the MCP servers of the settings (project, local, user; plugins when the registry can be read), each with the verdict
    of dbmcp.server_verdict: does ShedKeeper count it as a server of a database, and why."""
    out = []

    def add(name, cfg, where):
        v = dbmcp.server_verdict(name, cfg, cwd)
        out.append({"name": name, "where": where, "is_db": v[0], "why": v[1], "engine": v[2], "remote": v[3]})

    try:
        p = db._find_up(cwd, ".mcp.json")
        data = dbmcp._read_json(p) if p else None
        for name, cfg in ((data or {}).get("mcpServers") or {}).items():
            if isinstance(cfg, dict):
                add(name, cfg, p)
        cj = dbmcp.claude_json_path()
        data = dbmcp._read_json(cj)
        if data:
            for name, cfg in (data.get("mcpServers") or {}).items():
                if isinstance(cfg, dict):
                    add(name, cfg, cj + " (user)")
            projects = data.get("projects") if isinstance(data.get("projects"), dict) else {}
            want = dbmcp._slash_key(project_root(cwd))
            for k, ent in projects.items():
                if isinstance(ent, dict) and dbmcp._slash_key(k) == want:
                    for name, cfg in (ent.get("mcpServers") or {}).items():
                        if isinstance(cfg, dict):
                            add(name, cfg, cj + " (this project)")
    except Exception as e:
        out.append({"name": "?", "where": "", "is_db": None, "why": "the MCP settings could not be read (%s)" % e.__class__.__name__,
                    "engine": "", "remote": False})
    for why in dbmcp.unreadable_files(cwd):                 # a file that exists and cannot be read is not "no servers"
        out.append({"name": "?", "where": why.split(" cannot be read")[0], "is_db": None, "why": why, "engine": "", "remote": False})
    return out


def store_volume(cwd, settings):
    """{root, volume, free_gb} of the volume the store of this project would be on; free space None when it cannot be read."""
    out = {"root": None, "free_gb": None, "total_gb": None}
    try:
        root = store.store_root(project_root(cwd), settings)
        out["root"] = root
        base = root or project_root(cwd)
        probe = base
        while probe and not os.path.exists(probe):
            parent = os.path.dirname(probe)
            if parent == probe:
                break
            probe = parent
        u = shutil.disk_usage(probe)
        out["free_gb"] = round(u.free / 1073741824.0, 1)
        out["total_gb"] = round(u.total / 1073741824.0, 1)
    except Exception:
        pass
    return out


def recommendations(rep, settings):
    """[{key, value, why, weight}] -- what a person may want to set, from what the survey found. Plain words, English; the
    agent translates. A recommendation is an offer: nothing is changed by the survey."""
    recs = []
    bases = rep["databases"]
    pg_local = [b for b in bases if b["engine"] == "PostgreSQL"]
    if pg_local and not rep["pg_dump"]["path"]:
        recs.append({"key": "pg_dump_path", "value": "<folder or file of pg_dump>",
                     "why": "a PostgreSQL database is named in the project but pg_dump (the copying program) was not found: any command that "
                            "destroys data in it will be refused until ShedKeeper can find pg_dump. Install the PostgreSQL client tools, "
                            "or tell ShedKeeper where pg_dump is.", "weight": "tuning"})
    if any(b["where"] == "remote" for b in bases):
        recs.append({"key": "on_db_remote", "value": "deny",
                     "why": "a database on another machine is named in the project. The default (deny) refuses a destroying command there "
                            "and gives the agent the command that takes the copy by hand (`dbcopy`) -- keep it unless you copy remote databases "
                            "by another way.", "weight": "protection"})
    big = [b for b in bases if b.get("size_mb") and b["size_mb"] > settings["max_db_mb"]]
    for b in big:
        recs.append({"key": "max_db_mb", "value": int(b["size_mb"] * SIZE_ASK_MB_FACTOR) + 1,
                     "why": "the SQLite file %s is %.1f MB, above the limit of %d MB: the hook will not copy it by itself and will refuse the "
                            "destroying command (the agent then takes the copy with `dbcopy`). Raise the limit only if a copy of this size fits "
                            "in the time limit of the hook (db_deadline_s = %d s)." % (b["name"], b["size_mb"], settings["max_db_mb"],
                                                                                        settings["db_deadline_s"]),
                     "weight": "tuning"})
    others = sorted({b["engine"] for b in bases if b["supported"] is False})
    if others:
        recs.append({"key": "on_db_unsupported", "value": "deny",
                     "why": "the project uses %s, which this ShedKeeper cannot copy. Now (allow) such a command goes ahead after one "
                            "warning per project; with deny it is refused until you take the copy yourself. Choose deny if losing that data "
                            "would hurt." % ", ".join(others), "weight": "protection", "optional": True})
    unnamed = [b for b in bases if b["supported"] is None]
    if unnamed:
        recs.append({"key": "(none)", "value": "",
                     "why": "%d database source(s) could not be read to the end (%s). A command that destroys data there is refused with "
                            "the command that names the database (`SHEDKEEPER_DB=...`): nothing to set." % (len(unnamed), unnamed[0]["note"]),
                     "weight": "notice"})
    nodb = [s["name"] for s in rep["mcp"] if s["is_db"] is False]
    if nodb:
        recs.append({"key": "(none)", "value": "",
                     "why": "MCP server(s) %s hold no database by their settings and names: their delete-type tools pass without a stop. If one "
                            "of them does hold a database, tell the agent: the copy is then taken with `dbcopy <address> --mcp-server NAME`."
                            % ", ".join(nodb), "weight": "notice"})
    if rep["store"]["free_gb"] is not None and rep["store"]["free_gb"] < 5:
        recs.append({"key": "(none)", "value": "",
                     "why": "only %.1f GB are free on the volume of the store: copies of files and databases are kept there and only a person "
                            "cleans them." % rep["store"]["free_gb"], "weight": "notice"})
    if not settings.get("db_protection", True):
        recs.append({"key": "db_protection", "value": "true",
                     "why": "the protection of databases is switched OFF now: commands that destroy a database go ahead with no copy.",
                     "weight": "protection"})
    return recs


def gather(cwd, settings):
    cwd = cwd or os.getcwd()
    pgdump, ver, _major = db.find_pg_dump(settings)
    try:
        from . import dbcli
        dbcli.use_proofs(cwd, settings)          # (a proven adapter's engine is not reported as one that is not proven)
    except Exception:
        pass
    rep = {
        "project": project_root(cwd),
        "python": sys.version.split()[0],
        "pg_dump": {"path": pgdump, "version": ver if pgdump else "", "why_not": "" if pgdump else ver},
        "store": store_volume(cwd, settings),
        "databases": project_databases(cwd),
        "mcp": mcp_servers(cwd),
        "settings": {k: config.format_value(settings.get(k)) for k in config.KEYS},
    }
    rep["recommendations"] = recommendations(rep, settings)
    return rep


def render(rep):
    L = ["ShedKeeper survey of %s (read only: nothing was changed, no password is shown)" % rep["project"],
         "python: %s" % rep["python"]]
    pg = rep["pg_dump"]
    L.append("pg_dump: %s" % (("%s -- %s" % (pg["path"], pg["version"])) if pg["path"] else "NOT FOUND (%s)" % pg["why_not"]))
    st = rep["store"]
    L.append("store: %s; free space on its volume: %s" % (st["root"] or "(no folder can be named for this project)",
                                                       ("%.1f GB of %.1f GB" % (st["free_gb"], st["total_gb"])) if st["free_gb"] is not None else "unknown"))
    L.append("databases the files of the project name:")
    if not rep["databases"]:
        L.append("  none found (looked at .env, prisma/schema.prisma, supabase/config.toml, alembic.ini, config/database.yml, Laravel .env)")
    for b in rep["databases"]:
        L.append("  - %s %s %s [%s]%s%s" % (b["engine"], b["where"], b["name"], b["source"],
                                          (" (%.1f MB)" % b["size_mb"]) if b.get("size_mb") is not None else "",
                                          ("  -- " + b["note"]) if b["note"] else ""))
    L.append("MCP servers in the settings:")
    if not rep["mcp"]:
        L.append("  none found")
    for s in rep["mcp"]:
        verdict = {True: "counts as a DATABASE server", False: "no database (its delete-type tools pass)", None: "not decided"}[s["is_db"]]
        extra = ", ".join(x for x in (s.get("engine") or "", "remote" if s.get("remote") else "") if x)
        L.append("  - %s [%s]: %s%s -- %s" % (s["name"], s["where"], verdict, (" (%s)" % extra) if extra else "", s["why"]))
    L.append("current settings: " + ", ".join("%s = %s" % (k, v) for k, v in rep["settings"].items() if v != ""))
    L.append("recommendations (offers only; a setting is changed with `python %s set KEY VALUE`, and one that weakens protection needs the user's confirmation code):" % advice.kit_py())
    if not rep["recommendations"]:
        L.append("  none: the defaults fit what was found")
    for r in rep["recommendations"]:
        head = ("%s = %s" % (r["key"], r["value"])) if r["key"] != "(none)" else "(no setting)"
        L.append("  - %s [%s]%s: %s" % (head, r["weight"], " (optional)" if r.get("optional") else "", r["why"]))
    return "\n".join(L)


def run(cwd, settings, as_json=False, done_only=False, mark=True):
    """(exit code, text). The only write: the record `survey_done` in the personal folder of the store, when there is one."""
    cwd = cwd or os.getcwd()
    key = project_key(cwd)
    root = None
    try:
        root = store.store_root(project_root(cwd), settings)
    except Exception:
        root = None
    note = ""
    if done_only:
        ok = has(root, "survey_done", key) or state_add(root, {"event": "survey_done", "project": key, "by": "done flag"})
        return (0 if ok else 1), ("survey marked as done for %s" % key) if ok else \
            ("NOT recorded: the store of this project does not exist yet or is not usable (%s)" % (root or "no folder"))
    rep = gather(cwd, settings)
    if mark:
        ok = has(root, "survey_done", key) or state_add(root, {"event": "survey_done", "project": key, "by": "survey"})
        note = "recorded: survey done for this project" if ok else "not recorded (no store folder yet) -- the offer may come again"
        rep["recorded"] = ok
    if as_json:
        return 0, json.dumps(rep, indent=2, ensure_ascii=False, sort_keys=False)
    return 0, render(rep) + ("\n" + note if note else "")
