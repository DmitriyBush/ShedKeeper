# Changelog

What changed in each version of ShedKeeper. The version is in `VERSION` and `.claude-plugin/plugin.json`; Claude
Code decides whether a plugin has an update by that number. The date is the day the version reached this repository.

## 0.3.0 -- 2026-10-02

**Added**

- **Databases: PostgreSQL and SQLite.** A command or an MCP call that destroys data in one (`DROP DATABASE |
  SCHEMA | TABLE`, `TRUNCATE`, `DELETE` or `UPDATE` without a `WHERE`, `ALTER TABLE ... DROP COLUMN`, `dropdb`,
  `pg_restore --clean`, and the destroying commands of tools such as `prisma migrate reset`, `rails db:drop`,
  `supabase db reset`) gets a verified dump of that database first, read back in full; no verified copy, no
  destruction. The database is named from the command, the environment, the files of the project (`.env`,
  `prisma/schema.prisma`, `supabase/config.toml`, `alembic.ini`, Rails `config/database.yml`, Laravel) or the
  settings of the MCP server -- never guessed; when it cannot be named the command is refused with a ready command that
  names it. Remote databases and ones over `max_db_mb` are refused with a ready `dbcopy`; the limits are settings.
- **`dbcopy`, and `find` / `restore` for database copies.** A restore of a PostgreSQL copy goes into a NEW database;
  a SQLite copy comes back as a file after the current one goes to the trash. `status` has a `databases` section.
- **MCP tools.** The hook matcher carries `mcp__.*`. A call destroys when a string argument starts like destroying SQL,
  or when the tool name holds a destroying word (`drop`, `delete`, `reset`, `truncate`, `wipe`, `purge` -- those and no others) and either names an object of a database (`delete_database`,
  `drop_table`, `reset_branch` -- on any server) or the server is a server of a database. What the reader could not
  read is a doubt and is judged on the strict side.
- **Engine adapters and the proof gate.** Another engine is added by a Python file in `~/.shedkeeper/adapters/`; it counts
  as protection only after `prove` has copied, destroyed and restored a probe database and compared the rows, and the
  person has confirmed it (`activate`). `examples/adapter_litedb.py` is a tested example.
- **Settings by conversation.** `settings` (the menu, with the weight of each key: `protection`, `tuning`, `notice`),
  `set KEY VALUE`, a personal `~/.shedkeeper/shedkeeper.ini` that survives a plugin update, and `survey`, a read-only first
  look at the machine and the project, offered once per project.
- **A weakening of protection needs the person's code.** `set` prints `SK-nnnnnn`; the person types `CONFIRM SK-nnnnnn`
  in their own message; a new hook, `hooks/prompt.py` (`UserPromptSubmit`), receives it. The same code confirms an adapter and
  the ordinary ways of taking ShedKeeper out (an edit of a Claude Code settings file that removes the hooks,
  `install.py uninstall`, `claude plugin uninstall|disable`). A weakening that stands in an ini file acts; the start of a
  session marks one that was not made through `set`.
- **Words for two readers.** Every stop and warning gives the person a short line and gives the agent the facts and the lawful
  ways on; no text orders the agent how to behave. Once per user the agent is told the facts and ShedKeeper's recommendation
  that the person put a rule of their own into their agent's instructions.
- Documents: ARCHITECTURE.md (the device, the contract of an adapter, the declared limits), README sections "Risks",
  "For your agent", "A rule for your agent", "What ShedKeeper reads and keeps"; SECURITY.md; this file.

**Changed**

- The hook timeout the installer sets is 90 s (was 60 s), and the budget of one run is computed (`cmd_deadline_s` 20 + git 10 +
  `db_deadline_s` 25 + start about 3 = 58 s); `status` says `TOO SMALL` when a deadline is raised past it.
- The tool matcher gains `mcp__.*`. An installation made by 0.2.0 has the old timeout and matcher; `python install.py install`
  again brings them up to date, and `install.py check` says what is still the old form.
- A shell write into a settings file, an `install.py uninstall` and the other ordinary ways of switching ShedKeeper off are
  stopped until the person types the code.
- The hook reads wrappers (`timeout`, `nice`, `sudo -u`, `xargs`, `env`, `bash -c`, `docker exec`, `docker compose exec`) when it looks for
  the command that acts.

**Fixed**

- A copy over an existing file by `curl -o`, `wget -O`, `Invoke-WebRequest -OutFile` or `cp` now gets its copy first.
- A database named by a client path, `&` or a container is refused with the way on instead of passing.
- `quarantine` moves a damaged journal line to a `.bad` file instead of leaving the whole journal unusable.

**Declared limits** (the full list is in the README and in ARCHITECTURE.md, section 8): a program that destroys data by itself is not
read, and neither is SQL inside the code of one (`python -c` with `sqlite3`); MySQL and other engines are not copied unless an adapter is proven; Docker volumes are recognised by their text only.

## 0.2.0 -- 2026-10-01

**Added**

- **A personal closed folder per user.** The store is `<X>:\_shedkeeper\<login name>\`, closed to other users from the first moment
  (the permissions are given to the call that creates the folder and read back); `private_store` switches the closing off for a
  volume that has no permissions. A name taken by something else is not touched: the next free `<login name>.2`, `.3` ... is used.
- **A way back through the agent.** The start of a session tells the agent how to find and bring back; `find` prints what an item
  was and why it is there; the person is told in one paragraph when their folder is made.
- **A Claude Code plugin** (`.claude-plugin/plugin.json`, `marketplace.json`, `hooks/hooks.json`), and the two-doors rule: `install.py`
  refuses while the plugin is installed and enabled, and the plugin's hooks stay silent when an `install.py` entry covers the same
  event. `install.py check` runs the registered command once and says which door is open.
- A hook command that checks that its file is there: a moved or removed kit, or no working `python`, gives a loud warning that
  the protection is OFF and lets the action pass, instead of a silent block or a silent pass.
- The installer records every line it writes (`installs.jsonl`) and takes out a dead line only when it is proven its own and
  proven gone.
- LICENSE (Apache-2.0).

**Changed**

- `install` needs Git Bash (the shell Claude Code runs hooks in on Windows) and refuses without it.
- A restore never overwrites an occupied place (the last step is a rename); the session id of a transcript must be a plain name;
  "same" for a copy means the previous copy, read at that moment, still has the content of the file.
- `disableAllHooks` is reported as `door: none`; a test that cannot be measured is `NOT MEASURED`, not a green skip.

**Fixed**

- Creating the store under a name lock of Windows: twelve hooks started at once leave no refusal (measured 0 of 300).
- The registry of plugins is read from where `CLAUDE_CODE_PLUGIN_CACHE_DIR` and `CLAUDE_CONFIG_DIR` put it; a plugin from a folder
  marketplace runs from that folder.

**Not covered in this version:** databases (the README said so under the promises and in the borders).

## 0.1.0 -- 2026-10-01

**Added**

- **A copy before every change.** Before Edit, Write, NotebookEdit, MultiEdit, or a shell command that overwrites an existing file
  (a redirect, `tee`, `sed -i`, `cp`/`mv`, `Set-Content`, and others), a copy is taken and checked against the original by size and
  sha256; no verified copy, no change.
- **The trash instead of a ban.** A deletion (`rm`, `del`, `Remove-Item`, `git clean`, `find -delete` ...) is refused with the ready
  command that moves the item to the trash of the store.
- **The way back.** `find` and `restore` for the trash and for the earlier versions; a restore never overwrites.
- **Copies of session transcripts** in the store.
- An installer with a backup of the settings file, and a hook timeout of 60 s.
- The store: one folder per fixed volume, outside the projects, with an index of every move and copy; nothing in it is deleted by
  ShedKeeper.

**Fixed after an independent check**

- An empty folder is moved to the trash like any item; a store name is recognised whatever its letter case (Windows does not tell
  them apart); a junction into the store is the store; a command is judged by the head of its end, not by a substring;
  appending (`>>`, `tee -a`) is declared a border, not covered.
