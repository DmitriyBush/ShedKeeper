# Changelog

What changed in each version of ShedKeeper. The version is in `VERSION` and `.claude-plugin/plugin.json`; Claude
Code decides whether a plugin has an update by that number. The date is the day the version reached this repository.

## 0.3.0 -- 2026-10-02

**ShedKeeper 0.3.0 protects files. Databases are not protected in this version; a later version will add them.** Database protection (PostgreSQL, SQLite, MCP calls, engine adapters, `dbcopy`) was built and switched off for this release; the code stays in the kit as the base of a later version. **This build ("files first").**

- The protected list holds folders only: `protect add sqlite|pg|remote` is refused (exit code 2), the survey offers no database, a database entry from an earlier build is not checked
  and is announced as NOT protected at the start of a session and in `status`. The code of the databases is kept and tested (`DATABASES_IN_THIS_RELEASE`).
- The list of a project is looked for in the folder of the step and in every folder above it (and `CLAUDE_PROJECT_DIR`), not only in the repository root; the lists of projects UNDER the
  folder of the step are checked too.
- A damaged or removed journal of confirmations, a store that cannot be named, or a list changed by editing the file (an entry taken out, a target changed, an exclusion added) now
  REFUSES the step (`broken`) instead of reading as "never confirmed". `protect decline` needs your code.
- The hook matcher is `.*` (every tool; the readers pass at once), and a watchdog refuses the step before the hook's own time runs out. The time keys of the settings can no longer
  push the check past the time the hook has.
- The change journal: the root is compared in the form the system gives (junction, `subst`, 8.3 names); a file counts as deleted only when the system proves it; only junctions and
  symbolic links are skipped, other reparse points (cloud placeholders ...) are ordinary files; the number of skipped links is shown.
- `snapshot` says "NO copy" for an entry that got none (exit code 3); `restore --step` says so when the manifest is damaged or a file was too big to copy; `status` shows degradations.
- **The second correction of this build.** The check looks at the lists UNDER the folder of the step too (a session opened over the projects); a failed look for lists refuses the step instead of
  shrinking to the root of the project; an entry added after the confirmation and a taken-back exclusion are in the mark, so that undoing them by hand is noticed; one damaged line of the shared
  journal breaks only the lists it may have been the mark of, and the words name `quarantine`; a file carried out of the tree is told by the journal itself (no full walk, no refusal for an
  expensive walk); `restore --step` forgets a file that a later step copied and puts real failures first; `install.py uninstall` works when the package does not load; the tools that stop a process
  or ask you (`TaskStop`, `KillShell`, `AskUserQuestion`, `ExitPlanMode`) are never refused; the words about a lowered promise are spent only after they were delivered; the accelerators plan
  up to the watchdog; `status`, the start of a session, `snapshot` and `restore --step` see every list over the folder; `snapshot` and the old database entries tell the truth (exit code 3).
- A broken list UNDER the folder of the step (or of the session) refuses every step that is checked, as a broken list of the step's own folder does; the refusal names the project, says where the list was found and gives the two ways out (repair the list, or open the session in the folder of the project you work on).
- **What this build does NOT close** (declared limits, ARCHITECTURE section 8): a list confirmed by a build before 0.3.0 gets its second witness at the next confirmation (`protect confirm`), not
  before; a list folder removed by a command the kit does not recognise; links of other kinds (WSL, `AF_UNIX`) that cannot be opened.

**Built and switched off in this release (NOT SHIPPED; nothing below acts or is promised)**

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
- **MCP tools.** The hook matcher is `.*` (every tool reaches the hook). A call destroys when a string argument starts like destroying SQL,
  or when the tool name holds a destroying word (`drop`, `delete`, `reset`, `truncate`, `wipe`, `purge` -- those and no others) and either names an object of a database (`delete_database`,
  `drop_table`, `reset_branch` -- on any server) or the server is a server of a database. What the reader could not
  read is a doubt and is judged on the strict side.
- **Engine adapters and the proof gate.** Another engine is added by a Python file in `~/.shedkeeper/adapters/`; it counts
  as protection only after `prove` has copied, destroyed and restored a probe database and compared the rows, and the
  person has confirmed it (`activate`). `examples/adapter_litedb.py` is a tested example.

**Added**

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
  `db_deadline_s` 25 + start about 3 = 58 s); `status` says `TOO SMALL` when a deadline is raised past it. In this release, which protects no database, the budget has no database term
  (20 + 10 + 3 = 33 s) and `status` says so; the check against the timeout stays, because a hook cut off by its timeout lets the action through with no copy of a file.
- When the hook itself fails, or its input cannot be read, an MCP call whose tool name or arguments hold a destroying word (`drop`, `delete`, `update`, `reset`, ... -- file tools such as
  `delete_file` among them) is refused with the generic words «the hook failed ... no copy can be trusted», as before; the switch of the databases does not touch this. `status` and `survey` say
  «databases: not protected in this release; a later version will add them» also when the project looks like it has a database, and `find` says that records of database copies from an earlier
  build exist and are not listed.
- The tool matcher is `.*` (every tool, an MCP tool among them, reaches the hook; the check of the protected list looks at all but the readers). An installation made by 0.2.0 has the old timeout and matcher; `python install.py install`
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
