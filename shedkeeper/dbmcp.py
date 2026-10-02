r"""dbmcp.py -- a call of an MCP tool that may destroy data in a database (0.3.0).

  action_for(tool_name, tool_input, mcp_server, cwd, settings)   -> db.DbAction | None   (None: not a destroying call)
  name_mcp(action, cwd)                                          -> Base | Unnamed | Unsupported

A call of `mcp__<server>__<tool>` (a server of a plugin: `mcp__plugin_<plugin>_<server>__<tool>`) is DESTROYING when (a) a string
argument holds SQL of the destroying class (the same reader as for psql: comments and literals are cut out first) -- only a string
that STARTS like SQL is read, so a sentence of a chat message that happens to hold "delete from" is not -- or (b) the name of the
tool holds drop, delete, reset, truncate, wipe or purge (`delete_branch`, `reset_from_parent`) AND either (b1) the tool name itself names an OBJECT of a
database (DB_OBJECT_WORDS: database, db, table, schema, branch, collection, index, bucket -- a whole word of the tool name, split at `_` / `-` / a space / a
capital letter, singular or plural: `delete_database`, `reset-branch`, `DropTable`) -- on ANY server, so the rule does not depend on a list of products -- or
(b2) the server is a server of a DATABASE: its settings give the address of a database, or its name, command or address holds a word of a database
(NAME_WORDS below: postgres, pg, sqlite, supabase, neon, mysql, maria, mongo, redis, turso, libsql, planetscale, cockroach, duckdb, firebase, firestore, convex,
appwrite, d1, prisma, db, sql, database -- a whole word or a part of the name between `-` / `_` / `.` / `:` / `/` / a space). A chat or a tracker with a
`delete_message` or `delete_file` is not (no object of a database, no database server): its call passes with no record. A tool of another kind that carries an
object word (`delete_branch` of a code-hosting server) is stopped too; `mcp_no_db` is the way out.
(a) stays for EVERY server (SQL in an argument is a strong sign of a database). The ini key `mcp_no_db` stays as an explicit exception.

The database behind the server is read from the settings of the server (Claude Code gives the hook `mcp_server {name, source}`):
source `project` -> `.mcp.json` of the project (from cwd upward); `local` -> `projects[<path>].mcpServers` of ~/.claude.json
(CLAUDE_CONFIG_DIR is honoured); `user` -> `mcpServers` of the same file; `plugin` -> the `.mcp.json` (or the `mcpServers` of
`plugin.json`) of the plugin, found by the plugin registry exactly as install.py finds the plugin (install.plugin_registry,
install.plugin_run_folder -- a folder that cannot be determined is "cannot be named", never a guess). A server of the type
http / sse (or with a `url` and no `command`) is a REMOTE database whose address is not known; a stdio server names its database
in `args` or `env` (an address postgres(ql):// or a SQLite file with an absolute path, or PGHOST/PGDATABASE ...). Another engine
named there (mysql://, mongodb:// ...) is `Unsupported`; so is a server with no address at all whose name, command or args hold the product of an engine the kit does not copy
(NOT_COPIED_WORDS: planetscale, duckdb, firebase, firestore, convex, appwrite, turso, libsql, d1). No `mcp_server` in the input (Claude Code older than 2.1.274), an
unknown `source`, a server that is not found, two different candidates, a `${...}` in the address -> `Unnamed` (cannot be named).
No project code is run.

What the reader could NOT do is never silent: a settings file that exists but cannot be read is named in the refusal (SQL in an argument, or a
server whose name holds a word of a database) and in a line of the journal (`mcp_settings_unreadable`); a call with more strings than
MAX_STRINGS, a deeper nesting than MAX_DEPTH, or a string over MAX_STRING_LEN is read as far as the limit and the rest is a line of the journal
(`mcp_read_limit`) -- see take_notes(). A part that was not read is also a DOUBT (`_doubt`), judged on the strict side: the call goes to a server of a
database, or the part that WAS read looks like SQL -> the call is treated as destroying (a copy of the database behind the server, or a refusal when it cannot be
named); a big harmless call to another server passes with the journal line. The depth limit counts strings and containers that hold something, not a number or an
empty container. A call that drops several databases is one action per database (actions_for).
"""
import json
import os
import re

from . import db, engines

DESTROY_WORDS = ("drop", "delete", "reset", "truncate", "wipe", "purge")
REMOTE_TYPES = ("http", "sse", "streamable-http", "streamable_http", "ws", "websocket")
MAX_STRINGS = 400
MAX_DEPTH = 8
MAX_STRING_LEN = 5 * 1024 * 1024
_NOTES = []                 # [(event, why)] of what the reader could not do in this call (a hook lives for one call); take_notes() hands them over
SQLISH = re.compile(
    r"(?is)\A(?:\s|--[^\n]*(?:\n|\Z)|/\*.*?\*/)*(?:(?:drop|truncate|delete|update|alter|with|begin|start|do|explain|call|execute|merge|"
    r"insert|select|create|set|copy|vacuum|grant|revoke|comment|lock|values|table|show)\b|\()")
ADDR_RE = re.compile(
    r"(?i)(?<![\w+.-])(?:postgres(?:ql)?|pgsql|postgis|sqlite3?|mysql\w*|mariadb|mongodb(?:\+srv)?|sqlserver|mssql|oracle|"
    r"cockroachdb|clickhouse|redis)(?:\+\w+)?://[^\s'\"<>]+")
# the words of a server of a database: a token of the name/command/address equals one of these, or begins with one of the
# first group (postgresql, mysql2, mariadb, mongodb, sqlite3, databases ...)
NAME_WORDS = ("postgres", "pg", "sqlite", "supabase", "neon", "mysql", "maria", "mongo", "redis", "turso", "libsql", "planetscale", "cockroach", "duckdb",
              "firebase", "firestore", "convex", "appwrite", "d1", "prisma", "db", "sql", "database")
NAME_PREFIXES = ("postgres", "sqlite", "supabase", "mysql", "maria", "mongo", "redis", "database", "cockroach")
# the objects a tool of a database destroys: a whole word of the tool name (see name_object)
DB_OBJECT_WORDS = ("database", "db", "table", "schema", "branch", "collection", "index", "bucket")
# the products of engines the kit does NOT copy: a server of one of them with no PostgreSQL / SQLite address in its settings is `Unsupported` (named, not copied), not `Unnamed`
NOT_COPIED_WORDS = ("planetscale", "duckdb", "firebase", "firestore", "convex", "appwrite", "turso", "libsql", "d1")
DB_ENV_KEY = re.compile(r"(?i)(?:database|db|dsn|connection|conn|postgres|pg)[\w]*(?:url|uri|dsn|string)|^(?:url|uri)$")


def server_parts(tool_name):
    """(server segment, tool) of `mcp__server__tool`; ('', '') for a name of another shape."""
    parts = (tool_name or "").split("__")
    if len(parts) >= 3 and parts[0] == "mcp":
        return parts[1], "__".join(parts[2:])
    return "", ""


def _note(event, why):
    if (event, why) not in _NOTES:
        _NOTES.append((event, why))


def take_notes():
    """[(event, why)] of what the reader could not do since the last call (the hook writes them to the journal of the store), and forget them."""
    out = list(_NOTES)
    del _NOTES[:]
    return out


def _is_unread_kind(obj):
    """Is this part of the arguments something that CAN hold SQL: a string, or a container that holds something? A number, a boolean, None or an
    empty container below the depth limit is nothing that was left unread."""
    return isinstance(obj, str) or (isinstance(obj, (dict, list, tuple)) and len(obj) > 0)


def _strings(obj, out, depth=0):
    """Collect the strings of the arguments into `out`. -> True when every part was read, False when a limit (strings, depth) stopped the reading."""
    if depth > MAX_DEPTH and _is_unread_kind(obj):
        _note("mcp_read_limit", "a part of the arguments is nested deeper than %d levels and was not read" % MAX_DEPTH)
        return False
    if _is_unread_kind(obj) and len(out) >= MAX_STRINGS:
        _note("mcp_read_limit", "the arguments hold more than %d strings: the rest was not read" % MAX_STRINGS)
        return False
    complete = True
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            complete = _strings(v, out, depth + 1) and complete
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            complete = _strings(v, out, depth + 1) and complete
    return complete


def name_word(tool_name):
    """The destroying word (drop, delete ...) in the part of the tool name AFTER the server, or None."""
    _server, tool = server_parts(tool_name)
    low = tool.lower()
    return next((w for w in DESTROY_WORDS if w in low), None)


def name_object(tool_name):
    """The object of a database (DB_OBJECT_WORDS) that the part of the tool name AFTER the server names as a whole word, or None. The name is split at every
    character that is not a letter or a digit and at a lower-to-capital step (`DropTable` -> drop, table); a word counts in the singular, with `s` / `es` (`tables`,
    `branches`) and as `indices`. A part of a word (`tablet`, `indexing`, `dbx`) is not the object."""
    _server, tool = server_parts(tool_name)
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", tool)
    for tok in re.split(r"[^A-Za-z0-9]+", spaced.lower()):
        for w in DB_OBJECT_WORDS:
            if tok in (w, w + "s", w + "es") or (w == "index" and tok == "indices"):
                return w
    return None


def _read_call(tool_name, tool_input, name_rule=True):
    """(reasons, dropped database names, complete, sqlish) of a call. `complete` is False when a part of the arguments was NOT read (more strings than
    MAX_STRINGS, nested deeper than MAX_DEPTH, a string over MAX_STRING_LEN); `sqlish` is True when a string that starts like SQL was among what was read (or
    started a string that was too long to read)."""
    reasons, dropped = [], []
    _server, tool = server_parts(tool_name)
    word = name_word(tool_name) if name_rule else None
    if word:
        reasons.append("the tool name %r holds %r" % (tool, word))
    strings = []
    complete = _strings(tool_input, strings)
    sqlish = False
    for s in strings:
        if len(s) > MAX_STRING_LEN:
            _note("mcp_read_limit", "a string of %d characters (limit %d) was not read" % (len(s), MAX_STRING_LEN))
            complete = False
            sqlish = sqlish or bool(SQLISH.match(s[:4096]))
            continue
        if not SQLISH.match(s):
            continue
        sqlish = True
        found, _meta = db.sql_findings(s)
        for r, d in found:
            if d:
                dropped.append(d)
                r = "DROP DATABASE " + d
            if r not in reasons:
                reasons.append(r)
    return reasons, list(dict.fromkeys(dropped)), complete, sqlish


def destructive(tool_name, tool_input, name_rule=True):
    """(reasons, dropped database names) of a call: [] when it is not destroying. `name_rule=False`: only rule (a), SQL in an
    argument; actions_for applies rule (b), the word in the name, when the tool names an object of a database (b1) or the server is one (b2)."""
    reasons, dropped, _complete, _sqlish = _read_call(tool_name, tool_input, name_rule)
    return reasons, dropped


def _doubt(complete, sqlish, is_db):
    """A part of the arguments that was NOT read is a doubt, and a doubt is judged on the strict side (a refusal when it cannot be copied): the call goes to a
    server of a database, or the part that WAS read looks like SQL. A big harmless call to a server that is no database, with nothing SQL-like read, passes
    (with a line in the journal): refusing it would only teach the person to switch the guard off."""
    return (not complete) and (sqlish or is_db)


def actions_for(tool_name, tool_input, mcp_server, cwd, settings):
    """[DbAction] of a destroying call of an MCP tool: one per database the call DROPS (`DROP DATABASE a; DROP DATABASE b` is two), else one;
    [] when the call destroys nothing."""
    reasons, dropped, complete, sqlish = _read_call(tool_name, tool_input, name_rule=False)         # (a) SQL in an argument: any server
    seg, tool = server_parts(tool_name)
    word = name_word(tool_name)
    obj = name_object(tool_name) if word else None
    if not reasons and not word and complete:
        return []
    name = source = ""
    if isinstance(mcp_server, dict):
        name = mcp_server.get("name") if isinstance(mcp_server.get("name"), str) else ""
        source = mcp_server.get("source") if isinstance(mcp_server.get("source"), str) else ""
    skip = {str(x).lower() for x in (settings.get("mcp_no_db") or ())}
    if skip and ({name.lower(), seg.lower()} & skip):
        return []
    asked = []

    def on_db_server():
        if not asked:
            asked.append(is_db_server(name, source, seg, cwd)[0])
        return asked[0]
    if word and (obj or on_db_server()):                                            # (b) the word in the name: the tool names an object of a database (b1), or the server is one (b2)
        reasons.insert(0, "the tool name %r holds %r" % (tool, word) + (" and names a database object (%r)" % obj if obj else ""))
    if not reasons and _doubt(complete, sqlish, on_db_server()):                    # a part of the arguments was not read
        reasons.append("a part of the arguments was not read (more than %d strings, deeper than %d levels, or a string over %d MB), so a destroying part "
                       "cannot be ruled out" % (MAX_STRINGS, MAX_DEPTH, MAX_STRING_LEN // (1024 * 1024)))
    if not reasons:
        return []
    mcp = {"name": name, "source": source, "tool": tool, "segment": seg}
    if len(dropped) > 1:
        return [db.DbAction("mcp", tool_name, reasons, cwd, {}, conn={"dropped_db": d, "multi": True}, mcp=dict(mcp)) for d in dropped]
    conn = {"dropped_db": dropped[0]} if dropped else {}
    return [db.DbAction("mcp", tool_name, reasons, cwd, {}, conn=conn, mcp=mcp)]


def action_for(tool_name, tool_input, mcp_server, cwd, settings):
    """The first DbAction of a destroying call of an MCP tool, or None (see actions_for: a call that drops several databases has one per database)."""
    acts = actions_for(tool_name, tool_input, mcp_server, cwd, settings)
    return acts[0] if acts else None


# ============================================================================================ settings of the server

def _read_json(path):
    return _read_json_why(path)[0]


def _read_json_why(path):
    """(data, why). `why` is '' when the file was read, or does not exist (nothing to say about it); else a text: the file EXISTS and cannot be read
    (rights, a file being rewritten, a damaged one, not a JSON object). The text is also a note for the journal (`mcp_settings_unreadable`)."""
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None, ""
    except (OSError, ValueError) as e:
        why = "%s cannot be read (%s)" % (path, e.__class__.__name__)
        _note("mcp_settings_unreadable", why)
        return None, why
    if not isinstance(data, dict):
        why = "%s cannot be read (it is not a JSON object)" % path
        _note("mcp_settings_unreadable", why)
        return None, why
    return data, ""


def unreadable_files(cwd):
    """[text] -- the MCP settings files of this project / user that exist and cannot be read: `status` and `survey` name them (a file that cannot be
    read is not `no servers`)."""
    out = []
    try:
        p = db._find_up(cwd, ".mcp.json")
        for f in (p, claude_json_path()):
            if f and os.path.exists(f):
                _data, why = _read_json_why(f)
                if why and why not in out:
                    out.append(why)
    except Exception:
        pass
    take_notes()                  # these are told by the caller, not journalled again
    return out


def _norm_key(s):
    return re.sub(r"[^A-Za-z0-9_-]", "_", s or "").lower()


def _pick(servers, name):
    """The entry `name` of a dict of servers (a name is also compared in the form Claude Code gives it inside a tool name)."""
    if not isinstance(servers, dict):
        return None
    if isinstance(servers.get(name), dict):
        return servers[name]
    want = _norm_key(name)
    for k, v in servers.items():
        if _norm_key(k) == want and isinstance(v, dict):
            return v
    return None


def claude_json_path():
    home = os.environ.get("SHEDKEEPER_USER_HOME")
    if home:
        return os.path.join(home, ".claude.json")
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    if cfg:
        return os.path.join(os.path.abspath(os.path.expanduser(cfg)), ".claude.json")
    return os.path.join(os.path.expanduser("~"), ".claude.json")


def _slash_key(p):
    return os.path.normcase(os.path.abspath(p)).replace("\\", "/").rstrip("/")


def _plugin_servers(name, seg, source_note):
    """([(config, where)], why not). The plugin is found by the registry, the way install.py finds it."""
    try:
        import install
    except Exception as e:
        return [], "the plugin registry cannot be read (install.py is not importable: %s)" % e.__class__.__name__
    reg, problem = install.plugin_registry()
    if problem:
        return [], problem
    plugin = server = None
    if name.startswith("plugin:") and name.count(":") >= 2:
        _p, plugin, server = name.split(":", 2)
    keys = []
    for key in reg:
        pname = key.split("@", 1)[0]
        if plugin is not None:
            if pname == plugin:
                keys.append((key, server))
        elif seg.startswith("plugin_") and seg.lower().startswith("plugin_" + _norm_key(pname) + "_"):
            keys.append((key, seg[len("plugin_" + _norm_key(pname) + "_"):]))
    if not keys:
        return [], "the plugin behind the server %r is not in the plugin registry (or its name cannot be read)" % (name or seg)
    out, why = [], []
    for key, srv in keys:
        run, note = install.plugin_run_folder(key)
        folders = [run] if run else ([ip for ip, _v in reg.get(key, []) if isinstance(ip, str)] if run == "" else [])
        if not folders:
            why.append("the folder the plugin %s runs from is not determined (%s)" % (key, note))
            continue
        for folder in folders:
            cfg = None
            data = _read_json(os.path.join(folder, ".mcp.json"))
            if data is not None:
                cfg = _pick(data.get("mcpServers") if isinstance(data.get("mcpServers"), dict) else data, srv)
            if cfg is None:
                pj = _read_json(os.path.join(folder, ".claude-plugin", "plugin.json")) or {}
                ms = pj.get("mcpServers")
                if isinstance(ms, str):
                    f2 = _read_json(os.path.normpath(os.path.join(folder, ms)))
                    ms = (f2.get("mcpServers") if f2 and isinstance(f2.get("mcpServers"), dict) else f2) if f2 else None
                cfg = _pick(ms, srv)
            if cfg is not None:
                out.append((cfg, os.path.join(folder, ".mcp.json")))
                break
            why.append("the server %r is not in the files of the plugin %s (%s)" % (srv, key, folder))
    if not out:
        return [], "; ".join(why) or "the server was not found in a plugin"
    return out, ""


def server_configs(name, source, seg, cwd):
    """([(config dict, file)], why not) for the server named by Claude Code's `mcp_server`."""
    if source == "project":
        p = db._find_up(cwd, ".mcp.json")
        data, why = _read_json_why(p) if p else (None, "")
        if why:
            return [], why
        cfg = _pick(data.get("mcpServers"), name) if data else None
        return ([(cfg, p)], "") if cfg else ([], "the server %r is not in .mcp.json above %s" % (name, cwd))
    if source in ("local", "user"):
        path = claude_json_path()
        data, why = _read_json_why(path)
        if data is None:
            return [], why or "%s cannot be read (it does not exist)" % path
        if source == "user":
            cfg = _pick(data.get("mcpServers"), name)
            return ([(cfg, path)], "") if cfg else ([], "the server %r is not in mcpServers of %s" % (name, path))
        projects = data.get("projects") if isinstance(data.get("projects"), dict) else {}
        by_key = {_slash_key(k): v for k, v in projects.items() if isinstance(v, dict)}
        d = os.path.abspath(cwd or os.getcwd())
        for _ in range(20):
            ent = by_key.get(_slash_key(d))
            cfg = _pick(ent.get("mcpServers"), name) if ent else None
            if cfg:
                return [(cfg, path)], ""
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
        return [], "the server %r is not in projects[...].mcpServers of %s for %s or a folder above it" % (name, path, cwd)
    if source == "plugin":
        return _plugin_servers(name, seg, source)
    return [], "the source %r of the MCP server is not one this version reads" % (source or "(none)")


# ============================================================================================ the database behind the server

def _adapter_addr_re():
    """The addresses of the engines of adapters of the user (their schemes), or None."""
    schemes = [re.escape(x) for ad in engines.user_adapters() for x in ad.schemes]
    if not schemes:
        return None
    return re.compile(r"(?i)(?<![\w+.-])(?:%s)(?:\+\w+)?://[^\s'\"<>]+" % "|".join(schemes))


def _addr_candidates(cfg, cwd, label):
    """[Base | Unnamed | Unsupported] from the command, args and env of a stdio server."""
    strings = []
    if isinstance(cfg.get("command"), str):
        strings.append(("command", cfg["command"]))
    for a in cfg.get("args") if isinstance(cfg.get("args"), list) else []:
        if isinstance(a, str):
            strings.append(("arg", a))
    env_ = cfg.get("env") if isinstance(cfg.get("env"), dict) else {}
    for k, v in env_.items():
        if isinstance(v, str):
            strings.append((k, v))
    out = []
    adapter_rx = _adapter_addr_re()
    for key, s in strings:
        found = False
        for rx in (ADDR_RE, adapter_rx):
            for m in (rx.finditer(s) if rx is not None else ()):
                found = True
                out.append(db.parse_db_url(m.group(0).rstrip(",;"), cwd))
        if found:
            continue
        if re.search(r"\b(?:dbname|host)\s*=", s) and "=" in s and " " in s.strip():
            c = db.parse_conninfo(s)
            if c.get("dbname") or c.get("host"):
                out.append(db.pg_base(c.get("host", ""), c.get("port", ""), c.get("dbname", ""), c.get("user", ""), c.get("password", ""), "mcp"))
                continue
        if re.search(r"\$\{|\$\(|%\(", s) and key not in ("command", "arg") and DB_ENV_KEY.search(key):
            out.append(db.Unnamed("%s of the server %s holds a substitution that cannot be read" % (key, label)))
            continue
        tail = s.split("=", 1)[-1].strip().strip("\"'") if key == "arg" or "=" in s else s.strip().strip("\"'")
        owner = engines.for_ext(os.path.splitext(tail)[1]) if tail else None
        if owner is not None:                      # a file of an engine of an adapter of the user (named by the adapter, and only when it is proven)
            out.append(engines.name_for(owner.id, {"target": tail, "source": "mcp", "reasons": []}, cwd, dict(os.environ)))
        elif tail.lower().endswith(db.SQLITE_EXT):
            if re.search(r"[$%{}]", tail):
                out.append(db.Unnamed("the file name %r holds a substitution" % tail))
            elif not (os.path.isabs(tail) or re.match(r"^[A-Za-z]:[\\/]", tail)):
                out.append(db.Unnamed("the SQLite file %r of the server is a relative path: the folder it is relative to is not known" % tail))
            else:
                out.append(db.Base("sqlite", path=tail, source="mcp"))
    pg = {k: env_.get(k) for k in ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD") if isinstance(env_.get(k), str)}
    if pg.get("PGDATABASE") or pg.get("PGHOST"):
        out.append(db.pg_base(pg.get("PGHOST", ""), pg.get("PGPORT", ""), pg.get("PGDATABASE", ""), pg.get("PGUSER", ""),
                              pg.get("PGPASSWORD", ""), "mcp"))
    return out


def base_from_config(cfg, label, cwd):
    typ = str(cfg.get("type") or "").lower()
    if typ in REMOTE_TYPES or (cfg.get("url") and not cfg.get("command")):
        b = db.Base("postgres", remote=True, linked=True, source="mcp %s server" % (typ or "url"))
        b.shown = "MCP server %s (%s: the database behind it is remote, its address is not known)" % (label, typ or "url")
        return b
    cands = _addr_candidates(cfg, cwd, label)
    if not cands:
        args = cfg.get("args") if isinstance(cfg.get("args"), list) else []
        word = next((tok for tok in _tokens(label, str(cfg.get("command") or ""), *[a for a in args if isinstance(a, str)]) if tok in NOT_COPIED_WORDS), None)
        if word:                                   # a product of an engine the kit does not copy: named, not copied (a warning), not "cannot be named" (a refusal)
            return db._unsupported(word, cwd)
        return db.Unnamed("the settings of the MCP server %s name no database (no address and no SQLite file in its command, args or env)" % label)
    return db._one_of(cands)


def _tokens(*texts):
    out = []
    for t in texts:
        out += [x for x in re.split(r"[^a-z0-9]+", (t or "").lower()) if x]
    return out


def _db_word(tokens):
    for tok in tokens:
        if tok in NAME_WORDS or any(tok.startswith(p) for p in NAME_PREFIXES):
            return tok
    return None


def server_verdict(name, cfg, cwd, extra=""):
    """(is a server of a database, why in plain words, engine, remote) for a server of the settings (`cfg`: its entry, a dict; None when it
    was not found). The two signs of a server of a database: the settings give the address of a database, or a word of a database stands in the name,
    the command or the address."""
    engine, remote = "", False
    if isinstance(cfg, dict):
        typ = str(cfg.get("type") or "").lower()
        if typ in REMOTE_TYPES or (cfg.get("url") and not cfg.get("command")):
            remote = True
        try:
            cands = _addr_candidates(cfg, cwd, name)
        except Exception:
            cands = []
        if cands:
            c = cands[0]
            if isinstance(c, db.Unsupported):
                engine = c.engine
            elif isinstance(c, db.Base):
                engine = engines.title(c.engine)
                remote = remote or c.remote
            return True, "its settings give the address of a database", engine, remote
        parts = [str(cfg.get("command") or ""), str(cfg.get("url") or "")] + [a for a in (cfg.get("args") or []) if isinstance(a, str)]
    else:
        parts = []
    w = _db_word(_tokens(name, extra))
    where = "name"
    if not w:
        w, where = _db_word(_tokens(*parts)), "command or address"
    if w:
        return True, "the word %r stands in its %s" % (w, where), engine, remote
    return False, ("no database address in its settings and no database word in its name, command or address"
                   if isinstance(cfg, dict) else "its settings were not found and no database word stands in its name"), engine, remote


def is_db_server(name, source, seg, cwd):
    """(bool, why): is the MCP server of a call a server of a database? Its settings are read when they can be found."""
    try:
        cfgs, _why = server_configs(name, source, seg, cwd) if name else ([], "")
    except Exception:
        cfgs = []
    if not cfgs:
        v = server_verdict(name or seg, None, cwd, extra=seg)
        return v[0], v[1]
    first = None
    for cfg, _f in cfgs:
        v = server_verdict(name or seg, cfg, cwd, extra=seg)
        first = first or v
        if v[0]:
            return True, v[1]
    return False, first[1]


def name_mcp(action, cwd):
    """The database behind the MCP server of this call -> Base | Unnamed | Unsupported."""
    m = action.mcp or {}
    name, source, seg = m.get("name", ""), m.get("source", ""), m.get("segment", "")
    if not name:
        return db.Unnamed("the hook input does not name the MCP server (Claude Code older than 2.1.274 sends no `mcp_server`)")
    cfgs, why = server_configs(name, source, seg, cwd or action.cwd)
    if not cfgs:
        return db.Unnamed(why)
    results = [base_from_config(cfg, name, cwd or action.cwd) for cfg, _f in cfgs]
    base = db._one_of(results) if len(results) > 1 else results[0]
    dropped = (action.conn or {}).get("dropped_db")
    if isinstance(base, db.Base) and base.engine == "postgres" and dropped and not base.linked:
        base.dbname = dropped
        base.conninfo["PGDATABASE"] = dropped
    return base
