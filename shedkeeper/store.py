"""store.py -- the store on a volume: label, copies of versions, trash, index, search, restore.

Layout (one store per VOLUME, outside any project, so `git clean -x` of a project cannot reach it):

  <X>:\\<store_dir_name>\\
      .shedkeeper-store                       label (JSON: product, created)
      trash\\<YYYY-MM-DD>\\<DRIVE>\\<original path without the letter>
      trash\\_index\\<YYYY-MM-DD>.jsonl    one line per move (id t:<day>:<line>)
      trash\\_index\\_orphans.jsonl        records that could not go to the day index (id t:orphans:<line>)
      versions\\<YYYY-MM-DD>\\<DRIVE>\\<folders>\\<name>.v_<HHMMSS>_<n><ext>
      versions\\_index\\<YYYY-MM-DD>.jsonl one line per copy (id v:<day>:<line>)
      transcripts\\<project folder>\\<session>.jsonl
      log.jsonl                           one line per hook firing

Moving into the trash is a rename: same volume, atomic, no copy. Nothing here ever deletes: there
is NO command that empties the store -- only a person cleans it (for example by removing old day
folders).

Environment: SHEDKEEPER_STORE_ROOT names ONE store root for every volume (used by the tests; the volume
type check is skipped for it).
"""
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import time
from datetime import datetime

from . import config

LABEL = ".shedkeeper-store"
DRIVE_RE = re.compile(r"^[A-Za-z]:$")
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ID_RE = re.compile(r"^([tv]):(\d{4}-\d{2}-\d{2}|orphans):(\d+)$")
DRIVE_FIXED = 3
DRIVE_TYPE_NAMES = {0: "unknown", 1: "no root", 2: "removable", 3: "fixed", 4: "network",
                    5: "optical", 6: "RAM disk"}
MAX_SUFFIX = 99
DIR_COUNT_CAP = 200000
LOCK_WAIT_S = 3.0               # how long an index write waits for another writer
LOCK_POS = 1 << 40              # the lock byte lies far behind the data: readers never touch it
_REPARSE_TAGS = (0xA0000003, 0xA000000C)     # junction, symbolic link


# ---------------------------------------------------------------- small helpers

def _lp(p):
    """Long-path form of an absolute Windows path (\\\\?\\ prefix) when it is long; else unchanged."""
    if os.name != "nt":
        return p
    p = os.path.abspath(p)
    if len(p) >= 240 and not p.startswith("\\\\?\\"):
        return "\\\\?\\UNC\\" + p[2:] if p.startswith("\\\\") else "\\\\?\\" + p
    return p


def _norm(p):
    return os.path.normcase(os.path.abspath(p))


def _within(child, parent):
    c, p = _norm(child), _norm(parent)
    return c == p or c.startswith(p.rstrip("\\/") + os.sep)


def _is_link(p):
    """A symbolic link or a junction (not every reparse point)."""
    try:
        st = os.lstat(_lp(p))
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    return getattr(st, "st_reparse_tag", 0) in _REPARSE_TAGS


def _lexists(p):
    return os.path.lexists(_lp(p))


def _absent(e):
    """Does this OSError of a stat mean 'there is nothing at this path' (and not 'cannot tell')?"""
    return (isinstance(e, (FileNotFoundError, NotADirectoryError))
            or getattr(e, "winerror", None) in (2, 3, 123, 161)
            or e.errno in (errno.ENOENT, errno.ENOTDIR, errno.EINVAL))


def lexists(p):
    """Like os.path.lexists, with the long-path form; an error that is not 'absent' (access denied,
    a sharing violation ...) is RAISED, not read as 'absent': the hook then refuses instead of letting
    a change through on a path it could not look at."""
    try:
        os.lstat(_lp(p))
        return True
    except OSError as e:
        if _absent(e):
            return False
        raise


def isfile(p):
    """Like os.path.isfile, with the long-path form; same rule for errors as lexists()."""
    try:
        return stat.S_ISREG(os.stat(_lp(p)).st_mode)
    except OSError as e:
        if _absent(e):
            return False
        raise


def _occupied(p):
    """Is the place taken? The guard that keeps a restore from overwriting anything."""
    return _lexists(p)


def _sha256(p):
    h = hashlib.sha256()
    with open(_lp(p), "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _verify_copy(src, copy):
    """(ok, why): the copy has the size and the sha256 of the source. The guard of invariant I5."""
    try:
        s1, s2 = os.path.getsize(_lp(src)), os.path.getsize(_lp(copy))
        if s1 != s2:
            return False, "size differs (source %d, copy %d)" % (s1, s2)
        if _sha256(src) != _sha256(copy):
            return False, "sha256 differs"
    except OSError as e:
        return False, "%s: %s" % (e.__class__.__name__, e)
    return True, ""


def _now_day(now):
    return now.strftime("%Y-%m-%d")


def _measure(p):
    """(kind, size, files, partial)."""
    if _is_link(p):
        return "link", None, None, False
    if os.path.isfile(_lp(p)):
        try:
            return "file", os.lstat(_lp(p)).st_size, 1, False
        except OSError:
            return "file", None, 1, True
    errs, size, files = [], 0, 0
    for dp, _dns, fns in os.walk(_lp(p), followlinks=False, onerror=errs.append):
        for fn in fns:
            files += 1
            if files > DIR_COUNT_CAP:
                return "dir", None, None, True
            try:
                size += os.lstat(os.path.join(dp, fn)).st_size
            except OSError as e:
                errs.append(e)
    return "dir", size, files, bool(errs)


class IndexBusy(OSError):
    """Another process held the index for longer than LOCK_WAIT_S."""


def _lock(fd, path):
    """Exclusive lock of ONE byte at LOCK_POS of the open index file. The operating system releases it
    when the process dies, so a crash never leaves a stale lock (a lock FILE would). Waits up to
    LOCK_WAIT_S in steps of 50 ms, then raises IndexBusy."""
    deadline = time.monotonic() + LOCK_WAIT_S
    while True:
        try:
            os.lseek(fd, LOCK_POS, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 1, LOCK_POS)
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise IndexBusy("the index %s is held by another process for more than %.0f s"
                                % (path, LOCK_WAIT_S))
            time.sleep(0.05)


def _unlock(fd):
    try:
        os.lseek(fd, LOCK_POS, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.lockf(fd, fcntl.LOCK_UN, 1, LOCK_POS)
    except OSError:
        pass


def _append_jsonl(path, make, lock=True):
    """Append one JSON line built by make(line_number); return the line number. The count and the
    append happen under an exclusive lock (two hooks at the same moment must not get the same number);
    the lock not taken in LOCK_WAIT_S -> IndexBusy. A file that does not end in a newline (torn write)
    gets one first so records never glue together."""
    os.makedirs(_lp(os.path.dirname(path)), exist_ok=True)
    with open(_lp(path), "a+b") as fh:
        if lock:
            _lock(fh.fileno(), path)
        try:
            fh.seek(0, os.SEEK_END)
            if fh.tell():
                fh.seek(-1, os.SEEK_END)
                if fh.read(1) != b"\n":
                    fh.write(b"\n")
            fh.seek(0)
            n = fh.read().count(b"\n") + 1
            fh.seek(0, os.SEEK_END)
            fh.write(json.dumps(make(n)).encode("utf-8") + b"\n")
            fh.flush()
            os.fsync(fh.fileno())
        finally:
            if lock:
                _unlock(fh.fileno())
    return n


def _append_orphan(path, make):
    """The record that could not go to its day index: to the orphans file. Locked when the lock can be
    had; when even that is busy, unlocked -- a possibly duplicated number is better than a lost record."""
    try:
        return _append_jsonl(path, make)
    except IndexBusy:
        return _append_jsonl(path, make, lock=False)


def _read_jsonl(path):
    """([(line_no, record)], number of unreadable lines). Raises OSError."""
    out, bad = [], 0
    with open(_lp(path), encoding="utf-8", errors="replace") as fh:
        for n, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                bad += 1
                continue
            if isinstance(rec, dict):
                out.append((n, rec))
            else:
                bad += 1
    return out, bad


def log(root, rec):
    """One line in <root>/log.jsonl. A failure of the log never changes anything else."""
    try:
        if root and os.path.isdir(root):
            with open(os.path.join(root, "log.jsonl"), "ab") as fh:
                fh.write(json.dumps(rec).encode("utf-8") + b"\n")
    except Exception:
        pass


# ---------------------------------------------------------------- the store itself

def env_root():
    r = os.environ.get("SHEDKEEPER_STORE_ROOT")
    return os.path.abspath(r) if r else None


def store_root(path, settings):
    """The store root for the volume of `path`; None when the path has no drive letter."""
    er = env_root()
    if er:
        return er
    drive = os.path.splitdrive(os.path.abspath(path or ""))[0]
    if not DRIVE_RE.match(drive):
        return None
    return drive.upper() + os.sep + settings["store_dir_name"]


def all_roots(settings):
    """Existing stores of ours: the environment root, or <X>:\\<name> for every letter."""
    er = env_root()
    if er:
        return [er] if os.path.isdir(er) else []
    out = []
    for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        r = "%s:%s%s" % (c, os.sep, settings["store_dir_name"])
        try:
            if os.path.isdir(r) and label_state(r)[0] == "ours":
                out.append(r)
        except OSError:
            pass
    return out


def is_store_path(path, settings):
    """True when `path` lies inside a store: a DIRECTORY component equal to the store folder name
    (whole name, case-insensitive on Windows, trailing dots/spaces dropped as Windows does), or under the
    environment root. Never a substring test: a file `x_shedkeeper.txt` or a folder `my_shedkeeper_notes`
    is an ordinary path."""
    # Checked on the path as written AND on its resolved form: a junction or symlink elsewhere that
    # points into a store is inside the store. Names compare case-insensitively on Windows (`_ShedKeeper`
    # IS `_shedkeeper` there) -- both found by an independent review.
    cands = [os.path.abspath(path)]
    try:
        real = os.path.realpath(path)
        if real not in cands:
            cands.append(real)
    except (OSError, ValueError):
        pass
    er = env_root()
    name = os.path.normcase(settings["store_dir_name"])
    for cand in cands:
        if er and _within(cand, er):
            return True
        parts = [p for p in cand.replace("\\", "/").split("/") if p]
        if any(os.path.normcase(p.rstrip(". ")) == name for p in parts[:-1]):
            return True
    return False


def label_state(root):
    """('ours', None) | ('none', None) | ('bad', why)."""
    m = os.path.join(root, LABEL)
    if not os.path.lexists(m):
        return "none", None
    try:
        with open(m, encoding="utf-8") as fh:
            rec = json.load(fh)
    except (OSError, ValueError) as e:
        return "bad", "%s: %s" % (e.__class__.__name__, e)
    if not isinstance(rec, dict) or rec.get("product") != config.PRODUCT:
        return "bad", "not a label of this tool"
    return "ours", None


def _write_label(root):
    rec = {"product": config.PRODUCT, "created": datetime.now().isoformat(timespec="seconds")}
    data = json.dumps(rec, indent=1).encode("utf-8")
    tmp = os.path.join(root, "%s.part-%d-%d" % (LABEL, os.getpid(), time.time_ns()))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0))
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.rename(tmp, os.path.join(root, LABEL))


def _drive_type(root):
    drive = os.path.splitdrive(os.path.abspath(root))[0]
    if not DRIVE_RE.match(drive):
        return None
    try:
        import ctypes
        return ctypes.windll.kernel32.GetDriveTypeW(drive + os.sep)
    except Exception:
        return None


def _foreign_text(root, settings):
    return ("%s exists, has content and no %s label: it is someone else's folder; nothing was "
            "touched. If it is yours, accept it: python shedkeeper.py adopt %s -- or choose another "
            "folder name: store_dir_name in shedkeeper.ini (now %r)."
            % (root, LABEL, os.path.splitdrive(root)[0] or root, settings["store_dir_name"]))


def check_available(root, settings):
    """(ok, why) without creating anything: can this store be used (now or after auto-creation)?"""
    if not root:
        return False, "the path has no drive letter"
    if os.path.lexists(root) and not os.path.isdir(root):
        return False, "%s exists and is not a folder" % root
    if os.path.isdir(root):
        state, why = label_state(root)
        if state == "ours":
            return True, ""
        if state == "bad":
            return False, "the label in %s is unreadable (%s)" % (root, why)
        try:
            empty = all(n.startswith(LABEL + ".part-") for n in os.listdir(root))
        except OSError as e:
            return False, "cannot read %s (%s)" % (root, e)
        return (True, "") if empty else (False, _foreign_text(root, settings))
    if not settings["auto_create_store"]:
        return False, "no store at %s and auto_create_store = false" % root
    if not env_root():
        dt = _drive_type(root)
        if dt != DRIVE_FIXED:
            return False, "no store at %s and it is not created on a %s volume" % (
                root, DRIVE_TYPE_NAMES.get(dt, "unknown") if dt is not None else "unknown")
    return True, ""


def ensure_store(root, settings):
    """(ok, note). Make sure `root` is a usable store of ours, creating it when allowed. On success
    the note is a line to print when the store was created or taken just now (else ''); on failure
    it is the refusal text. Nothing is created on a removable or network volume."""
    ok, why = check_available(root, settings)
    if not ok:
        return False, why
    if os.path.isdir(root):
        if label_state(root)[0] == "ours":
            return True, ""
        try:
            _write_label(root)
        except OSError as e:
            return False, "could not mark %s (%s: %s)" % (root, e.__class__.__name__, e)
        return True, "the empty unmarked folder %s was taken as the store (label written)" % root
    try:
        os.makedirs(root)
        _write_label(root)
    except FileExistsError:
        return ensure_store(root, settings)
    except OSError as e:
        return False, "could not create the store %s (%s: %s)" % (root, e.__class__.__name__, e)
    return True, "created the store %s (automatic; to turn off: auto_create_store = false in shedkeeper.ini)" % root


def adopt(target, settings):
    """A person's decision: mark an existing folder as the store (any volume type). (ok, message)."""
    t = (target or "").strip().rstrip("\\/")
    root = env_root()
    if not root:
        if DRIVE_RE.match(t):
            root = t[0].upper() + ":" + os.sep + settings["store_dir_name"]
        else:
            root = os.path.abspath(t)
            if os.path.basename(root) != settings["store_dir_name"]:
                return False, "adopt takes a drive (X:) or the store folder %r: %s" % (
                    settings["store_dir_name"], target)
    if not os.path.isdir(root):
        return False, "no folder %s -- nothing to adopt" % root
    state, why = label_state(root)
    if state == "ours":
        return True, "already the store of this tool: %s" % root
    if state == "bad":
        return False, "a label is there but unreadable (%s): look at it by hand" % why
    try:
        _write_label(root)
    except OSError as e:
        return False, "could not write the label (%s: %s)" % (e.__class__.__name__, e)
    return True, "adopted %s as the store of this tool" % root


# ---------------------------------------------------------------- trash

def _free_dest(base, parts):
    """A free place under base for the path parts; a taken name (or an ancestor that is a file or a
    link) gets a `.N` suffix, N = 1..MAX_SUFFIX. None -- no free name."""
    cur = base
    for idx, part in enumerate(parts):
        cand = os.path.join(cur, part)
        last = idx == len(parts) - 1
        taken = _lexists(cand) if last else (_lexists(cand) and (_is_link(cand) or not os.path.isdir(_lp(cand))))
        if taken:
            for n in range(1, MAX_SUFFIX + 1):
                c2 = "%s.%d" % (cand, n)
                if not _lexists(c2):
                    cand = c2
                    break
            else:
                return None
        cur = cand
    return cur


def trash(path, reason, settings, session="", now=None):
    """Move one item into the trash of its volume. Returns (status, message, info); status is
    'ok' | 'refused' | 'unindexed' (moved, but the index line could not be written -- the record is
    in the message; DO NOT repeat). info: id, dst."""
    now = now or datetime.now()
    day = _now_day(now)
    src = os.path.abspath(path)
    if not _lexists(src):
        return "refused", "REFUSED no such path: %s" % src, {}
    drive = os.path.splitdrive(src)[0]
    if not DRIVE_RE.match(drive):
        return "refused", "REFUSED the path is not on a drive letter (UNC / \\\\?\\ are not taken): %s" % src, {}
    if os.path.splitdrive(src)[1].strip("\\/") == "":
        return "refused", "REFUSED a drive root: %s" % src, {}
    root = store_root(src, settings)
    if is_store_path(src, settings) or _within(src, root):
        return "refused", "REFUSED already inside the store: %s" % src, {}
    if _within(root, src):
        return "refused", "REFUSED the path contains the store itself: %s" % src, {}
    ok, note = ensure_store(root, settings)
    if not ok:
        return "refused", "REFUSED " + note, {}
    kind, size, files, partial = _measure(src)
    try:
        mtime = datetime.fromtimestamp(os.lstat(_lp(src)).st_mtime).isoformat(timespec="seconds")
    except OSError:
        mtime = None
    base = os.path.join(root, "trash", day, drive[0].upper())
    parts = [x for x in re.split(r"[\\/]+", os.path.splitdrive(src)[1]) if x]
    dst = _free_dest(base, parts)
    if dst is None:
        return "refused", ("REFUSED no free name in the trash for this item (%d names taken); this is "
                           "not a lack of disk space -- the move is a rename: %s" % (MAX_SUFFIX, src)), {}
    try:
        os.makedirs(_lp(os.path.dirname(dst)), exist_ok=True)
        os.rename(_lp(src), _lp(dst))
    except OSError as e:
        return "refused", "REFUSED the move failed (%s: %s): %s" % (e.__class__.__name__, e, src), {}
    try:
        cwd = os.getcwd()
    except OSError:
        cwd = ""

    def make(label):
        return lambda n: {"id": "t:%s:%d" % (label, n), "src": src, "dst": dst,
                          "time": now.isoformat(timespec="seconds"), "size": size, "kind": kind,
                          "files": files, "partial": partial, "mtime": mtime, "reason": reason or "",
                          "session": session or "", "cwd": cwd}

    prefix = (note + "\n") if note else ""
    try:
        n = _append_jsonl(os.path.join(root, "trash", "_index", day + ".jsonl"), make(day))
        return "ok", prefix + "OK %s -> %s" % (src, dst), {"id": "t:%s:%d" % (day, n), "dst": dst}
    except Exception as e:
        rec, where = None, "nowhere"
        try:
            holder = {}

            def make_orphan(n):
                holder["r"] = make("orphans")(n)
                return holder["r"]

            _append_orphan(os.path.join(root, "trash", "_index", "_orphans.jsonl"), make_orphan)
            rec = holder.get("r")
            where = os.path.join(root, "trash", "_index", "_orphans.jsonl")
        except Exception:
            rec = make(day)(0)
        return "unindexed", (prefix + "MOVED BUT NOT RECORDED (%s: %s); DO NOT REPEAT -- the item is "
                             "safe: %s; record -> %s; RECORD: %s"
                             % (e.__class__.__name__, e, dst, where, json.dumps(rec))), {"dst": dst}


# ---------------------------------------------------------------- versions

def _version_dest(root, src, now, n):
    drive, rest = os.path.splitdrive(os.path.abspath(src))
    if len(drive) == 2 and drive[1] == ":":
        lead = drive[0].upper()
    else:
        lead = "UNC"
    parts = [re.sub(r'[<>:"|?*]', "_", p) for p in re.split(r"[\\/]+", rest) if p]
    name, ext = os.path.splitext(parts[-1] if parts else "file")
    fname = "%s.v_%s_%d%s" % (name, now.strftime("%H%M%S"), n, ext)
    return os.path.join(root, "versions", _now_day(now), lead, *(parts[:-1] + [fname]))


def copy_version(src, root, settings, session="", via="", now=None):
    """A copy of the file BEFORE it changes. Returns a dict: status 'ok' | 'same' (the last copy of
    this file today already has this content) | 'toolarge' | 'failed'; msg; copy; id; indexed.
    The copy is verified (size + sha256) against the source; a copy that does not match counts as
    NOT taken. A copy never replaces a copy. `root` must be a usable store (ensure_store first)."""
    now = now or datetime.now()
    src = os.path.abspath(src)
    res = {"status": "failed", "msg": "", "copy": None, "id": None, "indexed": False}
    try:
        size = os.path.getsize(_lp(src))
    except OSError as e:
        res["msg"] = "cannot read %s (%s: %s)" % (src, e.__class__.__name__, e)
        return res
    limit = settings["max_copy_mb"] * 1024 * 1024
    if size > limit:
        res["status"] = "toolarge"
        res["msg"] = "%s is %.1f MB, over max_copy_mb = %d" % (src, size / 1048576.0, settings["max_copy_mb"])
        return res
    try:
        sha = _sha256(src)
    except OSError as e:
        res["msg"] = "cannot read %s (%s: %s)" % (src, e.__class__.__name__, e)
        return res
    index = os.path.join(root, "versions", "_index", _now_day(now) + ".jsonl")
    try:
        prior = [r for _n, r in _read_jsonl(index)[0] if r.get("src") and not r.get("op")
                 and _norm(r["src"]) == _norm(src)] if os.path.isfile(index) else []
    except OSError:
        prior = []
    if prior:
        last = prior[-1]
        lc = last.get("copy")
        if last.get("sha256") == sha and lc and _lexists(lc):
            res.update(status="same", copy=lc, id=last.get("id"),
                       msg="the last copy of this file already has this content")
            return res
    final = None
    for n in range(1, 1000):
        cand = _version_dest(root, src, now, n)
        try:
            os.makedirs(_lp(os.path.dirname(cand)), exist_ok=True)
            fd = os.open(_lp(cand), os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0))
        except FileExistsError:
            continue
        except OSError as e:
            res["msg"] = "cannot create %s (%s: %s)" % (cand, e.__class__.__name__, e)
            return res
        os.close(fd)
        final = cand
        break
    if final is None:
        res["msg"] = "no free copy name for %s" % src
        return res
    try:
        shutil.copy2(_lp(src), _lp(final))
    except Exception as e:
        res["msg"] = "copy failed (%s: %s); an incomplete %s stays" % (e.__class__.__name__, e, final)
        return res
    ok, why = _verify_copy(src, final)
    if not ok:
        res["msg"] = "the copy does not match the source (%s); not counted as taken; it stays at %s" % (why, final)
        return res
    res.update(status="ok", copy=final)
    holder = {}

    def make_for(label):
        def make(n):
            holder["id"] = "v:%s:%d" % (label, n)
            return {"id": holder["id"], "src": src, "copy": final, "time": now.isoformat(timespec="seconds"),
                    "size": size, "sha256": sha, "via": via or "", "session": session or ""}
        return make

    try:
        _append_jsonl(index, make_for(_now_day(now)))
        res["id"], res["indexed"] = holder.get("id"), True
    except Exception as e:
        res["msg"] = "the copy is made but NOT in the day index (%s: %s)" % (e.__class__.__name__, e)
        try:
            orphans = os.path.join(root, "versions", "_index", "_orphans.jsonl")
            _append_orphan(orphans, make_for("orphans"))
            res["id"] = holder.get("id")
            res["msg"] += "; its record is in %s as %s" % (orphans, res["id"])
        except Exception as e2:
            res["msg"] += "; and the record could not be saved anywhere (%s: %s): copy at %s" % (
                e2.__class__.__name__, e2, final)
    return res


# ---------------------------------------------------------------- find / restore

def _slash(s):
    return str(s).replace("\\", "/").lower()


def find(text, settings, days=None, volume=None):
    """Search the indexes of trash and versions. Returns (hits, problems); a hit is a dict with
    kind ('trash'|'version'), id, time, src, where, present, restored, reason, root."""
    needle = _slash(text or "")
    cutoff = None if days is None else datetime.now().date().toordinal() - days
    hits, problems = [], []
    for root in all_roots(settings):
        if volume and os.path.splitdrive(root)[0][:1].upper() != volume[:1].upper() and not env_root():
            continue
        for kind, sub, prefix, wherekey in (("trash", "trash", "t", "dst"), ("version", "versions", "v", "copy")):
            idx = os.path.join(root, sub, "_index")
            if not os.path.isdir(idx):
                continue
            try:
                names = sorted(os.listdir(idx))
            except OSError as e:
                problems.append("cannot read %s: %s" % (idx, e))
                continue
            for name in names:
                stem = name[:-6] if name.endswith(".jsonl") else None
                if stem is None or not (DAY_RE.match(stem) or stem == "_orphans"):
                    continue
                if cutoff is not None and DAY_RE.match(stem):
                    if datetime.strptime(stem, "%Y-%m-%d").date().toordinal() < cutoff:
                        continue
                path = os.path.join(idx, name)
                try:
                    recs, bad = _read_jsonl(path)
                except OSError as e:
                    problems.append("cannot read %s: %s" % (path, e))
                    continue
                if bad:
                    problems.append("%d unreadable line(s) in %s" % (bad, path))
                restored = {r.get("of"): r for _n, r in recs if r.get("op") == "restore"}
                for n, rec in recs:
                    if rec.get("op"):
                        continue
                    blob = " ".join(_slash(rec.get(k, "")) for k in ("src", wherekey, "reason"))
                    if needle in blob:
                        rid = rec.get("id") or "%s:%s:%d" % (prefix, "orphans" if stem == "_orphans" else stem, n)
                        hits.append({"kind": kind, "id": rid, "time": rec.get("time", ""),
                                     "src": rec.get("src", ""), "where": rec.get(wherekey, ""),
                                     "present": bool(rec.get(wherekey)) and _lexists(rec.get(wherekey)),
                                     "restored": restored.get(rid), "reason": rec.get("reason", ""),
                                     "root": root})
    hits.sort(key=lambda h: h["time"])
    return hits, problems


def _locate(item_id, settings, volume):
    """(root, index path, records list, line number, label) or (None, message)."""
    m = ID_RE.match((item_id or "").strip())
    if not m:
        return None, "REFUSED bad id %r: expected t:YYYY-MM-DD:N, t:orphans:N or v:YYYY-MM-DD:N" % item_id
    typ, label, n = m.group(1), m.group(2), int(m.group(3))
    found = []
    for root in all_roots(settings):
        if volume and os.path.splitdrive(root)[0][:1].upper() != volume[:1].upper() and not env_root():
            continue
        sub = "trash" if typ == "t" else "versions"
        idx = os.path.join(root, sub, "_index", ("_orphans" if label == "orphans" else label) + ".jsonl")
        if not os.path.isfile(idx):
            continue
        try:
            recs, bad = _read_jsonl(idx)
        except OSError as e:
            return None, "REFUSED cannot read %s: %s" % (idx, e)
        rec = dict(recs).get(n)
        if rec is not None:
            found.append((root, idx, recs, n, label, bad))
    if not found:
        return None, "REFUSED no readable record %s in any store" % item_id
    if len(found) > 1:
        return None, ("REFUSED the id %s exists in several stores (%s) -- name the volume: "
                      "--volume X:" % (item_id, ", ".join(f[0] for f in found)))
    return found[0], None


def _record_restore(idx, orphans, rec_make):
    """Append the restore record; on failure try the orphans file. (recorded_ok, where_or_error)."""
    try:
        _append_jsonl(idx, rec_make)
        return True, idx
    except Exception as e:
        try:
            _append_jsonl(orphans, rec_make)
            return False, "%s; record -> %s" % (e, orphans)
        except Exception:
            return False, "%s; record nowhere" % e


def restore(item_id, settings, to=None, session="", volume=None, now=None):
    """Bring one item back by its id. Returns (status, message): 'ok' | 'refused' | 'unindexed'.
    A trash item comes back by a rename and is refused when the place is occupied (a restore never
    overwrites; --to names another place). A version comes back as a copy verified by sha256; the
    file that is there now goes to the trash FIRST (reason 'replaced by restore'), and only after
    the verification of the copy."""
    now = now or datetime.now()
    loc, err = _locate(item_id, settings, volume)
    if loc is None:
        return "refused", err
    root, idx, recs, n, label, bad = loc
    rec = dict(recs)[n]
    item_id = item_id.strip()
    typ = item_id[0]
    if any(r.get("op") == "restore" and r.get("of") == item_id for _n, r in recs) and typ == "t":
        return "refused", "REFUSED %s was already restored" % item_id
    if rec.get("op") or not rec.get("src"):
        return "refused", "REFUSED %s is not a record of an item" % item_id
    orphans = os.path.join(root, "trash" if typ == "t" else "versions", "_index", "_orphans.jsonl")
    target = os.path.abspath(to) if to else os.path.abspath(rec["src"])
    if not DRIVE_RE.match(os.path.splitdrive(target)[0]):
        return "refused", "REFUSED the target is not on a drive letter: %s" % target
    if os.path.splitdrive(target)[1].strip("\\/") == "":
        return "refused", "REFUSED the target is a drive root: %s" % target
    if _within(target, root):
        return "refused", "REFUSED the target is inside the store: %s" % target
    anc = os.path.dirname(target)
    while not _lexists(anc) and os.path.dirname(anc) != anc:
        anc = os.path.dirname(anc)
    if not os.path.isdir(_lp(anc)):
        return "refused", "REFUSED a parent of the target is not a folder: %s" % anc

    def rec_make(extra):
        return lambda k: dict({"op": "restore", "of": item_id, "time": now.isoformat(timespec="seconds"),
                               "to": target, "session": session or ""}, **extra)

    if typ == "t":
        dst = os.path.abspath(rec.get("dst") or "")
        if not _within(dst, os.path.join(root, "trash")) or not _lexists(dst):
            return "refused", "REFUSED the item is not in the trash any more (or the record is damaged): %s" % dst
        if os.path.splitdrive(target)[0].upper() != os.path.splitdrive(dst)[0].upper():
            return "refused", "REFUSED the target is on another volume than the trash (a restore is a rename): %s" % target
        if _occupied(target):
            return "refused", ("REFUSED the place is occupied: %s -- nothing changed; give another place "
                               "with --to PATH" % target)
        try:
            os.makedirs(_lp(os.path.dirname(target)), exist_ok=True)
            os.replace(_lp(dst), _lp(target))
        except OSError as e:
            return "refused", "REFUSED the rename failed (%s: %s): %s" % (e.__class__.__name__, e, dst)
        okrec, where = _record_restore(idx, orphans, rec_make({"from": dst}))
        if not okrec:
            return "unindexed", "RESTORED BUT NOT RECORDED (%s); DO NOT REPEAT -- the item is at %s" % (where, target)
        return "ok", "OK restored %s -> %s" % (dst, target)

    # a version
    copy = rec.get("copy") or ""
    vdir = os.path.join(root, "versions")
    if not copy or not _within(copy, vdir) or not os.path.isfile(_lp(copy)) or _is_link(copy):
        return "refused", "REFUSED the copy is not in the store (cleaned by a person, or the record is damaged): %s" % copy
    try:
        if rec.get("sha256") and _sha256(copy) != rec["sha256"]:
            return "refused", "REFUSED the copy no longer matches its recorded sha256: %s" % copy
    except OSError as e:
        return "refused", "REFUSED cannot read the copy (%s): %s" % (e, copy)
    if _lexists(target) and (_is_link(target) or not os.path.isfile(_lp(target))):
        return "refused", "REFUSED the target is a folder or a link, not a file: %s" % target
    part = "%s.shedkeeper-restore-%d.part" % (target, os.getpid())
    if _lexists(part):
        return "refused", "REFUSED temporary name taken: %s" % part
    try:
        os.makedirs(_lp(os.path.dirname(target)), exist_ok=True)
        shutil.copy2(_lp(copy), _lp(part))
    except Exception as e:
        return "refused", "REFUSED the copy could not be written (%s: %s); %s may stay" % (e.__class__.__name__, e, part)
    ok, why = _verify_copy(copy, part)
    if not ok:
        return "refused", "REFUSED the restored file does not match the copy (%s); nothing replaced; it stays at %s" % (why, part)
    displaced = None
    if _occupied(target):
        st, msg, info = trash(target, "replaced by restore of %s" % item_id, settings, session, now)
        if st == "refused":
            return "refused", "REFUSED the current file could not be moved to the trash (%s); nothing replaced; the verified copy is at %s" % (msg, part)
        displaced = info.get("dst")
    try:
        os.replace(_lp(part), _lp(target))
    except OSError as e:
        back = ""
        if displaced:
            try:
                os.rename(_lp(displaced), _lp(target))
                back = "; the displaced file was put back"
            except OSError as e2:
                back = "; the displaced file could NOT be put back (%s) -- it is at %s" % (e2, displaced)
        return "refused", "REFUSED the final rename failed (%s: %s)%s; the verified copy is at %s" % (e.__class__.__name__, e, back, part)
    okrec, where = _record_restore(idx, orphans, rec_make({"from": copy, "displaced": displaced}))
    note = "; the file that was there is in the trash: %s" % displaced if displaced else ""
    if not okrec:
        return "unindexed", "RESTORED BUT NOT RECORDED (%s); the file is at %s%s" % (where, target, note)
    return "ok", "OK restored the version %s -> %s%s" % (copy, target, note)


# ---------------------------------------------------------------- transcripts

def save_transcript(src, root, project_name, session_id):
    """Copy a transcript into the store unless a copy at least as long is there. The copy goes to a
    temporary name and one os.replace puts it in place. Returns 'copied' | 'same' | 'failed:<why>'."""
    try:
        dst = os.path.join(root, "transcripts", project_name, session_id + ".jsonl")
        size = os.path.getsize(src)
        if os.path.isfile(dst) and os.path.getsize(dst) >= size:
            return "same"
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        tmp = "%s.part-%d" % (dst, os.getpid())
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)
        return "copied"
    except Exception as e:
        return "failed:%s: %s" % (e.__class__.__name__, e)


# ---------------------------------------------------------------- status

def summary(root):
    """A few facts about one store, for `status`."""
    out = {"root": root, "label": label_state(root)[0]}
    for sub in ("trash", "versions"):
        idx = os.path.join(root, sub, "_index")
        days, recs = 0, 0
        if os.path.isdir(idx):
            for name in os.listdir(idx):
                if name.endswith(".jsonl"):
                    days += 1
                    try:
                        recs += sum(1 for _n, r in _read_jsonl(os.path.join(idx, name))[0] if not r.get("op"))
                    except OSError:
                        pass
        out[sub] = "%d index file(s), %d record(s)" % (days, recs)
    tdir = os.path.join(root, "transcripts")
    out["transcripts"] = sum(len(fs) for _d, _s, fs in os.walk(tdir)) if os.path.isdir(tdir) else 0
    return out
