# ShedKeeper

A safety net for work done by an AI coding agent (Claude Code) on your files. Three promises:

1. **A copy before every change.** Before the agent edits or overwrites an existing file, ShedKeeper saves a
   verified copy (size and sha256 compared with the original). No verified copy -- no change.
2. **Deletion becomes the trash, not a ban.** When the agent tries to delete something, ShedKeeper refuses
   and hands it the ready command that moves the item to a trash instead. Nothing is lost, and the
   agent is not stuck: it just runs the command it was given.
3. **The way back is a human's hand.** You find what was changed or removed and bring it back with one
   command. Nothing in ShedKeeper empties the store -- only you do.

Plain Python 3.9+, standard library only. Windows only in v1.

## Install

```
python install.py install --project C:\path\to\your\project     # this project only
python install.py install --user                                # every project of this user
python install.py check   --project C:\path\to\your\project     # are the hooks in? is the store there?
python install.py uninstall --project C:\path\to\your\project   # take only ShedKeeper's hooks out
```

The installer copies your settings file next to itself first (`settings.json.shedkeeper-backup-<time>`),
adds ShedKeeper's hook entries without touching anyone else's, and does nothing on a second run (on a
second run it also puts back a timeout or a tool matcher that was changed). The hooks are given a
60-second timeout: the hook spends at most 20 s on copies (`cmd_deadline_s`) and at most 10 s on git
calls, and the rest is margin -- a hook that times out lets the action through. `python` must be on
PATH. Keep this folder where it is: the hooks are registered by its absolute path (if the folder is
moved or deleted the hook cannot start -- `python` exits with code 2 -- and by the Claude Code hook
contract exit code 2 is a blocking error, so the guarded tools are refused until the old entries are
taken out of the settings file by hand: the installer of a copy in another place recognises only entries
that name ITS OWN folder, so it adds new entries but does not remove the old ones).

`python install.py check` does not only read the settings: it runs the registered pre_tool command once
on a harmless input and says FAILED when the command does not start (a missing interpreter, the
Microsoft Store `python` stub, a moved folder), when the tool matcher was edited, or when
`disableAllHooks` is set.

Settings live in `shedkeeper.ini` (every key is commented there). The name `shedkeeper` and the folder name
`_shedkeeper` are each set in one place: `shedkeeper/config.py`.

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

`find` prints an id for every hit. A restore from the trash never overwrites: if the place is taken it
refuses and suggests `--to`. Restoring a version puts the file that is there now into the trash first,
and only after the copy has been verified. Exit codes: `0` done, `1` refused (nothing changed), `3` done
but not written to the index (the record is printed; do not repeat the command), `2` wrong usage.

## Where the store is, and who cleans it

One store per volume: `<X>:\_shedkeeper\`, outside your projects (so `git clean -x` in a project cannot
reach it):

```
_shedkeeper\trash\<day>\<drive letter>\<original path>     what was "deleted"
_shedkeeper\trash\_index\<day>.jsonl                        one line per move
_shedkeeper\versions\<day>\<drive letter>\<folders>\name.v_<HHMMSS>_<n>.ext   copies made before changes
_shedkeeper\versions\_index\<day>.jsonl                     one line per copy (with sha256)
_shedkeeper\transcripts\<project>\<session>.jsonl           copies of session transcripts
_shedkeeper\log.jsonl                                       one line per hook firing
```

The trash is on the same volume as the item: a move is a rename, so it is instant and copies nothing.
Copies of versions go to the store of the project's volume. **ShedKeeper never deletes anything from the
store and has no command for it. You clean it**, for example by removing old day folders. It grows
until you do.

A `_shedkeeper` folder that ShedKeeper did not create and that holds files is left alone; ShedKeeper says so and
how to accept it (`python shedkeeper.py adopt X:`) or to rename the store folder (`store_dir_name`).

## Borders -- what ShedKeeper does NOT see

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
  gone the hook cannot start (`python` exits with code 2, which the Claude Code hook contract treats as
  a blocking error), so the guarded tools are refused until you take the old entries out of the
  settings file by hand and, if you want ShedKeeper back, run `python install.py install` from the new place
  (an installer only recognises entries that name its own folder). Not measured in a live session:
  this is the documented contract.
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
- **Who can read the store.** `<X>:\_shedkeeper\` is created at the root of the volume with the default
  permissions of that folder. On a machine shared with other local users they may be able to read the
  copies -- including copies of secret files such as `.env`. Restrict the folder's permissions if that
  matters to you.
- **A shared project settings file.** `install --project` writes the absolute path of this folder into
  `<project>\.claude\settings.json`. If that file is committed and shared with a team, the path goes
  with it and will not exist on other machines. For a personal install on a shared project use
  `--user`.

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
- **To bring something back**: `python "<kit folder>/shedkeeper.py" find <part of the path>`, then
  `python "<kit folder>/shedkeeper.py" restore <id>`. Restoring never overwrites; use `--to PATH` if the
  place is taken.
- **Never touch `_shedkeeper`** and never try to empty it: only the human cleans the store.
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
several months they lived inside one private working repository. In September 2026 they were separated
from it, rewritten without any links to that setup, and packaged as this kit.
