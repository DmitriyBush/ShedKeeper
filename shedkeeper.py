"""shedkeeper.py -- the command line of the kit.

  python shedkeeper.py trash PATH [PATH ...] --reason "why"   move to the trash of the volume (nothing is deleted)
  python shedkeeper.py find TEXT [--days N] [--volume X:]    search the trash and the copies by a part of the path
  python shedkeeper.py restore ID [--to PATH] [--volume X:]  bring an item or an old version back
  python shedkeeper.py status                                where the store is, what is in it, are the hooks in (and by which door)
  python shedkeeper.py adopt X:                              accept an existing folder as the store (a person's decision)
  python shedkeeper.py selftest                              run the tests of the kit

ID is what `find` prints: t:<day>:<n> (trash), t:orphans:<n>, v:<day>:<n> (an earlier version).
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

from shedkeeper import config, store  # noqa: E402


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


def cmd_find(a, settings):
    hits, problems = store.find(a.text, settings, days=a.days, volume=a.volume)
    for h in hits:
        state = "restored" if h["restored"] else ("in place" if h["present"] else "GONE from the store")
        label = "trash  " if h["kind"] == "trash" else "version"
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


def cmd_restore(a, settings):
    status, msg = store.restore(a.id, settings, to=a.to, session=_session(), volume=a.volume)
    print(msg)
    return {"ok": 0, "refused": 1, "unindexed": 3, "blind": 2}[status]


def cmd_status(a, settings):
    src = os.environ.get("SHEDKEEPER_INI") or config.DEFAULT_INI
    print("product: %s; settings: %s (%s)" % (config.PRODUCT, src, "found" if os.path.exists(src) else "not found, defaults"))
    for key in sorted(settings):
        print("  %s = %s" % (key, settings[key]))
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
            print("hooks in settings for %s: pre_tool %s, session %s%s" % (
                os.getcwd(), "yes" if f["pre_tool"] else "NO", "yes" if f["session"] else "NO",
                ("; found in " + ", ".join(f["where"])) if f["where"] else ""))
    except Exception as e:
        print("could not read the settings: %s" % e)
    print("python: %s" % sys.version.split()[0])
    return 0


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
    p = sub.add_parser("find", help="search trash and copies")
    p.add_argument("text")
    p.add_argument("--days", type=int)
    p.add_argument("--volume")
    p = sub.add_parser("restore", help="bring back")
    p.add_argument("id")
    p.add_argument("--to")
    p.add_argument("--volume")
    sub.add_parser("status", help="show the state")
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
    return {"trash": cmd_trash, "find": cmd_find, "restore": cmd_restore, "status": cmd_status,
            "adopt": cmd_adopt, "selftest": cmd_selftest}[a.cmd](a, settings)


if __name__ == "__main__":
    sys.exit(main())
