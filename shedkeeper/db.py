r"""db.py -- databases: recognise a destroying action, name the database, take a copy BEFORE it (0.3.0).

The invariant of the kit stays: a change does not run when its copy was not taken (I1). For a database the
copy is a dump (PostgreSQL: `pg_dump -Fc`, read back in full with `pg_restore`; SQLite: the backup API of the
python module, checked with `PRAGMA integrity_check`). Engines: PostgreSQL and SQLite. Any other engine NAMED by a tool
(Rails `mysql2`, Laravel `mysql`, Prisma `mysql` ...) is not copied: the command passes with a loud warning to the person and
to the agent (ini key on_db_unsupported, default allow; deny refuses) -- see the README, "Adding another database engine".
A call of an MCP tool is read by dbmcp.py and decided here the same way.

  db_actions(cmd, cwd, shell)       the destroying actions over databases in a command text -> [DbAction]
  name_base(action, cwd, env)       the database an action works on -> Base | Unnamed(reason)
  plan(...) / execute(...)          the decision of the hook (the table of decisions): refusals first, then copies
  copy_base(base, root, ...)        one verified copy -> {status ok|absent|toolarge|timeout|error, ...}
  find_fresh(...)                   a verified copy taken by `dbcopy` not longer ago than db_fresh_minutes
  find_pg_dump(settings)            the pg_dump of this machine (ini key, PATH, usual places) -> (path, version, major)

Recognition rides on the scanner of recognize.py (segments, `$( )`, `bash -c`, `cmd /c`, heredocs, `python -c`):
db_actions hands it an observer (`visit`), so the places where a command is EXECUTED are the same as for a deletion
and a mention (`grep "DROP TABLE" f`, `echo prisma migrate reset`) is not an action.

Recognised: SQL of the class DROP DATABASE/SCHEMA/TABLE/OWNED, TRUNCATE, DELETE without WHERE, UPDATE
without WHERE, ALTER TABLE ... DROP COLUMN / ALTER COLUMN ... TYPE (comments and string literals are cut out BEFORE
the words are looked for) given to `psql` (-c, -f, heredoc, here-string, `< file`, a pipe from cat/echo) or `sqlite3`;
`dropdb`; `pg_restore --clean -d`; the commands of tools (prisma, supabase, rails/rake, manage.py, alembic, artisan,
typeorm, drizzle-kit, sequelize, docker volumes) from a TABLE (TOOL_RULES) that shedkeeper.ini extends (db_command).
A SQL file or a pipe that cannot be read counts as destroying (the strict side is a COPY, not a refusal).
Named from: SHEDKEEPER_DB=... in front of the command (nothing else is read then); the command
(`-d -h -p -U`, an address, `dropdb NAME`, `DROP DATABASE NAME`, the file of `sqlite3`); the environment (PG*, and
DATABASE_URL for the tools); the project (.env, prisma/schema.prisma, supabase/config.toml, alembic.ini, Rails
config/database.yml without ERB, Laravel DB_*). Two different candidates, a `${...}` substitution, an incomplete
address -> cannot be named. No project code is ever run to learn an address (Django settings.py, drizzle.config.ts).
NOT seen (declared): destruction inside a program or script the command runs, SQL assembled at run time or taken
from a variable, roles and other objects of a cluster (not in pg_dump), Docker volumes were not tried on a machine
with Docker.

A password lives ONLY in Base.conninfo (the environment of the child process); everything printable, logged or
recorded carries `***` in its place (mask_secrets, Base.url_masked).
"""
import glob
import hashlib
import getpass
import json
import os
import pathlib
import re
import sqlite3
import subprocess
import time
from datetime import datetime

from . import advice, engines, recognize

SQL_FILE_CAP = 5 * 1024 * 1024          # a SQL file bigger than this is not read: it counts as unreadable (= destroying)
LOCAL_HOSTS = ("", "localhost", "127.0.0.1", "::1", "[::1]")
PG_SCHEMES = ("postgres", "postgresql", "pgsql", "postgis")
SQLITE_SCHEMES = ("sqlite", "sqlite3", "file")
SQLITE_EXT = (".db", ".sqlite", ".sqlite3", ".db3")
# engines that an address may name and this version does not copy (an address with another scheme is not an address of a database at all)
OTHER_DB_SCHEMES = ("mysql", "mysql2", "mariadb", "mongodb", "sqlserver", "mssql", "oracle", "cockroachdb", "clickhouse", "redis",
                    "rediss", "trilogy")
PROBE_TIMEOUT_S = 10
EXPLICIT_DEADLINE_S = 3600             # `dbcopy` (an explicit act) has no hook limit; this is only a sanity ceiling
DB_WORD = re.compile(
    r"(?<![\w.-])(?:psql|sqlite3|dropdb|pg_restore|prisma|supabase|rails|rake|manage\.py|django-admin|alembic|artisan|"
    r"typeorm[\w-]*|drizzle-kit|sequelize[\w-]*|docker|docker-compose|podman)(?![\w-])|\bdrop\s+(?:database|schema|table|owned)\b|\btruncate\b",
    re.I)


# ============================================================================================ small things

# `DB_PASSWORD=x`, `-e POSTGRES_PASSWORD=x`, `$env:DB_PASSWORD='x'`, `MYSQL_PWD="x"`, `API_TOKEN=x`, `--db-pass=x`: a name that holds PASS / PWD / SECRET / TOKEN.
# (`\bpassword` misses `DB_PASSWORD`: an underscore is a letter to the regex.) A name like `bypass` or `max_tokens` is masked too: a hidden value costs nothing.
_SECRET_NAMED = re.compile(r"(?i)(?<![A-Za-z0-9])([A-Za-z0-9_]*(?:pass|pwd|secret|token)[A-Za-z0-9_]*\s*=\s*)('(?:[^'\\]|\\.)*'|\"[^\"]*\"|[^\s'\";&|)]+)")


def _mask_named_variables(text):
    return _SECRET_NAMED.sub(r"\1***", text)


def mask_secrets(text):
    """A text with every password of an address, PGPASSWORD=..., `password=...` and the value of any variable named PASS / PWD / SECRET / TOKEN replaced by ***."""
    if not isinstance(text, str):
        return text
    text = re.sub(r"(?P<pre>[A-Za-z][\w+.-]*://[^\s:/@?#'\"]*):(?P<pw>[^\s'\"/]*)@", r"\g<pre>:***@", text)
    # (an unquoted value ends at a space, `;`, `&`, `|` or `)`: the shell ends it there, and masking a masked text again must change nothing -- the check of a
    # record of a copy masks it once more, and `PGPASSWORD=***;` was eaten whole the second time, which counted a clean record as a leak)
    text = re.sub(r"(?i)(PGPASSWORD\s*=\s*)(\"[^\"]*\"|'[^']*'|[^\s;&|)]+)", r"\1***", text)
    text = re.sub(r"(?i)(\bpassword\s*=\s*)('(?:[^'\\]|\\.)*'|\"[^\"]*\"|[^\s'\";&|)]+)", r"\1***", text)
    text = re.sub(r"(?i)(\bpassword\s+)('(?:[^'\\]|\\.)*'|\"[^\"]*\")", r"\1'***'", text)      # ALTER ROLE x PASSWORD 's' / WITH ENCRYPTED PASSWORD "s"
    text = _mask_named_variables(text)
    return text


def no_copy_notice(what, setting, why=""):
    """The line for the PERSON when something went ahead with NO copy because a setting says `allow` (the README promises it for every such setting)."""
    return ("ShedKeeper: NO COPY was taken of %s -- it went ahead because %s = allow%s. Set it back to deny to get a copy before such a change "
            "(`%s`)." % (mask_secrets(str(what)), setting, (" (" + mask_secrets(str(why))[:160] + ")") if why else "", advice.set_command(setting, "deny")))


def is_local_host(host):
    h = (host or "").strip().lower()
    return h in LOCAL_HOSTS or h.startswith("/") or bool(re.match(r"^[a-z]:[\\/]", h))


def pg_identity(host, port, dbname):
    """(host, port, database name). Every local spelling of the host (empty, localhost, 127.0.0.1, ::1, a socket
    folder) is ONE host -- the same server; a refinement of 'the host in lower case' that can only make more copies match."""
    h = "localhost" if is_local_host(host) else (host or "").strip().lower()
    return "pg://%s:%d/%s" % (h, int(port or 5432), dbname)


def sqlite_identity(path):
    return "sqlite:" + os.path.normcase(os.path.abspath(path)).replace("\\", "/")


class Base(object):
    """One database. `conninfo` (PG* variables of the child process) is the only place a password lives."""

    def __init__(self, engine, host="", port=0, dbname="", path="", user="", conninfo=None, remote=False,
                 linked=False, source="", shown="", ident="", label="", secrets=()):
        self.engine = engine
        self.host = host or ""
        self.port = int(port or 0)
        self.dbname = dbname or ""
        self.path = recognize.to_abs(path, os.getcwd()) if path else ""
        self.user = user or ""
        self.conninfo = dict(conninfo or {})
        self.remote = bool(remote)
        self.linked = bool(linked)         # a project linked to a hosted service (supabase --linked): no address known
        self.source = source
        self.shown = shown                 # a text for the person instead of the address (the database behind an MCP server)
        self.ident = ident                 # the identity of a database of an engine that is not built in (adapters): never a password in it
        self.label_ = label                # the short name of such a database
        # every secret of the connection: the framework refuses to write a record that carries one (copy_base)
        self.secrets = [x for x in list(secrets) + [self.conninfo.get("PGPASSWORD")] if isinstance(x, str) and len(x) >= 4]

    @property
    def identity(self):
        if self.linked:
            return ""
        if self.ident:
            return self.ident
        if self.engine == "sqlite":
            return sqlite_identity(self.path)
        return pg_identity(self.host, self.port or 5432, self.dbname)

    @property
    def label(self):
        if self.label_:
            return self.label_
        return os.path.basename(self.path) if self.engine == "sqlite" else (self.dbname or "?")

    @property
    def url_masked(self):
        if self.shown:
            return self.shown
        if self.linked:
            return "(a hosted project, address unknown)"
        if self.engine == "sqlite":
            return self.path.replace("\\", "/")
        if self.engine != "postgres":
            return "a %s database %s" % (engines.title(self.engine), self.label)
        user = (self.user + ("@" if self.user else "")) if not self.conninfo.get("PGPASSWORD") else (self.user + ":***@")
        host = self.host or "localhost"
        if ":" in host and not host.startswith("["):
            host = "[%s]" % host
        return "postgresql://%s%s:%d/%s" % (user, host, self.port or 5432, self.dbname)

    def __repr__(self):
        return "Base(%s %s%s)" % (self.engine, self.url_masked, " remote" if self.remote else "")


class Unnamed(object):
    def __init__(self, reason):
        self.reason = reason

    def __repr__(self):
        return "Unnamed(%s)" % self.reason


class Unsupported(Unnamed):
    """The database IS named (by a tool, an address, a server) but its engine is not PostgreSQL or SQLite: nothing can be copied
    (decision: the hook lets the command pass with a warning, or refuses with on_db_unsupported = deny)."""

    def __init__(self, engine, reason=""):
        Unnamed.__init__(self, reason or "the engine %r is not supported by this version (PostgreSQL and SQLite are)" % engine)
        self.engine = engine
        self.adapter = ""                 # the id of an adapter of the user that is installed for this engine but not proven (or broken)
        self.identity = "unsupported:" + str(engine).lower()
        self.url_masked = "a %s database" % engine
        self.label = str(engine)

    def __repr__(self):
        return "Unsupported(%s)" % self.engine


class DbAction(object):
    """One destroying action over ONE database (a command that destroys two databases gives two actions)."""

    def __init__(self, kind, head, reasons, cwd, env, **kw):
        self.kind = kind                  # sql | dropdb | restore | tool | unreadable | mcp | adapter
        self.head = head
        self.reasons = list(reasons)      # words for the person: "DROP TABLE", "DELETE without WHERE" ...
        self.cwd = cwd
        self.env = dict(env)              # assigned in front of the command / earlier in it
        self.engine = kw.get("engine")    # postgres | sqlite | None (decided by the address for tools)
        self.conn = kw.get("conn") or {}  # dbname, host, port, user taken from the command
        self.sqlite_file = kw.get("sqlite_file")
        self.words = kw.get("words") or []    # the normalised words of a tool command
        self.tool = kw.get("tool", "")
        self.unnamed = kw.get("unnamed")      # a reason the database cannot be named, known already while reading the command
        self.raw_db = kw.get("raw_db")        # the address/name given as a plain word (dropdb NAME, psql NAME)
        self.mcp = kw.get("mcp")              # kind 'mcp': {name, source, tool, segment} of the server (dbmcp.py)
        self.finding = kw.get("finding")      # kind 'adapter': the finding an adapter of the user made (engines.py)
        self.cwd_unsure = False               # the command went to another folder first (`cd "$X"`), which was not read: a relative path may point somewhere else
        self.container = bool(kw.get("container"))    # read inside `docker exec` and the like: ShedKeeper takes no copy there (`unnamed` says so)

    def __repr__(self):
        return "DbAction(%s %s %s)" % (self.kind, self.head, self.reasons)


# ============================================================================================ SQL

_DOLLAR = re.compile(r"\$(?:[A-Za-z_\u0080-\uffff][\w\u0080-\uffff]*)?\$")


_SPECIAL = re.compile(r"--|/\*|'|\"|\$")
_STR_ESC = re.compile(r"[\\']")


_AFTER_DO = re.compile(r"(?i)(?:^|[\s;(])do\s+(?:language\s+(?:\"[^\"]*\"|\w+)\s+)?$")


def strip_sql(text, do_bodies=None):
    """(text without comments and string literals, [quoted identifiers]). `--` and `/* */` (nested) go; a string
    ('..' with '' inside, E'..' with a backslash, $$..$$, $tag$..$tag$) becomes ''; a quoted identifier becomes "qN" so a
    `;` or a keyword inside it cannot be read as SQL (its text is kept in the list). Jumps from one special place to the
    next (a dump of megabytes is read in well under a second). `do_bodies` (a list) receives the text of every dollar-quoted string
    that follows the word DO (the body of a DO block is code that RUNS, not a string: sql_findings reads it)."""
    out, idents, i, n = [], [], 0, len(text)
    while i < n:
        m = _SPECIAL.search(text, i)
        if not m:
            out.append(text[i:])
            break
        if m.start() > i:
            out.append(text[i:m.start()])
            i = m.start()
        ch = text[i]
        if ch == "-":                                   # `--`
            j = text.find("\n", i)
            i = n if j < 0 else j
            out.append(" ")
            continue
        if ch == "/":                                   # `/*`
            depth, j = 1, i + 2
            while j < n and depth:
                k = min([x for x in (text.find("/*", j), text.find("*/", j)) if x >= 0] or [n])
                if k >= n:
                    j = n
                    break
                if text.startswith("/*", k):
                    depth += 1
                else:
                    depth -= 1
                j = k + 2
            out.append(" ")
            i = j
            continue
        if ch == "'":
            esc = i > 0 and text[i - 1] in "eE" and (i == 1 or not (text[i - 2].isalnum() or text[i - 2] == "_"))
            j = i + 1
            while j < n:
                if esc:
                    k = _STR_ESC.search(text, j)
                    k = k.start() if k else -1
                else:
                    k = text.find("'", j)
                if k < 0:
                    j = n
                    break
                if text[k] == "\\":
                    j = k + 2
                    continue
                if text.startswith("''", k):
                    j = k + 2
                    continue
                j = k
                break
            out.append("''")
            i = j + 1
            continue
        if ch == '"':
            j = i + 1
            while j < n:
                k = text.find('"', j)
                if k < 0:
                    j = n
                    break
                if text.startswith('""', k):
                    j = k + 2
                    continue
                j = k
                break
            idents.append(text[i + 1:j].replace('""', '"'))
            out.append('"q%d"' % (len(idents) - 1))
            i = j + 1
            continue
        if ch == "$" and not (i > 0 and (text[i - 1].isalnum() or text[i - 1] == "_")):
            dm = _DOLLAR.match(text, i)
            if dm:
                tag = dm.group(0)
                end = text.find(tag, dm.end())
                if do_bodies is not None and _AFTER_DO.search("".join(out[-40:])[-80:]):
                    do_bodies.append(text[dm.end():(n if end < 0 else end)])
                out.append("''")
                i = n if end < 0 else end + len(tag)
                continue
        out.append(ch)
        i += 1
    return "".join(out), idents


def _top_where(tail):
    """Is there a WHERE at the depth of the statement itself (not inside a sub-select)? A `)` that closes more than
    was opened (the end of a CTE) ends the look."""
    depth = 0
    for m in re.finditer(r"[()]|[A-Za-z_]\w*", tail):
        w = m.group(0)
        if w == "(":
            depth += 1
        elif w == ")":
            depth -= 1
            if depth < 0:
                return False
        elif depth == 0 and w.lower() == "where":
            return True
    return False


def _ident_name(token, idents):
    m = re.fullmatch(r'"q(\d+)"', token)
    if m:
        return idents[int(m.group(1))]
    return token.strip('"').lower()


_PLPGSQL_BREAKS = re.compile(r"(?i)\b(?:begin|declare|loop|exception|then|else|elsif)\b")
_EXEC_LITERAL = re.compile(
    r"(?is)\bexecute\s*(?:format\s*\(\s*)?(?:[eE])?(?:'((?:[^']|'')*)'|(\$(?:[A-Za-z_\u0080-\uffff][\w\u0080-\uffff]*)?\$)(.*?)\2)")
DO_NOTE = " (in a DO block)"


def _do_findings(body, depth):
    """The destroying statements inside the body of a DO block: the body is plpgsql, so its block words (BEGIN, THEN, LOOP ...) are cut
    to statement ends and the statements are read like any SQL; a literal after EXECUTE (also inside format( )) that starts as SQL
    is read too (the strict side: a copy)."""
    found = []
    if depth < 3:
        for m in _EXEC_LITERAL.finditer(body):
            lit = m.group(3) if m.group(2) else (m.group(1) or "").replace("''", "'")
            f, _meta = sql_findings(lit, depth + 1)
            found += f
        inner = []
        clean, _ids = strip_sql(body, inner)
        f, _meta = sql_findings(_PLPGSQL_BREAKS.sub(";", clean), depth + 1)
        found += f
        for inner_body in inner:                    # a DO block inside a DO block (through EXECUTE or not)
            found += _do_findings(inner_body, depth + 1)
    return [(r if r == "DROP DATABASE" or r.endswith(DO_NOTE) else r + DO_NOTE, d) for r, d in found]


def sql_findings(text, _depth=0):
    r"""([(reason, dropped database or None)], [meta]) for a SQL text. `meta` names what the text does that this reader
    cannot follow: 'reconnect' (\c, \connect, .open: the database may change), 'include' (\i ...: another file),
    'shell' (\!, .shell), 'gexec' (generated statements). The body of `DO $$ ... $$` is code that runs and is read as SQL
    (see _do_findings); the body of `CREATE FUNCTION ... AS $$ ... $$` is only a definition and is not."""
    do_bodies = []
    clean, idents = strip_sql(text, do_bodies)
    meta, body = [], []
    for line in clean.split("\n"):
        ls = line.lstrip()
        if ls.startswith("\\"):
            cmd = re.match(r"\\([A-Za-z!]+)", ls)
            c = cmd.group(1).lower() if cmd else "!"
            if c in ("c", "connect"):
                meta.append("reconnect")
            elif c in ("i", "ir", "include", "include_relative"):
                meta.append("include")
            elif c == "!":
                meta.append("shell")
            elif c in ("gexec", "g", "gx"):
                meta.append("gexec")
            continue
        if re.match(r"\.(open|read|shell|system)\b", ls, re.I):
            c = ls[1:].split()[0].lower()
            meta.append({"open": "reconnect", "read": "include"}.get(c, "shell"))
            continue
        body.append(line)
    found = []
    for stmt in "\n".join(body).split(";"):
        s = " ".join(stmt.split())
        if not s:
            continue
        m = re.match(r"(?i)drop\s+(database|schema|table|owned)\b", s)
        if m:
            kind = m.group(1).upper()
            if kind == "DATABASE":
                nm = re.match(r"(?i)drop\s+database\s+(?:if\s+exists\s+)?(\"q\d+\"|[^\s;(]+)", s)
                found.append(("DROP DATABASE", _ident_name(nm.group(1), idents) if nm else None))
            else:
                found.append(("DROP " + kind, None))
            continue
        if re.match(r"(?i)truncate\b", s):
            found.append(("TRUNCATE", None))
            continue
        for m in re.finditer(r"(?i)\bdelete\s+from\b", s):
            if not _top_where(s[m.end():]):
                found.append(("DELETE without WHERE", None))
        for m in re.finditer(r"(?i)\bupdate\s+(?:only\s+)?[\w.$\"]+(?:\s+(?:as\s+)?\w+)?\s+set\b", s):
            if not _top_where(s[m.end():]):
                found.append(("UPDATE without WHERE", None))
        if re.match(r"(?i)alter\s+table\b", s):
            if re.search(r"(?i)\bdrop\s+column\b", s) or re.search(
                    r"(?i)\bdrop\s+(?:if\s+exists\s+)?(?!(?:constraint|default|not|identity|expression|column)\b)[\w\"]+", s):
                found.append(("ALTER TABLE DROP COLUMN", None))
            if re.search(r"(?i)\balter\s+(?:column\s+)?[\w\"]+\s+(?:set\s+data\s+)?type\b", s):
                found.append(("ALTER COLUMN TYPE", None))
    for body_text in do_bodies:
        found += _do_findings(body_text, _depth)
    return found, meta


# ============================================================================================ tools

# The command names the kit reads itself -- the clients and the tools of its table, and the launchers it strips (`npx prisma ...`): an adapter of
# the user may not list one of them as a head (engines._validate), and the kit's own table is asked first in any case.
KIT_HEADS = frozenset((
    "psql", "dropdb", "pg_restore", "sqlite3", "prisma", "supabase", "rails", "rake", "manage", "manage.py", "django-admin", "django-admin.py",
    "alembic", "artisan", "drizzle-kit", "docker", "podman", "docker-compose", "npx", "bunx", "pnpx", "pnpm", "yarn", "npm", "bun", "bundle",
    "php", "ruby", "node", "nodejs", "deno", "poetry", "pipenv", "uv", "pipx", "rye"))


def is_kit_head(head):
    """Is this command name one the kit reads itself (see KIT_HEADS; typeorm*, sequelize* and every python launcher too)?"""
    h = _bname(head or "")
    return h in KIT_HEADS or h.startswith("typeorm") or h.startswith("sequelize") or bool(recognize.PYTHONS.match(h))


def _bname(tok):
    b = re.split(r"[\\/]", tok)[-1].lower()
    for ext in (".exe", ".cmd", ".bat", ".ps1", ".js", ".mjs"):
        if b.endswith(ext):
            b = b[:-len(ext)]
    return b


def _skip_opts(words):
    i = 0
    while i < len(words) and words[i].startswith("-") and words[i] != "-m":
        i += 1
    return words[i:]


def normalise_words(head, args):
    """The command of a tool without its launchers: `npx prisma migrate reset`, `pnpm exec`, `yarn`, `bundle exec rails`,
    `python manage.py`, `python -m alembic`, `php artisan`, `node .../prisma` -> [tool, args...] (tool = lower-case base name)."""
    w = [head] + list(args)
    for _ in range(5):
        if not w:
            return []
        h = _bname(w[0])
        if h in ("npx", "bunx", "pnpx"):
            w = _skip_opts(w[1:])
            continue
        if h in ("pnpm", "yarn", "npm", "bun") and len(w) > 1:
            rest = _skip_opts(w[1:])
            if rest and rest[0] in ("exec", "dlx", "x", "run", "run-script"):
                rest = rest[1:]
            w = _skip_opts(rest)
            continue
        if h == "bundle" and len(w) > 1 and w[1] == "exec":
            w = w[2:]
            continue
        if recognize.PYTHONS.match(h):
            rest = _skip_opts(w[1:])
            if rest and rest[0] == "-m":
                w = rest[1:]
                continue
            if rest and rest[0] == "-c":
                return []
            w = rest
            continue
        if h in ("php", "ruby", "node", "nodejs", "deno") and len(w) > 1:
            w = _skip_opts(w[1:])
            continue
        if h in ("poetry", "pipenv", "uv", "pipx", "rye") and "run" in w[1:3]:
            w = _skip_opts(w[w.index("run") + 1:])
            continue
        break
    return [_bname(w[0])] + w[1:] if w else []


def _sub_and_flags(w):
    sub = [a for a in w[1:] if not a.startswith("-")]
    flags = set()
    for a in w[1:]:
        if a.startswith("--"):
            flags.add(a.split("=", 1)[0].lower())
        elif a.startswith("-") and len(a) > 1:
            flags.add(a.lower())
            flags.update("-" + c for c in a[1:] if c.isalpha())
    return sub, flags


RAILS_TASKS = re.compile(r"^db:(?:drop(?::\w+)*|reset|schema:load|migrate:reset|purge)$", re.I)


def tool_rule(w):
    """The name of the destroying command of a tool, or None. `w` = normalise_words(...)."""
    if not w:
        return None
    t = w[0]
    sub, flags = _sub_and_flags(w)
    if t == "prisma":
        if _has_seq(sub, ["migrate", "reset"]):
            return "prisma migrate reset"
        if _has_seq(sub, ["db", "push"]) and flags & {"--force-reset", "--accept-data-loss"}:
            return "prisma db push --force-reset"
    elif t == "supabase":
        if _has_seq(sub, ["db", "reset"]):
            return "supabase db reset"
    elif t in ("rails", "rake"):
        for a in w[1:]:
            if RAILS_TASKS.match(a):
                return "%s %s" % (t, a.lower())
    elif t in ("manage", "manage.py", "django-admin", "django-admin.py"):
        if "flush" in sub[:2]:
            return "manage.py flush"
        if "migrate" in sub[:2] and "zero" in sub:
            return "manage.py migrate zero"
    elif t == "alembic":
        if "downgrade" in sub:
            return "alembic downgrade"
    elif t == "artisan":
        if sub[:1] and sub[0] in ("migrate:fresh", "migrate:refresh", "migrate:reset", "db:wipe"):
            return "artisan " + sub[0]
    elif t.startswith("typeorm"):
        if "schema:drop" in sub:
            return "typeorm schema:drop"
    elif t == "drizzle-kit":
        if sub[:1] and sub[0].startswith("push"):
            return "drizzle-kit push"
    elif t.startswith("sequelize"):
        if "db:drop" in sub:
            return "sequelize db:drop"
    elif t in ("docker", "podman"):
        for i, a in enumerate(sub[:3]):
            if a == "volume" and i + 1 < len(sub) and sub[i + 1] in ("rm", "remove", "prune"):
                return "docker volume " + sub[i + 1]
            if a == "compose" and "down" in sub[i + 1:] and _has_volumes_flag(flags):
                return "docker compose down -v"
    elif t == "docker-compose":
        if "down" in sub and _has_volumes_flag(flags):
            return "docker-compose down -v"
    return None


def _has_seq(words, seq):
    """Do `seq` words stand next to each other somewhere in `words` (a flag with a value may stand in front of them)."""
    n = len(seq)
    return any(words[i:i + n] == seq for i in range(len(words) - n + 1))


def _has_volumes_flag(flags):
    return "--volumes" in flags or "-v" in flags


def parse_extra_rule(line):
    """One `db_command` line of the ini -> (words, flags) or None. Format: `word word word [| --flag --flag]`: the words are
    the first words of the command (launchers are stripped, the tool by its base name); with `|` at least one of the flags
    must be there too."""
    left, _bar, right = line.partition("|")
    words = left.split()
    if not words:
        return None
    return [_bname(words[0])] + words[1:], {f.lower() for f in right.split()}


def extra_rule(w, rules):
    sub, flags = _sub_and_flags(w)
    for line in rules or ():
        parsed = parse_extra_rule(line)
        if not parsed:
            continue
        words, need = parsed
        if w[0] != words[0] or sub[:len(words) - 1] != words[1:]:
            continue
        if need and not (flags & need):
            continue
        return " ".join(words)
    return None


# ============================================================================================ reading a command

def _getopt(args, short_arg, long_arg):
    """GNU-like: ([(name, value or None)], [positional]). `-Atc SQL` = A, t, c with the value; `--command=SQL`; `--` ends options."""
    opts, pos, i = [], [], 0
    while i < len(args):
        a = args[i]
        if a == "--":
            pos += args[i + 1:]
            break
        if a.startswith("--") and len(a) > 2:
            name, eq, val = a[2:].partition("=")
            if name in long_arg:
                if eq:
                    opts.append((name, val))
                else:
                    opts.append((name, args[i + 1] if i + 1 < len(args) else ""))
                    i += 1
            else:
                opts.append((name, None))
        elif a.startswith("-") and len(a) > 1:
            j = 1
            while j < len(a):
                c = a[j]
                if c in short_arg:
                    val = a[j + 1:]
                    if not val:
                        val = args[i + 1] if i + 1 < len(args) else ""
                        i += 1
                    opts.append((c, val))
                    break
                opts.append((c, None))
                j += 1
        else:
            pos.append(a)
        i += 1
    return opts, pos


PSQL_SHORT = set("cdfFhLopPRTUv")
PSQL_LONG = {"command", "dbname", "file", "field-separator", "host", "log-file", "output", "port", "pset",
             "record-separator", "table-attr", "username", "set", "variable"}
DROPDB_SHORT = set("hpU")
DROPDB_LONG = {"host", "port", "username", "maintenance-db"}
RESTORE_SHORT = set("dfFhIjLnNpPStTU")
RESTORE_LONG = {"dbname", "file", "format", "host", "index", "jobs", "use-list", "schema", "exclude-schema", "port",
                "superuser", "table", "trigger", "username", "role", "section", "filter"}
SQLITE_OPT_ARG = {"init", "cmd", "separator", "newline", "nullvalue", "vfs", "mmap", "lookaside", "pagecache", "maxsize",
                  "nonce"}


def _conn_from(opts):
    conn = {}
    for name, val in opts:
        if name in ("d", "dbname") and val is not None:
            conn["dbname"] = val
        elif name in ("h", "host"):
            conn["host"] = val
        elif name in ("p", "port"):
            conn["port"] = val
        elif name in ("U", "username"):
            conn["user"] = val
    return conn


def _read_sql_file(word, cwd):
    """(text, why not). A file given as a literal path; read as UTF-8 with replacement; at most SQL_FILE_CAP."""
    if not word or re.search(r"[$%`*?{}<>|]", word):
        return None, "the SQL file %r is not a literal path" % word
    path = recognize.to_abs(word, cwd)
    try:
        if not os.path.isfile(path):
            return None, "the SQL file %s does not exist (this command may be making it)" % word
        if os.path.getsize(path) > SQL_FILE_CAP:
            return None, "the SQL file %s is bigger than %d MB" % (word, SQL_FILE_CAP // 1048576)
        with open(path, "rb") as fh:
            return fh.read().decode("utf-8", "replace"), None
    except OSError as e:
        return None, "the SQL file %s cannot be read (%s)" % (word, e.__class__.__name__)


def _stdin_sql(ctx, args):
    """(text, why not, has stdin): the SQL a client reads from standard input -- here-string, heredoc, `< file`, a pipe."""
    if "<<<" in args:
        j = args.index("<<<")
        return (args[j + 1] if j + 1 < len(args) else ""), None, True
    if ctx.get("bodies"):
        return "\n".join(ctx["bodies"]), None, True
    m = re.search(r"(?<![<\d])<(?![<(])\s*(\"[^\"]+\"|'[^']+'|[^\s;|&<>()]+)", ctx.get("piece", ""))
    if m:
        txt, why = _read_sql_file(m.group(1).strip("\"'"), ctx["cwd"])
        return txt, why, True
    if ctx.get("sep") == "|":
        prev = recognize._strip_redirects(recognize._words(ctx.get("prev", ""), ctx["tool"] == "PowerShell"))
        k = recognize._peel(prev)
        ph = recognize._basename(prev[k]) if k >= 0 else ""
        pa = prev[k + 1:] if k >= 0 else []
        if ph in ("cat", "type", "get-content", "gc"):
            texts = []
            for f in [a for a in pa if not a.startswith("-")]:
                txt, why = _read_sql_file(f, ctx["cwd"])
                if why:
                    return None, why, True
                texts.append(txt)
            return "\n".join(texts), None, True
        if ph in ("echo", "printf", "write-output", "write-host"):
            return " ".join(a for a in pa if not (a.startswith("-") and ph != "printf")), None, True
        return None, "the SQL comes through a pipe from %s" % (ph or "a command that cannot be read"), True
    return "", None, False


CONTAINER_WHY = "the database is inside a container, and ShedKeeper takes no copy there"
_CONTAINER_HEADS = ("docker", "podman", "docker-compose", "podman-compose")
_CONTAINER_GLOBAL_VALUE = {"-H", "--host", "-c", "--context", "-f", "--file", "-p", "--project-name", "--profile", "--env-file", "--project-directory",
                           "--log-level", "-l", "--config", "--parallel", "--ansi"}
_CONTAINER_EXEC_VALUE = {"-e", "--env", "-u", "--user", "-w", "--workdir", "--env-file", "--detach-keys", "--index"}


def container_inner(head, args):
    """The command that runs INSIDE a container: `docker exec [flags] CONTAINER CMD...`, `docker container exec ...`, `docker compose [flags] exec [flags]
    SERVICE CMD...`, `docker-compose exec ...`, `podman exec ...` -> [CMD, args...]; None when this is not an `exec` or no command follows."""
    if _bname(head) not in _CONTAINER_HEADS:
        return None
    n, i = len(args), 0
    while i < n:
        a = args[i]
        if a.startswith("-"):
            i += 2 if (a in _CONTAINER_GLOBAL_VALUE and "=" not in a) else 1
        elif a in ("compose", "container"):
            i += 1
        else:
            break
    if i >= n or args[i] != "exec":
        return None
    i += 1
    while i < n:
        a = args[i]
        if a == "--":
            i += 1
            break
        if a.startswith("-"):
            i += 2 if (a in _CONTAINER_EXEC_VALUE and "=" not in a) else 1
            continue
        break
    inner = args[i + 1:]               # args[i] is the container (the service)
    return inner or None


class _Acc(recognize.Deletion):
    """The observer handed to the scanner of recognize.py: collects DbActions while the scanner walks the command."""

    def __init__(self, cwd, command):
        recognize.Deletion.__init__(self)
        self.cwd = cwd
        self.command = command
        self.env = {}                 # variables assigned earlier in the command (upper-case names)
        self.actions = []
        self.extra_rules = ()
        self._cdepth = 0              # how many `docker exec` are being read inside one another

    def _unreadable(self, why, env, cwd):
        self.actions.append(DbAction("unreadable", "", [why], cwd, env, unnamed=why))

    def visit(self, head, args, pre, ctx):
        n0 = len(self.actions)
        try:
            self._visit(head, args, pre, ctx)
            if not ctx.get("cwd_sure", True):
                for a in self.actions[n0:]:
                    a.cwd_unsure = True
        except Exception as e:      # a fault of this reader on something that looks like a database command: a refusal, not a pass
            if DB_WORD.search(ctx.get("piece", "") + " " + head):
                self._unreadable("the command could not be read (%s: %s)" % (e.__class__.__name__, e), self.env, ctx["cwd"])

    def _visit(self, head, args, pre, ctx):
        cwd = ctx["cwd"]
        piece = ctx.get("piece", "")
        m = re.match(r"^\s*\$env:(\w+)\s*=\s*(.*?)\s*$", piece, re.I)
        if m:
            self.env[m.group(1).upper()] = m.group(2).strip("\"'")
            return
        local = {}
        for t in pre:
            a = re.match(r"^([A-Za-z_]\w*)=(.*)$", t, re.S)
            if a:
                local[a.group(1).upper()] = a.group(2)
        if not head:                    # `NAME=value` on its own: the shell variable the next words may use
            self.env.update(local)
            return
        if head in ("export", "set") and args:
            for t in args:
                a = re.match(r"^([A-Za-z_]\w*)=(.*)$", t, re.S)
                if a:
                    self.env[a.group(1).upper()] = a.group(2)
            return
        env = dict(self.env)
        env.update(local)
        if head == "psql":
            self._psql(args, env, ctx)
        elif head == "sqlite3":
            self._sqlite(args, env, ctx)
        elif head == "dropdb":
            self._dropdb(args, env, ctx)
        elif head == "pg_restore":
            self._restore(args, env, ctx)
        else:
            inner = container_inner(head, args)
            if inner is not None:
                self._container(inner, local, ctx)
                return
            w = normalise_words(head, args)
            name = (tool_rule(w) or extra_rule(w, self.extra_rules)) if w else None
            if name:                     # what the kit itself reads comes FIRST: an adapter of the user never takes a command the kit knows
                self.actions.append(DbAction("tool", head, [name], cwd, env, words=w, tool=name))
                return
            ad = engines.for_head(head)
            if ad is not None and not is_kit_head(head):
                self._adapter(ad, head, args, env, ctx)

    def _container(self, inner, local, ctx):
        """What runs inside a container (`docker exec db psql ...`, `... sh -c "psql ..."`) is read like the same command on this machine, and every
        action found there is marked: the database is in the container, ShedKeeper cannot copy it, so it cannot be NAMED (a refusal), unless the
        person names it through the port the container publishes (SHEDKEEPER_DB). The words in front of the command (`SHEDKEEPER_DB=... docker exec`) count."""
        n0 = len(self.actions)
        ih, ia = recognize._basename(inner[0]), inner[1:]
        if self._cdepth >= recognize.MAX_NEST_DEPTH:
            self._unreadable("a command inside a container that is nested too deeply to read", self.env, ctx["cwd"])
            return
        self._cdepth += 1
        try:
            for text2, kind in recognize._nested(ih, ia):
                if kind != "python":
                    recognize._walk(text2, "PowerShell" if kind == "pwsh" else "Bash", 1, self, ctx["cwd"])
            self._visit(ih, ia, [], ctx)
        finally:
            self._cdepth -= 1
        for a in self.actions[n0:]:
            a.env = dict(local, **a.env)
            a.unnamed = a.unnamed or CONTAINER_WHY
            a.container = True

    def _adapter(self, ad, head, args, env, ctx):
        """A command whose head an adapter of the user reads. Its findings are protection only for a PROVEN adapter; for any other the finding
        only gives the warning (the engine is treated as not supported). A fault of a proven adapter is an unreadable command (the strict
        side); a fault of an unproven one is silence."""
        try:
            findings = ad.recognize([head] + list(args), engines.RecognizeCtx(ctx, env)) or []
            clean = []
            for f in findings:
                reasons = [r for r in (f.get("reasons") or []) if isinstance(r, str) and r]
                if not reasons:
                    continue
                clean.append(dict(f, reasons=reasons))
        except Exception as e:
            if engines.is_proven(ad) or engines.proofs_problem(ad=ad):          # (proofs that cannot be read: whether this adapter protects is not known)
                self._unreadable("the adapter %s could not read the command (%s: %s)" % (ad.id, e.__class__.__name__, mask_secrets(str(e))[:120]),
                                 env, ctx["cwd"])
            return
        for f in clean:
            self.actions.append(DbAction("adapter", head, f["reasons"], ctx["cwd"], env, engine=ad.id, finding=f, unnamed=f.get("unnamed")))

    # ---- psql / sqlite3 share the way of getting SQL
    def _collect(self, sqls, files, args, ctx, stdin_wanted):
        texts, why = list(sqls), []
        for f in files:
            if f == "-":
                stdin_wanted = True
                continue
            t, w = _read_sql_file(f, ctx["cwd"])
            if w:
                why.append(w)
            else:
                texts.append(t)
        if stdin_wanted:
            t, w, has = _stdin_sql(ctx, args)
            if w:
                why.append(w)
            elif has:
                texts.append(t)
        return texts, why

    def _emit(self, kind, head, texts, why, ctx, env, make_conn):
        reasons, drops, reconnect = sql_reasons(texts, why)
        said = _SQL_SAID
        if reasons:
            self.actions.append(DbAction(kind, head, reasons, ctx["cwd"], env, unnamed=said["reconnect"] if reconnect else None,
                                         **make_conn()))
        for dropped in dict.fromkeys(drops):
            kw = make_conn()
            kw["conn"] = dict(kw.get("conn") or {}, dropped_db=dropped)
            self.actions.append(DbAction(kind, head, ["DROP DATABASE " + dropped], ctx["cwd"], env,
                                         unnamed=said["reconnect"] if reconnect else None, **kw))

    def _psql(self, args, env, ctx):
        opts, pos = _getopt(args, PSQL_SHORT, PSQL_LONG)
        if any(n in ("V", "version", "?", "help", "l", "list") for n, _v in opts) and not any(n in ("c", "command", "f", "file") for n, _v in opts):
            return
        sqls = [v for n, v in opts if n in ("c", "command") and v is not None]
        files = [v for n, v in opts if n in ("f", "file") and v is not None]
        texts, why = self._collect(sqls, files, args, ctx, stdin_wanted=not sqls and not files)
        conn = _conn_from(opts)
        if "dbname" not in conn and pos:
            conn["dbname"], pos = pos[0], pos[1:]
        if "user" not in conn and pos:
            conn["user"] = pos[0]               # psql DBNAME USERNAME
        self._emit("sql", "psql", texts, why, ctx, env, lambda: {"engine": "postgres", "conn": dict(conn)})

    def _sqlite(self, args, env, ctx):
        i, pos, sqls = 0, [], []
        while i < len(args):
            a = args[i]
            if a.startswith("-") and len(a) > 1 and not (pos and a == "-"):
                name = a.lstrip("-")
                if name in SQLITE_OPT_ARG:
                    if name == "cmd" and i + 1 < len(args):
                        sqls.append(args[i + 1])
                    i += 2
                    continue
                i += 1
                continue
            pos.append(a)
            i += 1
        if not pos or pos[0] == ":memory:" or pos[0].startswith("file::memory:"):
            return                                  # no file = a database in memory: nothing stays behind
        dbfile, sqls = pos[0], sqls + pos[1:]
        texts, why = self._collect(sqls, [], args, ctx, stdin_wanted=not sqls)
        self._emit("sql", "sqlite3", texts, why, ctx, env, lambda: {"engine": "sqlite", "sqlite_file": dbfile})

    def _dropdb(self, args, env, ctx):
        opts, pos = _getopt(args, DROPDB_SHORT, DROPDB_LONG)
        if any(n in ("V", "version", "?", "help") for n, _v in opts):
            return
        conn = _conn_from(opts)
        self.actions.append(DbAction("dropdb", "dropdb", ["dropdb"], ctx["cwd"], env, engine="postgres",
                                     conn=dict(conn, dropped_db=pos[0]) if pos else conn,
                                     unnamed=None if pos else "dropdb names no database"))

    def _restore(self, args, env, ctx):
        opts, pos = _getopt(args, RESTORE_SHORT, RESTORE_LONG)
        names = {n for n, _v in opts}
        if not (names & {"c", "clean"}):
            return
        conn = _conn_from(opts)
        if not conn.get("dbname"):
            return                                  # no -d: pg_restore writes a script, it changes no database
        unnamed = "pg_restore --clean --create drops the database named inside the archive" if names & {"C", "create"} else None
        self.actions.append(DbAction("restore", "pg_restore", ["pg_restore --clean"], ctx["cwd"], env, engine="postgres",
                                     conn=conn, unnamed=unnamed))


_SQL_SAID = {"reconnect": "the SQL switches database (\\c, .open)", "include": "the SQL includes another file",
             "shell": "the SQL runs a shell command", "gexec": "the SQL executes generated statements"}


def sql_reasons(texts, why=()):
    """(reasons, names of databases a DROP DATABASE drops, does the SQL switch database) of the texts a client is given. A text that cannot
    be read to the end adds ONE reason (the strict side: a copy is taken, not a refusal)."""
    found, meta = sql_findings(";\n".join(texts))
    reasons = list(dict.fromkeys(r for r, dropped in found if not dropped))
    drops = [dropped for r, dropped in found if dropped]
    unreadable = list(why)
    for mt in dict.fromkeys(meta):
        unreadable.append(_SQL_SAID[mt])
    if any(r == "DROP DATABASE" and not d for r, d in found):
        unreadable.append("a DROP DATABASE whose name cannot be read")
    if unreadable:
        reasons.append("SQL that cannot be read to the end: " + "; ".join(unreadable))
    return reasons, drops, "reconnect" in meta


def db_actions(cmd, cwd, shell="bash", extra_rules=()):
    """The destroying actions over databases in the text of a Bash/PowerShell command -> [DbAction]. A text the scanner
    cannot parse (too long, too deep) that has a database word in it gives an 'unreadable' action (the strict side)."""
    if not isinstance(cmd, str) or not cmd.strip():
        return []
    cwd = cwd or os.getcwd()
    tool = "PowerShell" if str(shell).lower() in ("powershell", "pwsh", "ps") else "Bash"
    acc = _Acc(cwd, cmd)
    acc.extra_rules = extra_rules
    try:
        recognize._walk(cmd, tool, 0, acc, cwd)
    except (RecursionError, recognize._Unparsed, ValueError, IndexError):
        acc.coarse = True
    if acc.coarse and DB_WORD.search(cmd):
        acc.actions.append(DbAction("unreadable", "", ["a command that is too long or too deeply nested to read"], cwd, acc.env,
                                    unnamed="the command is too long or too deeply nested to read"))
    return acc.actions


# ============================================================================================ naming (M3)

def load_proofs(roots_for):
    """The proofs of the adapters of the user are looked for in the folders of the store, from now on: called BEFORE a command is read (the
    reader asks whether an adapter is proven when it faults) and again by `plan`. Nothing to do when the user has no adapter."""
    if engines.load():
        try:
            engines.use_proofs(roots_for(None))
        except Exception as e:
            engines.use_proofs([], fault="the folders of the store could not be listed (%s: %s)" % (e.__class__.__name__, mask_secrets(str(e))[:120]))


def _lookup_factory(action, env):
    eff = {str(k).upper(): v for k, v in (env or {}).items()}
    eff.update(action.env)

    def look(name):
        return eff.get(name.upper())
    return eff, look


_VAR = re.compile(r"\$\{(\w+)\}|\$env:(\w+)|\$(\w+)|%(\w+)%", re.I)


def expand_vars(text, look):
    """(text with variables replaced, [names that could not be found])."""
    missing = []

    def rep(m):
        name = next(g for g in m.groups() if g)
        v = look(name)
        if v is None:
            missing.append(name)
            return m.group(0)
        return v
    return _VAR.sub(rep, text), missing


def read_dotenv(path):
    out = {}
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:].lstrip()
                key, sep, val = line.partition("=")
                if not sep:
                    continue
                val = val.strip()
                if val[:1] in ("'", '"'):
                    end = val.find(val[0], 1)
                    val = val[1:end] if end > 0 else val[1:]
                else:
                    val = re.split(r"\s+#", val, 1)[0].strip()
                out[key.strip().upper()] = val
    except OSError:
        return {}
    return out


def _ancestors(cwd, limit=12):
    """cwd and its parents up to the project root (the first folder with .git, or the drive root)."""
    d = os.path.abspath(cwd or os.getcwd())
    for _ in range(limit):
        yield d
        if os.path.exists(os.path.join(d, ".git")):
            return
        parent = os.path.dirname(d)
        if parent == d:
            return
        d = parent


def _find_up(cwd, *rel):
    for d in _ancestors(cwd):
        p = os.path.join(d, *rel)
        if os.path.isfile(p):
            return p
    return None


def _dotenv_for(cwd, extra_dirs=()):
    for d in list(extra_dirs) + list(_ancestors(cwd)):
        p = os.path.join(d, ".env")
        if os.path.isfile(p):
            return read_dotenv(p)
    return {}


_URL_RE = re.compile(r"^(?P<scheme>[A-Za-z][\w+.-]*):(?P<rest>.*)$", re.S)


def parse_db_url(url, base_dir):
    """An address -> Base | Unnamed. postgres(ql)://user:pw@host:port/db?params, sqlalchemy `postgresql+psycopg2://`,
    sqlite:///rel, sqlite:////abs, file:./dev.db. Another engine -> Unnamed("... not supported")."""
    url = (url or "").strip()
    m = _URL_RE.match(url)
    if not m:
        return Unnamed("%r is not an address" % mask_secrets(url)[:80])
    if re.search(r"\$\{|\$\(|%\(", url):
        return Unnamed("the address holds a substitution (${...}, $(...), %(...)) that cannot be read")
    scheme = m.group("scheme").lower().split("+")[0]
    rest = m.group("rest")
    if scheme == "jdbc":                  # jdbc:postgresql://h/db, jdbc:mysql://h/db -- the address after the prefix is the address
        return parse_db_url(rest, base_dir)
    if scheme in SQLITE_SCHEMES:
        p = rest.split("?", 1)[0]
        if scheme == "file":
            if p.startswith("//"):
                p = p[2:]
                if re.match(r"^/[A-Za-z]:", p):
                    p = p[1:]
        elif p.startswith("//"):
            p = p[2:]                       # sqlite:///rel -> /rel, sqlite:////abs -> //abs: one slash less is the path
            p = p[1:] if p.startswith("/") else p
        if not p or p == ":memory:":
            return Unnamed("a database in memory has nothing to copy")
        if re.search(r"[$%{}]", p):
            return Unnamed("the file name %r holds a substitution" % p)
        return Base("sqlite", path=recognize.to_abs(p, base_dir), source="url")
    if scheme not in PG_SCHEMES:
        ad = engines.for_scheme(scheme)
        if ad is not None:
            return engines.name_for(ad.id, {"target": url, "source": "url", "reasons": []}, base_dir, dict(os.environ))
        if scheme in OTHER_DB_SCHEMES:
            return _broken_or_unsupported(scheme)
        return Unnamed("the address has the scheme %r, which is not one of a database this version knows" % scheme)
    if not rest.startswith("//"):
        return Unnamed("the address is incomplete")
    am = re.match(r"^([^/?#]*)(.*)$", rest[2:], re.S)
    authority, tail = am.group(1), am.group(2)
    userinfo, _at, hostport = authority.rpartition("@")
    user, _c, pw = userinfo.partition(":")
    host, port = hostport, ""
    if hostport.startswith("["):
        close = hostport.find("]")
        host = hostport[1:close] if close > 0 else hostport[1:]
        port = hostport[close + 2:] if hostport[close + 1:close + 2] == ":" else ""
    elif ":" in hostport:
        host, _c, port = hostport.rpartition(":")
    path, _q, query = tail.partition("?")
    query = query.split("#", 1)[0]
    db = path.lstrip("/").split("#", 1)[0]
    qs = {}
    for kv in query.split("&"):
        k, _e, v = kv.partition("=")
        if k:
            qs[k.lower()] = _unquote(v)
    host, user, pw, db = _unquote(host), _unquote(user), _unquote(pw), _unquote(db)
    host = host or qs.get("host", "")
    user = user or qs.get("user", "")
    pw = pw or qs.get("password", "")
    db = db or qs.get("dbname", "")
    port = port or qs.get("port", "")
    return pg_base(host, port, db, user, pw, source="url")


def _unquote(s):
    from urllib.parse import unquote
    return unquote(s or "")


def pg_base(host, port, dbname, user, pw, source=""):
    """A PostgreSQL Base from its parts, with the libpq defaults (user = the login name, database = the user name)."""
    if pw == "***":
        pw = ""                       # the placeholder of a masked address: the password comes from pgpass / PGPASSWORD of the caller
    if "," in (host or ""):
        return Unnamed("several hosts in one address")
    if re.search(r"[${}]|%\w+%", "%s %s %s %s" % (host, port, dbname, user)):
        return Unnamed("the address holds a substitution that cannot be read")
    try:
        port_n = int(port) if str(port or "").strip() else 5432
    except ValueError:
        return Unnamed("the port %r is not a number" % port)
    if not user:
        try:
            user = getpass.getuser()
        except Exception:
            user = ""
    dbname = dbname or user
    if not dbname:
        return Unnamed("the database name is not in the address and the user name is unknown")
    ci = {"PGPORT": str(port_n), "PGDATABASE": dbname}
    if host:
        ci["PGHOST"] = host
    if user:
        ci["PGUSER"] = user
    if pw:
        ci["PGPASSWORD"] = pw
    return Base("postgres", host=host, port=port_n, dbname=dbname, user=user, conninfo=ci,
                remote=not is_local_host(host), source=source)


def parse_conninfo(text):
    """`host=h port=5 dbname=d user=u password='x y'` -> dict (lower-case keys)."""
    out = {}
    for m in re.finditer(r"(\w+)\s*=\s*('(?:[^'\\]|\\.)*'|\S*)", text):
        v = m.group(2)
        if v.startswith("'"):
            v = re.sub(r"\\(.)", r"\1", v[1:-1])
        out[m.group(1).lower()] = v
    return out


def target_from_spec(spec, cwd):
    """The word of SHEDKEEPER_DB (an address, a conninfo string, or a file) -> Base | Unnamed."""
    spec = (spec or "").strip().strip("\"'")
    if not spec:
        return Unnamed("SHEDKEEPER_DB is empty")
    scheme_m = re.match(r"^([A-Za-z][\w+.-]+):", spec)           # two letters or more: `C:` is a drive, not a scheme
    owner = engines.for_scheme(scheme_m.group(1).split("+")[0]) if scheme_m else engines.for_ext(os.path.splitext(spec)[1])
    if owner is not None:
        return engines.name_for(owner.id, {"target": spec, "source": "SHEDKEEPER_DB", "reasons": []}, cwd, dict(os.environ))
    if re.match(r"^[A-Za-z][\w+.-]*://", spec) or re.match(r"^(?:sqlite|file):", spec, re.I):
        return parse_db_url(spec, cwd)
    if re.search(r"\b(?:host|dbname|port|user)\s*=", spec):
        c = parse_conninfo(spec)
        return pg_base(c.get("host", ""), c.get("port", ""), c.get("dbname", ""), c.get("user", ""), c.get("password", ""),
                       "conninfo")
    if re.search(r"[$%{}]", spec):
        return Unnamed("SHEDKEEPER_DB holds a substitution that cannot be read")
    path = recognize.to_abs(spec, cwd)
    if os.path.isfile(path) or spec.lower().endswith(SQLITE_EXT):
        return Base("sqlite", path=path, source="SHEDKEEPER_DB")
    return Unnamed("SHEDKEEPER_DB=%r is neither an address nor a file of a SQLite database" % mask_secrets(spec)[:80])


def _service_decides(look, c, arg):
    """PGSERVICE / PGSERVICEFILE is in the environment and the command does not name the host itself (-h / --host, `host=` of a conninfo, an address postgres://): the
    connection comes from the service file, which the kit does not read -- and which its own pg_dump does not get (`_child_env`), so a copy of a LOCAL database of that name
    would be taken. No database named at all is the same."""
    if not (look("PGSERVICE") or look("PGSERVICEFILE")):
        return False
    if not arg:
        return True
    if c.get("host") or re.match(r"^postgres(?:ql)?://", arg, re.I) or re.search(r"\bhost\s*=", arg):
        return False
    return True


def _from_command(action, look):
    """PostgreSQL address from -d/-h/-p/-U, positionals, PG* of the environment (psql, dropdb, pg_restore)."""
    c = dict(action.conn)
    for k in list(c):
        if isinstance(c[k], str):
            c[k], missing = expand_vars(c[k], look)
            if missing:
                return Unnamed("%s comes from the variable $%s, which has no value here" % (k, missing[0]))
    dropped = c.get("dropped_db")
    arg = c.get("dbname")
    pw = look("PGPASSWORD") or ""
    if _service_decides(look, c, arg):
        return Unnamed("the connection comes from PGSERVICE or PGSERVICEFILE (a service file), and the command does not name the host")
    if arg and (re.match(r"^postgres(?:ql)?://", arg, re.I)):
        b = parse_db_url(arg, action.cwd)
        if isinstance(b, Base) and dropped:
            b.dbname, b.conninfo["PGDATABASE"] = dropped, dropped
        return b
    host, port, user = c.get("host"), c.get("port"), c.get("user")
    if arg and re.search(r"\b(?:host|dbname|port|user|service)\s*=", arg):
        ci = parse_conninfo(arg)
        if "service" in ci:
            return Unnamed("the connection comes from a service file")
        host, port, user, pw = ci.get("host", host), ci.get("port", port), ci.get("user", user), ci.get("password", pw)
        arg = ci.get("dbname")
    host = host if host is not None else (look("PGHOST") or "")
    port = port if port is not None else (look("PGPORT") or "")
    user = user if user is not None else (look("PGUSER") or "")
    db = dropped or arg or look("PGDATABASE") or ""
    return pg_base(host, port, db, user, pw, "command")


# ---- tool sources

def _broken_or_unsupported(word):
    """The engine is named but nothing here copies it. An adapter file of that name that failed to load is said so in the reason."""
    broke = engines.broken_claims(word)
    if broke:
        u = Unsupported(word, "the adapter %s failed to load (%s): the engine %r is not supported until it is fixed" % (
            os.path.basename(broke[0].path), broke[0].error, word))
        u.adapter = word
        return u
    return Unsupported(word)


def _unsupported(engine, cwd="", url=None, parts=None):
    """A tool (Rails, Laravel, Prisma ...) names an engine that is not PostgreSQL or SQLite: an adapter of the user that claims the word
    names the database (from the address, or from the parts the tool gave); else the engine is not supported."""
    ad = engines.for_tool_name(engine)
    if ad is not None:
        return engines.name_for(ad.id, {"target": url, "parts": parts or {}, "source": "tool", "tool_engine": engine, "reasons": []},
                                cwd or os.getcwd(), dict(os.environ))
    return _broken_or_unsupported(engine)


def _from_prisma(action, look, cwd):
    w = action.words
    schema = None
    for i, a in enumerate(w):
        if a == "--schema" and i + 1 < len(w):
            schema = recognize.to_abs(w[i + 1], cwd)
        elif a.startswith("--schema="):
            schema = recognize.to_abs(a.split("=", 1)[1], cwd)
    if schema is None:
        for d in _ancestors(cwd):
            for rel in (("prisma", "schema.prisma"), ("schema.prisma",)):
                if os.path.isfile(os.path.join(d, *rel)):
                    schema = os.path.join(d, *rel)
                    break
            if schema:
                break
    if not schema or not os.path.isfile(schema):
        return None                                           # no source of its own: the generic ones are tried
    try:
        with open(schema, encoding="utf-8", errors="replace") as fh:
            text = re.sub(r'("(?:[^"\\]|\\.)*")|//[^\n]*', lambda m: m.group(1) or "", fh.read())
    except OSError as e:
        return Unnamed("the schema %s cannot be read (%s)" % (schema, e.__class__.__name__))
    blk = re.search(r"datasource\s+\w+\s*\{(.*?)\}", text, re.S)
    if not blk:
        return Unnamed("no datasource in %s" % schema)
    body = blk.group(1)
    prov = re.search(r'provider\s*=\s*"([^"]+)"', body)
    urlm = re.search(r'\burl\s*=\s*(?:env\(\s*"([^"]+)"\s*\)|"([^"]*)")', body)
    if not prov or not urlm:
        return Unnamed("the datasource of %s has no readable provider/url" % schema)
    provider = prov.group(1).lower()
    sdir = os.path.dirname(schema)
    if urlm.group(1):
        name = urlm.group(1)
        val = look(name)
        if val is None:
            val = _dotenv_for(cwd, [os.path.join(sdir), os.path.dirname(sdir), cwd]).get(name.upper())
        if val is None:
            return Unnamed("%s of %s has no value in the environment or .env" % (name, schema))
    else:
        val = urlm.group(2)
    if provider == "sqlite":
        return parse_db_url(val if val.lower().startswith("file:") else "file:" + val, sdir)
    if provider in ("postgresql", "postgres"):
        return parse_db_url(val, sdir)
    return _unsupported(provider, sdir, val)


def _from_supabase(action, look, cwd):
    w = action.words
    for i, a in enumerate(w):
        if a == "--db-url" and i + 1 < len(w):
            return parse_db_url(w[i + 1], cwd)
        if a.startswith("--db-url="):
            return parse_db_url(a.split("=", 1)[1], cwd)
    if "--linked" in w:
        return Base("postgres", remote=True, linked=True, source="supabase --linked")
    cfg = _find_up(cwd, "supabase", "config.toml")
    if not cfg:
        return Unnamed("no supabase/config.toml found above %s" % cwd)
    port, section = 54322, ""
    try:
        with open(cfg, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if line.startswith("["):
                    section = line.strip("[] ")
                elif section == "db":
                    m = re.match(r"port\s*=\s*(\d+)", line)
                    if m:
                        port = int(m.group(1))
    except OSError as e:
        return Unnamed("%s cannot be read (%s)" % (cfg, e.__class__.__name__))
    return pg_base("127.0.0.1", port, "postgres", "postgres", "postgres", "supabase config.toml")


def _from_alembic(action, look, cwd):
    w = action.words
    ini = None
    for i, a in enumerate(w):
        if a in ("-c", "--config") and i + 1 < len(w):
            ini = recognize.to_abs(w[i + 1], cwd)
        elif a.startswith("--config="):
            ini = recognize.to_abs(a.split("=", 1)[1], cwd)
    if ini is None:
        ini = _find_up(cwd, "alembic.ini")
    if not ini or not os.path.isfile(ini):
        return None
    try:
        with open(ini, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as e:
        return Unnamed("%s cannot be read (%s)" % (ini, e.__class__.__name__))
    m = re.search(r"(?mi)^\s*sqlalchemy\.url\s*=\s*(\S.*?)\s*$", text)
    if not m:
        return None
    url = m.group(1)
    if "%(" in url:
        return Unnamed("sqlalchemy.url of %s holds a %%(...) substitution" % ini)
    if url.lower().startswith("driver://"):
        return Unnamed("sqlalchemy.url of %s is the template address (the real one is set elsewhere)" % ini)
    return parse_db_url(url, cwd)


class _Sec(dict):
    """The keys of one section of database.yml; `subs` names the sub-sections it holds (`primary:`, `animals:` of a Rails with several databases)."""

    def __init__(self):
        dict.__init__(self)
        self.subs = []


def _yaml_sections(path):
    """A very small reader of config/database.yml: {section: {key: value}}, anchors (&a) and merges (<<: *a) of one level. A key with no value
    followed by deeper lines is a sub-section: it is named in `.subs` of the section (several of them = several databases)."""
    secs, anchors, cur, merges = {}, {}, None, {}
    pending = None                                  # (section, key, indent) of a `key:` with no value: a sub-section when the next line is deeper
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            indent = len(line) - len(line.lstrip())
            body = line.strip()
            if pending is not None:
                if indent > pending[2] and pending[0] in secs:
                    secs[pending[0]].subs.append(pending[1])
                pending = None
            if indent == 0:
                m = re.match(r"^([\w-]+):\s*(&(\w+))?\s*(#.*)?$", body)
                if m:
                    cur = m.group(1)
                    secs[cur] = _Sec()
                    if m.group(3):
                        anchors[m.group(3)] = cur
                else:
                    cur = None
            elif cur is not None:
                m = re.match(r"^<<:\s*\*(\w+)", body)
                if m:
                    merges.setdefault(cur, []).append(m.group(1))
                    continue
                m = re.match(r"^([\w-]+):\s*(.*?)\s*$", body)
                if m and indent <= 4:
                    val = re.split(r"\s+#", m.group(2), 1)[0].strip().strip("\"'")
                    secs[cur][m.group(1)] = val
                    if not val:
                        pending = (cur, m.group(1), indent)
    for sec, names in merges.items():
        for n in names:
            src = secs.get(anchors.get(n, ""), {})
            for k, v in src.items():
                secs[sec].setdefault(k, v)
    return secs


def _from_rails(action, look, cwd):
    cfg = _find_up(cwd, "config", "database.yml")
    if not cfg:
        return None
    rails_root = os.path.dirname(os.path.dirname(cfg))
    envname = look("RAILS_ENV") or look("RACK_ENV") or "development"
    try:
        secs = _yaml_sections(cfg)
    except OSError as e:
        return Unnamed("%s cannot be read (%s)" % (cfg, e.__class__.__name__))
    sec = secs.get(envname)
    if sec is None:
        return Unnamed("%s has no section %r" % (cfg, envname))
    if len(getattr(sec, "subs", ())) >= 2:
        return Unnamed("the section %r of %s holds several databases (%s): a command that drops or resets destroys all of them, and this version "
                       "names ONE database" % (envname, cfg, ", ".join(sec.subs)))
    if any("<%" in v for v in sec.values()):
        return Unnamed("the section %r of %s holds ERB (<% %>), which is not evaluated" % (envname, cfg))
    if sec.get("url"):
        return parse_db_url(sec["url"], rails_root)
    adapter = (sec.get("adapter") or "").lower()
    if adapter in ("sqlite3", "sqlite"):
        db = sec.get("database")
        return Base("sqlite", path=recognize.to_abs(db, rails_root), source="database.yml") if db else Unnamed("no database in %s" % cfg)
    if adapter in ("postgresql", "postgis", "pg"):
        return pg_base(sec.get("host", ""), sec.get("port", ""), sec.get("database", ""), sec.get("username", ""),
                       sec.get("password", ""), "database.yml")
    return _unsupported(adapter or "?", rails_root, sec.get("url") or None,
                        {"host": sec.get("host", ""), "port": sec.get("port", ""), "database": sec.get("database", ""),
                         "user": sec.get("username", ""), "password": sec.get("password", "")})


def _from_laravel(action, look, cwd):
    art = _find_up(cwd, "artisan")
    root = os.path.dirname(art) if art else cwd
    env = _dotenv_for(cwd, [root])

    def get(name):
        v = look(name)
        return v if v is not None else env.get(name)
    url = get("DB_URL") or get("DATABASE_URL")
    if url:
        return parse_db_url(url, root)
    conn = (get("DB_CONNECTION") or "").lower()
    if not conn:
        return None
    if conn == "sqlite":
        db = get("DB_DATABASE")
        if not db:
            return Base("sqlite", path=os.path.join(root, "database", "database.sqlite"), source=".env")
        if not os.path.isabs(db) and not re.match(r"^[A-Za-z]:", db):
            return Unnamed("DB_DATABASE=%r is a relative path: the folder it is relative to is not known" % db)
        return Base("sqlite", path=db, source=".env")
    if conn in ("pgsql", "postgres", "postgresql"):
        return pg_base(get("DB_HOST") or "", get("DB_PORT") or "", get("DB_DATABASE") or "", get("DB_USERNAME") or "",
                       get("DB_PASSWORD") or "", ".env")
    return _unsupported(conn, root, None, {"host": get("DB_HOST") or "", "port": get("DB_PORT") or "", "database": get("DB_DATABASE") or "",
                                           "user": get("DB_USERNAME") or "", "password": get("DB_PASSWORD") or ""})


def _from_database_url(action, look, cwd, with_dotenv=True):
    val = look("DATABASE_URL")
    if val is None and with_dotenv:
        val = _dotenv_for(cwd).get("DATABASE_URL")
    if val is None:
        return None
    return parse_db_url(val, cwd)


def _name_tool(action, look, cwd):
    t = action.words[0] if action.words else ""
    cands = []
    if t == "prisma":
        b = _from_prisma(action, look, cwd)
        if b is not None:
            return b
    elif t == "supabase":
        return _from_supabase(action, look, cwd)
    elif t == "alembic":
        b = _from_alembic(action, look, cwd)
        if b is not None:
            return b
    elif t in ("rails", "rake"):
        y = _from_rails(action, look, cwd)
        e = _from_database_url(action, look, cwd, with_dotenv=False)
        cands = [c for c in (y, e) if c is not None]
        if cands:
            return _one_of(cands)
    elif t == "artisan":
        b = _from_laravel(action, look, cwd)
        if b is not None:
            return b
    b = _from_database_url(action, look, cwd)
    if b is not None:
        return b
    return Unnamed("%s: no address in the command, the environment or the project files that this version reads" % action.tool)


class Several(Unnamed):
    """Candidates of ONE destroying action that are not the same database: one database the kit can copy, and one or more of an engine it
    cannot. `items` are them. The plan copies the one it can and warns about the others (the engine that is not copied); a reader that does
    not know this class sees an Unnamed, which is a refusal -- the strict side."""

    def __init__(self, items):
        Unnamed.__init__(self, "candidates of different engines: " + " and ".join(getattr(c, "url_masked", "?") for c in items))
        self.items = list(items)


def _one_of(cands):
    """One candidate -> it; the same database twice -> it; one database the kit can copy beside others it cannot (an unsupported engine) -> Several
    (copy the one, warn about the others); two different ones the kit can copy, or any not understood -> cannot be named."""
    for c in cands:
        if isinstance(c, Unnamed) and not isinstance(c, Unsupported):
            return c
    unsup = [c for c in cands if isinstance(c, Unsupported)]
    sup = [c for c in cands if not isinstance(c, Unsupported)]
    if unsup and not sup:
        same = {c.engine for c in unsup}
        return unsup[0] if len(same) == 1 else Unnamed("two different engines are candidates: " + " and ".join(c.url_masked for c in unsup))
    ids = {c.identity for c in sup}
    if len(ids) > 1:
        return Unnamed("two different databases are candidates: " + " and ".join(c.url_masked for c in sup))
    if unsup:
        seen_eng, items = set(), [sup[0]]
        for c in unsup:
            if c.engine not in seen_eng:
                seen_eng.add(c.engine)
                items.append(c)
        return Several(items)
    return sup[0]


def name_base(action, cwd, env):
    """The database an action works on -> Base | Unnamed(reason). `env` = the environment of the hook; the variables assigned
    in the command (action.env) win over it. Never raises: a fault is an Unnamed (the hook refuses)."""
    try:
        return _name_base(action, action.cwd or cwd, env)
    except Exception as e:
        return Unnamed("the database could not be named (%s: %s)" % (e.__class__.__name__, e))


def _name_base(action, cwd, env):
    spec = action.env.get("SHEDKEEPER_DB")
    if action.unnamed and not (spec is not None and action.kind != "unreadable"):
        return Unnamed(action.unnamed)            # (the person's own word names the database for a command that switches database or names none)
    eff, look = _lookup_factory(action, env)
    if spec is not None:
        return target_from_spec(spec, cwd)
    if action.kind == "mcp":
        from . import dbmcp
        return dbmcp.name_mcp(action, cwd)
    if action.kind == "tool":
        return _name_tool(action, look, cwd)
    if action.kind == "adapter":
        return engines.name_for(action.engine, dict(action.finding or {}), cwd, eff)
    if action.engine == "sqlite":
        f, missing = expand_vars(action.sqlite_file or "", look)
        if missing or re.search(r"[$%`*?{}]", f):
            return Unnamed("the file of the database comes from a variable that has no value here")
        return Base("sqlite", path=recognize.to_abs(f, cwd), source="command")
    return _from_command(action, look)


# ============================================================================================ copy (M4)

def _pg_major(path):
    nums = re.findall(r"(\d+)", os.path.basename(os.path.dirname(os.path.dirname(path))) or "")
    return int(nums[-1]) if nums else 0


_DEADLINE = [None]          # the monotonic time the copy being made must end by (set by copy_base): every probe of it is cut to what is left


def _cap(timeout):
    """`timeout` cut to the time left of the copy being made (at least 1 s), so the probes of one copy never add up to more than its deadline."""
    if _DEADLINE[0] is None:
        return timeout
    return max(1.0, min(timeout, _DEADLINE[0] - time.monotonic()))


def find_pg_dump(settings, limit=None):
    """(path, version text, major) of the pg_dump to use; (None, why, 0) when there is none. Order: the ini key
    pg_dump_path, PATH, the usual places (the newest). Not cached: the hook lives for one command. `limit`: the seconds the
    `--version` probe may take (default PROBE_TIMEOUT_S, and never more than what is left of the copy being made)."""
    import shutil
    cand = (settings.get("pg_dump_path") or "").strip()
    path = None
    if cand:
        if os.path.isdir(cand):
            for n in ("pg_dump.exe", "pg_dump"):
                if os.path.isfile(os.path.join(cand, n)):
                    cand = os.path.join(cand, n)
                    break
        if not os.path.isfile(cand):
            return None, "pg_dump_path = %s is not a file" % cand, 0
        path = cand
    if path is None:
        path = shutil.which("pg_dump")
    if path is None:
        pats = []
        for env in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
            if os.environ.get(env):
                pats.append(os.path.join(os.environ[env], "PostgreSQL", "*", "bin", "pg_dump.exe"))
        pats += ["/opt/homebrew/opt/postgresql@*/bin/pg_dump", "/usr/local/opt/postgresql@*/bin/pg_dump",
                 "/Applications/Postgres.app/Contents/Versions/*/bin/pg_dump", "/usr/lib/postgresql/*/bin/pg_dump"]
        hits = []
        for p in pats:
            hits += glob.glob(p)
        if hits:
            path = sorted(hits, key=_pg_major)[-1]
    if path is None:
        return None, "pg_dump was not found (not in shedkeeper.ini pg_dump_path, not in PATH, not in the usual places)", 0
    rc, out, err, timed = _run([path, "--version"], dict(os.environ), _cap(PROBE_TIMEOUT_S if limit is None else limit))
    if rc != 0:
        return None, "%s --version failed (%s)" % (path, "timeout" if timed else (err or out).strip()[:120]), 0
    m = re.search(r"(\d+)(?:\.(\d+))?", out)
    return path, out.strip(), int(m.group(1)) if m else 0


def _run(cmd, env, timeout):
    """(returncode, stdout, stderr, timed_out). The child never reads the keyboard; on a timeout it is killed."""
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as e:
        return None, "", "%s: %s" % (e.__class__.__name__, e), False
    try:
        out, err = p.communicate(timeout=max(0.5, timeout))
    except subprocess.TimeoutExpired:
        p.kill()
        try:
            p.communicate(timeout=5)
        except Exception:
            pass
        return None, "", "", True
    return p.returncode, _dec(out), _dec(err), False


def _run_input(cmd, env, timeout, data):
    """Like _run, with `data` (bytes) on the standard input of the child."""
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as e:
        return None, "", "%s: %s" % (e.__class__.__name__, e), False
    try:
        out, err = p.communicate(input=data, timeout=max(0.5, timeout))
    except subprocess.TimeoutExpired:
        p.kill()
        try:
            p.communicate(timeout=5)
        except Exception:
            pass
        return None, "", "", True
    return p.returncode, _dec(out), _dec(err), False


def _dec(data):
    """What a tool printed -> text. UTF-8 when it is UTF-8, else (psql's own messages in the language of the machine) the
    ANSI code page of Windows, line by line; never U+FFFD for a letter the machine can read."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    enc = "mbcs" if os.name == "nt" else "latin-1"
    out = []
    for line in data.split(b"\n"):
        try:
            out.append(line.decode("utf-8"))
        except UnicodeDecodeError:
            try:
                out.append(line.decode(enc))
            except (UnicodeDecodeError, LookupError):
                out.append(line.decode("utf-8", "replace"))
    return "\n".join(out)


def _sibling(pgdump, name):
    d = os.path.dirname(pgdump)
    for n in (name + ".exe", name):
        if os.path.isfile(os.path.join(d, n)):
            return os.path.join(d, n)
    return None


def _child_env(base, timeout):
    env = dict(os.environ)
    for k in ("PGHOST", "PGHOSTADDR", "PGPORT", "PGUSER", "PGDATABASE", "PGPASSWORD", "PGSERVICE"):
        env.pop(k, None)
    env.update(base.conninfo)
    env["PGCONNECT_TIMEOUT"] = str(max(1, int(min(PROBE_TIMEOUT_S, timeout))))
    return env


def measure_size(base, settings, pgdump, timeout):
    """('ok', bytes, server major) | ('absent', 0, 0) | ('error', 0, why). SQLite: the size of the file (and its -wal).
    PostgreSQL: pg_database_size through psql found next to pg_dump; a database that cannot be connected to is 'absent' only
    when the list of databases of the server (read through a maintenance database) proves that it is not there -- a refusal of
    the server for another reason (no right, no server, wrong password) is an 'error'."""
    if base.engine == "sqlite":
        if not os.path.isfile(base.path):
            return ("absent", 0, 0) if not os.path.exists(base.path) else ("error", 0, "%s is not a file" % base.path)
        size = os.path.getsize(base.path)
        for ext in ("-wal",):
            if os.path.isfile(base.path + ext):
                size += os.path.getsize(base.path + ext)
        return "ok", size, 0
    psql = _sibling(pgdump, "psql")
    if not psql:
        return "error", 0, "psql was not found next to %s (needed to measure the size)" % pgdump
    q = "select pg_database_size(current_database()), current_setting('server_version_num')"
    argv = [psql, "-X", "-w", "-At", "-v", "ON_ERROR_STOP=1", "-c", q]
    rc, out, err, timed = _run(argv, _child_env(base, _cap(timeout)), _cap(timeout))
    if timed:
        return "error", 0, "the size query did not finish in time"
    if rc == 0:
        m = re.match(r"\s*(\d+)\|(\d+)", out)
        if not m:
            return "error", 0, "unexpected answer of psql: %r" % out[:80]
        return "ok", int(m.group(1)), int(m.group(2)) // 10000
    first = mask_secrets(err.strip())[:300] or "psql exit %s" % rc
    lit = base.dbname.replace("'", "''")
    for maint in ("postgres", "template1"):
        if maint == base.dbname:
            continue
        env = _child_env(base, timeout)
        env["PGDATABASE"] = maint
        rc2, out2, err2, timed2 = _run([psql, "-X", "-w", "-At", "-c", "select count(*) from pg_database where datname = '%s'" % lit],
                                       env, _cap(timeout))
        if rc2 == 0 and out2.strip() == "0":
            return "absent", 0, 0
        if rc2 == 0:
            break
    return "error", 0, first


def _reserve(dirpath, stem, ext):
    """A free file name stem[_n]ext made with O_EXCL (nothing is replaced). -> path."""
    os.makedirs(dirpath, exist_ok=True)
    for n in range(0, 1000):
        cand = os.path.join(dirpath, "%s%s%s" % (stem, ("_%d" % n) if n else "", ext))
        try:
            fd = os.open(cand, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0))
        except FileExistsError:
            continue
        os.close(fd)
        return cand
    raise OSError("no free name for a copy in %s" % dirpath)


def _drop_own(path):
    """OUR partial copy (made a moment ago, never a copy that was verified, never the person's file)."""
    try:
        os.unlink(path)
    except OSError:
        pass


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_name(s):
    return re.sub(r"[^\w.-]", "_", s)[:60] or "db"


def index_path(root):
    return os.path.join(root, "dbcopies.jsonl")


def copy_base(base, root, settings, deadline_s, enforce_limits=True, session="", via="hook", command="", mcp_server="", now=None):
    """One verified copy of a database (the probes of it never run past `deadline_s`: see _cap) -- see _copy_base."""
    _DEADLINE[0] = time.monotonic() + (deadline_s if deadline_s else EXPLICIT_DEADLINE_S)
    try:
        return _copy_base(base, root, settings, deadline_s, enforce_limits, session, via, command, mcp_server, now)
    finally:
        _DEADLINE[0] = None


def _copy_base(base, root, settings, deadline_s, enforce_limits=True, session="", via="hook", command="", mcp_server="", now=None):
    """One verified copy of a database into <root>\\dbcopies\\<day>\\ and a line in <root>\\dbcopies.jsonl.
    -> dict: status ok | absent (nothing to copy) | toolarge | timeout | error; msg; path; size (of the database);
    copy_size; sha256; id; indexed; identity. `deadline_s` covers the measuring, the dump and the check together;
    enforce_limits=False (the explicit `dbcopy`) skips max_db_mb."""
    from . import store
    now = now or datetime.now()
    t_end = time.monotonic() + (deadline_s if deadline_s else EXPLICIT_DEADLINE_S)
    res = {"status": "error", "msg": "", "path": None, "size": 0, "copy_size": 0, "sha256": "", "id": None,
           "indexed": False, "identity": base.identity}

    def left():
        return t_end - time.monotonic()

    if base.linked:
        res["msg"] = "a hosted project has no address to copy from"
        return res
    ad = engines.get(base.engine)
    if ad is None:
        res["msg"] = "no adapter for the engine %r" % base.engine
        return res
    tools, why = ad.prepare(settings)
    if tools is None:
        res["msg"] = why
        return res
    kind, size, extra = ad.size(base, settings, tools, max(1.0, left()))
    if kind == "absent":
        res.update(status="absent", msg="the database does not exist: nothing to copy")
        return res
    if kind != "ok":
        res["msg"] = "the size of the database could not be measured: %s" % extra
        return res
    res["size"] = size
    why = ad.check(base, tools, extra)
    if why:
        res["msg"] = why
        return res
    limit = int(settings["max_db_mb"]) * 1048576
    if enforce_limits and size > limit:
        res.update(status="toolarge", msg="the database is %.1f MB, over max_db_mb = %d" % (size / 1048576.0, settings["max_db_mb"]))
        return res
    day = now.strftime("%Y-%m-%d")
    folder = os.path.join(root, "dbcopies", day)
    stem = "%s_%s_%s" % (ad.prefix or ad.id, _safe_name(base.label), now.strftime("%H%M%S"))
    try:
        final = _reserve(folder, stem, ad.ext)
    except OSError as e:
        res["msg"] = "the copy cannot be created (%s: %s)" % (e.__class__.__name__, e)
        return res
    tmp = final + ".part"
    res["path"] = final
    try:
        ok, verified = ad.copy(base, tmp, tools, left, res)
    except Exception as e:             # a fault of an adapter is no copy (the strict side), never a crash of the hook
        ok, verified = False, ""
        res["msg"] = "the adapter %s failed while copying (%s: %s)" % (ad.id, e.__class__.__name__, _scrub(mask_secrets(str(e)), base)[:200])
    if ok and not os.path.isfile(tmp):
        ok, res["msg"] = False, "the adapter %s reported a copy but wrote no file" % ad.id
    elif ok and os.path.getsize(tmp) <= 0:
        ok, res["msg"] = False, "the adapter %s reported a copy but wrote an EMPTY file (a copy of zero bytes is no copy)" % ad.id
    if not ok:
        _drop_own(tmp)
        _drop_own(final)                 # the empty name reserved above
        res["path"] = None
        return res
    try:
        os.replace(tmp, final)
        res["copy_size"] = os.path.getsize(final)
        res["sha256"] = _sha(final)
    except OSError as e:
        _drop_own(tmp)
        res.update(status="error", msg="the verified copy could not be put in place (%s: %s)" % (e.__class__.__name__, e), path=None)
        return res
    res["status"] = "ok"
    res["verified"] = verified
    holder = {}

    def make(n):
        holder["id"] = "d:%s:%d" % (day, n)
        return {"id": holder["id"], "engine": base.engine, "identity": base.identity, "name": base.label,
                "url": base.url_masked, "size": size, "copy": final, "copy_size": res["copy_size"], "sha256": res["sha256"],
                "verified": verified, "command": _scrub(mask_secrets(command)[:1000], base), "session": session or "", "via": via,
                "mcp_server": mcp_server or "", "time": now.isoformat(timespec="seconds")}
    leak = _leak(make(0), base)
    if leak:                       # the record would carry a secret of the connection: the copy does not count (the adapter's fault)
        _drop_own(final)
        res.update(status="error", msg=leak, path=None, copy_size=0, sha256="")
        return res
    try:
        store._append_jsonl(index_path(root), make)
        res["id"], res["indexed"] = holder.get("id"), True
    except Exception as e:
        res["msg"] = "the copy is made but NOT in the list of copies (%s: %s)" % (e.__class__.__name__, e)
    return res


def _scrub(text, base):
    """The text of the command with every secret of the connection replaced by *** (the command is the agent's own text: what it typed is
    scrubbed, not refused). For a built-in engine the password is masked STRUCTURALLY by mask_secrets (in the address, PGPASSWORD=, password=,
    PASSWORD '...'), not looked for as a substring everywhere: a password that is also a user or a database name (`postgres`) would otherwise
    turn the whole command into ***."""
    if base.engine in engines.BUILTIN_IDS:
        rx = _secret_boundary(base)
        return rx.sub("***", text) if rx is not None else text
    for sec in base.secrets:
        text = text.replace(sec, "***")
    return text


def _secret_boundary(base):
    """A pattern for the secrets of a connection of a BUILT-IN engine where they stand ALONE: at the start or after a space, `=`, `:` or a quote, and before
    the end, a space, a quote, `;`, `&`, `@` or `)` -- so `DB_PASSWORD=x` and `sh deploy.sh x` are caught, a word that merely holds the same letters (`test_db`) is
    not. A secret of 3 characters or less, or one that equals the user, the database, the host, the port or the engine (`postgres/postgres`), is the word's own
    sake, not a leak: it is not looked for (F2). None when nothing is left to look for."""
    skip = set()
    for name in ("user", "dbname", "host", "engine"):
        v = getattr(base, name, "")
        if isinstance(v, str) and v:
            skip.add(v.lower())
    if getattr(base, "port", 0):
        skip.add(str(base.port))
    try:
        skip.add(str(base.label).lower())
    except Exception:
        pass
    vals = sorted({s for s in base.secrets if isinstance(s, str) and len(s) > 3 and s.lower() not in skip}, key=len, reverse=True)
    if not vals:
        return None
    return re.compile(r"(?:^|(?<=[\s=:'\"]))(?:%s)(?=$|[\s'\";&@)])" % "|".join(re.escape(v) for v in vals))


def _leak(rec, base):
    """A text when the record of a copy carries a secret of the connection (the password of the address, a value the adapter named in
    Base.secrets), else ''. The framework does not trust an adapter to keep a password out of the record: it looks."""
    blob = json.dumps(rec, ensure_ascii=False)
    if base.engine in engines.BUILTIN_IDS:
        # The record of a built-in engine is built by this kit from masked parts. A password shows only in a FORM a password has (`user:secret@`,
        # PGPASSWORD=secret, password=secret, PASSWORD 'secret') -- and every such form is what mask_secrets would change. A password equal to the user,
        # the database or the engine name is no leak: the word stands there for its own sake.
        if mask_secrets(blob) != blob:
            return "the record of the copy would carry a password of the connection in the form of one (user:password@, PGPASSWORD=, password=); nothing was recorded"
        rx = _secret_boundary(base)
        if rx is not None and rx.search(blob):
            return "the record of the copy would carry the password of the connection as a word of its own; nothing was recorded"
        return ""
    for sec in base.secrets:
        if sec and sec in blob:
            return "the record of the copy would carry a secret of the connection (the adapter of engine %s put it into an address, a name or a path); nothing was recorded" % base.engine
    return ""


def _dump_pg(base, pgdump, tmp, left, res):
    """pg_dump -Fc into tmp, then the dump is READ IN FULL with pg_restore (code 0, nothing on stderr). -> (ok, how verified)."""
    env = _child_env(base, left())
    rc, out, err, timed = _run([pgdump, "-Fc", "-w", "-f", tmp], env, left())
    if timed or left() <= 0:
        res.update(status="timeout", msg="pg_dump did not finish in time")
        return False, ""
    if rc != 0:
        res["msg"] = "pg_dump failed (exit %s): %s" % (rc, mask_secrets(err.strip())[:300])
        return False, ""
    restore = _sibling(pgdump, "pg_restore")
    if not restore:
        res["msg"] = "pg_restore was not found next to %s: the dump cannot be read back" % pgdump
        return False, ""
    rc, out, err, timed = _run([restore, "-f", os.devnull, tmp], dict(os.environ), left())
    if timed or left() <= 0:
        res.update(status="timeout", msg="the check of the dump (pg_restore) did not finish in time")
        return False, ""
    if rc != 0 or err.strip():
        res["msg"] = "the dump does not read back in full (pg_restore exit %s): %s" % (rc, err.strip()[:300])
        return False, ""
    return True, "pg_restore full read: exit 0, no messages"


class _Timeout(Exception):
    pass


def _dump_sqlite(base, tmp, left, res):
    src = dst = None
    try:
        uri = pathlib.Path(os.path.abspath(base.path)).as_uri() + "?mode=ro"
        src = sqlite3.connect(uri, uri=True, timeout=min(5.0, max(0.5, left())))
        dst = sqlite3.connect(tmp)

        def progress(status, remaining, total):
            if left() <= 0:
                raise _Timeout()
        src.backup(dst, pages=100, progress=progress)
        dst.commit()
        row = dst.execute("PRAGMA integrity_check").fetchall()
        if row != [("ok",)]:
            res["msg"] = "PRAGMA integrity_check of the copy: %s" % (row[:3],)
            return False, ""
    except _Timeout:
        res.update(status="timeout", msg="the backup of the SQLite file did not finish in time")
        return False, ""
    except sqlite3.Error as e:
        res["msg"] = "the SQLite backup failed (%s: %s)" % (e.__class__.__name__, e)
        return False, ""
    finally:
        for c in (dst, src):
            try:
                if c is not None:
                    c.close()
            except Exception:
                pass
    return True, "PRAGMA integrity_check = ok"


def find_fresh(roots, identity, minutes, now=None):
    """The record of a verified copy of this database taken by `dbcopy` less than `minutes` ago (whose file is still there
    with its size), or None. 9 min 59 s ago passes for 10, 10 min 01 s does not; exactly 10:00 does not."""
    from . import store
    now = now or datetime.now()
    best = None
    if not identity:
        return None
    for root in dict.fromkeys(r for r in roots if r):
        try:
            recs, _bad = store._read_jsonl(index_path(root))
        except OSError:
            continue
        for _n, r in recs:
            if r.get("identity") != identity or r.get("via") != "dbcopy" or not r.get("verified"):
                continue
            try:
                age = (now - datetime.fromisoformat(r["time"])).total_seconds()
                if not (-5 <= age < minutes * 60):
                    continue
                if os.path.getsize(r["copy"]) != r.get("copy_size"):
                    continue
            except (KeyError, ValueError, OSError, TypeError):
                continue
            if best is None or r["time"] > best["time"]:
                best = dict(r, root=root)
    return best


def find_fresh_mcp(roots, server, minutes, now=None):
    """The record of a verified copy taken by `dbcopy --mcp-server <server>` less than `minutes` ago (the file still there with its
    size), or None. The same border as find_fresh: 9 min 59 s is fresh for 10, 10 min 01 s is not. It rests on the word of the
    agent that the copy was of the database behind THIS server (declared limit)."""
    from . import store
    now = now or datetime.now()
    best = None
    if not server:
        return None
    for root in dict.fromkeys(r for r in roots if r):
        try:
            recs, _bad = store._read_jsonl(index_path(root))
        except OSError:
            continue
        for _n, r in recs:
            if r.get("op") or r.get("via") != "dbcopy" or not r.get("verified") or r.get("mcp_server") != server:
                continue
            try:
                age = (now - datetime.fromisoformat(r["time"])).total_seconds()
                if not (-5 <= age < minutes * 60):
                    continue
                if os.path.getsize(r["copy"]) != r.get("copy_size"):
                    continue
            except (KeyError, ValueError, OSError, TypeError):
                continue
            if best is None or r["time"] > best["time"]:
                best = dict(r, root=root)
    return best


# ============================================================================================ the decision

class Item(object):
    def __init__(self, action, base):
        self.action = action
        self.base = base
        self.mode = ""              # fresh | copy | remote_allow | unsupported
        self.fresh = None

    @property
    def label(self):
        if self.base is not None:
            return self.base.label
        return (self.action.mcp or {}).get("name") or "?"


class Plan(object):
    def __init__(self):
        self.items = []
        self.refusal = None


def _is_mcp(action):
    return action is not None and action.kind == "mcp"


def _mcp_name(action):
    """The name of the MCP server of a call ('' when the input named none, or the action is no MCP call)."""
    return ((action.mcp or {}).get("name") or "") if _is_mcp(action) else ""


def _dbcopy_cmd(base, kit_py, quote, shell, named, action=None):
    """(the ready `dbcopy` command, what to do after it)."""
    if _is_mcp(action):
        mcp = _mcp_name(action) or "<name of the MCP server>"
        addr = base.url_masked if (base is not None and not base.linked and not base.shown) else "<address of the database behind the server>"
        return "python %s dbcopy %s --mcp-server %s" % (quote(kit_py, shell), quote(addr, shell), quote(mcp, shell)), "then repeat the call"
    addr = base.url_masked if not base.linked else "<address of the project>"
    cmd = "python %s dbcopy %s" % (quote(kit_py, shell), quote(addr, shell))
    return cmd, "then repeat your command" + ("" if named else " with %s" % _assign_text("SHEDKEEPER_DB", addr, quote, shell))


def _ready_dbcopy(base, kit_py, quote, shell, named, action=None):
    cmd, after = _dbcopy_cmd(base, kit_py, quote, shell, named, action)
    if _is_mcp(action):
        return ("Take the copy outside the limits of the hook: %s -- %s. The address has *** in place of a password: put "
                "it in yourself or let pgpass supply it." % (cmd, after))
    return ("Take the copy outside the limits of the hook: %s -- %s. The address above has *** in place of the password: put it "
            "in yourself or let pgpass supply it." % (cmd, after))


def _block(what, shell, kit_py, quote, **kw):
    """The instruction to the agent (advice.py) with the kit path and the quoting this hook run was given."""
    return advice.block(mask_secrets(what), shell=shell, kit=kit_py or None, quote=quote, **kw)


def _assign_text(name, value, quote, shell):
    return ("$env:%s=%s;" % (name, quote(value, shell))) if shell == "powershell" else "%s=%s" % (name, quote(value, shell))


def _reasons(action):
    return ", ".join(action.reasons[:3]) or "a destroying action"


def unsupported_notes(engine, mcp=False, shell="bash", kit_py="", quote=None, adapter="", why=""):
    """(text for the agent, text for the person) of a destroying action over an engine this version does not copy . The text for
    the agent ends with the block of advice.block: what happened, who decides, the options."""
    what = "this MCP call" if mcp else "this command"
    agent = ("shedkeeper: WARNING -- %s destroys data in a %s database, and this version does NOT protect that engine: NO COPY WAS TAKEN "
             "and it was let through (on_db_unsupported = allow). A copy of such data is taken with the tool of that "
             "engine; an adapter for the engine can be built -- README, \"Adding another database engine\". "
             "To refuse such actions instead, set on_db_unsupported = deny in shedkeeper.ini." % (what, engine))
    agent += " " + advice.block(
        "an action was just let through that destroys data in a %s database: ShedKeeper cannot copy that engine, so NO COPY was taken "
        "and the data may be gone for good; ShedKeeper says this once for this project and this engine" % engine,
        agent="take a copy of that data with the tool of that engine (for MySQL or MariaDB: `mysqldump --single-transaction DBNAME > FILE.sql`)",
        user="take the copies of that database themselves, or have an adapter built for the engine (README, \"Adding another database "
             "engine\")",
        settings=[("on_db_unsupported", "deny")], shell=shell, kit=kit_py or None, quote=quote)
    person = ("ShedKeeper: the agent is doing something that destroys data in a %s database. ShedKeeper does not protect that engine, so NO "
              "COPY was made and the action was let through. (on_db_unsupported = deny in shedkeeper.ini refuses such actions; the README, "
              "\"Adding another database engine\", tells how to add the engine.)" % engine)
    if adapter:
        how = ("`python %s activate %s` makes it count once the person has typed the code" % (kit_py or "<kit>/shedkeeper.py", adapter)
               if "NOT ACTIVATED" in (why or "") else "`python %s prove %s --target <a probe database>` proves it, and the person then confirms it with `activate`" % (
                   kit_py or "<kit>/shedkeeper.py", adapter))
        agent += (" NOTE: an adapter for this engine is in the adapters folder but it is NOT usable as protection (%s): `python %s status` says which; %s."
                  % (why or "not proven for the current sha256 of its file, or it failed to load", kit_py or "<kit>/shedkeeper.py", how))
        person += " An adapter for the engine is installed, but it is not proven (or broken), so it does not protect yet: see `status`."
    return agent, person


def plan(actions, cwd, settings, roots_for, env=None, quote=None, kit_py="", shell="bash", now=None):
    """Name the databases and apply every rule that needs no copy. `roots_for(base)` -> the personal folders where a copy of
    this database may have been listed (`roots_for(None)`: the folders of the user, for a copy listed by MCP server). -> Plan:
    .items (what must be copied or passed), .refusal (a text when the command is refused before any copy)."""
    pl = Plan()
    seen = set()
    env = os.environ if env is None else env
    load_proofs(roots_for)                   # the adapters of the user exist: their proofs are looked for in the folders of the store
    for a in actions:
        mcp = _mcp_name(a)
        is_mcp = _is_mcp(a)
        if mcp and not (a.conn or {}).get("multi"):          # (a call that drops several databases is judged database by database)
            fresh = find_fresh_mcp(roots_for(None), mcp, settings["db_fresh_minutes"], now)
            if fresh:
                item = Item(a, None)
                item.mode, item.fresh = "fresh", fresh
                if ("mcp", mcp) not in seen:
                    seen.add(("mcp", mcp))
                    pl.items.append(item)
                continue
        base0 = name_base(a, cwd, env)
        for base in (base0.items if isinstance(base0, Several) else [base0]):        # candidates of different engines: the one the kit can copy, and a warning for the others
            if isinstance(base, Unsupported):
                if settings.get("on_db_unsupported", "allow") == "deny":
                    pl.refusal = ("shedkeeper: %s destroys data in a %s database (%s). This version does not copy that engine, and "
                                  "on_db_unsupported = deny: refused. Take the copy yourself with the tool of that engine, then the person allows it with "
                                  "`set on_db_unsupported allow` (their confirmation code), or an adapter is built -- README, \"Adding another database "
                                  "engine\"." % ("this MCP call" if is_mcp else "this command", base.engine, _reasons(a)))
                    pl.refusal += " " + _block(
                        "this %s would destroy data in a %s database; ShedKeeper cannot copy that engine, and the setting on_db_unsupported = deny "
                        "makes it stop such actions" % ("call" if is_mcp else "command", base.engine),
                        shell, kit_py, quote,
                        user="take the copy of the database themselves with the tool of that engine (or have an adapter built for it -- README, "
                             "\"Adding another database engine\"), then let the action through with the setting below",
                        settings=[("on_db_unsupported", "allow")])
                    return pl
                if base.identity in seen:
                    continue
                seen.add(base.identity)
                item = Item(a, base)
                item.mode = "unsupported"
                pl.items.append(item)
                continue
            if isinstance(base, Unnamed):
                if is_mcp:
                    hint = ("%s Take the copy of the database behind the server and say whose it is: python %s dbcopy '<address of the "
                            "database>' --mcp-server %s -- then repeat the call. (If this server holds no database at all, its name goes into "
                            "`set mcp_no_db <all the names>` -- it replaces the list and asks for the user's confirmation code.)" % (base.reason.rstrip(".") + ".", quote(kit_py, shell),
                                                                       quote(mcp or "<name of the MCP server>", shell)))
                    pl.refusal = ("shedkeeper: this call of the MCP tool %s may destroy data in a database (%s), and the database cannot be "
                                  "named from the settings of the server. Refused, as a file would be. %s" % (a.head, _reasons(a), hint))
                    cmd = "python %s dbcopy '<address of the database>' --mcp-server %s" % (quote(kit_py, shell), quote(mcp or "<name of the MCP server>", shell))
                    pl.refusal += " " + _block(
                        "this call of the tool %s may destroy data in a database, and ShedKeeper cannot tell which database is behind the server "
                        "(%s), so it stopped the call to keep the data safe" % (a.head, base.reason.rstrip(".")),
                        shell, kit_py, quote,
                        agent="after the user names the database (its address) run `%s`, then repeat the call" % cmd,
                        user="tell the agent which database is behind the server, or take a copy of it themselves",
                        settings=[("mcp_no_db", mcp)] if mcp else ())
                    return pl
                if getattr(a, "container", False):
                    named_cmd = _assign_text("SHEDKEEPER_DB", "postgresql://localhost:<port>/<database>", lambda v, s: "'%s'" % v, shell)
                    pl.refusal = ("shedkeeper: this command destroys data in a database inside a container (%s): ShedKeeper takes no copy there, so it is "
                                  "refused. Name the database through the port the container publishes: %s <your command> -- the copy is then taken first, "
                                  "from that address. Or ask the user." % (_reasons(a), named_cmd))
                    pl.refusal += " " + _block(
                        "this command would destroy data in a database inside a container, and ShedKeeper cannot copy a database there (it takes the copy "
                        "from an address, and only the port the container publishes gives one), so it stopped the command to keep the data safe",
                        shell, kit_py, quote,
                        agent="after the user says which port the database is published on, run the same command with %s in front of it: the copy is "
                              "then taken first, from that address" % named_cmd,
                        user="tell the agent the published port and the database, or take a copy of the database themselves (for example with `pg_dump` "
                             "through that port)",
                        settings=[("db_protection", "false")])
                    return pl
                if a.kind == "unreadable":
                    pl.refusal = ("shedkeeper: this command may destroy data in a database, and it cannot be read (%s). Refused. Naming the database does not help: it is the "
                                  "command itself that cannot be read; a shorter command (the destroying part on its own) can be." % base.reason.rstrip("."))
                    pl.refusal += " " + _block(
                        "this command may destroy data in a database, and ShedKeeper cannot read it (%s), so it stopped the command to keep the data safe" % base.reason.rstrip("."),
                        shell, kit_py, quote,
                        agent="run the destroying part as a command of its own, shorter and without nesting",
                        user="run the command themselves, outside the agent",
                        settings=[("db_protection", "false")])
                    return pl
                hint = ("%s Name it: %s <your command> -- the copy is then taken first. (For an engine this version does not "
                        "copy: `set db_protection false`, with the user's confirmation code, lets such commands through with no copy at all.)"
                        % (base.reason.rstrip(".") + ".", _assign_text("SHEDKEEPER_DB", "<address or file>", lambda v, s: "'%s'" % v, shell)))
                pl.refusal = ("shedkeeper: this command destroys data in a database (%s), and the database cannot be named from it. "
                              "Refused, as a file would be. %s" % (_reasons(a), hint))
                pl.refusal += " " + _block(
                    "this command would destroy data in a database, and ShedKeeper cannot tell which database it is (%s), so it stopped the "
                    "command to keep the data safe" % base.reason.rstrip("."),
                    shell, kit_py, quote,
                    agent="after the user names the database (an address or a SQLite file), run the same command with %s in front of it: the "
                          "copy is then taken first" % _assign_text("SHEDKEEPER_DB", "<address or file>", lambda v, s: "'%s'" % v, shell),
                    user="tell the agent which database it is, or take a copy of it themselves",
                    settings=[("db_protection", "false")])
                return pl
            key = base.identity or base.url_masked
            if key in seen:
                continue
            seen.add(key)
            item = Item(a, base)
            named = "SHEDKEEPER_DB" in a.env
            fresh = find_fresh(roots_for(base), base.identity, settings["db_fresh_minutes"], now)
            if fresh:
                item.mode, item.fresh = "fresh", fresh
            elif base.remote:
                if settings["on_db_remote"] == "allow":
                    item.mode = "remote_allow"
                else:
                    pl.refusal = ("shedkeeper: %s destroys data in a REMOTE database (%s) %s. Refused: a remote database is "
                                  "not copied by the hook. %s To let such actions pass without a copy: `set on_db_remote allow` "
                                  "(the user's confirmation code)." % ("this MCP call" if is_mcp else "this command", _reasons(a), base.url_masked,
                                                       _ready_dbcopy(base, kit_py, quote, shell, named, a)))
                    cmd, after = _dbcopy_cmd(base, kit_py, quote, shell, named, a)
                    pl.refusal += " " + _block(
                        "this %s would destroy data in the database %s on ANOTHER computer; ShedKeeper does not copy remote databases by itself "
                        "(a copy over the network can be slow and needs the password), so it stopped the %s" % (
                            "call" if is_mcp else "command", base.url_masked, "call" if is_mcp else "command"),
                        shell, kit_py, quote,
                        agent="run `%s` (put the password in, or let pgpass supply it), %s" % (cmd, after),
                        user="run that `dbcopy` command themselves (the password is theirs), or take the copy with their own tools, then tell "
                             "the agent",
                        settings=[("on_db_remote", "allow")])
                    return pl
            else:
                item.mode = "copy"
            pl.items.append(item)
    return pl


def _mb(n):
    return "%.1f MB" % (n / 1048576.0)


def execute(pl, cwd, session, settings, store, command, roots_for_write, quote=None, kit_py="", shell="bash", now=None):
    """Take the copies of a plan. -> (refusal text or None, context text, created-store report or None, meta).
    `roots_for_write(base)` -> the personal folder to write into. One budget (db_deadline_s) for all the copies of a command.
    meta: copies (number made), notice (a text for the PERSON, set when something passed with no copy: the engine was not supported)."""
    budget_end = time.monotonic() + settings["db_deadline_s"]
    notes, created, made, notices, engines = [], None, 0, [], []
    marks = []                     # (folder, project, engine): warnings to record AFTER they were delivered in a decision that lets the command through
    meta = {"copies": 0}
    for it in pl.items:
        b = it.base
        a = it.action
        named = "SHEDKEEPER_DB" in a.env
        mcp = _mcp_name(a)
        if it.mode == "fresh":
            notes.append("copy of %s from %s is fresh (%s): %s" % (it.label, it.fresh["time"], it.fresh["id"], it.fresh["copy"]))
            continue
        if it.mode == "unsupported":
            engines.append(b.engine)
            first = True
            try:
                from . import survey
                uroot = roots_for_write(b)
                ok_u, _n, rep_u = store.ensure_store_report(uroot, settings)
                created = created or rep_u
                first = survey.unsupported_pending(uroot, cwd, b.engine) if ok_u else True
                if first and ok_u:
                    marks.append((uroot, cwd, b.engine))
                if not first:
                    store.log(uroot, {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": "db_unsupported_passed", "engine": b.engine,
                                      "why": "the warning for this project and engine was given before", "session": session or ""})
            except Exception:
                first = True
            if first:
                agent_text, person_text = unsupported_notes(b.engine, _is_mcp(a), shell, kit_py, quote, getattr(b, "adapter", ""), getattr(b, "reason", ""))
                notes.append(agent_text[len("shedkeeper: "):])
                notices.append(person_text)
            continue
        root = roots_for_write(b)
        ok, note, rep = store.ensure_store_report(root, settings)
        created = created or rep
        if not ok:
            return ("shedkeeper: the store is not usable, so no copy of the database %s can be made: %s. The action is refused."
                    % (b.label, note) + " " + _block(
                        "the folder where ShedKeeper keeps its copies cannot be used (%s), so no copy of the database %s can be made and the "
                        "action was stopped" % (note, b.label), shell, kit_py, quote,
                        agent="run `%s status` to see the state of the store" % advice.py_cmd(shell, kit_py or None, quote),
                        user="repair the folder (see `status`), or tell the agent where copies may be kept", no_setting=True),
                    "", created, dict(meta, copies=made))
        if it.mode == "remote_allow":
            store.log(root, {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": "db_no_copy_allowed", "base": b.url_masked,
                             "why": "remote database, on_db_remote = allow", "session": session or ""})
            notes.append("no copy of %s (on_db_remote = allow)" % b.url_masked)
            notices.append(no_copy_notice(b.url_masked, "on_db_remote", "a database on another machine"))
            continue
        remaining = budget_end - time.monotonic()
        res = copy_base(b, root, settings, remaining, True, session, "hook", command, mcp, now) if remaining > 0 else \
            {"status": "timeout", "msg": "the budget db_deadline_s = %d s was used up by the earlier copies" % settings["db_deadline_s"]}
        st = res["status"]
        if st == "ok":
            made += 1
            notes.append("copy of the database %s saved (%s, %s): %s%s" % (
                b.label, res.get("id") or "NOT LISTED", _mb(res["size"]), res["path"], "" if res.get("indexed") else " -- NOT LISTED: " + res["msg"]))
        elif st == "absent" and getattr(a, "cwd_unsure", False):
            return ("shedkeeper: the database %s was not found, but the command goes to another folder first (a `cd` whose target ShedKeeper cannot read), so it cannot be told "
                    "whether the file exists where the command will look. Refused, so that nothing is destroyed without a copy." % b.label + " " + _block(
                        "the database file %s was not found, and the command changes folder first (a `cd` ShedKeeper cannot read), so it cannot tell whether the file "
                        "exists there; it stopped the command to keep the data safe" % b.label, shell, kit_py, quote,
                        agent="run the same command with the file named by its full path, or with %s in front of it" % _assign_text(
                            "SHEDKEEPER_DB", "<address or file>", lambda v, s: "'%s'" % v, shell),
                        user="tell the agent which database file it is, or take a copy of it themselves", no_setting=True), "", created, dict(meta, copies=made))
        elif st == "absent":
            notes.append("database %s does not exist: nothing to copy" % b.label)
        elif st in ("toolarge", "timeout"):
            if settings["on_db_too_large"] == "allow":
                store.log(root, {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": "db_no_copy_allowed", "base": b.url_masked,
                                 "why": res["msg"], "session": session or ""})
                notes.append("no copy of %s (on_db_too_large = allow): %s" % (b.label, res["msg"]))
                notices.append(no_copy_notice(b.url_masked, "on_db_too_large", res["msg"]))
            else:
                limits = "max_db_mb = %d, db_deadline_s = %d" % (settings["max_db_mb"], settings["db_deadline_s"])
                cmd, after = _dbcopy_cmd(b, kit_py, quote, shell, named, a)
                return ("shedkeeper: no copy of the database %s was taken: %s (%s). Refused, so that nothing is destroyed without "
                        "a copy. %s To let such actions pass without a copy: `set on_db_too_large allow` (the user's confirmation code)."
                        % (b.url_masked, res["msg"], limits, _ready_dbcopy(b, kit_py, quote, shell, named, a)) + " " + _block(
                            "the database %s is bigger than the hook may copy in its time (%s), or its copy was too slow, so ShedKeeper stopped "
                            "the %s to keep the data safe" % (b.url_masked, res["msg"], "call" if _is_mcp(a) else "command"),
                            shell, kit_py, quote,
                            agent="run `%s` (it has no size or time limit; put the password in, or let pgpass supply it), %s" % (cmd, after),
                            user="run that `dbcopy` command themselves, then tell the agent",
                            settings=[("on_db_too_large", "allow")]), "", created, dict(meta, copies=made))
        else:
            cmd, _after = _dbcopy_cmd(b, kit_py, quote, shell, named, a)
            return ("shedkeeper: no copy of the database %s could be taken (%s). Refused, so that nothing is destroyed without a "
                    "copy. If the cause is yours to fix (no pg_dump, no right to read the database), fix it and repeat; "
                    "`dbcopy` shows the same reason." % (b.url_masked, res["msg"] or "unknown reason") + " " + _block(
                        "ShedKeeper could not take a copy of the database %s (%s), so it stopped the %s to keep the data safe" % (
                            b.url_masked, res["msg"] or "unknown reason", "call" if _is_mcp(a) else "command"),
                        shell, kit_py, quote,
                        agent="run `%s` to see the same reason in full" % cmd,
                        user="remove the cause (for example install pg_dump, or give the right to read the database), then tell the agent",
                        no_setting=True), "", created, dict(meta, copies=made))
    meta["copies"] = made
    if marks:
        meta["warn_marks"] = marks
    if engines:
        meta["unsupported"] = list(dict.fromkeys(engines))
    if notices:
        meta["notice"] = " ".join(dict.fromkeys(notices))
    return None, ("shedkeeper: " + "; ".join(notes)) if notes else "", created, meta


def budget_s(settings, git_total_s=10, start_s=3):
    """What one hook run may spend in the worst case: file copies + git + the database copies + the start of the interpreter."""
    return settings["cmd_deadline_s"] + git_total_s + settings["db_deadline_s"] + start_s
