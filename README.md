# ShedKeeper

A safety net for work done by an AI coding agent (Claude Code) on your files. Three promises:

1. **A copy before every change.** Before the agent edits or overwrites an existing file, ShedKeeper saves a
   verified copy (size and sha256 compared with the original). No verified copy -- no change. A new copy is skipped
   as "same" only when the previous copy, read at that moment, still has the content of the file; a damaged or
   shortened one is not trusted: a new copy is taken and the journal says why.
2. **Deletion becomes the trash, not a ban.** When the agent tries to delete something, ShedKeeper refuses
   and hands it the ready command that moves the item to a trash instead. Nothing is lost, and the
   agent is not stuck: it just runs the command it was given.
3. **The way back is by your word.** Ask your agent: it finds what was changed or removed, tells you what it
   was and why it is gone, and brings it back once you say yes -- or you do it yourself with one command.
   Nothing in ShedKeeper empties the store -- only you do.

**These promises are about files. Databases are not covered in this version:** a `DROP TABLE`, `TRUNCATE`,
`DELETE` or `UPDATE` the agent runs goes through with no copy of the table. A copy of a table before a
destructive statement is planned for a later version (0.3.0); until then, back up your databases yourself.

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
  its folder holds `hooks/hooks.json` and both hook files. It names the plugin and what to do. A plugin that is
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
second run it also puts back a timeout or a tool matcher that was changed). The hooks are given a
60-second timeout: the hook spends at most 20 s on copies (`cmd_deadline_s`) and at most 10 s on git
calls, and the rest is margin -- a hook that times out lets the action through. `python` must be on
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
  if [ $rc -eq 126 ] || [ $rc -eq 127 ]; then printf '%s' '<JSON: Python cannot run>'; exit 0; fi;
  exit $rc; else printf '%s' '<JSON warning>'; fi
```

(one line in the settings.) While the hook file is there, `python` runs it and its exit code is the exit code of
the whole command (a refusal, exit 2, still refuses). Bash answers 127 when `python` is not found and 126 when
it cannot be started (the Microsoft Store placeholder); `python` itself never exits so, so in those two cases
the command prints the warning that the protection is OFF and exits 0 -- loudly, not silently. When the file is
not there the command prints the other warning and exits 0.

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
  command has exactly the form this installer writes; (3) it names a ShedKeeper hook file (`pre_tool.py` or
  `session.py`, in a folder `hooks`) at a path with a drive letter or share; (4) the system answers "not found"
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
  branch for a Python that cannot start: `check` names it ("the 0.2.0 form") and `install` rewrites it. A dead old line of ANOTHER folder was never recorded
  (those releases kept no record), so it is not taken out by the tool: `check` names it, says it BLOCKS, and
  that it has to be removed by hand.

Settings live in `shedkeeper.ini` (every key is commented there; `private_store` closes your personal folder
to other users, on by default). The name `shedkeeper` and the folder name `_shedkeeper` are each set in
one place: `shedkeeper/config.py`.

## Find and bring back

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
_shedkeeper\<login name>\log.jsonl                          one line per hook firing
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

## Borders -- what ShedKeeper does NOT see

- **Databases.** SQL the agent runs (`psql`, `sqlite3`, a script, a database tool) is not read: a dropped,
  truncated or rewritten table gets no copy. Planned for a later version (0.3.0), not promised for a date.
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
  non-literal path (`cd "$DIR"`) is not followed: later relative paths are read against the old folder.
- **Timeout or a crash of the interpreter.** By the Claude Code hook contract a hook that times out, or
  cannot start, lets the action through. The timeout is set to 60 seconds for that reason (see Install).
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
- **The store is written by a person only.** Edit/Write and shell writes into `_shedkeeper` are refused;
  `shedkeeper.py` itself writes there from inside its own process, which the hook cannot see and should
  not.
- **The agent can remove the hooks** by editing the settings file. `python install.py check` shows
  whether they are still there, and the session-start hook says so when they are not.
- **Only Windows** in v1 (drive letters, junctions, volume types).
- Big files: over `max_copy_mb` (512 by default) no copy is made; by default the change is refused
  (`on_too_large = deny`).
- An action with no copy possible is refused, so a full disk or a broken store stops edits rather than
  letting them go unprotected. `python install.py uninstall` removes the hooks if you need to.
- **Appending is not copied.** `>>`, `tee -a`, `Add-Content` add to the end of a file and remove nothing
  that was there, so ShedKeeper takes no copy before them. Everything that was in the file stays in it.
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

## For your agent

If you are an AI agent working in a project where ShedKeeper is installed:

- **Do not delete** (`rm`, `del`, `Remove-Item`, `git clean`, `find -delete` ...). The hook will refuse.
  Move to the trash instead:

  ```
  python "<kit folder>/shedkeeper.py" trash "<path1>" "<path2>" --reason "why"
  ```

  The refusal message contains this command with the paths filled in; run exactly that. If your command
  used variables or masks, resolve them to literal paths first.
- **Before you change a file** nothing is needed: a copy is taken automatically. If the hook refuses
  because no copy could be taken, read the reason (store not usable, file too large ...) and tell the
  user; do not look for a way around it.
- **To bring something back -- only after the person agrees.** Find with
  `python "<kit folder>/shedkeeper.py" find <part of the path>`; it prints, for every item, the id, the time,
  the reason (trash), how it was changed and the session. Then bring it back with
  `python "<kit folder>/shedkeeper.py" restore <id>` (it never overwrites; use `--to PATH` if the place is
  taken). Two cases, and in both you ask first:
  (a) **you noticed a loss** -- say what was lost, when and why (from `find`), offer to bring it back, and wait
  for a "yes";
  (b) **the person asks** -- show everything that fits (all matching versions, with their times), explain
  what each is and why it is there, and ask which one to bring back.
  Never restore on your own initiative without the person's word.
- **`NOT SEARCHED: <path> -- <why>` in the output of `find` or `restore` (exit code 2)** means some place that
  may hold this person's copies was not looked at (their folder under another name, a folder whose owner is not
  them, a SID that could not be read). "Nothing found" is then NOT an answer about those places: tell the person
  which places and why; never say the file is gone.
- **Never touch `_shedkeeper`** and never try to empty it: only the human cleans the store.
- **To remove ShedKeeper** (the person says: remove it, uninstall it): if it came as a PLUGIN (the start message
  says `ShedKeeper (plugin)`), the person (or you, if they ask) runs `/plugin uninstall shedkeeper@shedkeeper`
  -- from a terminal `claude plugin uninstall shedkeeper@shedkeeper`; there is no settings file to edit. If it was
  put in by `install.py`, run
  `python "<kit folder>/install.py" uninstall --project <project folder>` (or `--user` if it was installed
  for the user), then move the kit folder to the trash (`python "<kit folder>/install.py" check` says which door
  is open). The store `_shedkeeper` is NOT removed: it holds the
  person's copies, and only the person cleans it -- tell them so. `uninstall` also takes out the dead lines
  of an earlier installation of another folder that this installer recorded (and names, leaving them, the
  lines it did not write).
- **`ShedKeeper plugin files were not found: its protection is OFF`** is the same case for the plugin: tell the
  person; they reinstall with `/plugin install shedkeeper@shedkeeper` or remove with `/plugin uninstall
  shedkeeper@shedkeeper`. Do not edit the plugin cache by hand.
- **`ShedKeeper was not found at <path>: its protection is OFF` in a message at a tool call or at the start**
  means the kit folder was removed or moved and the settings still name it. The protection is off: tell the
  person. You are allowed to edit that settings file: if ShedKeeper was removed, delete the entries whose
  command names that path (or run `python "<kit folder>/install.py" uninstall --project <project folder>` (or
  `--user`) from a kit that is still there); if the folder was moved, run `python "<new folder>/install.py"
  install --project <project folder>` (or `--user`) from the new place -- it takes out the old lines that it
  wrote itself; any it did not write (it names them), delete by hand.
- The exit code `3` means the item was moved but the index line failed: do not repeat the command, the
  record is in the output.

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
