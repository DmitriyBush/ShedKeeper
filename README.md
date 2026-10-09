# ShedKeeper

**A safety net for the work an AI coding agent (Claude Code) does on your files -- on Windows.**

**ShedKeeper 0.3.0 protects files. Databases are not protected in this version; a later version will add them.**

What it protects you from:

- **A file overwritten.** Before an edit or an overwrite it recognizes (the edit tools, `>`, `cp`, `mv`, `sed -i` ... -- not every program that
  writes: see [Borders](#borders----what-shedkeeper-does-not-see)), ShedKeeper saves a verified copy (size and sha256 compared). No verified copy -- no change.
- **A file deleted.** The deletion is refused and the agent is handed the ready command that moves the item to a trash instead.
  Nothing is lost, and the agent is not stuck.
- **A loss with no way back.** Ask your agent: it finds what was changed or removed, tells you what it was, and brings it back once
  you say yes. Nothing in ShedKeeper empties the store -- only you do.
- **The protection switched off by the agent.** A change that weakens it is made only with a confirmation code you type yourself,
  in your own message.

'**Install, about a minute.** You need Windows, Python 3.9+ on PATH (standard library only) and Git for Windows (Claude Code runs hooks in its Git Bash, and `install.py` refuses without it). One way in, not both.

As a plugin -- two commands, typed inside Claude Code, one after the other:

```
/plugin marketplace add Serpentis-Git/ShedKeeper
/plugin install shedkeeper@shedkeeper
```

Without plugins -- in a terminal (the folder `install.py` needs is what `git clone` downloads):

```
git clone https://github.com/Serpentis-Git/ShedKeeper
cd ShedKeeper
python install.py install --user
```

**What it does not do:** it reads the text of a command, so it is a helper for an agent that makes mistakes, not a lock against one
that looks for a way round; it does not look inside a program that runs; it works on Windows only. The
full list: [Borders](#borders----what-shedkeeper-does-not-see) and [Risks](#risks).

## Contents

- [What it promises](#what-it-promises)
- [Install](#install)
- [Updating from 0.2.0](#updating-from-020)
- [Moving, removing and reinstalling](#moving-removing-and-reinstalling)
- [Find and bring back](#find-and-bring-back)
- [Settings, the first look, and the words of a stop (0.3.0)](#settings-the-first-look-and-the-words-of-a-stop-030)
- [Where the store is, and who cleans it](#where-the-store-is-and-who-cleans-it)
- [What ShedKeeper reads and keeps](#what-shedkeeper-reads-and-keeps)
- [Borders -- what ShedKeeper does NOT see](#borders----what-shedkeeper-does-not-see)
- [Risks](#risks)
- [For your agent](#for-your-agent)
- [A rule for your agent](#a-rule-for-your-agent)
- [Related tools](#related-tools)
- [History](#history)
- [License](#license)

## What it promises

A safety net for work done by an AI coding agent (Claude Code) on your files. Three promises:

1. **A copy before a change it recognizes.** Before the agent edits or overwrites an existing file in a way ShedKeeper reads (the edit tools, redirects,
   `cp`, `mv`, `sed -i` ...; what it does not read is in [Borders](#borders----what-shedkeeper-does-not-see)), ShedKeeper saves a
   verified copy (size and sha256 compared with the original). No verified copy -- no change. A new copy is skipped
   as "same" only when the previous copy, read at that moment, still has the content of the file; a damaged or
   shortened one is not trusted: a new copy is taken and the journal says why.
2. **Deletion becomes the trash, not a ban.** When the agent tries to delete something, ShedKeeper refuses
   and hands it the ready command that moves the item to a trash instead. Nothing is lost, and the
   agent is not stuck: it just runs the command it was given.
3. **The way back is by your word.** Ask your agent: it finds what was changed or removed, tells you what it
   was and why it is gone, and brings it back once you say yes -- or you do it yourself with one command.
   Nothing in ShedKeeper empties the store -- only you do.

**ShedKeeper 0.3.0 protects files. Databases are not protected in this version; a later version will add them.** Database protection was built and is switched off for this release: `protect add sqlite|pg|remote`, `dbcopy`, `prove` and `activate` refuse with the words of the release, the survey offers and names no database, a command or an MCP call that destroys data in a database passes unseen, and a database entry that an earlier build left in your list is not checked -- `status` and the line at the start of a session say so, loudly. How the system is built and why: **[ARCHITECTURE.md](ARCHITECTURE.md)**.

Plain Python 3.9+, standard library only. Windows only in v1.

## Install

There are two ways in to the same hooks. **Use one of them, not both.**

### 1. As a Claude Code plugin (two commands inside Claude Code)

```
/plugin marketplace add Serpentis-Git/ShedKeeper
/plugin install shedkeeper@shedkeeper
```

The first command adds this repository as a *marketplace* (Claude Code reads `.claude-plugin/marketplace.json`
from it); the second installs the plugin `shedkeeper` of the marketplace `shedkeeper` (the form is
`plugin@marketplace`). Claude Code asks for the scope: **user** (every project of yours) or **project** (this
project, shared through `.claude/settings.json`). Nothing else to run: no download, no unpacking, no `install.py`.
`python` must be on PATH, as for the other way. Without it ShedKeeper cannot run, and says so: the first
guarded action and the start of a session give you and the agent a loud warning that the protection is OFF
(no copy before an edit, nothing goes to the trash) and how to fix it; the action itself passes.

**Honest limits of this way, today:**

- **While the repository is closed, the first command works only for someone who has access to it.** Claude Code
  uses your own git credentials for it (for GitHub, for example `gh auth login`); a background update of a private
  marketplace additionally needs `GITHUB_TOKEN` (or `GH_TOKEN`) in the environment, otherwise update by hand with
  `/plugin marketplace update shedkeeper`.
- **Without access, or to try it from a folder:** `/plugin marketplace add C:\path\to\this\folder` (a local
  folder works as a marketplace), then the same `/plugin install shedkeeper@shedkeeper`.
- **At project scope, the setting is shared; the installation is not.** `enabledPlugins` in a committed
  `.claude/settings.json` reaches a colleague who has not added the marketplace or installed the plugin: for
  them nothing runs. `install.py` and `check` do not take that for an open door (`plugin door NOT PROVEN`).
- **A plugin folder whose path holds a `$` or a backtick** (a Windows user name with one) is expanded by bash,
  because Claude Code puts the folder into the command inline: that user's plugin hooks print the warning
  and let the action pass (the guarded command). The `install.py` way is not affected. Not measured.
- **Where the plugin runs from depends on how the marketplace was added.**
  A marketplace from GitHub or git -- expected from the documentation, not measured here: Claude Code copies the plugin into its plugin cache
  (`~/.claude/plugins/cache/...`; under `CLAUDE_CONFIG_DIR` when it is set; under `CLAUDE_CODE_PLUGIN_CACHE_DIR` when
  that is set -- despite the name that variable moves the whole plugins root: registry, marketplaces and cache) and
  runs the hooks from the copy. If so, `shedkeeper.ini` inside the plugin is the copy in the cache: a change you make
  there is lost with the next update. To keep your own settings across updates, keep your own copy of the file
  anywhere and set the environment variable `SHEDKEEPER_INI` to its path where Claude Code starts.
- **A marketplace added from a folder (`/plugin marketplace add C:\path\to\this\folder`) runs from that folder
  itself, not from the copy** (measured on Claude Code 2.1.284, Windows). The registry still names a copy in the cache as `installPath`, but the plugin
  that loads is the folder (measured: the plugin path Claude Code reports at start, and the `K=` in the start line,
  are the folder). So the `shedkeeper.ini` in it is the one in force, and do not move or delete that folder while the
  plugin is installed: the hooks would find nothing (the start hook then says so). `install.py check` reads
  `known_marketplaces.json` to know which of the two it is, probes the folder that runs and names both when they
  differ; when it cannot tell (the file is missing, unreadable or of another form, the folder's path is not
  absolute, or the marketplace comes from any other kind of source than `github`, `git` or a folder -- `url`, `npm`,
  `file`, a kind a later Claude Code adds) it says "not determined", tries the cache copy and does NOT call the plugin
  door proven: `install.py` then puts its own door in (a doubled hook is untidy; a silent pair is no protection). It takes
  the folder of a folder-marketplace to be the plugin's folder, which is right for a plugin with `"source": "./"` (this
  one); for a plugin kept in a subfolder of a marketplace the files are not found there and the plugin is not proven.
  The GitHub marketplace was not measured (the repository is closed).
- **`install.py` and `check` read the plugin registry from the environment of the process that runs `install.py`.** Claude
  Code reads its own environment: a `CLAUDE_CODE_PLUGIN_CACHE_DIR` or `CLAUDE_CONFIG_DIR` set in the launcher or in the
  `env` of `settings.json` is not seen from a plain terminal. Both commands print one line saying which folder the
  registry was read from. A relative `CLAUDE_CODE_PLUGIN_CACHE_DIR` is not guessed at (not measured whether Claude Code
  ignores it or resolves it against its start folder): the root is "not determined" and the plugin is not proven; `~` is
  expanded.
- An update arrives when `VERSION` / `plugin.json` carry a new version number (Claude Code decides by the
  version); the old folder in the cache is marked and removed 7 days later (the documentation).
- **Checked on a stand with a live agent on 2026-10-01** (Claude Code 2.1.284, Windows, `claude -p`, a marketplace
  added from a folder, the plugin registry moved with `CLAUDE_CODE_PLUGIN_CACHE_DIR`; first pass, 7 runs). Passed:
  the files pass `claude plugin validate --strict`; `marketplace add` and `install` leave the plugin enabled; a copy
  is taken before a Write (the plugin's hook), and the start line says the plugin is on; a `rm` is refused and the agent
  moves the file to the trash by itself; the installer's record is written, and a moved kit is found (the old lines are
  taken out, a hand-made line to a folder that never existed is left); with both doors open there is exactly one
  refusal; a deliberately broken copy function (a mutant that claims "same" and copies nothing) turns the stand red. What
  the pass found wrong and was corrected in the code afterwards (not yet repeated live): the registry was looked for
  in `~/.claude/plugins` although `CLAUDE_CODE_PLUGIN_CACHE_DIR` moves it, and the folder a folder-marketplace runs
  from was taken to be the cache copy. **Not checked:** how the message to the person looks in the window (only the
  stream of the agent was read), a GitHub marketplace, an elevated (administrator) session, a second user account,
  an exFAT disk, and `CLAUDE_CODE_PLUGIN_SEED_DIR` (a pre-seeded plugin directory) -- the kit does not read it.

**Remove it:** `/plugin uninstall shedkeeper@shedkeeper` (or the **Installed** tab of `/plugin`; from a terminal
`claude plugin uninstall shedkeeper@shedkeeper`). **The store stays**, as with the other way.

**If both ways are used by mistake**, in the normal case nothing runs twice -- and in the other cases it runs twice,
which is untidy (two copies, two refusals) but harmless. What the code does, with its conditions:

- `python install.py install` refuses while the plugin is enabled **and installed**: enabled by the order of
  the settings files (project-local, then project, then user: the first file that says `true` or `false` for
  the key decides, as in Claude Code), listed in the plugin registry (`plugins/installed_plugins.json`), and
  its folder holds `hooks/hooks.json` and the hook files. It names the plugin and what to do. A plugin that is
  only *written* as enabled (a teammate's committed setting, a leftover of an uninstall) is not counted:
  `install` goes on and says `the plugin ... is written as enabled ... but not found installed: <why>`.
- If the plugin is enabled AFTER `install.py` put its entries in, the hooks of the plugin stay silent -- the
  entries of `install.py` do the work -- **only when all of these hold**: Claude Code gives the hook
  `CLAUDE_PROJECT_DIR` (the project root; without it the hooks cannot tell which settings apply, and both run);
  a live entry of `install.py` covers the same event in the settings of that project (project-local, project,
  user): its hook file is there, the event matches and, for `PreToolUse`, the full tool matcher; and
  `disableAllHooks` is on in none of those files. `claude --plugin-dir`, managed (enterprise) settings,
  and a second kit folder installed by `install.py` are not covered: both run.
- `python install.py check` and `python shedkeeper.py status` print which door is open: `door: the plugin ...`,
  `door: install.py ...`, `TWO DOORS ...` or `door: none ...`, and `plugin door NOT PROVEN` when the plugin is
  written as enabled but not found installed. When `disableAllHooks` is on (by precedence, the file is named) the
  line is `door: none -- disableAllHooks is on in <file>: Claude Code runs no hooks, ShedKeeper does not run`
  whatever the settings or the plugin carry, and `check` exits 1.

### 2. With `install.py` (for those who do not use plugins)

```
python install.py install --project C:\path\to\your\project     # this project only
python install.py install --user                                # every project of this user
python install.py check   --project C:\path\to\your\project     # are the hooks in? is the store there?
python install.py uninstall --project C:\path\to\your\project   # take ShedKeeper's hooks out (and the dead lines of an earlier place)
```

**A project installation covers the folder Claude Code is started in -- not its subfolders.** Measured on 2026-10-01
(Claude Code 2.1.284): with the plugin AND the entries of `install.py` both set for a project, a session started in
`project\sub` ran no hook at all and a `rm` went through. The documentation says the same: Claude Code reads the project
settings only from the `.claude` of the folder it is started in. So `install --project` (and a plugin enabled at the
project or local scope) does not protect a session started in a subfolder; whoever works that way installs with
`--user` (or the plugin at the user scope: expected, since the user settings are read in any folder -- not measured
for the plugin). `install --project` prints one line about it, and so does `check --project` when the door is in the
settings of that project. This is a limit of Claude
Code, not something the kit can close: it cannot load itself where Claude Code does not read the settings.

The installer copies your settings file next to itself first (`settings.json.shedkeeper-backup-<time>`),
adds ShedKeeper's hook entries without touching anyone else's, and does nothing on a second run (on a
second run it also puts back a timeout or a tool matcher that was changed). The hooks that copy are given a
90-second timeout (the hook of the prompt, which only reads one message and writes one line, 30 seconds): the hook spends at most 20 s on copies (`cmd_deadline_s`), at most 10 s on git
calls (the 25 s that `db_deadline_s` allows for the copies of databases are not used in this release), and the rest is margin -- a hook that times out lets the action through. `python` must be on
PATH; if it is not (or `python` is the Microsoft Store placeholder), every hook prints a loud warning that the
protection is OFF and the action passes WITHOUT a copy -- install Python (python.org, tick Add python.exe to
PATH) and restart Claude Code. The hooks are registered by the absolute path of this folder; see "Moving, removing and
reinstalling" below for what happens when the folder is moved or deleted.

**A shared project file is not for a personal install.** `install --project` writes the absolute path of this
folder, so a `settings.json` that is committed (or not ignored by git, so it will be) carries a path that is
useless on a colleague's machine. `install` says so (`WARNING: ... is committed to git ...`) and goes on; use
`--user`, or ask your agent to put the entries into `<project>\.claude\settings.local.json`, which git does not
share. The lines of a colleague that are already in a shared file are not touched.

**What `install` needs and writes down.** It needs Git Bash (below): without it `install` refuses, with the
reason and the link to the documentation, and changes nothing -- a line that does nothing, silently, is worse
than no line. Every line it writes is also recorded -- the settings file, the exact command, the kit folder and
the time -- in `installs.jsonl` in your personal folder of the store (`<X>:\_shedkeeper\<login name>\`, on the
volume of the settings file). That record is the proof that a line is the installer's own (see "Moving"). If
the record cannot be written the install is done, but it says so: such lines will have to be taken out by
hand. Claude Code's user settings and plugin registry are read from `CLAUDE_CONFIG_DIR` when Claude Code is
started with that variable (it must be set in the shell that runs `install.py` too), else from `~/.claude`.

The command the installer writes is a **Git Bash** command, because Claude Code runs a hook command in
Git Bash on Windows (the documentation: the `shell` field of a command hook is `bash` unless you set
`powershell`; and measured on Claude Code 2.1.284 on Windows 11, 2026-10-01: the shell of a hook is
`/usr/bin/bash`, GNU bash 5.2.37 of Git for Windows, started as `bash -c`):

```
if [ -f '<kit>/hooks/pre_tool.py' ]; then python '<kit>/hooks/pre_tool.py'; rc=$?;
  if [ $rc -ne 0 ] && [ $rc -ne 2 ]; then printf '%s' '<JSON: Python cannot run>'; exit 0; fi;
  exit $rc; else printf '%s' '<JSON warning>'; fi
```

(one line in the settings.) While the hook file is there, `python` runs it and its exit code is the exit code of
the whole command (a refusal, exit 2, still refuses), and only 0 and 2 are the hook's own: the scripts of the kit
exit with nothing else. Any other code means the hook did not decide: Bash answers 127 when `python` is not found
and 126 when it cannot be started (the Microsoft Store placeholder), and the interpreter itself exits 1 when it
fails (a broken `PYTHONHOME`, a damaged module, a half-updated plugin) -- in all those cases the command prints the
warning that the protection is OFF and exits 0 -- loudly, not silently. When the file is not there the command
prints the other warning and exits 0.

`python install.py check` does not only read the settings: it runs the registered pre_tool command once
on a harmless input -- in Git Bash, the way Claude Code runs it -- and says FAILED when the command does
not start (a missing interpreter, the Microsoft Store `python` stub, no Git Bash), when the tool matcher
was edited, or when `disableAllHooks` is set -- read over all the settings that apply, by precedence
(project-local, project, user; managed settings are not read), with the file that sets it named. It also runs
the same command form pointed at a hook file that does not exist, to see that the warning comes out and the
exit code is 0. It names every line of an earlier installation whose folder is gone, and every entry still in
the old form (0.1.0 / 0.2.0, see below). What the probe proves is that **the hook starts and answers**; it
does not prove that a copy would be taken before an edit (the store line of `check` covers part of that).
When the plugin is the open door, `check` does the same against the **installed copy** in Claude Code's
plugin cache -- not against this folder -- and lists the dead lines of the settings as well.

## Updating from 0.2.0

- **The plugin way:** a marketplace that is not Anthropic's does not update by itself (Claude Code keeps auto-update off for it unless you
  turn it on: `/plugin` -> Marketplaces -> shedkeeper -> Enable auto-update). By hand: `/plugin marketplace update shedkeeper`, then
  `/plugin update shedkeeper@shedkeeper`, then `/reload-plugins` (or start Claude Code again). Measured: after the first command alone the
  installed version was still 0.2.0; after the second it was 0.3.0.
- **The `install.py` way:** put the new kit folder in place of the old one, then run `python install.py install --user` (or
  `--project <dir>`, as before) once from it. It brings the entries up to date and adds none twice: the timeout 60 s -> 90 s, the tool
  matcher gains `mcp__.*`, the command of 0.2.0 (it warns only when `python` cannot be found or started, exit 126 / 127) is rewritten to the form of this version, and the fourth entry, `UserPromptSubmit` (the hook that receives the
  confirmation code you type), is added.
- **`python install.py check` is red on an installation of 0.2.0 -- and that is true, not a breakage.** It exits with code 1. Measured on an
  installation made by the installer of 0.2.0 as released (the kit folder the same), the lines are: for `PreToolUse/pre_tool.py` -- ``installed, timeout is 60, expected 90, matcher is
  '...', expected '...|mcp__.*', the command is of the 0.2.0 form (a Python that fails with an exit code other than 126 / 127, 1 for one, lets the action pass WITHOUT a warning): `python install.py install` brings it up to date``;
  for `SessionStart/session.py` and `SessionEnd/session.py` the same without the matcher; and `UserPromptSubmit/prompt.py: NOT installed`, which carries no hint. What it means in
  practice: the old entries still copy files and move deletions to the trash, but calls of MCP tools are not seen, a confirmation code you type is not
  received (so a change that weakens the protection cannot be confirmed), a Python that fails with an exit code other than 126 / 127 lets the action pass without a warning, and the 60 s timeout leaves only 2 s over the
  58 s a run may take (`status` says `TOO SMALL`). What to do: run `install` again; `check` is green after it (measured: exit code 0, all four entries `installed`).
- **What stays as it was:** your store of 0.2.0 (the personal folder, the copies, the trash) is read as it is; nothing is moved. A store
  of 0.1.0 is described under "Where the store is, and who cleans it". Your own `~/.shedkeeper/shedkeeper.ini`, if you made one, still wins over
  the shipped file.

## Moving, removing and reinstalling

- **Removing ShedKeeper, the plugin way.** `/plugin uninstall shedkeeper@shedkeeper` (see Install, way 1): Claude
  Code takes the plugin out, and its hooks go with it -- there are no lines of ShedKeeper in your settings to
  clean. **The store stays.** The plugin folder of the removed (or replaced) version stays in the plugin cache
  for 7 days, marked as orphaned, and is then removed by Claude Code (the documentation). If the cache folder is
  gone while a hook of the plugin is still registered in a running session (a session older than the 7 days
  after an update), the command of the hook notices the missing file, tells you and the agent `ShedKeeper plugin
  files were not found: its protection is OFF ...` and lets the action pass -- the same loud pass as below, with
  the plugin words (`/plugin install ...` or `/plugin uninstall ...`). What Claude Code itself does when the cache
  folder of an INSTALLED plugin disappears is not in its documentation and was not measured here.
- **Removing ShedKeeper (the `install.py` way).** Ask your agent to remove it: it runs
  `python "<kit folder>/install.py" uninstall --project <project folder>` (or `--user`), then moves the kit
  folder to the trash. `uninstall` takes out ShedKeeper's entries and also the dead ones of an earlier
  installation (below). It takes out a line only when it is EXACTLY a command this installer writes for this kit folder
  (or one it recorded); a line that merely ends with the path of a hook -- a colleague's wrapper, a note -- stays and
  is named in the output. **The store stays** (`<X>:\_shedkeeper\<login name>\`): your copies and your trash
  are yours, and only you delete them.
- **If the folder is already gone** (removed or moved without running `uninstall`), the next tool call is
  NOT blocked: the command in the settings notices that the hook file is missing, tells you in a message
  `ShedKeeper was not found at <path>: its protection is OFF ...` and tells the agent the same, and lets
  the action pass. The message says what to do: if you removed ShedKeeper, ask the agent to run
  `python install.py uninstall` (from wherever ShedKeeper is now) or to remove the ShedKeeper lines from the
  settings file (the agent is told that editing this file is allowed); if you moved it, run
  `python install.py install` from the new place -- it takes out the old lines it wrote itself. The protection
  is OFF while this goes on: nothing is copied before an edit and nothing is moved to the trash.
- **Moving the folder.** Move it, then run `python install.py install --project <dir>` (or `--user`) from the
  NEW place. It adds the entries of the new place and **takes out the dead lines of the old place**. A line is
  taken out only when it is *proven* to be the installer's own and *proven* gone -- all four hold: (1) its exact
  command text is in the record `install` keeps (see Install, way 2) for this very settings file; (2) the
  command has exactly the form this installer writes; (3) it names a ShedKeeper hook file (`pre_tool.py`, `session.py` or
  `prompt.py`, in a folder `hooks`) at a path with a drive letter or share; (4) the system answers "not found"
  for that file. Each removal is printed:
  `removed a line of a previous installation from <settings file> -- that folder no longer exists`.
  The settings are copied first, as always. Everything else stays and is named: a line whose file EXISTS (it may be another copy of the kit that is alive); a line whose file cannot
  be looked at -- no permission, as with a folder of another account -- or that is on a volume that cannot be
  reached right now (an unplugged disk): *not known*, kept; and a line in ShedKeeper's form that this
  installer did not write (`left a line in ShedKeeper's form that this installer did not write`) -- a
  colleague's line in a shared file, another tool's, or one from an earlier release that kept no record: it is
  never taken out by the tool; remove it by hand. `python install.py check` from the new folder names these
  lines too.
- **Entries of 0.1.0 and 0.2.0** have the old command form, `python "<path>"`: with the folder gone,
  `python` exits with code 2 and Claude Code treats that as a block, so every guarded tool would be refused
  and the agent could not even correct the settings. `install` rewrites such an entry of THIS folder into
  the new form; `check` says that an entry is still old. The same holds for the guarded form that had no
  branch for a Python that cannot start: `check` names it ("an earlier form") and `install` rewrites it. A dead old line of ANOTHER folder was never recorded
  (those releases kept no record), so it is not taken out by the tool: `check` names it, says it BLOCKS, and
  that it has to be removed by hand.

Settings live in `shedkeeper.ini` (every key is commented there; `private_store` closes your personal folder
to other users, on by default). The name `shedkeeper` and the folder name `_shedkeeper` are each set in
one place: `shedkeeper/config.py`.

## Find and bring back

Run these in the folder of the kit (the one with `shedkeeper.py`); with the plugin that folder is in the plugin cache of Claude Code -- the start of a session names it, and your agent has it.

```
python shedkeeper.py find report.md              # a part of a path; searches the trash and the old copies
python shedkeeper.py find report.md --days 3     # only the last days
python shedkeeper.py restore t:2026-09-30:4      # an item from the trash (a rename back)
python shedkeeper.py restore v:2026-09-30:7      # an earlier version of a file
python shedkeeper.py restore t:2026-09-30:4 --to D:\somewhere\else.md
python shedkeeper.py status                      # where the store is, what it holds, whether the hooks are in
python shedkeeper.py selftest                    # run the tests
```

`find` prints an id for every hit, with the time, the reason (for the trash), how the change was made and the
session it came from -- enough to tell a person what an item was and why it is there. A restore never overwrites: if the place is taken it
refuses and suggests `--to` -- also when the file appears there between the check and the last step (that step is a
rename that refuses an occupied place; the verified copy then stays next to it as a `.part` file, and a file that was
moved to the trash for the restore stays in the trash, with a line saying so). Restoring a version puts the file that is there now into the trash first,
and only after the copy has been verified. Exit codes: `0` done, `1` refused (nothing changed), `3` done
but not written to the index (the record is printed; do not repeat the command), `2` wrong usage.

## Settings, the first look, and the words of a stop (0.3.0)

ShedKeeper is meant to be used **through a conversation with your own agent**, not by editing files: you do not need to be a programmer.
The hook that stops or warns does not know the language you speak with your agent -- the agent does. So every stop and every warning has
two readers, and each gets its own words.

### The words of a stop: for the person and for the agent

- The **person** gets a short English line (`systemMessage`) -- a fallback, in case the agent says nothing.
- The **agent** gets the facts and the lawful ways on (the `permissionDecisionReason` of a refusal, the `additionalContext` of a pass), ending like this:

  > What happened: *what happened and why*. The user decides whether they do it themselves or have the agent do it. Options: (a) the AGENT
  > does it: *the exact command*; (b) the USER does it: *what to do*; (c) a SETTING: `python <kit>/shedkeeper.py set <key> <value>` -- *what
  > stops being protected*. A setting is changed with `python <kit>/shedkeeper.py set <key> <value>`; one that weakens protection needs the
  > user's confirmation code (`CONFIRM SK-nnnnnn`, typed in their own message).

  The block holds no order about how the agent behaves: what happened, who decides, and the ways on.

Only the options that really apply are listed: a refused deletion has the trash command and **no** setting (none changes that stop). The words that were there before (the reason, the ready command after `Run:`) are kept; the
block is added after them, set off by ` -- ` where the older text ended with a command, so a reader that cuts the command at ` -- `
still gets the command. A refusal that has no special block gets a general one (the person decides; nothing is invented). The code is
`shedkeeper/advice.py`; the tests are `t_advice_files`.

### Settings, and changing them by a conversation

```
python shedkeeper.py settings [--json]     every setting: now, default, allowed values, weight, what it does in plain words
python shedkeeper.py set KEY VALUE         change one setting (a weakening of protection needs the person's code)
```

The agent shows you the menu in your language, you say which you want, the agent runs `set`. **A change that would WEAKEN protection is not applied on the
agent's word:** `set` prints a one-time code (`SK-nnnnnn`, valid 30 minutes) and the agent asks you to type `CONFIRM SK-nnnnnn` in your own message. ShedKeeper learns that
you typed it from its own hook of the event **`UserPromptSubmit`**: Claude Code runs it when you submit a message and gives it the text of that message, and the agent cannot
make that event happen by running a command. (The same event also carries what the runtime wraps -- the note of a finished background task, a message of another agent, a reminder: text an agent
can influence. The hook counts only the text you typed before any such wrapper and says aloud when it ignores a code found in one.) Only then does the repeated `set` apply the change (exit code `4` = not applied yet). An Edit of the ini by the agent that weakens
protection is refused the same way, and **a shell command of the agent that writes into a settings file** (`>>`, `sed -i`, `tee`, `cp`, a python write with a literal path in
any mode that changes the file -- `w`, `x`, `a`, `r+`; `a` is read like `>>` -- also behind `timeout`, `nice`, `sudo -u`, `xargs`, `env` and `bash -c "..."`) is refused: settings change through `set`.
**That guard is a list of forms of commands. It stops an agent that makes a mistake; against one that looks for a way round it is no wall** (the shell has no end: a quote inside a
word, a mask, a name made of variables, a program that opens the file itself). So the settings do not rest on it: **a weakening that stands in an ini file ACTS -- the hand that edits the file is taken for yours, and ShedKeeper cannot tell
your hand from a program the agent ran.** What it does is remind: at the start of every session you and the agent are told which setting weakens protection and how to put it back, and a weakening that
was **not made through `set`** (the journal of the store holds no record that `set` wrote it with your code) carries the mark "not through `set`"; `status` shows the same. The hook says nothing about it at its
decisions: ShedKeeper does not get in the way. If you did not put the line there, put the value back. Declared limit: a program the agent runs that writes the ini by a path it builds itself is not seen --
see "Risks" and ARCHITECTURE.md, section 7. If a journal of the store has a damaged line, `python shedkeeper.py quarantine` moves it to a `.bad` file (nothing is deleted) and closes every code
issued so far; ask for a new code after it. While any setting
weakens protection, every session start tells you and the agent which one and how to put it back (tightening needs no code). Details: ARCHITECTURE.md, section 7. `set` checks the value with **the same reader as the
ini** (a value that is not understood is **refused and nothing is written**, with the allowed values named), keeps every other line of the file
(comments, blank lines, the comment after a value, the line ending), writes through a temporary file and one `os.replace`, keeps the version
before in the store, puts a line in the log of the store (who: the session; what: the key; was; now; which file), and prints what changed -- and,
for a setting that can stop protecting something, one direct sentence saying **what stops being protected**. Exit codes: `0` changed and in effect;
`1` refused; `3` written but not in effect (another file read later overrides it; the text says so); `4` not applied: the person's confirmation code is needed.

**You see every change, whoever makes it.** A command that runs `shedkeeper.py set ...` (also inside `cd ... &&`, `bash -c`, PowerShell), and an
Edit / Write / MultiEdit of an ini file the kit reads, give you a `systemMessage`: `ShedKeeper setting KEY: was -> will be`, and for a heavy
one the sentence of what stops. The action is not stopped (the copy of the ini is made like any file's). An `echo "... set ..."` that only
mentions the command shows nothing.

**Where the settings live.** The shipped `shedkeeper.ini` of the kit is read first; then **your own** `~/.shedkeeper/shedkeeper.ini`; the later
line wins. `set` writes to your own file. **A plugin update replaces the kit folder -- and the
shipped ini with it -- but not your own file, so what you set survives an update** (the new defaults come through; your lines still win). With
`install.py` the kit folder is yours, but `set` still writes to your own file, so `git pull` has nothing to overwrite. If the environment
variable `SHEDKEEPER_INI` names a file, **only that file** is read, and `set` writes to it (this is what the tests use). `python shedkeeper.py
settings` and `status` name the files in use.

**The weight of a setting** says how heavy a change is. It is decided by the **code** of the kit (`config.WEIGHTS`); the comments of `shedkeeper.ini` describe it for you, and a test fails when they differ -- editing or deleting a comment changes nothing. `protection`: some value of it stops protecting something -- change it only with the
person's word, and the text says what stops. `tuning`: a limit or a place; protection stays (a wrong number may cause refusals). `notice`: only what
you are told. **No setting is a silent switch:** every one that lets an action through without a copy says so in the message to the person and in
the log; and **a copy that was tried and failed with an error is never let through by any setting** -- no setting does that. The test `t_settings_i1` runs every key in several values over a failed copy of a file.

| Key | Default | Weight | What it does |
|---|---|---|---|
| `store_dir_name` | `_shedkeeper` | protection | name of the store folder on each volume (after a change the old folders are no longer guarded as the store) |
| `auto_create_store` | `true` | tuning | create the store by itself on first use of a fixed volume |
| `private_store` | `true` | protection | close your personal folder to the other users of the machine |
| `max_copy_mb` | `512` | tuning | a file bigger than this gets no copy before it changes |
| `on_too_large` | `deny` | protection | such a file: refuse (`deny`) or change it without a copy (`allow`, logged) |
| `cmd_max_files` | `200` | tuning | one command may overwrite at most this many files |
| `cmd_deadline_s` | `20` | protection | seconds the file copies of one command may take; raised past the budget of the hook, the hook can be cut off |
| `step_budget_s` | `60` | protection | seconds the check of the protected list may take before one step (all entries and their copies); raised past what the hook may take, the hook can be cut off |
| `walk_budget_ms` | `3000` | tuning | a walk of a tree that takes longer is an EXPENSIVE check: the survey says so and the step is refused until you choose |
| `usn_resolve_max` | `2000` | tuning | on a volume with the change journal, more changed files than this are cheaper to find by a full walk |
| `lock_wait_s` | `30` | tuning | how long a check waits for another check of the same project (parallel calls, sub-agents) |
| `sqlite_busy_s` | `5` | tuning | how long the copy of a SQLite file waits for a writer that holds it |
| `before_confirm` | `accelerators` | tuning | before you confirm the protected list: only the recognised commands (`accelerators`, default) or the candidates of the survey as a temporary list (`provisional`) |

The settings for databases (`db_protection`, `pg_dump_path`, `max_db_mb`, `db_deadline_s`, `on_db_too_large`, `on_db_remote`, `on_db_unsupported`, `db_fresh_minutes`, `mcp_no_db`, `db_command`, `max_db_mb_every_step`) are in `shedkeeper.ini` for a later version and do nothing in this release.

### The first look: `survey`

```
python shedkeeper.py survey [--json] [--project PATH]     READ ONLY: what is here, what ShedKeeper would do, what to set
python shedkeeper.py survey --done                        only records that the survey was done for the project
```

`survey` reads and prints: Python and its version; the store of the project and the free space of its volume; the candidates for the protected list (the project folder); the current settings; and **recommendations** -- `{key, value, why, weight}` in plain words. It writes **nothing except one line** -- `survey_done` -- in the personal folder of the store (`guide.jsonl`). A recommendation is an offer: nothing is changed.

**Offered once.** At the start of a session, if the store has no record that the survey was offered for this project (the root of its git
repository, else the folder), the context of the agent carries one more sentence: at a natural moment, offer the user -- in their language --
a setup check (`python <kit>/shedkeeper.py survey`), then explain the findings and ask before changing anything. The record "offered" is
written **at once**: the offer is made **once per project**, answered or not. If no record can be kept (the store is not usable) nothing is
offered -- an offer that cannot be remembered would come every session. Likewise **the warning about an engine this kit does not copy is given
once for a project and an engine**; afterwards such a command passes quietly, with a line `db_unsupported_passed` in the log of the store.

## Where the store is, and who cleans it

One store per volume, outside your projects (so `git clean -x` in a project cannot reach it): the folder
`<X>:\_shedkeeper\`, and inside it **one personal folder per user**, named by the Windows login name. It is
built the way the Windows recycle bin is (`$Recycle.Bin` -- a folder per user on every disk):

```
_shedkeeper\                                                the volume folder (label .shedkeeper-store)
_shedkeeper\<login name>\                                   YOUR folder -- closed to other users, see below
_shedkeeper\<login name>\.shedkeeper-user                   label: product, SID, login name, full name, created
_shedkeeper\<login name>\README.txt                       what this folder is, for whoever opens it (UTF-8 with BOM)
_shedkeeper\<login name>\trash\<day>\<drive letter>\<original path>     what was "deleted"
_shedkeeper\<login name>\trash\_index\<day>.jsonl           one line per move
_shedkeeper\<login name>\versions\<day>\<drive letter>\<folders>\name.v_<HHMMSS>_<n>.ext   copies made before changes
_shedkeeper\<login name>\versions\_index\<day>.jsonl        one line per copy (with sha256)
_shedkeeper\<login name>\transcripts\<project>\<session>.jsonl   copies of session transcripts (the session id must be a
                                                            plain name, [A-Za-z0-9_-], up to 128 characters; any other is
                                                            not copied and a line goes to the journal)
_shedkeeper\<login name>\log.jsonl                          one line per hook firing that did something, and per notable event
```

The trash is on the same volume as the item: a move is a rename, so it is instant and copies nothing.
Copies of versions go to the store of the project's volume. **ShedKeeper never deletes anything from the
store and has no command for it. You clean it**, for example by removing old day folders. It grows
until you do.

Windows has three names for an account: the number (SID), the login name and the full name. The folder is
named by the login name, and the label keeps all three. The number is what identifies the account. A folder
is recognised as YOURS by its **owner and permissions, read from the file system** -- not by the label, which
is only a file that anybody can write: the label holds your SID and the owner is you (or the Administrators
group, which owns what an elevated administrator makes); it is not a link or a junction. The permissions
are read too, but they do not decide whose the folder is: with `private_store` on they should be exactly you
and SYSTEM, not inherited, and when they are not (an administrator or a backup program added an entry) the
folder stays yours and `status`, `install check` and the session start say `private: NO` with the list --
ShedKeeper does not close it again on its own, because that would undo someone's decision, and it does not
start a new folder, because the copies in the old one must stay findable. If the owner cannot be read, the
label is believed only on a volume that keeps no owners (exFAT); on a volume that does, such a folder is
not taken. If the name is taken by anything else -- the
folder of another account with the same login name, a folder somebody made in advance, a forged label, a
folder with no label -- ShedKeeper does not refuse and does not touch it: it uses the next free name,
`<login name>.2`, `.3` ... (up to `.99`; if all are taken it refuses, with the reason), and tells you so
(`python shedkeeper.py status` lists the folders it skipped and why). Your SID is read from the token of the
running process, no program is started; if it cannot be read, no personal folder is made (without the number
the folder can be neither closed nor recognised) and `find` / `restore` say that NOTHING was searched.

When your personal folder is made, you are told in one paragraph: where it is, who it is closed to, why the
name has a number (if it does), and how to switch the closing off. The folder gets a `README.txt` of its own
for anyone who opens it, and the event is written to `log.jsonl` (`store_created`).

A `_shedkeeper` folder that ShedKeeper did not create and that holds files is left alone; ShedKeeper says so and
how to accept it (`python shedkeeper.py adopt X:`) or to rename the store folder (`store_dir_name`).

A store made by version 0.1.0 has its `trash\`, `versions\` and the rest directly in `_shedkeeper\`.
Version 0.2.0 does not move or read them: `find` and `restore` look only in your personal folder. Move
them there by hand if you want them found.

## What ShedKeeper reads and keeps

This is what the code reads and keeps -- each line was checked against the code of 0.3.0, and a read that is missing here is a fault of this text. Everything ShedKeeper does happens on your machine, in the processes of its own hooks and commands.

### What it reads

- **The text of what the agent does:** the path of an Edit or Write, the text of a shell command -- to see whether something would be overwritten, deleted or destroyed.
- **The programs it starts.** The hook runs `git` (to list the files a rollback would overwrite). `install.py` runs `git` (to ask whether a settings file is
  committed or ignored) and Git Bash (the probe of `check`: it runs the registered hook command once on a harmless input). A SQLite file inside a protected folder is copied with the backup API of SQLite.
- **Your Windows account:** the login name, the number (SID) and the full name of the account that runs the hook (`GetUserNameW`, the token of the process,
  `NetUserGetInfo`) -- to name your personal folder and to write its label `.shedkeeper-user`; `status` shows them.
- **The file it is about to protect:** to copy it before the change.
- **Settings files:** its own `shedkeeper.ini` files, and the Claude Code settings files (`.claude/settings.json`,
  `settings.local.json`, your user `settings.json`) and plugin registry (`installed_plugins.json`, `known_marketplaces.json`) --
  `install.py` reads them to add or take out its hook entries and to say which door is open, and the hook reads a settings file when the
  agent tries to edit it, to see whether the edit would take ShedKeeper out.
- **The message you type, only to look for a confirmation code.** Claude Code gives the hook of the event `UserPromptSubmit` the text of
  each message you send; the hook looks in it for `CONFIRM SK-nnnnnn` and does nothing else with it. The text is not kept: the journal
  gets the code, the time and the session id.
- **The record of the conversation (the transcript) is copied, not read.** ShedKeeper never reads what is in it.
- It does not read the file of instructions of your system (`CLAUDE.md` and the like) for its own needs, and the start of a session does not touch it. When the
  agent edits that file, it is a file like any other: a copy of it is taken before the change.

### What it copies, and where

All of it goes into your personal folder of the store, `<X>:\_shedkeeper\<login name>\`, on the volume of the item it concerns (see
"Where the store is"):

| What | Where | Note |
|---|---|---|
| a file before the agent changes it | `versions\` + an index (path, time, size, sha256, how, session) | a copy of `.env` or of a key file holds what the file held |
| an item the agent "deleted" | `trash\` + an index | moved, not copied; it keeps its own permissions |
| **the records of your conversations with the agent** | `transcripts\<project>\<session>.jsonl` | as in 0.2.0: at the start of a session the transcripts of **every session of that project** are copied whole, at its end the current one. A conversation can hold anything you or the agent typed -- a password too |
| a line for a firing of a hook that refused, had a target or failed (a pass with nothing to do leaves none), and one per notable event | `log.jsonl` | time, tool, decision, number of targets, milliseconds; a few events name a path -- no command text, no password |
| codes and proofs | `confirm.jsonl`, `proofs.jsonl` | the code, its key and value, the session, the time -- not your message |
| once-only notes | `guide.jsonl` | the survey offered or done, the rule recommended, a warning about an engine given |
| what the installer wrote | `installs.jsonl` | the settings file, the exact command, the kit folder, the time |

Outside the store: your own settings in `~/.shedkeeper\`, and `settings.json.shedkeeper-backup-<time>` beside the Claude Code
settings file that `install.py` changed.

### What does not go out

**Nothing is sent anywhere.** The kit has no network code of its own: it opens no connection, makes no request, reports nothing to anyone.
ShedKeeper itself sends neither the message you type nor the conversation anywhere; the transcripts are copied from one
folder of this machine to another. But what its hooks say to the agent (the text of a refusal or a warning, a masked address, paths; the list is in "For your agent") enters the
conversation, and the conversation goes where every conversation with Claude Code goes: to the provider of the model.

### Who can see the store

Only you and the system: your personal folder is closed to the other users of the machine (details: "Where the store is" and, in "Borders", "Who can read
the store"). Not covered: an administrator of the machine, who can take over any folder; and a trash item that keeps the permissions it had.
The kit refuses the agent every write into the store that it recognizes (an edit, a shell write with a path it can read -- see "Risks": a program that builds the path itself is not seen); the agent can still read it with its own tools, as it can read any file that you can.
Anyone who can read your account can read the copies -- treat the store like the files it holds.

### How to remove all of it

ShedKeeper never deletes anything from the store and has no command for it; you do it, outside the agent (the hook refuses the agent the writes
into the store that it recognizes). 1. Take ShedKeeper out: `/plugin uninstall shedkeeper@shedkeeper`, or `python install.py uninstall --user` (or
`--project <dir>`) -- see "Moving, removing and reinstalling". 2. Delete your personal folder `<X>:\_shedkeeper\<login name>\` on every
volume that has one (and the folder `<X>:\_shedkeeper\` when it is empty). 3. Delete `~\.shedkeeper\` (your settings) and the
`settings.json.shedkeeper-backup-*` files if you do not want them. 4. Delete the kit folder, if you used `install.py`.

## Borders -- what ShedKeeper does NOT see

- **Databases.** Not protected in this version (see the top of this file): a command or an MCP call that destroys data in a database passes unseen.
- **Inside a program that runs.** `python script.py` that deletes or overwrites files passes: ShedKeeper
  reads the text of the command, not what a program does. Deletion or writing inside `bash -c`,
  `bash -lc`, `powershell -Command`, `cmd /c` and `python -c` text IS read; a script file is not.
- **Programs that write and that ShedKeeper does not recognise as writers** (a copy is NOT taken before
  them): `robocopy` and `xcopy` without a delete switch, `rsync`, `patch`, `truncate`, `Clear-Content`,
  `New-Item -Force`, `[IO.File]::WriteAllText(...)`, `Compress-Archive -Force`, `Expand-Archive -Force`,
  `tar -x`, `unzip -o`, `awk -i inplace`, `Rename-Item -Force`, `ln -sf`, `git apply`. What ShedKeeper does read
  as writing: redirects (`>`), `tee`, `sed -i`, `perl -i`, `dd of=`, `cp`/`mv`/`install` (a folder copied
  or moved over an existing folder: every file it overwrites), `curl -o`, `wget -O`, `Set-Content`,
  `Out-File`, `Copy-Item`/`Move-Item`, `Export-Csv`, `Tee-Object`, `Invoke-WebRequest -OutFile`, literal
  writes in python text, and the files a git rollback would overwrite -- in the forms it can read.
  Paths written with `[ ]`, `{ }`, `%` or `$` in them are not read as targets.
- **Paths that are not in the text**: variables (`$X`, `%X%`), computed paths, piped input
  (`ls | xargs rm`). For deletion ShedKeeper still refuses and gives a general instruction; for writing it
  cannot copy what it cannot name (a recursive copy from a variable is refused). A `cd` to a
  non-literal path (`cd "$DIR"`) is not followed: later relative paths are read against the old folder (a database file named
  relatively after such a `cd` that is not found there is refused -- "cannot tell" -- not passed as "does not exist").
- **Timeout or a crash of the interpreter.** By the Claude Code hook contract a hook that times out, or
  cannot start, lets the action through. The timeout is set to 90 seconds for that reason (see Install).
  A single copy of a very big file is not time-boxed inside the hook.
- **A moved or deleted ShedKeeper folder.** The hooks are registered by absolute path. If the folder is
  gone, the command in the settings (version 0.2.0 and later: the form shown under Install) prints a loud
  warning and lets the action through -- **the protection is OFF** until the lines are taken out or the
  kit is installed again from its new place (see "Moving, removing and reinstalling"). Entries of the
  old form (0.1.0, 0.2.0 as installed before this change) are different: `python` exits with code 2,
  which Claude Code treats as a block, and every guarded tool is refused until the entries are taken out;
  `install.py install` from the folder that exists rewrites its own entries and removes the dead ones that
  it recorded (older ones: by hand, see "Moving, removing and reinstalling").
  Measured live (Claude Code 2.1.284, `claude -p`, 2026-10-01): old form -- SessionStart and PreToolUse
  hooks end with exit code 2 and `can't open file`; new form -- both end with exit code 0 and the warning,
  the Bash tool ran. Not measured: the warning in the interactive screen of Claude Code (only its text in
  the event stream), and a volume that is removed while a session runs.
- **A project on a removable or network volume.** ShedKeeper creates its store only on a fixed volume. On
  any other volume there is no store, so every change to an existing file there is REFUSED (there is
  nowhere to put the copy); `python shedkeeper.py adopt X:` is a person's decision to use such a folder
  anyway (the copies would leave with the medium).
- **A folder of your own named like the store** (`_shedkeeper`, or your `store_dir_name`) anywhere in a path
  is taken for the store: edits inside it are refused and deletions there are not handed to the trash.
  Rename the store folder in `shedkeeper.ini` if that is a problem.
- **The store is written by a person only.** Edit/Write and shell writes (appends too) into `_shedkeeper` are refused;
  `shedkeeper.py` itself writes there from inside its own process, which the hook cannot see and should
  not.
- **The ordinary ways of removing the hooks need your code** (an Edit or Write of the settings file that takes them out, `install.py uninstall`, `claude plugin uninstall|disable shedkeeper`; see "Risks"); a
  settings file written by a shell command is not read. `python install.py check` shows whether the hooks are still there. When the hooks are gone altogether (taken out, plugin off, `disableAllHooks`),
  nothing of ShedKeeper runs, so the start of a session cannot say so: it says so only while a hook is there and its file is not.
- **Only Windows** in v1 (drive letters, junctions, volume types).
- Big files: over `max_copy_mb` (512 by default) no copy is made; by default the change is refused
  (`on_too_large = deny`).
- An action with no copy possible is refused, so a full disk or a broken store stops edits rather than
  letting them go unprotected. `python install.py uninstall` removes the hooks if you need to (when the agent runs it, it needs your confirmation code).
- **Appending is not copied.** `>>`, `tee -a`, `Add-Content` and a python `open('f', 'a')` / `Path('f').open('a')` add to the end of a file and remove nothing
  that was there, so ShedKeeper takes no copy before them (and refuses none for the size of the file). Everything that was in the file stays in it. The settings files and the store stay guarded against them.
- **Who can read the store.** Your personal folder `<X>:\_shedkeeper\<login name>\` is closed to other
  users of the machine: when it is made, its inheritance is removed and only you and SYSTEM may open it.
  ShedKeeper does not take the word of the tool that set this: it reads the permissions back and goes on
  only if the list holds exactly those two entries and does not inherit. If it does not, the folder is not
  made and the change is refused with the reason (on a volume without permissions, such as exFAT, set
  `private_store = false` in `shedkeeper.ini`: the folder is then made with the permissions it inherits,
  and `status` says `private: no (private_store = false)`; it is recognised as yours by its owner and its
  label). The folder is made CLOSED FROM THE FIRST MOMENT (the permissions are given to the system call that
  creates it), so there is no instant at which another user could reach it; it is also required to be empty
  when the label goes in, and after the final rename it is judged again. The permissions are read again
  by `python shedkeeper.py status`, `python install.py check` and at every session start: `private: yes` or
  `private: NO (why)`; a folder that was opened up later (or made earlier with `private_store = false`) stays
  yours -- see above -- and is reported aloud until you close it. Copies made before
  changes are new files in your folder and inherit its permissions -- secret files such as `.env` are as
  private as the folder.
  What is NOT covered: **an administrator of the machine can take over any folder** (take ownership), this
  one too -- the folder is closed to other users, not to the people who run the machine. And **what you
  move to the trash keeps its own permissions** (it is a move, not a copy, and a restore must bring the
  file back as it was): whoever could read the file at its old place can open it in the trash by its exact
  path (Windows lets every user pass through folders by default) -- but cannot list your personal folder to
  find it. The shared volume folder `<X>:\_shedkeeper\` is made closed as well, the way `$Recycle.Bin` is:
  other users may list it, make their OWN folder in it (each closed the same way) and read its label -- and
  nothing more, so nobody can rewrite the label or another user's folder (`status` reads this back and says
  so). Not covered: a shared folder made by an earlier version, or by hand, keeps the rights it had
  (`status` says `NOT closed as a shared folder`; closing it is a person's step); another user can still
  take the names `<login name>`, `<login name>.2` ... `.99` with folders of their own, which only moves
  you to the next number, and when all 99 are taken the use is refused with the reason; and a folder named `.<login name>.part-...` that a lost creation race leaves behind is
  never deleted by the tool (`status` names it; it holds a label and a README, no copies). Not tried: a
  second real user -- the rights are read from the permission lists and by `icacls`, nobody else logged in.
- **A shared project settings file.** `install --project` writes the absolute path of this folder into
  `<project>\.claude\settings.json`. If that file is committed and shared with a team, the path goes
  with it and will not exist on other machines. `install` warns when the file is tracked by git or not
  ignored (it asks git; without git it looks for a `.git` folder). For a personal install on a shared project
  use `--user`, or put the entries into `settings.local.json` (the installer has no switch for that file).
  The lines of colleagues in a shared file are never removed: only lines the installer recorded are.
- **Not measured, not covered by the two-doors rule or by `check`:** managed (enterprise) settings and
  command-line settings (they outrank the files read here); `claude --plugin-dir` sessions; an entry that
  looks live (the exact command, the file there) but carries `async`, `shell` or `if` and so would not run as
  written; two kit folders both installed by `install.py` in one file (both run); a plugin whose cache folder
  is intact and yet fails to load.

## Risks

**ShedKeeper is a helper for your agent, not a lock.** It is built for an agent that works for you in good faith and can still make a mistake: a wrong path, a variable that expanded to something else, a command run twice. For that agent the usual ways to lose your data are closed, and a stop tells it why and what the lawful way on is. Two tasks are promised, and only these:

1. **Your data does not disappear through what the agent does** -- in a project and outside it: a change keeps a copy, a deletion goes to a trash.
2. **The agent does not switch the protection off by the usual way**: the settings of ShedKeeper are changed with `set` and your confirmation code, and the commands that write into them (an edit, `>>`, `sed -i`, `tee`, `cp`, a python write with a literal path) are refused. The same code is asked for the ordinary ways of taking ShedKeeper itself out: an Edit or Write of a Claude Code settings file (`.claude/settings.json`, `settings.local.json`, your user `settings.json`) that takes the hooks of ShedKeeper out, switches its plugin off or sets `disableAllHooks`; `python <kit>/install.py uninstall`; `claude plugin uninstall|remove|disable shedkeeper`. An edit of those files that leaves the hooks alone (a permission, a timeout) is not stopped. You can always do any of it yourself, outside the agent.

What it does **not** stop, because no list of command forms can: a deliberate way round. Among them -- a command made confusing on purpose (a quote inside a word, a mask, a name made of variables); a program that builds the path itself and writes it; **a mod of Claude Code**, which works with the rights of Claude Code, without a sandbox, and can block or rewrite the calls of the tools -- the documentation says a mod that approves tool calls can approve one that a PreToolUse hook of yours blocked, and ShedKeeper's refusal is such a hook (Anthropic's blog of 2026-10-01, https://claude.com/blog/claude-code-mods, and the documentation, https://code.claude.com/docs/en/plugins/mods/overview; both pages answered on 2026-10-02); a scheduled prompt that carries code; a settings file of Claude Code written by a shell command (`sed -i`, `jq ... > settings.json`) -- only the Edit and Write tools are read there, and the settings of Claude Code are your layer, not ShedKeeper's. When the whole of ShedKeeper is gone (its hooks taken out, its plugin off, `disableAllHooks` on), nothing of it runs and nothing tells you: `python install.py check` is the way to look. What keeps an agent from going round is that it understands what ShedKeeper is for -- which is why the kit gives the agent its facts and recommends a rule in **your** system (section "A rule for your agent"). Protection against an agent that was taught to do harm, or one that is malicious, is **cybersecurity**: ShedKeeper does not promise it and does not offer it. Everything the kit says to your agent, reads and keeps is described in "For your agent", so none of it is hidden from you.

## For your agent

If you are an AI agent working in a project where ShedKeeper is installed, this section says what the kit does, why, and the lawful way through each stop. ShedKeeper exists to protect the data of the person you work for -- also from the mistakes an agent can make by accident. A stop is a fact about a place where data could be lost, with the way on named in it.

- **Deleting.** `rm`, `del`, `Remove-Item`, `git clean`, `find -delete` ... are refused, because a deletion cannot be undone and a move to the trash can. The lawful way is the trash command; the refusal holds it with the paths filled in:

  ```
  python "<kit folder>/shedkeeper.py" trash "<path1>" "<path2>" --reason "why"
  ```

  A command that used variables or masks is refused because the kit can only name what is written out; with the names resolved to literal paths it goes through.
- **Changing a file.** A copy is taken automatically, so nothing is needed. If no copy could be taken (the store is not usable, the file is too large ...), the refusal says why and names the ways on: remove the cause, or ask the user.
- **Bringing something back.** `python "<kit folder>/shedkeeper.py" find <part of the path>` prints, for every item, the id, the time, the reason (trash), how it was changed and the session; `python "<kit folder>/shedkeeper.py" restore <id>` brings it back (the file there now goes to the trash first; `--to PATH` if the place is taken). A restore changes the person's files, so it is made with the person's word: when you noticed a loss, the lawful move is to say what was lost, when and why (from `find`) and offer to bring it back; when the person asks, to show every version that fits, with its time, and ask which one.
- **`NOT SEARCHED: <path> -- <why>`** in the output of `find` or `restore` (exit code 2) says that a place that may hold this person's copies was not looked at (their folder under another name, a folder whose owner is not them, a SID that could not be read). "Nothing found" is then not an answer about those places; the accurate report names them and the reason.
- **A weakening of protection needs the person's code.** The settings of ShedKeeper can switch protection off, so a weakening is made only with the person's word: `set` for such a value prints `SK-nnnnnn`, the user types exactly `CONFIRM SK-nnnnnn` in their own message, and the same `set` is run again. An edit of the ini that weakens protection, and a shell write into a settings file, are refused; settings change through `set`.
- **The store `_shedkeeper`** holds the person's copies and trash. ShedKeeper writes there; only the person cleans it, so the hook refuses writes into it (an edit, a shell write, an append) and there is no command that empties it.
- **Removing ShedKeeper** (when the person asks; the hook stops `uninstall`, `claude plugin uninstall|disable` and an edit of the settings that takes the hooks out until the person types the code `CONFIRM SK-nnnnnn` -- the refusal says so, and the person can do it themselves outside the agent): if it came as a PLUGIN (the start message says `ShedKeeper (plugin)`), `/plugin uninstall shedkeeper@shedkeeper` -- from a terminal `claude plugin uninstall shedkeeper@shedkeeper`; there is no settings file to edit. If it was put in by `install.py`: `python "<kit folder>/install.py" uninstall --project <project folder>` (or `--user` if it was installed for the user), then the kit folder goes to the trash (`python "<kit folder>/install.py" check` says which door is open). The store `_shedkeeper` is not removed: it holds the person's copies. `uninstall` also takes out the dead lines of an earlier installation of another folder that this installer recorded (and names, leaving them, the lines it did not write).
- **`ShedKeeper plugin files were not found: its protection is OFF`** is the same case for the plugin: the person reinstalls with `/plugin install shedkeeper@shedkeeper` or removes it with `/plugin uninstall shedkeeper@shedkeeper`; the plugin cache is not edited by hand.
- **`ShedKeeper was not found at <path>: its protection is OFF`** in a message at a tool call or at the start means the kit folder was removed or moved and the settings still name it. Editing that settings file is allowed: if ShedKeeper was removed, delete the entries whose command names that path (or run `python "<kit folder>/install.py" uninstall --project <project folder>` (or `--user`) from a kit that is still there); if the folder was moved, run `python "<new folder>/install.py" install --project <project folder>` (or `--user`) from the new place -- it takes out the old lines that it wrote itself; any it did not write (it names them), delete by hand.
- **Exit code `3`** of `trash` or `restore` means the item was moved but the index line failed: the move is done, and the record is in the output.

### What ShedKeeper puts into your context, and what it reads and keeps

Nothing here is hidden; the full account of what it reads and keeps is the section "What ShedKeeper reads and keeps".

- **At the start of a session** (`additionalContext`): one or two lines -- that ShedKeeper is on, the kit folder, how to find and bring back, how to remove it, and the warnings of the start (the store not usable, a hook not found, a hook timeout that differs). Then, once per project, a note that a read-only setup check exists (`survey`); once per **user**, the facts about ShedKeeper and its recommendation to the user (section "A rule for your agent"); and, while a setting weakens protection, one line saying which, the way back, and -- for one that was not made through `set` -- the mark "not through `set`".
- **At a decision of the hook**: for a refusal, the reason, the lawful command, and the block "What happened / who decides / options"; for a pass that took a copy or no copy, a line saying so. The hook says nothing at a decision about the state of the settings.
- **When the user types a confirmation code**: the hook of the prompt (`UserPromptSubmit`) gives one line to the agent (and the same to the person) -- the whole list of what it says: the code was **received**; it was **received before**; it is **unknown** here (no such code was issued, or its store cannot be read); it has **expired** (30 minutes); it was **already used**; it **stood in a text that is not the person's** and was ignored (a task notification, another agent, a reminder of the runtime); it **could not be recorded**, **could not be received** (the journal cannot be read) or **could not be processed** (a fault of the hook, with the fault named); and, while a code waits, that something in the message **looks like a code but is not in the form** `CONFIRM SK-nnnnnn` (nothing is counted then). A message with no code and nothing like one gets no answer.
- **Output of the commands** (`find`, `restore`, `trash`, `set`, `settings`, `status`, `survey`): facts and the next command.
- **What it reads:** the text of the commands the agent runs (to see what they would change), the files it is about to copy, the settings files, `~/.shedkeeper/shedkeeper.ini`, the transcripts of the session (to save a copy of them in the store), and the text of the user's message at `UserPromptSubmit` (only to find a code of the form `CONFIRM SK-nnnnnn`). It does not read the file of instructions of the user's system (`CLAUDE.md` and the like) for its own needs; when you edit that file, a copy of it is taken first, as of any file.
- **What it keeps** (in the personal folder of the store): copies of files, the trash and its index, the log, the journal of confirmations (`confirm.jsonl`), the once-only notes (`guide.jsonl`: setup check offered / done, the recommendation about the rule given), and copies of the session transcripts. No password of a connection is kept in a record (a command is kept with its addresses masked; a literal in it, such as a token in an `UPDATE`, is kept as typed).

## A rule for your agent

ShedKeeper does not ask your agent to follow an order it wrote; it **recommends that you give your agent a rule of your own**, in your own system. The agent is told this once (once per user, at the first session start), and passes it to you in your language. Ask your agent to write, in the file of instructions your agent reads (for Claude Code, `CLAUDE.md`), a short rule about working with ShedKeeper. ShedKeeper never writes that file and never checks that the rule is there: it is yours (it only takes a copy of the file before you or the agent change it, as of any file).

The principles of the rule:

1. **Why ShedKeeper is here.** It protects your data -- from the mistakes of your agent too.
2. **When ShedKeeper stops an action,** the way on is the lawful one it names, or a question to you -- not a way round it.
3. **Only you weaken the protection.** The agent asks; the change is made with your confirmation code, typed in your own message.

A model wording -- **write it in your own words**, in your language, as short as you like:

> ShedKeeper guards my files; it is there to protect my data, also from your own mistakes. When it stops something, use the way it names (for a deletion, the trash command) or ask me -- do not look for a way round it. Switching its protection off or down is my decision, made with my confirmation code in my own message.

## Related tools

- **`/rewind` (Claude Code checkpointing)** rolls back the agent's file edits per prompt. It is not
  enough on its own for deletions: the documentation says "Checkpointing does not track files modified
  by Bash commands. For example, if Claude Code runs: `rm file.txt` `mv old.txt new.txt`
  `cp source.txt dest.txt` These file modifications cannot be undone through rewind. Only direct file
  edits made through Claude's file editing tools are tracked."
  (https://code.claude.com/docs/en/checkpointing). ShedKeeper covers those Bash cases in the forms it can
  read (see Borders), and keeps its copies outside the session (checkpoints are swept by the runtime
  after about 30 days).
- **cc-safety-net** (https://github.com/kenryu42/cc-safety-net) and **dcg**
  (https://github.com/Dicklesworthstone/destructive_command_guard) block dangerous commands. They only
  block: the agent gets a "no" and must find another way. ShedKeeper answers a deletion with a working, reversible alternative instead.
  They can be used together with shedkeeper.

## History

These safeguards were not designed as a product first. They grew out of everyday work in which a
non-programmer and an AI coding agent built software together, starting in spring 2026: when an agent's
edit or cleanup cost work, a small guard was added so that the same kind of loss would not repeat. For
several months they lived inside one private working repository. The protection grew after cases in which
an agent deleted a person's files. In September 2026 they were separated
from it, rewritten without any links to that setup, and packaged as this kit.

## License

Apache License 2.0 -- see [LICENSE](LICENSE).

To report a vulnerability, see [SECURITY.md](SECURITY.md). What changed in each version: [CHANGELOG.md](CHANGELOG.md).
