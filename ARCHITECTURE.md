# ShedKeeper: how it is built

For an agent (or a person) who wants to understand the system well enough to decide for themselves what to build next --
including for a database engine this kit does not know. It describes what is there, why each decision is the way it is, what the
system deliberately does not see, and the one extension point (the engine adapter) with its contract. The README is the entry for a
person; this file is the description of the device. Everything here was checked against the code of this version (0.3.0) and its tests.

Two things are properties of the system, not a recipe, and are kept in every part of it:

1. **The properties of the protection** (section 10): a copy before the destruction, read back in full; nothing is deleted; a way
   back into a *new* place; a password is never printed; a stop always carries a next step.
2. **The proof gate** (section 9.6): an engine adapter written by someone else is *not* protection until it has been shown, on a probe
   database, to copy, survive a destruction and bring the data back equal.

How you get to a working adapter is your decision. Section 9 gives the contract and a tested example; it does not prescribe an order of work.

---

## 1. The shape in one page

> ShedKeeper 0.3.0 protects files. Databases are not protected in this version; a later version will add them. Everything below that speaks of a database, an MCP call, `dbcopy`, `prove`, `activate` or an engine adapter describes code that is built and switched off in this release (NOT SHIPPED; section 12). (`config.DATABASES_IN_THIS_RELEASE = False`: the code of the databases stays in the kit and keeps being tested; a database entry in the protected list is not checked, and `status` and the start of a session say so.)

ShedKeeper is three Claude Code hooks plus a command line, in plain Python 3.9+ (standard library only, Windows in v1).

- `hooks/pre_tool.py` -- **PreToolUse**. Runs before a tool call and may refuse it. It reads the *text* of the call (a path, a shell
  command, the arguments of an MCP tool), decides whether something would be overwritten, deleted or destroyed, and either takes a verified
  copy first and lets the call go, or refuses and hands the agent a ready command for what to do next.
- `hooks/prompt.py` -- **UserPromptSubmit**. Runs when the person submits a message; its input carries the text (`prompt`). It looks for `CONFIRM SK-nnnnnn`
  in it and records, in the journal of the store, that the PERSON typed that code (section 7). It blocks nothing and says nothing for a message with no code.
- `hooks/session.py` -- **SessionStart / SessionEnd**. Gives the agent a one-paragraph guide, saves transcripts, warns about a store
  that is not usable, offers the setup check once per project, and reminds (once per start) about any setting that weakens protection or
  an adapter that failed to load.
- `shedkeeper.py` -- the command line: `trash`, `find`, `restore`, `status`, `dbcopy`, `settings`, `set`, `survey`, `prove`, `activate`, `quarantine`, `adopt`, `selftest`.
- A **store** on each fixed volume (section 4) holds the copies, the trash and the lists.

The invariant of the whole kit: **a change does not run when its copy was not taken** (I1). Everything that cannot take a copy refuses; it
never lets the change through "just this once". The only ways past a refusal are the switches that the person's word turns on, and each of
those says aloud that something now goes through without a copy and writes a line in the log.

Why hooks, and why on the *text* of a call: a hook is the one place where a tool call can be stopped before it acts, and the text is all
it has. That is also the source of the limits in section 8: what the text does not show, the system does not see.

---

## 2. The files

| File | What it does |
|---|---|
| `hooks/pre_tool.py` | the decision of the hook: Edit/Write, Bash/PowerShell, MCP tools; builds every refusal; emits one JSON line; catches every exception (a fault is a refusal when a destruction was recognised or the tool is an editor) |
| `hooks/session.py` | SessionStart/SessionEnd: guide, transcript copies, warnings, the once-only offer, the reminder of weakened settings (with the mark "not through `set`") and broken adapters, the once-per-user recommendation about the rule |
| `hooks/prompt.py` | UserPromptSubmit: receives the code the person types (`CONFIRM SK-nnnnnn`) and writes it to the journal; blocks nothing |
| `hooks/hooks.json`, `.claude-plugin/` | the plugin way in (`--via-plugin`); the hooks stay silent when `install.py`'s entries cover the same event (one handling per event) |
| `install.py` | puts the hook entries into Claude Code settings (backup first, never touches others'), takes them out, `check` runs the registered command once |
| `shedkeeper.py` | the command line (above) |
| `shedkeeper/recognize.py` | reads the **text of a shell command** (Bash and PowerShell): which existing files it would overwrite, whether it deletes, and which paths. A scanner over segments (`;` `&&` `\|\|` `\|`), `$( )`, `bash -c`, `cmd /c`, `-Command`, heredocs, `python -c`; it calls an *observer* at every place a command is executed, so a mention (`grep rm f`, `echo prisma migrate reset`) is never an action |
| `shedkeeper/store.py` | the store: volume folders, a personal folder per user, labels, permissions, copies of versions, the trash, the indexes, `find`, `restore`, locks |
| `shedkeeper/config.py` | `shedkeeper.ini` (layers: shipped, then personal), safe defaults, `describe` (the meaning of each key is *read from the comments of the shipped ini*), `edit_text` (change one key, keep the rest of the file) |
| `shedkeeper/db.py` | databases: recognises destroying SQL and tool commands, names the database, decides (`plan`), copies (`copy_base`, `execute`), the SQL reader (`strip_sql`, `sql_findings`, `sql_reasons`) |
| `shedkeeper/dbmcp.py` | a call of an MCP tool: is it destroying, which database is behind the server |
| `shedkeeper/dbcli.py` | `dbcopy`, `find` for copies of databases, `restore` (through the adapter), the databases part of `status` |
| `shedkeeper/engines.py` | **the engine adapter**: the contract, the two built-in adapters (PostgreSQL, SQLite), the loader of the user's adapters, the proofs |
| `shedkeeper/prove.py` | the proof gate (section 9.6) |
| `shedkeeper/advice.py` | the words of a stop or a warning for its two readers: a short English line for the person, an instruction for the agent |
| `shedkeeper/settings_cli.py` | `settings` (the menu), `set` (change one setting), what the hook tells the person about a change, the reminder lines |
| `shedkeeper/confirm.py` | the confirmation code and the journal `confirm.jsonl`: issue, the person's word (`receive`, from the hook of the prompt), `gate`, the line `state`, `approved` (what the ini may make act), `quarantine` (section 7) |
| `shedkeeper/survey.py` | the read-only first look at the machine and project; the once-only records (`guide.jsonl`) |
| `shedkeeper/protect.py` | the LIST of what is protected (kinds D tree, S SQLite file, P local PostgreSQL, R remote PostgreSQL), its confirmation by the person (a mark in the journal of confirmations, apart from the list), the line at the start of a session |
| `shedkeeper/signals.py` | the signals `changed / same / unknown` of an entry: the walk with a signature (size, modification time, ChangeTime), the change journal read without administrator rights, the signature of a SQLite file and its -wal, the counters and sessions of a PostgreSQL database |
| `shedkeeper/guard.py` | the check before every step: lock, signal, verified copy, then (only then) the base of the signal and the manifest of the step; `snapshot`, `restore --step N`, `find --steps` |
| `examples/adapter_litedb.py` | a complete adapter that runs for real (a file engine that is SQLite inside); the tests load it |
| `tests/run_tests.py` | the tests (section 11); `python shedkeeper.py selftest` runs them |
| `SECURITY.md`, `CHANGELOG.md` | how to report a vulnerability and what counts as one; what changed in each version |
| `shedkeeper.ini` | the shipped settings; the comment above each key is also its documentation |

Where code reaches the outside: `pre_tool.py` runs `git` (to list what a rollback would overwrite), the engine tools (`pg_dump`, `pg_restore`,
`psql`, `createdb`) through `db._run`, and nothing else. No project code is ever executed to find an address.

---

## 3. The path of a call

The hook gets JSON on standard input: `tool_name`, `tool_input`, `cwd`, `session_id`, `transcript_path`, and for an MCP tool
`mcp_server {name, source}`. It answers with exit code 0 and at most one JSON object (if the hook itself fails after it has read a guarded tool's input -- the output cannot be written, memory runs out -- it ends with **exit code 2** and the reason on stderr: Claude Code blocks the call on 2 and only warns on any other code, so a hook that died with 1 would let the command run): a refusal
(`permissionDecision: deny` with the reason), a note to the agent (`additionalContext`), and/or a note to the person (`systemMessage`).
A hook that times out, or whose interpreter cannot start, lets the call through -- by the contract of Claude Code. So the hook budget is
a *calculation* (section 5.3), not a guess, and the installed command prints a loud warning instead of failing silently when `python` is
missing.

### 3.1 Edit / Write / NotebookEdit / MultiEdit

1. The path is resolved against `cwd`. A path inside a store is refused: the store is changed by a person only.
2. If it is an ini file the kit reads and the edit would **weaken** protection -> refused (section 7); otherwise a change of a setting is
   shown to the person (`systemMessage`).
3. The file does not exist -> nothing to copy, the call goes.
4. The personal folder of the store is found or made. `copy_version` writes the copy to a temporary name, **reads it back and compares size
   and sha256 with the original**, and only then renames it into place. A copy that does not pass is not a copy.
5. Copy taken -> the call goes, with a note to the agent where the copy is. No copy (full disk, no rights, over `max_copy_mb` with
   `on_too_large = deny`) -> the call is refused.

### 3.2 Bash / PowerShell

In this order, each stage able to refuse:

1. **Settings.** A command that *runs* `shedkeeper.py set ...` tells the person what it changes (section 7).
2. **Deletion** (`rm`, `del`, `Remove-Item`, `git clean -f`, `find -delete`, `robocopy /MIR` ...): refused, with the ready command
   `shedkeeper.py trash <paths> --reason ...`. A deletion of paths that do not exist removes nothing and is not stopped.
3. **Writing** (a redirect, `tee`, `sed -i`, `cp`/`mv` over an existing file, `Set-Content`, a literal write in `python -c`, the files a git
   rollback would overwrite): every existing target gets a verified copy first. Limits: `cmd_max_files`, `cmd_deadline_s`; a target inside the
   store, or a target list that cannot be named in full -> refused (what is not named cannot be copied first).
4. **Databases** (section 5.2): the destroying actions are recognised, the databases named, **every refusal comes before any copy** (so a
   command that overwrites a file and destroys a database gets both copies or is refused), then the copies are taken.

### 3.3 MCP tools (`mcp__*`) -- NOT SHIPPED in this release (databases, section 12)

The matcher of the hook is `.*`: every tool reaches it, an MCP tool among them (the closed list of the tools that only read, `config.READ_ONLY_TOOLS`, is cut by the hook itself, and every other tool is checked
by the protected list); an MCP tool that destroys no database passes unseen. A call destroys when
a string argument *starts like* SQL of the destroying class (so the chat message "please delete from my list" is not read as SQL), or the tool
name holds `drop`, `delete`, `reset`, `truncate`, `wipe` or `purge` **and** either the tool name itself names an object of a database **or** the server is
a server of a database. The database behind the server is read from the *settings of the server* (`.mcp.json`, `~/.claude.json`, a plugin's `.mcp.json`, by
`mcp_server.source`). Then it is the same decision as for a command.

**The name rule is two signs, and neither depends on a list of products (the verbs are a list: see below).** (1) *The tool names an object of a database:* `database`, `db`, `table`,
`schema`, `branch`, `collection`, `index` or `bucket` -- a whole word of the tool name, split at `_`, `-`, a space or a capital letter, in the singular or
the plural (`delete_database`, `reset-branch`, `DropTable`, `truncate_collections`) -- on ANY server: the object is what a database tool destroys, whoever
sells it. `delete_message`, `delete_file`, `delete_event`, `delete_channel` name no such object and pass. (2) *The server is a server of a database:* its settings give the address of a
database, or a word of a database stands in its name, command or address (`postgres`, `pg`, `sqlite`, `supabase`, `neon`, `mysql`, `maria`, `mongo`, `redis`,
`turso`, `libsql`, `planetscale`, `cockroach`, `duckdb`, `firebase`, `firestore`, `convex`, `appwrite`, `d1`, `prisma`, `db`, `sql`, `database`): then every destroying
word in the tool name counts. That list is only a help for names that carry no object (`wipe_all` on a server called `turso`); sign (1) is why a product that is not on it
is still seen when its tool says what it destroys. **The verbs are a list, and a limit:** `drop`, `delete`, `reset`, `truncate`, `wipe`, `purge` and no others -- `remove_table`, `destroy_database`, `clear_table` pass unseen -- and a verb is looked for as a part of a word (`name_word`) while the object is a whole word (`name_object`), so `list_deleted_tables` is stopped (a false stop) and a glued `droptable` names no object. Widening the verbs is a later version. The price is a false stop for a tool of another kind that carries such a word (`delete_branch` of a code-hosting
server): the refusal says that the database behind the server cannot be named, and the key `mcp_no_db` (the names of servers whose calls always pass) is the way out.

**What the reader could not read is a doubt, and a doubt takes the strict side.** The arguments are read up to 400 strings, 8 levels of nesting (counted for containers and strings, not for a number or an empty container) and 5 MB a string. A part that was not read is a line in the journal (`mcp_read_limit`) and, when the call goes to a server of a database or the part that was read looks like SQL, the call is treated as destroying: the database behind the server is copied, or the call is refused when that database cannot be named. A big harmless call to a server that is no database, with nothing SQL-like read, passes (refusing it would only teach the person to switch the guard off). Declared: a `DROP DATABASE other` in the part that was not read is copied for the database the server names, not for `other`.

### 3.4 What the hook says back

Every refusal and every warning has two readers (`advice.py`). The person gets a short English line. The agent gets, after the older text
(the reason, the ready command after `Run:`), an instruction: *tell the user, in the language of your conversation, what happened and why;
ask whether they will do it themselves or want you to; the options that apply: (a) the AGENT does it: the exact command, (b) the USER does it,
(c) a SETTING: the exact `set` command and what stops being protected; change a setting only after the user agrees.* The hook does not know
the language of the conversation; the agent does. Only the options that apply are listed (a refused deletion has no setting; an engine
that is not copied has no `dbcopy`).

---

## 4. The store

One folder per **fixed volume**, outside the projects (so `git clean -x` in a project cannot reach it): `<X>:\_shedkeeper\` (name from
`store_dir_name`). Inside, **one personal folder per user**, named by the login name, the way the Windows recycle bin keeps a folder per user.

```
<X>:\_shedkeeper\<login>\
    .shedkeeper-user                  label: product, SID, login, full name, created
    README.txt                        for whoever opens the folder
    log.jsonl                         one line per hook firing and per notable event
    trash\<day>\<DRIVE>\<original path>              what was "deleted" (a move/rename on the same volume: instant, copies nothing)
    trash\_index\<day>.jsonl                          one line per move        (id  t:<day>:<n>)
    versions\<day>\<DRIVE>\<folders>\name.v_<HHMMSS>_<n>.ext      copies made before changes
    versions\_index\<day>.jsonl                       one line per copy, with sha256   (id  v:<day>:<n>)
    dbcopies\<day>\<engine>_<name>_<time>.<ext>       copies of databases
    dbcopies.jsonl                    one line per copy of a database; and `op: restore` lines   (id  d:<day>:<n>)
    proofs.jsonl                      the proofs of engine adapters (`proved` / `prove_failed`)
    confirm.jsonl                     the journal of confirmations: codes issued, the person's word (`human`), codes spent (`used`), what `set` changed (`state`)
    guide.jsonl                       the once-only facts: survey offered/done, unsupported-engine warning given
    installs.jsonl                    what `install.py` wrote, to prove a line is its own
    transcripts\<project>\<session>.jsonl             copies of session transcripts
```

Facts that decide behaviour:

- **Nothing in the kit deletes anything from the store**, and there is no command that empties it. Only a person cleans it. The kit
  deletes only its *own failed attempt* (a `.part` file of a copy that did not verify, the empty name it reserved a moment before).
- **Whose folder it is** is decided by the *owner and the permissions read from the file system*, not by the label (which anybody can
  write). With `private_store = true` the folder is created closed from the first moment (the security descriptor is given to the call that
  creates it) to the user and SYSTEM only. A name taken by something else is not touched: the next free name `<login>.2` ... is used.
- **Indexes** are JSON lines appended under an exclusive lock on a byte far behind the data (readers never touch it); a torn last line gets a
  newline before the next record, so records never glue together; a record that cannot go to its day index goes to an `_orphans` file.
- **Writers outside the hook.** `shedkeeper.py` writes into the store from its own process (the hook cannot see that, and should not). The hook
  refuses every write of the *agent* into the store.
- `find` and `restore` say **`NOT SEARCHED: <path> -- <why>`** (exit 2) when a place that may hold this person's copies could not be looked at:
  "nothing found" is then not an answer about that place.
- A volume that is removable or a network drive gets no store; every change to an existing file there is refused (nowhere to put the copy),
  unless a person adopts a folder there (`adopt X:`).

### 4.1 The record of a copy of a database (`dbcopies.jsonl`) -- NOT SHIPPED in this release (section 12)

`id`, `engine`, `identity` (section 5.2, one string per database, no password), `name`, `url` (the address **with `***` where the password was**,
or the file path), `size` (of the database), `copy` (path), `copy_size`, `sha256`, `verified` (how it was read back), `command` (the command that
caused it, addresses masked and every known secret of the connection replaced), `session`, `via` (`hook`, `dbcopy`, `prove`), `mcp_server`,
`time`. A restore adds `{"op": "restore", "of": <id>, "to": ..., ...}`. **Before a record is written the framework looks for the secrets of
the connection in it** (`db._leak`); a record that would carry one is not written and the copy is dropped -- an adapter's mistake never reaches the disk.

---

## 5. The decisions

### 5.1 Files

| Case | Decision | Why |
|---|---|---|
| Edit/Write of an existing file | verified copy, then the call | the commonest loss is a rewrite that cannot be undone; a copy costs a file |
| the copy cannot be taken or does not verify | **refuse** | I1: an unverified copy is the illusion of a copy |
| file bigger than `max_copy_mb` | `on_too_large = deny` refuses; `allow` passes and logs | a limit that silently passes is a hole; one that refuses names itself |
| deletion | refuse **with the trash command** | a ban alone teaches the agent to look for another way; a working reversible alternative does not |
| deletion of a path that does not exist | passes | nothing is removed |
| target inside a store | refuse | the store is what the guarantee rests on |
| targets that cannot be named in full | refuse | what is not named cannot be copied first |
| more than `cmd_max_files` / not done in `cmd_deadline_s` | refuse, not skip | skipping the rest would be an unreported partial protection |
| `>>`, `tee -a`, `Add-Content` | passes | appending removes nothing that was there |

A copy of a file that is "the same" as the previous copy is skipped only when the previous copy, **read at that moment**, still has the
file's content -- a damaged or shortened copy is not trusted.

### 5.2 Databases -- NOT SHIPPED in this release (section 12)

**What counts as destroying.** SQL, read *outside comments and string literals*, each statement of a `;` list on its own: `DROP DATABASE |
SCHEMA | TABLE | OWNED`, `TRUNCATE`, `DELETE` and `UPDATE` without a `WHERE`, `ALTER TABLE ... DROP COLUMN`, `ALTER TABLE ... ALTER COLUMN ... TYPE`.
Given to a client (`psql -c/-f/<`, heredoc, here-string, pipe from `cat`/`echo`; `sqlite3 FILE "SQL"`), also inside a `DO $$ ... $$` block (its
body *runs*, so it is read as SQL; a `CREATE FUNCTION` body is only a definition and is not). `dropdb`; `pg_restore --clean -d`. Commands of tools from a
table (`prisma migrate reset`, `prisma db push --force-reset`, `supabase db reset`, `rails db:drop|reset|schema:load`, `manage.py flush`,
`alembic downgrade`, `artisan migrate:fresh`, `typeorm schema:drop`, `drizzle-kit push`, `sequelize db:drop`, `docker volume rm|prune`, `docker compose down -v`;
`db_command` lines of the ini add yours). SQL that cannot be read to the end -- a file that is missing or over 5 MB, `\i`, `\!`, `\gexec`,
`.read`, a pipe from a program -- **counts as destroying**: the strict side is a *copy*, not a refusal.

**What does not**: a mention (`grep "DROP TABLE" f`), `DELETE`/`UPDATE` *with* a `WHERE` (also `WHERE true` -- the class is "no `WHERE` of its own"),
`DROP VIEW|INDEX`, `CREATE`, `INSERT`, migrations forward.

**How the database is named** (never guessed; two candidates, a `${...}`/`%(...)` substitution, or an incomplete address -> *cannot be named*):
the command (`-d -h -p -U`, an address, `dropdb NAME`, `sqlite3 FILE`; for `DROP DATABASE name` the database to copy is `name`, **not** the one the client
is connected to); the environment of the hook (`DATABASE_URL`, `PG*`); the project, from the folder of the command upward (`.env`, `prisma/schema.prisma`,
`supabase/config.toml`, `alembic.ini`, Rails `config/database.yml` without ERB, Laravel `DB_*`); the MCP server's settings; or the agent
says it: `SHEDKEEPER_DB=<address or file>` in front of the command (nothing else is read then). A host that is not `localhost`, `127.0.0.1`,
`::1`, empty or a socket folder is **remote** (a private network counts too: it is the network).

**Identity** of a database (for the list and for "is there a fresh copy of *this* database"): PostgreSQL `pg://<host>:<port>/<name>` (every
local spelling of the host is one), SQLite `sqlite:<full path>`, an adapter's engine whatever string the adapter's `name` gives (no password in it).

**The table of decisions** (the order matters: refusals first, then copies):

| Case | Decision |
|---|---|
| nothing destroyed | passes |
| named, local, `<= max_db_mb`, copy taken and verified | passes; the note to the agent says where the copy is |
| a verified copy of **this** database made by `dbcopy` is younger than `db_fresh_minutes` | passes with no new copy |
| the database does not exist (the server itself says so; a SQLite file that is not there) | nothing to copy; passes |
| **cannot be named** | **refused**, with `SHEDKEEPER_DB='<address or file>' <your command>` |
| **remote** | `on_db_remote = deny` (default) refuses with the ready `dbcopy`; `allow` passes, logged |
| bigger than `max_db_mb` or did not fit `db_deadline_s` | `on_db_too_large = deny` (default) refuses with the ready `dbcopy` (it has no such limits); `allow` passes, logged |
| the copy could not be taken (no tool, no right, check failed) | **refused**, with the reason |
| an engine nothing here can copy (or an adapter that is not proven / did not load) | `on_db_unsupported = allow` (default): passes with one warning to the person and the agent, **once per project and engine**; `deny` refuses |
| `db_protection = false` | passes; `status` says the protection is OFF |

Reasons, in plain words:

- **Copy before, not an apology after.** A database cannot be un-dropped. A dump taken first is the only way back, and for PostgreSQL `DROP`,
  `TRUNCATE` and DDL commit at once.
- **The dump is read back in full.** For `pg_dump -Fc`, listing the table of contents does not see a truncated file; the dump is read through
  `pg_restore -f <null>` (exit 0 and nothing on stderr). For SQLite, `PRAGMA integrity_check` on the copy. A copy that does not pass is removed and the action refused.
- **SQLite by the backup API**, not by copying the file: a plain copy loses what is still in the write-ahead log.
- **The size is measured before the copy** (`pg_database_size`), so a huge database is refused with the way to copy it outside the limit,
  not discovered halfway. The defaults (200 MB, 25 s) come from one measurement: a 248 MB database dumped in 9 s with a 0.6 s read-back
  on the authors' machine, i.e. ~26 MB/s; 200 MB is about 8 s, the 25 s is about three times that.
- **"Cannot be named" is a refusal, not a guess.** A wrong guess copies the wrong database and the right one is destroyed unprotected. The
  refusal carries the command that names it.
- **An unknown address scheme (`foo://`) is not "unsupported"** -- it is an address that cannot be named, and is refused. A scheme of a
  *known* engine this kit does not copy (`mysql://`, ...) is "unsupported": the command passes with a loud warning, because the alternative
  (refusing every MySQL command forever) makes the person switch the whole thing off.
- **Unsupported is told once per project and engine**, then passes quietly with a line in the log: a warning that repeats on every
  command teaches everyone to ignore it, and then to switch the guard off.
- **The identity of a copy by `dbcopy` is the database, not the command**, so a `dbcopy` of `127.0.0.1` also covers `-h localhost`. A copy found
  *by MCP server name* rests on the word of the agent that it was a copy of the database behind that server (declared limit).
- **A restore never goes into an existing database** (section 9, step 7).

The budget of one hook run is `cmd_deadline_s (20) + git (10) + db_deadline_s (25) + start (~3) = 58 s` against the **90 s** timeout the installer sets; `status`
checks the margin (>= 10 s) against the timeout *read from the installed entry* and says `TOO SMALL` when a deadline was raised past it.

### 5.3 Settings

Every setting has a **weight**, decided by the code (`config.WEIGHTS`; the `@weight` comments of the shipped ini repeat it for the person, and a test keeps the two equal): `protection` (some value of it stops protecting something), `tuning` (a limit or a
place; protection stays), `notice` (only what you are told). *No setting is a silent switch*: each one that lets an action through without a copy
says so to the person and writes a line in the log; and **a copy that was tried and failed with an error is never let through by any setting** -- the only
way past is `db_protection = false`, which `status` shows. One exception is named: `on_db_too_large = allow` also lets through a database copy that **did not finish in time** (the dump, or the check of it, was cut off by `db_deadline_s`) -- too slow to copy counts as too big to copy. A value that is not understood becomes the **safe** one, and `status` names the reason.

The *description* of each key lives in the comment above it in the shipped `shedkeeper.ini` (`config.describe` reads it; `settings` prints it); its *weight* does not: an unreadable, edited or missing ini changes no decision. `store_dir_name` and `pg_dump_path` are weight `protection` (the first decides which folders count as the store, the second which program makes and checks the copies).
Settings are read from the shipped ini, then from the personal `~/.shedkeeper/shedkeeper.ini` (later wins; the lines of `mcp_no_db` and `db_command`
add up). `set` writes to the personal file, because a plugin update replaces the shipped one and would silently put a protection back (or take a person's choice away).
`SHEDKEEPER_INI` names a single file and then only that file is read (the tests use it).

---

## 6. Why it is built this way (the cases behind it)

Losses of this class are reported publicly. These are issues of the Claude Code repository (`github.com/anthropics/claude-code/issues/<N>`) in which an
agent ran a command that destroyed the data of a database. Each opened as an issue on the date given (checked on GitHub on 2026-10-02, with the command
checked on 2026-10-01); what was lost is as the person who wrote the issue reported it and was not checked against the text of the issue here.

| Issue | Opened | Command | What was lost |
|---|---|---|---|
| [#5370](https://github.com/anthropics/claude-code/issues/5370) | 2025-08-08 | `pnpm prisma migrate reset --force` | a development PostgreSQL database reset, after the person had told the agent to make sure the current database was not overwritten |
| [#14411](https://github.com/anthropics/claude-code/issues/14411) | 2025-12-18 | `npx prisma db push --accept-data-loss` | a production database |
| [#14944](https://github.com/anthropics/claude-code/issues/14944) | 2025-12-21 | `docker volume rm` | the data of a local Supabase PostgreSQL in Docker: "I wiped your data by removing the Docker volumes", the agent wrote |
| [#33183](https://github.com/anthropics/claude-code/issues/33183) | 2026-03-11 | `prisma db push` | a production PostgreSQL database on Railway; partly restored from a development backup and an export; the last month of data lost |
| [#34729](https://github.com/anthropics/claude-code/issues/34729) | 2026-03-15 | `npx prisma migrate reset --force` | a local PostgreSQL database (localhost:5432); not recovered |
| [#36183](https://github.com/anthropics/claude-code/issues/36183) | 2026-03-19 | `npx prisma db push --force-reset` | a production database on Railway with no backup; its tables were created again empty |
| [#45463](https://github.com/anthropics/claude-code/issues/45463) | 2026-04-08 | `dropdb genesis_master` | a local PostgreSQL 16 database; the return is not described |
| [#47694](https://github.com/anthropics/claude-code/issues/47694) | 2026-04-14 | `npx supabase db reset` (instead of `migration up`) | a local Supabase database; not recoverable, the seed brought back only a catalogue |
| [#57165](https://github.com/anthropics/claude-code/issues/57165) | 2026-05-08 | `supabase db reset` (instead of the `supabase migration up` it was asked for), by a subagent after the dump step had failed | a local Supabase database; partly rebuilt in a few hours; the source files were intact |

Two more are **reported in the press**, and nothing is claimed here beyond the reports: an agent of Replit ran commands on the production database of an
application during a code freeze (July 2025; The Register, <https://www.theregister.com/2025/07/22/replit_saastr_response/>); and an agent of Cursor deleted a
volume that held a production database on Railway with one call of the platform's API (April 2026; Decrypt,
<https://decrypt.co/365897/ai-agent-deletes-startup-database-9-seconds-founder-says>).

All nine issues are a shell command or a migration tool, which is why the hook reads the text of commands. The decisions rest on the mechanism, which
anyone can reproduce:

- A shell command's effect is invisible to the agent until it has happened, and a database has no recycle bin. So the guarantee has to sit
  *before* the action and has to be a **copy that was read back**, not a promise.
- A bare "no" makes an agent look for a way round (another command, another tool). So **every stop carries a next step** that works.
- Protection that is hard to live with gets switched off. So the strict side of every doubt is *a copy* (cheap) rather than *a refusal*
  (expensive), refusals name their own way out, warnings are said once, and the heavy switches are weighed and need the person.
- A guard that can be argued with is not a guard. So a weakening of protection needs words the *person* typed (section 7), not a question the
  agent can answer itself.
- What the hook cannot read (code run by a program, an address built at run time) is **declared**, not covered by a pretence of coverage.
- A new engine is the place where an unproved claim does the most harm: a copy that does not restore is worse than no copy, because it is
  believed. So an adapter is protection only after the proof gate.

---

## 7. A weakening of protection needs the person's code

A setting of weight `protection` whose new value weakens protection (`config.effect_for` says what stops being protected; the shipped default
is never a weakening) is not applied on the agent's word. A weakening that stands in an ini file **acts** (the hand of the person edits the file); the start of a session marks the one that was not made through `set`.

1. `python shedkeeper.py set KEY VALUE` does **not** write. It issues a one-time code `SK-nnnnnn` (valid 30 minutes, kept in `confirm.jsonl`), prints
   exit code 4 and tells the agent: *ask the user to type exactly `CONFIRM SK-nnnnnn` in their own message; then run the same set command again.*
2. **Where the person's word comes from: the hook of the prompt.** `hooks/prompt.py` is the hook of the event `UserPromptSubmit`. Claude Code runs it when the
   person submits a message and gives it the text of that message (the field `prompt`); the agent cannot make that event happen by running a command. For every
   `CONFIRM SK-nnnnnn` in the text THE PERSON TYPED the hook checks that such a code was issued, is not spent and is not expired (`confirm.receive`), writes a line `human` into
   `confirm.jsonl` and says one line to the agent and the person (*received / unknown / expired / already used*). It blocks nothing, adds nothing else to the
   context, and for a message with no code it costs one regular expression and says nothing. The installed timeout is 30 seconds; the measured run is a fraction of a second.
   A fault is told (*the code could NOT be recorded*), not hidden. **Not everything that arrives through that event is the person's typing** (measured while this kit was built: the text of a task
   notification, a message of another agent or teammate, a reminder of the runtime reach `prompt` too, wrapped in tags; what an agent can influence -- the command line of a background task, a helper's
   message -- is inside). `confirm.split_prompt` takes as the person's only the text BEFORE the first wrapper tag (`<task-notification`, `<system-reminder`, `<teammate-message`, `<agent-message`) and
   nothing from a message that starts with one or with the head the runtime puts on a message of another session; a code found elsewhere is IGNORED, and the person and the agent are told it was ignored.
   The earlier design read the person's message from the record of the conversation -- a file the agent
   can append to -- and guarded that file by a list of forms of commands; a list is never complete against an agent that looks for a way round, so that support is gone.
3. On the repeat, `set` looks in the journal for a line `human` for its open code, written after the issue. Found -> applied, the code is spent *before* the change is made.
   Not found -> refused, nothing written; the text says that when the person did type the code and this stays, the hook of the prompt is not running (`status` shows the hooks).
4. **What `set` records.** After the ini is written, a line `state` (the key, the value, `confirmed` with its code, or `free` when no code was needed). That line is what makes the
   record that the value was made through `set` (item 5). If it cannot be written, `set` says so (`NOTE: the value is in effect, but the record ... could not be written`); the start of a session then marks the value "not through `set`".
5. **The hand of the person acts; the journal is a reminder** (`config._integrity`, `confirm.approved`). A value of weight `protection` that weakens and stands in an ini file ACTS, whoever wrote it:
   the person who edits their own file, or a program the agent ran by a path the guard in item 6 cannot read -- ShedKeeper cannot tell the hands apart and does not pretend to. What the journal
   adds: a weakening that was **not made through `set`** -- the newest line `state` of its key does not hold exactly this value, confirmed, with the issue and the person's line `human` for the same
   code, key and value -- is listed in `settings.unconfirmed` (for `mcp_no_db`, the names that were not confirmed) and carries the mark "not through `set`" in the reminder at every session start (to the person
   in `systemMessage`, to the agent in `additionalContext`, with the way back) and in `status`. The hook says **nothing** about it at its decisions: ShedKeeper does not get in the way. A tightening made
   through `set` (it leaves a line `state`) does not let an older confirmation clear the mark of a later hand edit: the newest line of the key decides. Declared: a tightening done BY HAND leaves no line, so a
   weakening written by hand afterwards to the very value that was confirmed before is not marked. A check that cannot be made (the journal cannot be read or the module fails) marks every weakening
   "not through `set`"; it never makes one stop acting. The agent cannot clear the mark by `set` of a value already in force: that changes nothing, no code is asked and no line is written.
6. An Edit/Write/MultiEdit of an ini file the kit reads that would *weaken* protection is **refused by the hook** with the same road (`set`); an Edit whose check cannot be made is
   refused too. A tightening needs no code. A **shell command of the agent that writes into a settings file** (`>>`, `sed -i`, `tee`, `cp`, a python write with a literal path in a mode
   that changes the file -- `w`, `x`, `a`, `r+`; `a` and `ab` are read as `>>` (`recognize.PY_APPEND`, `append_targets`): no copy, no refusal for size, the guarded places stay guarded -- also the redirect of a wrapper, and a command behind `timeout 5`, `nice -n 5`, `sudo -u U`, `xargs -n 1`, `env`, `nohup`:
   `recognize._strip_to_head` peels them with their flags) is refused whatever it writes. A write whose target is **not a literal path** (`recognize.unreadable_writes`) is refused
   when the command points at what is guarded -- `.shedkeeper`, `shedkeeper.ini`, `SHEDKEEPER_INI`, `confirm.jsonl` or `proofs.jsonl` anywhere in the text: the agent is told to write the
   path out literally; `echo x >> $LOG` with nothing guarded in sight is not stopped. **These guards are lists of forms of commands: they stop an agent that mistakes the way, not one that
   looks for a way round** (a quote inside a word, a mask, a name made of variables, a program that opens the file itself). What they do not see is not stopped (item 5; README, "Risks"). The same
   guard refuses an append into the store (`>>`, python `a`): the store, the journals in it too, is written by a person or by ShedKeeper.
7. The same code mechanism confirms an **adapter** (section 9.6): `prove` issues a code bound to the sha256 of the adapter file, the person types it, `activate ENGINE` applies it. The code is *spent before* the change is made, so a failed write cannot be replayed.
8. **A damaged journal.** A `used` line cut off by a crash that still names its code spends that code. One that names none leaves it unknown which codes are spent: `set`, `prove` and `activate` REFUSE,
   naming the file and the line. `python shedkeeper.py quarantine` is the way out: the damaged lines of `confirm.jsonl` and of `proofs.jsonl` go to `.bad` files beside them (nothing is deleted), every
   code issued so far is closed, and every adapter a lost proof line may concern must be proved and activated again. The unreadable or damaged journal of ANOTHER store (a disk that is not there) is skipped:
   a code issued there cannot be used here, and a fault there does not lock this project; the project's own journal that cannot be read refuses.

Two things cannot weaken and are let go: setting a value that is already in force, and a shorter list of `mcp_no_db`.

**The reminder.** At every session start, while any `protection` setting stands at a weakening value, the person (`systemMessage`) and the agent (`additionalContext`) get one line: what is
weakened, the key, how to put it back; a weakening that was not made through `set` carries the mark "not through `set`" (item 5). Nothing weakened -> silence.

**What the agent is given is facts** (the contract of this version): every text of the kit for the agent says what was stopped, why, and the lawful way on (a ready command or "ask the user"); no
text orders the agent how to behave ("do not look for a way round", "never ..."). The rule of behaviour belongs to the person, in their own system: at the first session start of a user the agent is given,
once per USER (`survey.rule_offer_once`, the record `rule_offered` in `guide.jsonl`, looked up in every store of the user), the facts about ShedKeeper and ShedKeeper's recommendation to the user -- to ask the
agent to write, in the file of instructions the agent reads (`CLAUDE.md` for Claude Code), a rule by three principles (why ShedKeeper is here; when it stops an action, the lawful way or a question to the
user, not a way round; only the user weakens the protection). ShedKeeper does not write that file, does not read it and does not check that the rule is there. README: "A rule for your agent", "Risks",
"What ShedKeeper puts into your context".

**Declared limit.** The line `human` is the person's word as far as the event `UserPromptSubmit` is: a route that delivers text an agent made as a plain message of the person (a scheduled prompt) is not told from
their typing by anything in the input -- not measured here and not covered; the wrapped routes are (item 2). The journal is a file in the store: the hook refuses the agent's Edit / Write and a shell write with a literal path into it, and a command that points at it by name with a target it cannot read;
a program the agent runs that writes the ini AND the three lines of a confirmation (`issue`, `human`, `state`) into the journal by a path it builds itself is not seen and its weakening would act -- no
text-reading guard can stop it; what would remain is the person's own sight of the settings (`settings`, `status`). A program that writes the ini alone is marked "not through `set`" at the next session start.
Declared, not covered by a pretence of coverage: protection against an agent that was taught to do harm is cybersecurity, which ShedKeeper does not promise (README, "Risks": a command made confusing on purpose,
a program that builds the path, a mod of Claude Code, a scheduled prompt that carries code). The ground of the rebuilding is the reasoning that a guard by the text of a command suits an agent that errs and does not suit one that searches.

---

## 8. What the system does not see (declared limits)

- **Inside a program that runs.** `python script.py` or `node migrate.js` that deletes, overwrites or destroys passes: the hook reads the text of the command and
  the SQL it hands over, not what a program does. Code in `bash -c`, `powershell -Command`, `cmd /c`, `python -c` text *is* read for deleting and writing files; a script file is not.
- **SQL inside the code of a program is not read.** `python -c "import sqlite3; ..."`, `psycopg2` in a script: the command is `python`, not `sqlite3` or `psql`, so no copy is taken and nothing is refused (measured on `python -c` with `sqlite3`). The guarded forms are the commands `sqlite3` and `psql`, the destroying commands of tools, and MCP calls. Reading SQL out of program text is a later version.
- **Paths and SQL that are not in the text**: variables, computed paths, piped input from a program, SQL assembled at run time.
- **Project code is never run** to find an address (Django `settings.py`, `drizzle.config.ts`, `knexfile.js`, Rails ERB are not read): give `DATABASE_URL` or `SHEDKEEPER_DB`.
- **A copy is of one database.** Roles and other objects of a cluster are not in `pg_dump` of a database.
- **Docker volumes**: recognised by their text, never tried (no Docker on the authors' machine).
- **Inside a container.** `docker|podman exec [flags] CONTAINER CMD`, `docker [container] compose [flags] exec [flags] SERVICE CMD`, `docker-compose exec ...` -- the inner command (also `sh -c "..."`) is read like the same command on this machine and every destroying action found there is marked "inside a container": the copy cannot be taken from inside, so the database cannot be NAMED -> a refusal with the fact and the lawful way (`SHEDKEEPER_DB=postgresql://localhost:<port>/<database>`, the published port; or the user). A command the reader cannot see into (a program that calls the client, a script file) is not read, as everywhere.
- **Hosted projects** (`supabase db reset --linked`) have no address: refused with the ready command.
- **Naming by `SHEDKEEPER_DB` or `--mcp-server` rests on the agent's word.** It can name a database that is not the one its command hits.
- **The size is measured, then the dump is taken**: a database that grows between the two is bounded by `db_deadline_s`, not by `max_db_mb`.
- **The ordinary ways of removing the hooks need the person's code** (`shedkeeper/selfguard.py`, key `uninstall` of the journal of confirmations): an Edit / Write / MultiEdit of a Claude Code settings file that takes a hook entry of this kit out, switches the plugin off (`enabledPlugins`) or sets `disableAllHooks`; `python <this kit>/install.py uninstall`; `claude plugin uninstall|remove|disable shedkeeper[@...]` (read inside `bash -c`, wrappers and PowerShell `&` too). The code is bound to the action it was issued for (the edit with its effect, the command) and works once. NOT read: a settings file written by a shell command, a program that builds the path, a variable in front of `install.py`. `install.py check` shows whether the hooks are in; when the whole kit is gone (hooks out, plugin off, `disableAllHooks`) nothing runs, and the session start cannot say so.
- **A timeout or a crash of the interpreter** lets an action through (the contract of Claude Code); hence the 90 s and the loud warning of the installed command.
- **A once-only note is marked as given BEFORE it is delivered** (`survey.offer_once`, `survey.rule_offer_once`: the record `survey_offered` / `rule_offered` in `guide.jsonl` is written first, and
  nothing is offered when it cannot be written). If the start of a session does not reach the agent -- a headless first start, or an output the runtime cut at its ceiling of 10 000 characters --
  the note is not repeated. Cost: the person misses a one-time suggestion (the setup check, the recommendation about the rule); nothing is unprotected by it. It is taken on purpose: a note that could not
  be remembered would come at every start. The way back is to run `python shedkeeper.py survey` by hand, and to put the rule of section 7 into the file of instructions.
- **Limits of the protected list on files (the second correction of 0.3.0).** Each is named with what it costs and when it stops:
  - **A list confirmed by a build before 0.3.0 has no second witness.** The mark of such a list does not hold the shape of the confirmed entries, and the list does not say in itself that it
    was confirmed. With the journal of confirmations removed or garbled it reads as a draft (no check), and an exclusion added or a target changed by hand is not noticed. It gets the witness at the
    next confirmation (`protect confirm`). Cost: the guard of such a list is the guard of the earlier build until then; the builds that wrote such lists are workshop builds, and 0.1.0 and 0.2.0 were closed repositories, so no outside user has one.
  - **The folder of a list removed by a command the kit does not recognise.** The folder `protect/<key>` of the store is the sign that makes the hook look at a list. A recognised `rm` into the
    store is refused, but a folder removed by a program the kit does not read (a script, a tool of another kind) is no list for the hook, and the mark that stays in the journal of confirmations is
    not consulted for it. Cost: the guard of that project is off until the list is made again; it was so before this correction and was not measured.
  - **A list under the folder of the step is found through the name of its project.** The list holds the folder of its project (`project` in `protected.json`); a list file that is damaged or names
    none cannot be told to stand under a folder, and a step in the folder above it does not see it. The step in the project's own folder does. **Limit (pre-existing, not changed by order 12):** a list
    file under the folder that cannot be parsed, or that is gone while the journal says it was confirmed, names no project and is NOT seen from a folder above it; steps from there are checked as if
    that list were not there. The way out today: open the session in that project's folder.
  - **A BROKEN list under the folder of the step refuses every step that the guard checks (order 12, v6, variant A).** A broken list of the step's own folder or of a folder above it refuses every step, and so does a broken list found UNDER the folder of the step (or under the folder of the session, `CLAUDE_PROJECT_DIR`): an edit tool, the shell, PowerShell, an MCP tool, every tool but the readers and the control tools. Order 11 let an edit through when its path was proven to lie outside the project of the list; order 12 took that back, because a list may guard trees OUTSIDE its project (`protect.make_entry`) and a broken list does not tell its targets (the mark keeps only hashes), so no step can be proven not to reach what it guards. The refusal names the project of the list, says where the list was found (under the folder of the step, or of the session), and gives two ways out: repair the list (or rebuild it with `protect add ...`, which needs your code), or open the session in the folder of the project you work on (then that list is not under the folder of the step). Cost: a session opened in a folder over several projects is stopped by one broken list among them.
  - **Reparse points of other kinds that the system will not open.** A symbolic link of WSL (tag `0xA000001D`), a file-socket `AF_UNIX` (`0x80000023`) are taken for ordinary files like a cloud
    placeholder; the copy of one that cannot be opened refuses the step until the file is excluded (`protect exclude`). Not measured: the machine of the authors has none.
  - **A folder that moves inside a root named through a link, when the system gives no final path of the root.** The root is then compared as written and the fall is said (`degraded:`), but a
    moved folder may be misread as one that left the root until the next full walk.
- **Windows only** in v1: drive letters, junctions, volume types, the security descriptors of the store.
- An administrator of the machine can take over any folder, the store included.
- **An adapter of a user runs inside the hook process.** A file in the adapters folder is executed, with the rights of the hook, at every hook run. The proof gate shows that an adapter *can* round-trip data; it does not show that
  it cannot do anything else. Keep the adapters folder yours. An adapter that hangs while loading hangs the hook until its time limit, after which the runtime lets the action through.

---

## 9. Adding an engine: the engine adapter -- NOT SHIPPED in this release (section 12)

### 9.1 What an adapter is

The kit knows PostgreSQL and SQLite. Everything else about a database is engine-independent and already done: the decision table, the store, the list of copies, the
fresh-copy rule, `dbcopy`, `find`, `restore`, `status`, the refusal texts, the secret scrub. What differs between engines is a short list of questions, and an
**adapter** answers them for one engine. PostgreSQL and SQLite are adapters of the same contract (`engines.PostgresEngine`, `engines.SqliteEngine`); yours is a
Python file in the **adapters folder** that defines `ADAPTER`.

### 9.2 Where, and how it is loaded

- **Folder**: `~/.shedkeeper/adapters/` (`config.adapters_dir()`; `SHEDKEEPER_ADAPTERS_DIR` names another one, `SHEDKEEPER_USER_HOME` replaces `~`). It is the same personal folder that holds the
  personal ini, and for the same reason: an update of the kit replaces the kit's folder and must not take your adapter with it; it is outside the store (the store is a person's folder of copies, and the agent is refused writes into it).
  `python shedkeeper.py status` prints the folder and every adapter in it.
- **File**: `<anything>.py` (a name starting with `_` is not an adapter). It must define `ADAPTER`, an instance (or a class with a no-argument constructor) of a subclass of
  `engines.Engine`. It may `from shedkeeper import db, engines` (the kit's folder is on `sys.path` in the hook and the command line).
- **Loading** reads the file once, hashes the bytes (sha256) and executes *those bytes* (no gap between the hash and the code). **A file that fails to load** -- a syntax error, an exception at import, a
  `SystemExit`, no `ADAPTER`, an invalid one, an id or a head or a scheme claimed twice -- never stops the kit: it is `ADAPTER BROKEN: <file> -- <why>` in `status`, a line at
  every session start, and its engine (named by the file name) is treated as **not supported**: commands pass with the warning of section 5.2. Fix the file or move it away.
- **A loaded adapter that is not proven** (section 9.6) is also treated as not supported -- its `recognize` is still called, but only to give the warning, never as protection.

### 9.3 The contract

`engines.Engine` documents it in its module text. Class attributes first, then the methods, in the order the framework calls them.

```python
class MyEngine(engines.Engine):
    id = "duckdb"            # [a-z][a-z0-9_]{1,20}; not postgres/sqlite; written in every record of a copy; prefix of copy file names
    title = "DuckDB"         # for a person
    heads = ("duckdb",)      # command names recognize() is called for (not psql/dropdb/pg_restore/sqlite3 -- those are the built-ins')
    schemes = ("duckdb",)    # address schemes this engine owns: parse_db_url / SHEDKEEPER_DB route `duckdb://...` to name()
    exts = (".duckdb",)      # a named FILE with this extension is this engine's (SHEDKEEPER_DB=app.duckdb)
    tool_names = ()          # the words a project tool uses for it (Rails `mysql2`, Laravel `mysql`, Prisma `mysql`): they route to name() too
    ext = ".dump"            # extension of a copy (".duckdb_copy"): ^\.[A-Za-z0-9_]{1,12}$
    file_based = True        # the database is one file: Base.path is set and its copy goes on the volume of that file
```

| # | Method | Receives | Returns | Must |
|---|---|---|---|---|
| 1 recognise | `recognize(words, ctx)` | `words` = `[head, *args]` of ONE executed command; `ctx` (`engines.RecognizeCtx`) | `[finding]`, `[]` if not destroying | read only the text; `finding = {"reasons": ["DROP TABLE"], "target": "<address/file or None>", "unnamed": "<why it cannot be named> or None"}`. `ctx.sql_reasons(texts, why)` is the kit's SQL reader (an unreadable text yields a reason: copy, don't refuse), `ctx.stdin_sql(args)` reads `-e`-less input (heredoc, `<`, pipe), `ctx.read_file(path)`, `ctx.getopt(args, short_with_arg, long_with_arg)`; `ctx.cwd`, `ctx.env` (variables assigned in the command). Optional: omit it and the engine is named only by schemes, extensions, `SHEDKEEPER_DB`, tools and MCP |
| 2 name | `name(finding, cwd, env)` | the finding (also `{"target": url, "source": ...}` from an address, `{"parts": {host, port, database, user, password}, "tool_engine": ...}` from Rails/Laravel) | `db.Base` or `db.Unnamed(reason)` | **never guess**: two candidates, a substitution `${...}`, an incomplete address -> `Unnamed`. Build `db.Base("duckdb", path=..., host=..., port=..., dbname=..., user=..., conninfo={env of the child process, including any password}, remote=bool, source=..., ident="duckdb:<normalised identity>", label="<short name>", shown="<address WITH *** for the password>", secrets=[the password])`. `Base.engine` must equal `id` |
| 3 prepare | `prepare(settings)` | the settings | `(tools, why)` | find what the copy needs (a binary, a module). `(None, "why")`: no copy possible, and the action is refused with `why`. `tools` is passed to the next methods unchanged |
| 4 size | `size(base, settings, tools, timeout)` | | `("ok", bytes, extra)` / `("absent", 0, 0)` / `("error", 0, why)` | `absent` **only** when the engine itself says the database is not there; "no right" and "no server" are `error` (refused). Measured *before* the copy |
| 4b check | `check(base, tools, extra)` | | `""` or why | optional: "this tool cannot copy that server" (PostgreSQL: a `pg_dump` older than the server) |
| 5 copy | `copy(base, tmp, tools, left, res)` | the path to write (`tmp`), `left()` seconds remaining | `(ok, how_verified)` | write **one file** `tmp`, then **read it back in full** -- not its head, its size or its table of contents: a truncated copy must fail. On failure set `res["msg"]` (and `res["status"] = "timeout"` when `left()` ran out) and return `(False, "")`. Kill your child process on the deadline |
| 6 record | *the framework* | | | writes id, engine, identity, `shown`, size, sha256, how verified, the command (masked, secrets scrubbed). Looks for `Base.secrets` in the record and **refuses to write it** if one is there |
| 7 restore | `restore(rec, copy, root, settings, now, to, session)` | the record of the copy, the copy's path, the store folder, `to` (a place named by the person, or None) | `engines.Restored(text, to, extra, unindexed)` or `engines.Refused(text)` | bring the copy back into a **NEW** place -- a server engine: a new database named `<name>_restored_<YYYYMMDD_HHMMSS>` after asking the server that the name is free; a file engine: a new file, or the old one *after* the current one goes to the trash (`store.trash`). **Never over a live database.** If the load reports anything, leave what was made, say so, and return `Refused`: a restore that is not clean is not recorded. The framework verifies the sha256 and that the copy is inside the store before it calls you, and writes the record |
| prove | `restored_base(base, op)` | the probe's base and the restore record (`op["to"]`, your `extra`) | `db.Base` of the restored database | where did the restore put it |
| prove | `probe_state(base, settings, tools)` | | `(is_empty, why)` | no table, view, sequence, schema |
| prove | `probe_fill(base, settings, tools, rows)` | `rows` = `[(id:int, txt:str, num:int or None)]` | `""` or an error text | create `engines.PROBE_TABLE` and insert the rows |
| prove | `probe_read(base, settings, tools)` | | `(rows, "")` or `(None, why)` | the rows ordered by id; `None` when it cannot be read (after the destroy step: the table is gone) |
| prove | `probe_destroy(base, settings, tools)` | | `""` or an error text | drop `engines.PROBE_TABLE` |

Helpers: `engines.run(cmd, env, timeout, data=None)` runs a child that never reads the keyboard and is killed on a timeout, returning `(returncode, stdout, stderr, timed_out)`;
`engines.file_sha256(path)`; `db.mask_secrets(text)`; `db.is_local_host(host)`; `db.Unnamed`; `engines.Restored/Refused`.

`db.Base` carries the password **only** in `conninfo` (the environment of the child process) and `secrets`. Everything printable, logged or recorded carries `***` instead (`shown`).
Pass the password to a child by its environment (`MYSQL_PWD`, `PGPASSWORD`) or a file the person owns -- **never on a command line**, where every process of the machine can read it.

### 9.4 What the framework does around the adapter

- **Decision** (`db.plan`/`db.execute`): remote, too large, did not fit the time, fresh copy, unsupported -- the rows of section 5.2; nothing of that is the adapter's job.
- **Naming routes**: an address `duckdb://...` (also `jdbc:` and SQLAlchemy `+driver` forms), a file `*.duckdb`, `SHEDKEEPER_DB=...`, a Prisma `provider`, a Rails `adapter`, a Laravel `DB_CONNECTION`,
  an MCP server's address -> `name()` of the adapter that claims the scheme/extension/tool word. Only when the adapter is proven; else the engine is "unsupported".
- **Where the copy goes**: `<store>\dbcopies\<day>\<id-or-prefix>_<label>_<time><ext>`; a file engine's copy is on the volume of the file.
- **A fault in an adapter** is never a crash of the hook: in `recognize` of a proven adapter it is "the command cannot be read" (a copy/refusal, the strict side), in `name` an `Unnamed`, in `copy` no copy, in `restore` a refusal.

### 9.5 The kit's own SQL reader, and its limits

`ctx.sql_reasons` is the reader of PostgreSQL/SQLite syntax: `--` and `/* */` comments, `'..'`, `"..."`, `$tag$..$tag$` quoting. An engine with other quoting (MySQL: backticks, `#` comments, `\` in strings) needs its own cutter first:
copy `db.strip_sql` and change its special places, or do the engine's `recognize` without the helper.

### 9.6 The proof gate: `prove`

`python shedkeeper.py prove ENGINE [--target PROBE]`

An adapter of a user counts as protection **only while a proof exists for the current sha256 of its file** (and for the current `engines.CONTRACT_VERSION`) **and the person has confirmed it**: the agent may build and prove an adapter, only the person makes it count (`activate`, below). The proof runs on a **probe**, never on a working database:

- **The probe is refused** unless (a) its name begins with `shedkeeper_probe` and (b) it is *empty at the start* (`probe_state`). (a) is the person's explicit act -- a database that holds work is never called that, and an
  empty database an application is waiting for is not dropped by accident; (b) is what makes the destroy step lose nothing. A refusal changes nothing. A server engine needs a probe database that you made; a file engine may leave out `--target` (a probe file is made in the current folder, and the tool deletes nothing).
- **Five steps**: (1) *fill* a table of 120 known rows (ASCII with the hard characters: quotes, backslash, tab, newline, `%`, `--`, NULLs), read back through the adapter; (2) *copy* through the same code the hook and `dbcopy` use
  (`db.copy_base`: size, copy, full read-back, the record without a password, the secret check); (3) *destroy* the table and read again -- it must **not** hold the rows (a destroy that destroys nothing proves nothing);
  (4) *restore* through the same code `restore` uses, into a new place; (5) *compare* the rows of the restored database with the rows put in, **row by row**; one difference is a failure.
- **A record `proved`** (engine, sha256 of the file, contract version, time, probe) goes to `proofs.jsonl`; a failed run writes `prove_failed`, which also takes away an earlier proof of the same file (the newest word wins).
  The hook reads the proofs in the personal folders of the user; **change one byte of the adapter and the proof is gone**. A `proofs.jsonl` that cannot be read whole (it cannot be opened, or holds a damaged line) is the loud state `unreadable` (`engines.proofs_problem`): not "no proof" -- the adapter that stood behind it may have been protecting. A command of the engine of an adapter of the user is then refused with the reason and `status`; `status` and the start of a session say it; `activate` refuses.
- **The person makes the proof count.** `prove` ends with a code (`SK-nnnnnn`, 30 minutes, bound to the sha256 of the adapter file); the agent asks the user to type `CONFIRM SK-nnnnnn` in their own message and runs
  `python shedkeeper.py activate ENGINE`; found in a message of the person (section 7) -> a line `activated` goes to `proofs.jsonl`. States: *none*, *failed* (a failed run takes the proof and the activation away),
  *proved* (does NOT protect: the hook warns, `status` says NOT ACTIVATED), *active*. A newer `proved` for the same bytes keeps an activation; changed bytes need a new proof and a new code.
- Leftovers are named, not deleted: the restored database (a server engine), the copy in the store, the probe.

**The built-in adapters need no `prove` from you.** PostgreSQL and SQLite are proved by the tests of the kit, which run *this very function* on a SQLite file always and on a real PostgreSQL server when the stand is given
(`SHEDKEEPER_TEST_PG`), before every release. That is honest because it is the same five steps over the same code the hook runs. It is not a proof about *your* PostgreSQL: a server the tests never saw is covered only by
running `prove postgres` yourself, which you may.

### 9.7 A tested example and an illustration

`examples/adapter_litedb.py` is a complete adapter for a made-up file engine that is SQLite inside, so every method runs for real; the tests copy it into a folder, prove it and drive it through the hook. Read it next to the table above.

**MySQL is an illustration of the shape, not a recipe, and is NOT TESTED by this kit** (no MySQL server was available). A server engine differs from a file engine in these places:

```
size     mysql -N -B -h HOST -P PORT -u USER -e "SELECT COALESCE(SUM(data_length + index_length), 0) FROM information_schema.tables WHERE table_schema = 'shop'"
copy     mysqldump --single-transaction --routines --triggers --events --no-tablespaces -h HOST -P PORT -u USER shop > <tmp>      # the password by MYSQL_PWD in the child's environment, never -p
verify   mysqldump has no read-back mode: the file ends with `-- Dump completed on ...` only when whole (a check of the END). The full read: load it into a scratch database of the adapter's own
         (name it with a stamp, ask the server it is free), compare the number of rows of every table, drop ONLY that scratch database
restore  ask the server that `shop_restored_<stamp>` is free; CREATE DATABASE it; mysql ... shop_restored_<stamp> < copy; any message -> leave it, say so, Refused
```

What differs from PostgreSQL: the client is `mysql` (`-e` takes the SQL, `source FILE` includes a file, `\!` runs a shell, `USE db` switches database -- each means "SQL that cannot be read to the end"); the quoting is backtick, `#` is a comment, `\` escapes in strings;
`DROP SCHEMA` is a synonym of `DROP DATABASE`; `TRUNCATE` and `DROP` commit at once; a copy is of **one** database, the one the command names. The probe, `restored_base` and the four probe methods are small `mysql -e` calls in the same shape.

### 9.8 Checks an adapter should pass (patterns are in the tests)

1. A mention is not an action (`grep`, `echo`, a commit message); a comment and a string literal do not count.
2. Each destroying statement is found; the same statement with a `WHERE`, and a `SELECT`, are not.
3. Naming: from the command, the environment, each project file you read; two candidates and a substitution -> `Unnamed`.
4. The copy: a file made, listed, `verified` set; **a truncated copy fails the check** (cut the file and watch it go red); a copy over the deadline is thrown away.
5. The limits by value: `max_db_mb` 199 / 200 / 201 MB; `db_fresh_minutes` 9:59 / 10:00 / 10:01.
6. **The password appears nowhere**: not on a command line of a child, in the list, the log, the refusal, the output.
7. The hook end to end: a refusal has the ready command; a fresh `dbcopy` lets the repeat pass.
8. The restore: into a new place only; the name is checked first; a failing load leaves what was made and records nothing; no call anywhere drops or deletes what was not made by the adapter.
9. A **mutation** per guarded place: break it in memory and see a test turn red. A test that stays green over a broken place guards nothing.
10. `python shedkeeper.py prove <id> --target <probe>` ends with `PROVED`.

---

## 10. The properties of the protection

These hold for every engine, built in or not, and are what a new adapter must not weaken.

1. **A copy before the destruction**, taken by the tool and **read back in full** before the action may go.
2. **No verified copy, no destruction** (I1) -- including when the copy could not be taken: the action is refused; no setting but the total switch `db_protection = false` lets a failed copy through. One exception is named: `on_db_too_large = allow` also lets through a copy that did not finish in time (cut off by `db_deadline_s`) -- the database was not damaged, the copy was only too slow.
3. **Nothing is deleted** by the kit: not the store's content, not the person's database, not what a restore made. Only the kit's own failed attempt.
4. **A way back into a NEW place**; never over a live database; a restore that reports anything is not recorded.
5. **A password is never printed**, recorded or put on a command line: addresses show `***`, the secret goes by the environment of the child, the record is checked for secrets before it is written.
6. **A stop always carries a next step** that works, in words for the agent to relay in the person's language.
7. **A weakening needs the person's own words** (section 7); every switch that lets something through says so aloud.
8. **What is not seen is declared** (section 8); what is not proven is not protection (section 9.6).

---

## 11. The tests

`python shedkeeper.py selftest` (or `python tests/run_tests.py [group numbers]`). No pytest. Everything happens in a temporary folder; the tests delete nothing and print its path. A test that could
not be *measured* (a tool is missing) is `NOT MEASURED` and the run ends with exit code 2 -- a `SKIP` that ends green would be a green over nothing. The tests of a real PostgreSQL run only with `SHEDKEEPER_TEST_PG` (an address
of a stand, no password; `PGPASSFILE` for the password) and make and remove their own databases.

| Group | What it proves |
|---|---|
| 1 recognize | the scanner: which files a command overwrites, which paths it deletes; a mention is not an action |
| 2 store | labels, personal folders, permissions, the trash round trip, versions, the index under failure, restore that never overwrites |
| 3 pre_tool | the hook end to end: every refusal has its shape, the ceilings, the broken-store paths, the UTF-8 of the input |
| 4 install | the entries, the timeout budget, the two doors (plugin / `install.py`), dead lines, the command in Git Bash |
| 5 mutations | a guarded place is broken in memory and the matching test must turn red: CAUGHT / SLIPPED / NOT MEASURED |
| 31-34 databases | SQL class and its borders, naming, the copy (SQLite for real, PostgreSQL on a stand and on a stand-in of the tools), the decision table, MCP, `dbcopy`/`find`/`restore`/`status`, the secrets, README against code |
| 35 conversation | the words of every stop for the agent, the settings menu, `set`, what the person sees, `survey`, the once-only offer |
| 36 mutations | the guards of group 35 broken in memory |
| 37-38 adapters, proof, confirmation | the contract through a real adapter (`examples/adapter_litedb.py`): load, not proven -> warning, `prove`, protected, a changed file loses the proof, a broken file is loud and the kit lives, `prove` refuses a non-empty or unmarked probe, the framework refuses a record that carries a secret; the confirmation code typed in the prompt (received by the hook) / a record of the conversation, a mention, before issue, expired, repeated; the reminder at start; mutations of each |
| 39 findings of the reviews | each finding of the two reviews of 0.3.0, reproduced on the code before the fix and closed: the fault of a proven adapter, the activation of an adapter by the person, the weights in the code and the shell writes into the settings, the record of the conversation, the length of the answer, `cd`, a password equal to a name, `SHEDKEEPER_DB` over a switch of database, mixed candidates, every database an MCP call drops, an installation of the older form, unreadable MCP settings, Rails with several databases, the once-only warning |
| 40 mutations | the guards of group 39 broken in memory |
| 41 findings of the second round of reviews | the redirect of a wrapper, a write to a target that is not spelled out, a password under a variable named PASS / PWD / SECRET / TOKEN and a secret of the connection at a boundary, an MCP call with a part that was not read, proofs that cannot be read, a pass by `allow` told to the person, the last resort of the hook (exit code 2), the strict side when a fallback reader or a path guard faults, a damaged `used` line of the codes, a settings.json that cannot be checked, the reminder of a path setting, the word of the command in a request for a code, `python3.N` |
| 42 mutations | the guards of group 41 broken in memory |
| 43 the person's word from the prompt hook; the hand of the person acts | the hook of the prompt (UserPromptSubmit): received / unknown / expired / spent, the clock of the code, a fault told, a real process; the installer and the plugin with the fourth entry; the record of the conversation no longer guarded; the hand of the person (a weakening edited by hand ACTS and is marked "not through `set`" at the start of a session and in `status`, no alarm at the decisions of the hook, a replay, a half-forged confirmation, the list of servers, the layers, a check that cannot be made, a `set` whose record cannot be written); the once-per-user note about the rule (the facts and the three principles, the record at once, no repeat in any project or store of the user, no opening of the user's file of instructions); the texts for the agent are facts and ways on, with no order (every kind of text, and a real run); the README sections `Risks`, `For your agent`, `A rule for your agent`; wrappers and the modes of a python write (`a` and `ab` are appends: no copy, no refusal for size, the settings files and the store stay guarded); a damaged `used` line and `quarantine`; the exit code 2 with a broken stdout in a real process; a damaged line of the proofs that names an adapter; a foreign store that cannot be read; an Edit whose check fails; the path in a notice |
| 44 mutations | the guards of group 43 broken in memory (the hand of the person; python appends; the note about the rule; the texts with an order) |
| 45 the rails of the databases, the protection not switched off silently, time and size | a database client named by a path or called through an operator; a command inside a container; rough lists; a wrapper whatever its exit code; the ordinary ways of taking ShedKeeper out need the person's code (an Edit of a settings file, `install.py uninstall`, `claude plugin ...`); an edit of an ini that Python cannot recompute is refused, and one in CRLF is recomputed the way the Edit tool does it; the start output is bounded; a code that is nearly in the form; `quarantine` writes the whole text first; `mcp_no_db` is one list; the small texts |
| 46 mutations | the guards of group 45 broken in memory |
| 47 the name of an MCP tool, the edit tools, the documents | a tool name that names an object of a database is destroying on any server, and the products added to the words of a server of a database; the same decision for `Edit` and for `MultiEdit` of a settings file, a two-step `MultiEdit` included; the documents: the README first screen, the section of what is read and kept, the update from 0.2.0, SECURITY.md, CHANGELOG.md, the public cases; no word of the workshop or non-ASCII letter in the files of the kit |
| 48 mutations | the guards of group 47 broken in memory |
| 49 the second pass over the face | a server of an MCP tool named by a product of an engine the kit does not copy (no address) is `Unsupported`, a warning, not a refusal that asks for an impossible `dbcopy`; `PGSERVICE` / `PGSERVICEFILE` without a host in the command is "the connection comes from a service file"; the example adapter's restore does not overwrite a file that appeared in the last moment; the old rule of the name in no docstring, no BOM; and the mutations of the three |

**The groups 31-49 test the code of the databases, which is switched off in this release** (`config.DATABASES_IN_THIS_RELEASE = False`): each of their tests runs with that name put to `True` (in the test's own process, and
in every Python process it starts), so the code of the next version does not rot; no assertion of them was weakened. What the release itself promises -- the databases do nothing, the documents say so -- is
proved by the groups 55-60 (60 breaks every gate of the release in memory and requires the tests to go red).

The mutation groups are the answer to "do the tests test anything": for every guarded place a test breaks it *in memory* and requires its guarding test to go red.
A mutation whose guarding test cannot be measured is `NOT MEASURED`, never `CAUGHT`.

---

## 12. Not in this release: databases

**ShedKeeper 0.3.0 protects files. Databases are not protected in this version; a later version will add them.** This section is the former user-facing text about databases, moved here from the README. It is NOT SHIPPED: the code exists in the kit and its tests run with `config.DATABASES_IN_THIS_RELEASE` set to True, but in this release the hook, the command line, the survey and the start of a session do nothing about a database (`DB_OFF_TEXT`: "Databases are not protected in this version; a later version will add them. Files are protected."). It is the base of a later version and will be rewritten when that version is decided.

### 12.1 Databases (what the build does; switched off)

The same promise as for files: **no verified copy -- no destruction.** Before a command or an MCP call destroys data in a **PostgreSQL**
database or a **SQLite** file, ShedKeeper takes a dump of that database, reads it back in full, and only then lets the action go. If the copy
cannot be taken, the action is refused -- with a ready command that tells the agent what to do next, so it is never stuck.

What counts as destroying, how a database is named, what the hook decides in each case and why, what a copy is, and what is not seen are in
[ARCHITECTURE.md](ARCHITECTURE.md) (sections 5.2 and 8). In short:

- **Destroying:** `DROP DATABASE | SCHEMA | TABLE`, `TRUNCATE`, `DELETE` or `UPDATE` without a `WHERE`, `ALTER TABLE ... DROP COLUMN`, `dropdb`,
  `pg_restore --clean`, and the destroying commands of tools (`prisma migrate reset`, `rails db:drop`, `supabase db reset`, `drizzle-kit push` ...), also
  as a call of an MCP tool. A mention (`grep "DROP TABLE" f`) is not an action.
- **An MCP tool** is destroying when a string argument starts like destroying SQL, or when its name holds `drop`, `delete`, `reset`, `truncate`, `wipe` or
  `purge` and either the name itself names an object of a database -- `database`, `db`, `table`, `schema`, `branch`, `collection`, `index` or `bucket`, as a whole
  word (`delete_database`, `reset_branch`, `DropTable`) -- on ANY server, or the server is a server of a database (its settings give an address, or a word of a
  database -- `postgres`, `supabase`, `turso`, `duckdb` ... -- stands in its name, command or address). `delete_message` or `delete_file` of a chat or a file
  server passes. The price: a tool of another kind that carries an object word (`delete_branch` of a code-hosting server) is stopped too, with the reason; the
  setting `mcp_no_db` (the names of servers whose calls always pass) is the way out.
- **A refusal carries the next step**, with `***` where the password is: `SHEDKEEPER_DB='<address or file>' <your command>` when the database
  could not be named; `python "<kit>/shedkeeper.py" dbcopy '<address>'` for a database on another machine or one too big for the hook
  (after it, repeat the command within `db_fresh_minutes`); `... dbcopy '<address>' --mcp-server NAME` for an MCP call.
- **Another engine** (MySQL, DuckDB ...): not copied by the kit itself. The command passes with a loud warning (or is refused, with
  `on_db_unsupported = deny`). An engine is added by an **adapter** -- a Python file in `~/.shedkeeper/adapters/` -- and it counts as protection only after
  `python shedkeeper.py prove <engine> --target <an empty probe database>` has shown that it copies, survives a destruction and brings the data back equal.
  The contract, a tested example (`examples/adapter_litedb.py`) and the proof gate are in [ARCHITECTURE.md](ARCHITECTURE.md), section 9.

```
python shedkeeper.py dbcopy "postgresql://user@localhost:5432/shop"   # a verified copy NOW, no time limit, no size limit
python shedkeeper.py dbcopy C:\data\app.db                            # a SQLite file (the backup API: the write-ahead log is included)
python shedkeeper.py dbcopy "<address>" --mcp-server NAME             # the same, for an MCP tool of the server NAME
python shedkeeper.py find shop                                        # the copies of databases are found too (`db copy`, ids d:<day>:<n>)
python shedkeeper.py restore d:2026-10-01:7                           # PostgreSQL: into a NEW database; SQLite: back as a file
python shedkeeper.py restore d:2026-10-01:9 --to D:\elsewhere\app.db  # SQLite: another place
python shedkeeper.py prove sqlite                                     # the proof gate on a probe (adapters need it; the built-in engines are proved by the tests)
python shedkeeper.py status                                           # the section `databases`: on/off, pg_dump, limits, the budget, the copies, the adapters
```

`dbcopy` exits `0` (copy made and listed), `1` (refused or nothing to copy: no record is written), `3` (the copy is made but NOT listed; the file is named --
the hook will not know it). **`restore` of a PostgreSQL copy never goes into an existing database:** it makes a NEW one, `<name>_restored_<YYYYMMDD_HHMMSS>`
(`createdb` with the encoding and the locale of the original, then `pg_restore`), after asking the server that this name is free; the database that was there
is not touched, and the tool never drops a database. **A SQLite copy** comes back as the file: the file that is there now and its `-wal`, `-shm`, `-journal` go to the
trash of this kit first, and the copy is read back (sha256 and `PRAGMA integrity_check`) before anything is replaced. `restore` needs the password the way `pg_dump`
does: `pgpass`, or `PGPASSWORD` in your shell. `--to` is not used for PostgreSQL.

#### The keys of `shedkeeper.ini` for databases

| Key | Default | Meaning |
|---|---|---|
| `db_protection` | `true` | guard the databases at all; `false` -- they pass, and `status` says so |
| `pg_dump_path` | empty | the `pg_dump` to use (a file or its folder); empty = look for it (PATH, then the usual places) |
| `max_db_mb` | `200` | a database bigger than this (measured BEFORE the copy) gets no copy from the hook |
| `db_deadline_s` | `25` | the dump and its check, all the copies of one command together; a copy that does not fit is thrown away |
| `on_db_too_large` | `deny` | bigger / did not fit: `deny` refuses with the ready `dbcopy`; `allow` lets it pass and logs (also a copy that did not finish in time) |
| `on_db_remote` | `deny` | a database on another machine: `deny` refuses with the ready `dbcopy`; `allow` lets it pass and logs |
| `on_db_unsupported` | `allow` | an engine nothing here copies (or an adapter not proven / broken): `allow` passes with a warning; `deny` refuses |
| `db_fresh_minutes` | `10` | how long a copy of `dbcopy` lets the repeated command pass |
| `db_command` | none | one line per extra destroying command of a tool: `db_command = flyway clean`, `db_command = dotnet ef database drop \| --force` |
| `mcp_no_db` | none | names of MCP servers that hold no database: their calls pass |

A value that is not understood becomes the **safe** one and `status` names the reason. The budget of one hook run -- `cmd_deadline_s` 20 + git 10 +
`db_deadline_s` 25 + start about 3 = 58 s -- is held against the timeout the installer sets (**90 s**) with a margin of at least 10 s; `status` checks it and says
`TOO SMALL` when you have raised a deadline past it. An installation made by 0.2.0 has a timeout of 60 s: `python install.py install` again brings it to 90 s.
The matcher of an installation made by 0.2.0 does not carry `mcp__.*`: the same command puts the new one back.

#### Limits of the databases part, declared (the full list is in ARCHITECTURE.md, section 8)

- **Inside a program.** A script that destroys data on its own (`python seed.py`, `node migrate.js`) is not read: only the text of the command and of the SQL it hands over. **SQL written inside the code of a program** (`python -c "import sqlite3 ..."`, `psycopg2` in a script) is not read at all -- no copy, no refusal; what is read is the commands `sqlite3` and `psql`, the tools, and MCP calls.
- **The verbs of the name of an MCP tool** are `drop`, `delete`, `reset`, `truncate`, `wipe`, `purge` and no others (`remove_table`, `destroy_database`, `clear_table` pass), and a verb is found inside a word (`list_deleted_tables` is stopped, with its reason).
- **A part of an MCP call that is too big to read** (more than 400 strings, nested deeper than 8 levels, a string over 5 MB) is a doubt, judged on the strict side: if the call goes to a server of a database, or the part that was read looks like SQL, it is treated as destroying (copy, or refusal when the database cannot be named); a big harmless call to another server passes, with a line in the journal (`mcp_read_limit`).
- **Proofs that cannot be read.** If the file of proofs of the engine adapters (`proofs.jsonl`) cannot be read whole (rights, a damaged line), whether an adapter protects is not known: a command of that engine is refused, `status` and the start of a session say so; it is not "not proven" in silence.
- **Project code is not run.** Django `settings.py`, `drizzle.config.ts`, Rails with ERB are not read: give the address by `DATABASE_URL` or `SHEDKEEPER_DB`.
- **A copy is of one database;** roles and other objects of the cluster are not in it.
- **Inside a container, no copy.** `docker exec CONTAINER psql ...` (also `docker compose exec`, `docker-compose exec`, `podman exec`, and `sh -c "psql ..."` inside them) is read like the same command on this machine, but ShedKeeper cannot copy a database that lives inside a container: a destroying command there is refused, with the way on -- name the database through the port the container publishes (`SHEDKEEPER_DB=postgresql://localhost:<port>/<database>`, the copy is then taken from that address) or ask the user.
- **Not tried here:** Docker volumes (`docker volume rm`, `docker compose down -v` are recognised by their text only), hosted projects (`supabase db reset --linked` is refused with the ready command), **MySQL and the other engines** (no adapter is shipped).
- Naming by `SHEDKEEPER_DB` or `--mcp-server` rests on the word of the agent.

### 12.2 Adding another database engine (switched off)

The contract of the methods an adapter implements is in [ARCHITECTURE.md](ARCHITECTURE.md), section 9.3; a complete, tested example is `examples/adapter_litedb.py`.

An engine is added by an **adapter**: one Python file in `~/.shedkeeper/adapters/` (an update of the kit does not touch that folder) that answers a short list of questions
-- recognise the destroying commands of the engine, name the database, measure it, copy it and read the copy back in full, bring it back into a **new** place. The
framework does everything else (the decision, the store, the list, `dbcopy`, `find`, `restore`, `status`, the texts). An adapter is **not protection until it is proven**:
`python shedkeeper.py prove <engine> --target <an empty probe database whose name begins with shedkeeper_probe>` fills the probe, copies it, destroys it, restores it and compares
the rows one by one; the proof is bound to the sha256 of the file, so a changed file is "not proven" again. **A proof does not protect by itself: the PERSON makes it count** -- `prove` prints a code, the person types `CONFIRM SK-nnnnnn` in their own message, `python shedkeeper.py activate <engine>` applies it (the code is for these bytes of the file). Until then the engine is treated as one that is not supported, and a
file that does not load is a loud line in `status` and at every session start. A complete, tested example is `examples/adapter_litedb.py`; the contract, the signatures, the proof
gate and the properties every adapter must keep are in [ARCHITECTURE.md](ARCHITECTURE.md), section 9. MySQL is described there as an illustration of the shape and is **not tested by this kit**.
