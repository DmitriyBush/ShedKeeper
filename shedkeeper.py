"""shedkeeper.py -- the command line of the kit.

  python shedkeeper.py trash PATH [PATH ...] --reason "why"   move to the trash of the volume (nothing is deleted)
  python shedkeeper.py find TEXT [--days N] [--volume X:]    search the trash and the copies by a part of the path
  python shedkeeper.py restore ID [--to PATH] [--volume X:]  bring an item or an old version back
  python shedkeeper.py protect show|add|remove|exclude|unexclude|decline|confirm   the LIST of what is protected (checked before every step of the agent)
  python shedkeeper.py snapshot                              copy every entry of the list NOW, outside Claude Code (no time limit of the hook; prints progress)
  python shedkeeper.py find --steps                          the steps with copies; `restore --step N --to NEWPLACE` brings the list back as it was at step N
  python shedkeeper.py status                                where the store is, what is in it, are the hooks in (and by which door), the databases
  python shedkeeper.py dbcopy ADDRESS|FILE [--mcp-server NAME]   a verified copy of a database NOW (PostgreSQL address or SQLite file)
  python shedkeeper.py settings [--json]                     every setting: now, default, allowed, weight, what it does in plain words
  python shedkeeper.py set KEY VALUE                         change one setting (checked, logged, shown to the person); ONLY after the person agrees
  python shedkeeper.py survey [--json] [--project PATH] [--done]   getting to know the machine and the project: READ ONLY, passwords never shown
  python shedkeeper.py prove ENGINE [--target PROBE]         the proof of an engine adapter: fill, copy, destroy, restore, compare -- on a PROBE only
  python shedkeeper.py activate ENGINE                       the PERSON makes the proof of an adapter count (a code typed in their own message)
  python shedkeeper.py quarantine                            move the DAMAGED lines of the journals (confirm.jsonl, proofs.jsonl) to .bad files; closes every code issued so far
  python shedkeeper.py adopt X:                              accept an existing folder as the store (a person's decision)
  python shedkeeper.py selftest                              run the tests of the kit

`activate`: an adapter of the user protects only after the PERSON confirmed its proof: `prove` prints a code, the person types `CONFIRM SK-nnnnnn`
in their own message (the hook of the user's prompt receives it), `activate ENGINE` applies it (0 activated; 4 not applied, a code was issued; 1 refused). The code is for THESE bytes of the file.
`prove`: 0 proved (a line `proved` for the sha256 of the adapter file is in the store); 1 refused (a probe that is not empty or not named
shedkeeper_probe..., an adapter that does not load) or a step failed (nothing is deleted; the output says what was left behind).
ID is what `find` prints: t:<day>:<n> (trash), t:orphans:<n>, v:<day>:<n> (an earlier version), d:<day>:<n> (a copy of a database).
`restore d:...`: a PostgreSQL copy comes back into a NEW database `<name>_restored_<YYYYMMDD_HHMMSS>` (the old one is never touched;
`--to` is not used); a SQLite copy comes back as a file (the file there goes to the trash first; `--to PATH` names another place).
`dbcopy` has no time and size limit of the hook; the address may carry `***` for the password (pgpass / PGPASSWORD of your shell is used).
Exit codes of dbcopy: 0 copy made and listed; 1 refused or nothing to copy; 3 copy made but NOT listed.
`set`: 0 changed and in effect; 1 refused (a key that does not exist, a value that is not understood, a weakening that cannot be confirmed:
nothing is written); 3 written but NOT in effect (another file read later overrides it); 4 NOT applied: the value would WEAKEN protection, and
a weakening is applied only when the person types the code this command printed (`CONFIRM SK-nnnnnn`, in their own message) and the same
command is run again (shedkeeper/confirm.py). Your own settings go to ~/.shedkeeper/shedkeeper.ini (survives an update of the kit); when
SHEDKEEPER_INI names a file, that one file is read and written.
There is NO command that empties the store: only a person cleans it.

Exit codes. trash / restore: 0 done and recorded; 1 refused (the item is where it was; with several
paths: at least one was refused -- the output says which, and a last `summary:` line counts them); 3 done
but NOT recorded in the index and nothing was refused (the record is printed -- do not repeat the
command); 2 wrong usage.
find: 0 all indexes were read and every place that may hold copies of this user was searched; 2 some index or
line could not be read, or some place was NOT searched (the folder is named on stderr as `NOT SEARCHED: path -- why`:
"nothing found" is then not an answer about that place).
restore: 2 the id was not found AND some place was not searched (named in the output); a restore that was done
says in the same line what was not searched, and exits 0.
"""
import argparse
import io
import os
import subprocess
import sys

KIT = os.path.dirname(os.path.abspath(__file__))
if KIT not in sys.path:
    sys.path.insert(0, KIT)

from shedkeeper import config, confirm, db, dbcli, guard, protect, prove, settings_cli, store, survey  # noqa: E402


def _utf8():
    for stream in ("stdout", "stderr"):
        try:
            getattr(sys, stream).reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            try:
                setattr(sys, stream, io.TextIOWrapper(getattr(sys, stream).buffer, encoding="utf-8",
                                                      errors="replace"))
            except Exception:
                pass


def _session():
    return os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CLAUDE_SESSION_ID") or ""


def cmd_trash(a, settings):
    counts = {"ok": 0, "refused": 0, "unindexed": 0}
    for p in a.paths:
        status, msg, _info = store.trash(p, a.reason, settings, _session())
        print(msg)
        counts[status] += 1
    if len(a.paths) > 1 or counts["refused"] or counts["unindexed"]:
        print("summary: moved %d, refused %d, moved but not recorded %d"
              % (counts["ok"] + counts["unindexed"], counts["refused"], counts["unindexed"]))
    if counts["refused"]:
        return 1            # something stayed where it was: the caller must look at it
    return 3 if counts["unindexed"] else 0


def cmd_protect(a, settings):
    code, text = protect.cli(list(a.args), settings, os.getcwd(), _session())
    print(text)
    return code


def cmd_snapshot(a, settings):
    code, text = guard.snapshot(os.getcwd(), settings, _session(), out=lambda t: print(t, flush=True))
    print(text)
    return code


def cmd_find(a, settings):
    if a.steps or not a.text:
        code, text = guard.find_text(os.getcwd(), settings)
        print(text)
        return code
    hits, problems = store.find(a.text, settings, days=a.days, volume=a.volume)
    try:
        dhits, dproblems = dbcli.find_copies(a.text, settings, days=a.days, volume=a.volume)
    except Exception as e:      # a fault in the databases part must not hide what the files part found
        dhits, dproblems = [], ["the copies of databases could not be searched (%s: %s)" % (e.__class__.__name__, db.mask_secrets(str(e)))]
    hits = sorted(hits + dhits, key=lambda h: h["time"])
    problems = problems + dproblems
    for h in hits:
        state = "restored" if h["restored"] else ("in place" if h["present"] else "GONE from the store")
        label = {"trash": "trash  ", "dbcopy": "db copy"}.get(h["kind"], "version")
        print("%-18s %s  %s  %s  ->  %s  [%s]%s%s%s" % (
            h["id"], h["time"], label, h["src"], h["where"], state,
            ("  (%s)" % h["reason"]) if h["reason"] else "",
            ("  via %s" % h["via"]) if h["via"] else "",
            ("  session %s" % h["session"]) if h["session"] else ""))
    blind = [p for p in problems if p.startswith("NOT SEARCHED")]
    if not hits:
        print("nothing found for %r%s" % (a.text, " -- in what was searched; NOT everything was (see NOT SEARCHED "
                                                  "below)" if blind else ""))
    for p in problems:
        print(p if p.startswith("NOT SEARCHED") else "NOT READ: " + p, file=sys.stderr)
    return 2 if problems else 0


def cmd_dbcopy(a, settings):
    code, text = dbcli.dbcopy(a.target, a.mcp_server, os.getcwd(), settings, _session())
    print(text)
    return code


def cmd_restore(a, settings):
    if a.step is not None:
        if not a.to:
            print("REFUSED: `restore --step N` needs --to NEWPLACE (a place that does not exist yet: a restore overwrites nothing)")
            return 1
        code, text = guard.restore_step(os.getcwd(), settings, a.step, a.to, _session())
        print(text)
        return code
    if not (a.id or "").strip():
        print("usage: restore ID [--to PATH]   or   restore --step N --to NEWPLACE")
        return 2
    if (a.id or "").strip().startswith("d:"):
        status, msg = dbcli.restore_copy(a.id.strip(), settings, to=a.to, session=_session(), volume=a.volume)
    else:
        status, msg = store.restore(a.id, settings, to=a.to, session=_session(), volume=a.volume)
    print(msg)
    return {"ok": 0, "refused": 1, "unindexed": 3, "blind": 2}[status]


def cmd_settings(a, settings):
    print(settings_cli.settings_json(settings) if a.json else settings_cli.render_menu(settings))
    return 0


def cmd_set(a, settings):
    code, text = settings_cli.set_setting(a.key, " ".join(a.value), settings, os.getcwd(), _session())
    print(text)
    return code


def cmd_survey(a, settings):
    code, text = survey.run(a.project or os.getcwd(), settings, as_json=a.json, done_only=a.done)
    print(text)
    return code


def cmd_prove(a, settings):
    code, text = prove.run(a.engine, a.target, os.getcwd(), settings, _session())
    print(text)
    return code


def cmd_activate(a, settings):
    code, text = prove.activate(a.engine, os.getcwd(), settings, _session())
    print(text)
    return code


def cmd_status(a, settings):
    files = config.ini_files()
    print("product: %s; settings read from: %s; `set` writes to: %s (`python shedkeeper.py settings` lists them)" % (
        config.PRODUCT, ", ".join(files) if files else "no file found, defaults", config.target_ini()))
    for key in sorted(settings):
        print("  %s = %s" % (key, settings[key]))
    for key, val, eff in getattr(settings, "unconfirmed", ()) or ():
        print("NOT THROUGH `set`: %s = %s stands in a settings file and is IN EFFECT, but no record shows that `set` wrote it (the file was edited by hand). %s" % (key, val or "(empty)", eff))
    for key, val, default, eff in settings_cli.weakened(settings):
        print("PROTECTION WEAKENED: %s = %s (default %s) -- %s" % (key, val or "(empty)", default or "(empty)", eff))
    ident = store.identity()
    if not ident["sid"]:
        print("user: %s -- the SID cannot be found (%s): no personal folder can be made or recognised" % (
            ident["login"] or "?", ident["why"]))
    roots = store.all_roots(settings)
    if not roots:
        print("no store of %s found yet (it is created on first use on a fixed volume)" % (ident["login"] or "this user"))
    for r in roots:
        s = store.summary(r)
        print(store.describe(r, settings))
        print("  trash: %s; versions: %s; transcripts: %d file(s)" % (s["trash"], s["versions"], s["transcripts"]))
    if ident["login"] and ident["sid"]:
        for v in store._volume_dirs(settings):
            try:
                print("shared folder %s: %s" % (v, store.volume_state(v)[1]))
                for path, why in store.resolve_personal(v, ident["login"], settings)[2]:
                    print("skipped (not this user's store): %s -- %s" % (path, why))
            except OSError as e:
                print("could not read %s: %s" % (v, e))
    for line in store.unsearched_lines(settings):
        print(line + "  (find and restore do not look there)")
    for path in store.leftovers(settings):
        print("leftover of a creation (a lost race or a rejected folder; the tool never deletes it, it holds no copies -- "
              "yours to remove after a look): %s" % path)
    try:
        import install
        project = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
        state = install.door_state(project)
        print("\n".join(install.door_lines(state)))      # which door the hooks came by: the plugin, install.py, both, none
        if state["mode"] != "plugin":
            f = install.installed_hooks(os.getcwd())
            print("hooks in settings for %s: pre_tool %s, session %s, prompt %s%s" % (
                os.getcwd(), "yes" if f["pre_tool"] else "NO", "yes" if f["session"] else "NO", "yes" if f["prompt"] else "NO",
                ("; found in " + ", ".join(f["where"])) if f["where"] else ""))
    except Exception as e:
        print("could not read the settings: %s" % e)
    try:
        for line in guard.status_lines(os.getcwd(), settings):
            print(line)
        print("limits of the protected list: " + " ".join(x.split(" (limit")[0] for x in protect.LIMITS))
    except Exception as e:
        print("could not read the state of the protected list: %s: %s" % (e.__class__.__name__, db.mask_secrets(str(e))))
    try:
        for line in dbcli.status_lines(settings, os.getcwd()):
            print(line)
    except Exception as e:
        print("could not read the state of the databases part: %s: %s" % (e.__class__.__name__, db.mask_secrets(str(e))))
    print("python: %s" % sys.version.split()[0])
    return 0


def cmd_quarantine(a, settings):
    code, text = confirm.quarantine(settings, os.getcwd())
    print(text)
    return code


def cmd_adopt(a, settings):
    ok, msg = store.adopt(a.target, settings)
    print(("OK " if ok else "REFUSED ") + msg)
    return 0 if ok else 1


def cmd_selftest(a, settings):
    return subprocess.call([sys.executable, os.path.join(KIT, "tests", "run_tests.py")])


def main(argv=None):
    _utf8()
    ap = argparse.ArgumentParser(prog="shedkeeper.py", description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("trash", help="move to the trash")
    p.add_argument("paths", nargs="+")
    p.add_argument("--reason", default="")
    p = sub.add_parser("find", help="search trash and copies; --steps lists the steps with copies of the protected list")
    p.add_argument("text", nargs="?", default="")
    p.add_argument("--days", type=int)
    p.add_argument("--volume")
    p.add_argument("--steps", action="store_true", help="the steps with copies of the protected list")
    p = sub.add_parser("restore", help="bring back")
    p.add_argument("id", nargs="?", default="")
    p.add_argument("--to")
    p.add_argument("--volume")
    p.add_argument("--step", type=int, help="the state of the protected list as it was copied up to this step, into --to (a NEW place)")
    p = sub.add_parser("protect", help="the list of what is protected")
    p.add_argument("args", nargs=argparse.REMAINDER)
    sub.add_parser("snapshot", help="copy every entry of the protected list now (outside Claude Code)")
    sub.add_parser("status", help="show the state")
    p = sub.add_parser("settings", help="every setting and what it does")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("set", help="change one setting")
    p.add_argument("key")
    p.add_argument("value", nargs="*", help="the new value (several words for a list: mcp_no_db slack linear)")
    p = sub.add_parser("survey", help="a read-only look at the machine and the project")
    p.add_argument("--json", action="store_true")
    p.add_argument("--project")
    p.add_argument("--done", action="store_true", help="only record that the survey was done for the project")
    p = sub.add_parser("dbcopy", help="a verified copy of a database now")
    p.add_argument("target", help="a PostgreSQL address (postgresql://user:***@host:port/db) or a SQLite file")
    p.add_argument("--mcp-server", dest="mcp_server", default="", help="the name of the MCP server the database is behind")
    p = sub.add_parser("prove", help="the proof of an engine adapter, on a probe database")
    p.add_argument("engine", help="postgres, sqlite, or the id of an adapter in the adapters folder")
    p.add_argument("--target", help="the probe: an EMPTY database (or file) whose name begins with shedkeeper_probe; a file engine may leave it out")
    p = sub.add_parser("activate", help="the person makes the proof of an adapter count")
    p.add_argument("engine", help="the id of an adapter in the adapters folder")
    sub.add_parser("quarantine", help="move the damaged lines of the journals of the store to .bad files (nothing is deleted)")
    p = sub.add_parser("adopt", help="accept an existing folder as the store")
    p.add_argument("target")
    sub.add_parser("selftest", help="run the tests")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 2
    settings, problems = config.load()
    for pr in problems:
        print("note: " + pr)
    return {"trash": cmd_trash, "find": cmd_find, "restore": cmd_restore, "status": cmd_status, "dbcopy": cmd_dbcopy,
            "adopt": cmd_adopt, "selftest": cmd_selftest, "settings": cmd_settings, "set": cmd_set,
            "survey": cmd_survey, "protect": cmd_protect, "snapshot": cmd_snapshot, "prove": cmd_prove, "activate": cmd_activate, "quarantine": cmd_quarantine}[a.cmd](a, settings)


if __name__ == "__main__":
    sys.exit(main())
