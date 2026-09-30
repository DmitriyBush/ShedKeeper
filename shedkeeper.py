"""shedkeeper.py -- the command line of the kit.

  python shedkeeper.py trash PATH [PATH ...] --reason "why"   move to the trash of the volume (nothing is deleted)
  python shedkeeper.py find TEXT [--days N] [--volume X:]    search the trash and the copies by a part of the path
  python shedkeeper.py restore ID [--to PATH] [--volume X:]  bring an item or an old version back
  python shedkeeper.py status                                where the store is, what is in it, are the hooks in
  python shedkeeper.py adopt X:                              accept an existing folder as the store (a person's decision)
  python shedkeeper.py selftest                              run the tests of the kit

ID is what `find` prints: t:<day>:<n> (trash), t:orphans:<n>, v:<day>:<n> (an earlier version).
There is NO command that empties the store: only a person cleans it.

Exit codes. trash / restore: 0 done and recorded; 1 refused (the item is where it was; with several
paths: at least one was refused -- the output says which, and a last `summary:` line counts them); 3 done
but NOT recorded in the index and nothing was refused (the record is printed -- do not repeat the
command); 2 wrong usage.
find: 0 all indexes were read; 2 some index or line could not be read (said on stderr).
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
        print("%-18s %s  %s  %s  ->  %s  [%s]%s" % (h["id"], h["time"], label, h["src"], h["where"], state,
                                                     ("  (%s)" % h["reason"]) if h["reason"] else ""))
    if not hits:
        print("nothing found for %r" % a.text)
    for p in problems:
        print("NOT READ: " + p, file=sys.stderr)
    return 2 if problems else 0


def cmd_restore(a, settings):
    status, msg = store.restore(a.id, settings, to=a.to, session=_session(), volume=a.volume)
    print(msg)
    return {"ok": 0, "refused": 1, "unindexed": 3}[status]


def cmd_status(a, settings):
    src = os.environ.get("SHEDKEEPER_INI") or config.DEFAULT_INI
    print("product: %s; settings: %s (%s)" % (config.PRODUCT, src, "found" if os.path.exists(src) else "not found, defaults"))
    for key in sorted(settings):
        print("  %s = %s" % (key, settings[key]))
    roots = store.all_roots(settings)
    if not roots:
        print("no store found yet (it is created on first use on a fixed volume)")
    for r in roots:
        s = store.summary(r)
        print("store %s: label %s; trash: %s; versions: %s; transcripts: %d file(s)" % (
            s["root"], s["label"], s["trash"], s["versions"], s["transcripts"]))
    try:
        import install
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
