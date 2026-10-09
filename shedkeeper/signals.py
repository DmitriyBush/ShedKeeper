r"""signals.py -- the SIGNALS of 0.3.0 (section 4 of the design): for one entry of the protected list, did its state change since the last copy?

Every signal answers `changed`, `same` or `unknown`; `unknown` is NEVER read as `same` (C4.2: a signal that cannot work -- the journal is off,
`track_counts = off`, a session that is not visible -- is `unknown`, and the caller takes a copy). A signal READS and never writes: what it
returns for the next check (the new base) is in the result and is written by guard.py ONLY after the copy was taken and read back (M4.4a, C4.1).

  tree_signal   kind D   NTFS change journal read WITHOUT administrator rights (M4.5-M4.7, M4.5a-d), else a walk with a signature
                         (size, mtime_ns, ChangeTime: measured, the walk costs the same order as the plain one)
  sqlite_signal kind S   signature of the file and of its -wal (M4.11)
  pg_signal     kind P/R counters of the database read from the database `postgres` + the sessions of the database (M4.13-M4.17)

Windows only for the journal and ChangeTime (ctypes, standard library); a failure of either falls back to the walk, loudly in `info`.
"""
import ctypes
import fnmatch
import itertools
import json
import os
import re
import struct
import subprocess
import sys
import time

CHANGED, SAME, UNKNOWN = "changed", "same", "unknown"

DEFAULT_EXCLUDE = ("node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox")   # M4.10a: only what an install restores; `.git` is NOT here

ATTR_DIR = 0x10
ATTR_REPARSE = 0x400
REASON_DELETE = 0x200
REASON_RENAME_OLD = 0x1000
REASON_RENAME_NEW = 0x2000
REASON_CLOSE = 0x80000000
IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003      # a junction
IO_REPARSE_TAG_SYMLINK = 0xA000000C
LINK_TAGS = (IO_REPARSE_TAG_MOUNT_POINT, IO_REPARSE_TAG_SYMLINK)      # the only reparse points that are LINKS (limit 9); any other (a cloud placeholder, a deduplicated file ...) is an ordinary file or folder
DEGRADED = "degraded: "                       # the notes that begin with this are a fall of the promise (not a switch of the method): the person is told (guard)
SQLITE_MAGIC = b"SQLite format 3\x00"


class Budget(object):
    """The walk budget of one signal (`s` seconds, `walk_budget_ms`) and the clock of the step (`left()` -> seconds left of `step_budget_s`, or None: no limit). Every time limit
    the signal takes is `cap(x)`: never more than the step has left -- a key of the settings can make `x` large, it cannot make the check outlive the hook."""

    def __init__(self, s, left=None):
        self.s = float(s)
        self.left = left

    def cap(self, seconds):
        if self.left is None:
            return seconds
        return max(0.05, min(seconds, self.left()))

    def walk(self):
        return self.cap(max(self.s * 4, 5))


class Result(object):
    """What a signal found. `verdict`: changed | same | unknown. `why`: words for the log. `new_state`: the base for the NEXT check (written after the copy only).
    Trees: `changed` [rel] (new or modified files, to copy), `deleted` [rel], `upserts` {rel: (size, mtime_ns, ctime_ns, frn)}, `full` (no base: the whole tree is to be copied)."""

    def __init__(self, verdict, why="", new_state=None, **kw):
        self.verdict = verdict
        self.why = why
        self.new_state = new_state if new_state is not None else {}
        self.changed = kw.get("changed", [])
        self.deleted = kw.get("deleted", [])
        self.upserts = kw.get("upserts", {})
        self.full = kw.get("full", False)
        self.dir_map = kw.get("dir_map")           # {frn: rel} of the directories (journal mode)
        self.notes = list(kw.get("notes", []))     # said to the log (and, for the loud ones, to the person)
        self.info = kw.get("info", {})
        self.dead = kw.get("dead", False)          # the signal cannot work at all (track_counts off ...)

    def __repr__(self):
        return "Result(%s: %s)" % (self.verdict, self.why)


# ============================================================================================ Windows file facilities (ctypes)

_K = None
INVALID = None


def _kernel():
    """kernel32 with the signatures this module needs; None when this is not Windows."""
    global _K, INVALID
    if _K is not None:
        return _K
    if os.name != "nt":
        return None
    import ctypes.wintypes as wt
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateFileW.restype = wt.HANDLE
    k.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.HANDLE]
    k.DeviceIoControl.argtypes = [wt.HANDLE, wt.DWORD, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
    k.OpenFileById.restype = wt.HANDLE
    k.OpenFileById.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD]
    k.GetFinalPathNameByHandleW.argtypes = [wt.HANDLE, wt.LPWSTR, wt.DWORD, wt.DWORD]
    k.GetFileInformationByHandleEx.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
    k.CloseHandle.argtypes = [wt.HANDLE]
    k.GetVolumeInformationW.argtypes = [wt.LPCWSTR, wt.LPWSTR, wt.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wt.LPWSTR, wt.DWORD]
    INVALID = wt.HANDLE(-1).value
    _K = k
    return k


BACKUP = 0x02000000


def _lp(p):
    p = os.path.abspath(p)
    if os.name == "nt" and len(p) >= 240 and not p.startswith("\\\\?\\"):
        return "\\\\?\\UNC\\" + p[2:] if p.startswith("\\\\") else "\\\\?\\" + p
    return p


def basic_info(path):
    """(size, mtime_ns, ctime_ns) of a file -- ctime = the NTFS ChangeTime (changes at every change of data AND of attributes, also when the
    modification time is put back by hand). The size is the size of the data. None when the file is not there. Raises OSError for another fault."""
    k = _kernel()
    st = os.stat(_lp(path))
    mt = st.st_mtime_ns
    if k is None:
        return st.st_size, mt, getattr(st, "st_ctime_ns", 0)
    h = k.CreateFileW(_lp(path), 0x80, 7, None, 3, BACKUP, None)
    if h == INVALID:
        return st.st_size, mt, 0                 # no handle: the signature then lacks ChangeTime (a change of the content still moves size / mtime)
    try:
        buf = ctypes.create_string_buffer(40)
        if not k.GetFileInformationByHandleEx(h, 0, buf, 40):       # FileBasicInfo
            return st.st_size, mt, 0
        _cr, _ac, lw, ch = struct.unpack_from("<qqqq", buf.raw, 0)
        return st.st_size, lw, ch
    finally:
        k.CloseHandle(h)


def fs_name(path):
    """The file system of the volume of `path` ('NTFS', 'ReFS', 'FAT32' ...); '' when it cannot be read."""
    k = _kernel()
    if k is None:
        return ""
    drive = os.path.splitdrive(os.path.abspath(path))[0]
    if not drive:
        return ""
    import ctypes.wintypes as wt
    buf = ctypes.create_unicode_buffer(64)
    ok = k.GetVolumeInformationW(drive + "\\", None, 0, None, None, None, buf, 64)
    return buf.value if ok else ""


def _is_excluded(rel_parts, is_dir, names, patterns, paths):
    name = rel_parts[-1]
    if name in names:
        return True
    for pat in patterns:
        if fnmatch.fnmatch(name, pat):
            return True
    if paths:
        rel = "/".join(rel_parts)
        for p in paths:
            if rel == p or rel.startswith(p + "/"):
                return True
    return False


def make_exclusion(exclude):
    """(names, patterns, paths) from the exclusion list of an entry: a plain name matches a folder or file of that name anywhere; a pattern
    (`*` `?` `[`) matches names; a name with `/` is a path under the root."""
    names, patterns, paths = set(), [], []
    for e in exclude or ():
        e = (e or "").strip().replace("\\", "/").strip("/")
        if not e:
            continue
        if "/" in e:
            paths.append(e)
        elif any(c in e for c in "*?["):
            patterns.append(e)
        else:
            names.add(e)
    return names, patterns, paths


# ============================================================================================ the walk (with ChangeTime)

def _walk_ctypes(root, skip, excl, deadline, start_rel="", skipped=None):
    """{rel: (size, mtime_ns, ctime_ns, frn)}, {frn: rel} of the directories -- one handle and one call batch per directory (FileIdBothDirectoryInfo).
    A reparse point (junction, symbolic link) is not entered and a file that is a reparse point is not listed (limit 9). `skip`: absolute paths (normcase) that are not entered."""
    k = _kernel()
    files, dirs = {}, {}
    buf = ctypes.create_string_buffer(1 << 16)
    top = os.path.join(root, start_rel.replace("/", os.sep)) if start_rel else root
    stack = [(top, start_rel)]
    names, patterns, paths = excl
    while stack:
        d, rel_d = stack.pop()
        if deadline and time.monotonic() > deadline:
            raise TimeoutError("the walk of %s did not finish in time" % root)
        h = k.CreateFileW(_lp(d), 0x1, 7, None, 3, BACKUP, None)
        if h == INVALID:
            raise OSError(ctypes.get_last_error(), "cannot list %s" % d)
        try:
            cls = 11                                    # FileIdBothDirectoryRestartInfo, then FileIdBothDirectoryInfo
            while True:
                if not k.GetFileInformationByHandleEx(h, cls, buf, len(buf)):
                    err = ctypes.get_last_error()
                    if err in (18,):                    # ERROR_NO_MORE_FILES
                        break
                    raise OSError(err, "cannot list %s" % d)
                cls = 10
                raw = buf.raw
                off = 0
                while True:
                    nxt = struct.unpack_from("<I", raw, off)[0]
                    lw, ch, eof = struct.unpack_from("<qqq", raw, off + 24)
                    attrs, nlen = struct.unpack_from("<II", raw, off + 56)
                    frn = struct.unpack_from("<Q", raw, off + 96)[0]
                    name = raw[off + 104: off + 104 + nlen].decode("utf-16-le", errors="surrogatepass")
                    if name not in (".", ".."):
                        rel = (rel_d + "/" + name) if rel_d else name
                        absn = os.path.normcase(os.path.join(d, name))
                        if absn not in skip and not _is_excluded(rel.split("/"), bool(attrs & ATTR_DIR), names, patterns, paths):
                            # a LINK (junction, symbolic link) is not entered and not listed (limit 9); the tag of a reparse point is in `EaSize` (offset 64) of this structure.
                            # Any other reparse point is an ordinary file or folder.
                            link = bool(attrs & ATTR_REPARSE) and struct.unpack_from("<I", raw, off + 64)[0] in LINK_TAGS
                            if link:
                                if skipped is not None:
                                    skipped.append(rel)
                            elif attrs & ATTR_DIR:
                                dirs[frn] = rel
                                stack.append((os.path.join(d, name), rel))
                            else:
                                files[rel] = (eof, lw, ch, frn)
                    if not nxt:
                        break
                    off += nxt
        finally:
            k.CloseHandle(h)
    return files, dirs


def _walk_scandir(root, skip, excl, deadline, start_rel="", skipped=None):
    """The fallback walk (no ChangeTime: ctime 0). Same shape as _walk_ctypes."""
    files, dirs = {}, {}
    names, patterns, paths = excl
    top = os.path.join(root, start_rel.replace("/", os.sep)) if start_rel else root
    stack = [(top, start_rel)]
    while stack:
        d, rel_d = stack.pop()
        if deadline and time.monotonic() > deadline:
            raise TimeoutError("the walk of %s did not finish in time" % root)
        with os.scandir(_lp(d)) as it:
            for e in it:
                rel = (rel_d + "/" + e.name) if rel_d else e.name
                if os.path.normcase(os.path.join(d, e.name)) in skip or _is_excluded(rel.split("/"), False, names, patterns, paths):
                    continue
                if e.is_symlink() or (getattr(e.stat(follow_symlinks=False), "st_reparse_tag", 0) in LINK_TAGS):
                    if skipped is not None:
                        skipped.append(rel)
                    continue
                if e.is_dir(follow_symlinks=False):
                    dirs[e.inode()] = rel
                    stack.append((e.path, rel))
                else:
                    st = e.stat(follow_symlinks=False)
                    files[rel] = (st.st_size, _filetime(st.st_mtime_ns), 0, e.inode())
    return files, dirs


FILETIME_EPOCH_NS = 116444736000000000


def _filetime(st_mtime_ns):
    """The Windows FILETIME (100 ns since 1601) of a Python `st_mtime_ns` (ns since 1970): the one unit the signatures are kept in."""
    return st_mtime_ns // 100 + FILETIME_EPOCH_NS


_UNKNOWN_SEQ = itertools.count(1)


def _unknown_time():
    """A time that equals NO other: a file that could not be looked at is not the same as at the last copy, however many times it is asked (the monotonic clock of Windows ticks in
    about 16 ms, so the clock alone is not enough: the performance counter, the process and a sequence number go in)."""
    return -(time.perf_counter_ns() * 4096 + (os.getpid() & 0xF) * 256 + (next(_UNKNOWN_SEQ) & 0xFF))


def _refresh(root, files, deadline):
    """The directory listing is LAGGING for a file that another process holds open for writing (measured: the size and the times in the listing, from FindNextFile and from
    FileIdBothDirectoryInfo alike, show the state of an earlier write until the handle is closed); `os.stat` opens the file and is exact (about 6 microseconds a file). So size and
    modification time are taken from `os.stat`, ChangeTime (which the listing alone gives) stays from the listing. -> (number refreshed, cut: True when the time ran out, number of
    files that could not be looked at). A file that is gone between the listing and the look keeps its listing value (the copy then finds it gone). Any OTHER fault is "not known" for the file:
    its time becomes a value that equals nothing (a new one every time), so the file is `changed`, the copy is tried and an unreadable file refuses the step (limit 10)."""
    n, unknown = 0, 0
    for rel, v in list(files.items()):
        if deadline and time.monotonic() > deadline:
            return n, True, unknown
        try:
            st = os.stat(_lp(os.path.join(root, rel.replace("/", os.sep))))
        except FileNotFoundError:
            continue
        except OSError:
            files[rel] = (v[0], _unknown_time(), -1, v[3])
            unknown += 1
            continue
        files[rel] = (st.st_size, _filetime(st.st_mtime_ns), v[2], v[3])
        n += 1
    return n, False, unknown


def walk_tree(root, skip=(), exclude=(), deadline=None, start_rel="", fresh_s=None, skipped=None):
    """-> ({rel: (size, mtime, ctime, frn)}, {frn: rel dirs}, note). The walk with ChangeTime where it works, else the plain one (the note says so); then a fresh look at every file
    (`_refresh`) for at most `fresh_s` seconds (None: no limit but the deadline); when it is cut the note says how many files were looked at and which limit that is.
    `skipped`: a list that gets the relative path of every LINK (junction, symbolic link) that was not entered."""
    excl = make_exclusion(exclude)
    skip = {os.path.normcase(os.path.abspath(s)) for s in skip}
    note = ""
    if _kernel() is not None:
        try:
            f, d = _walk_ctypes(root, skip, excl, deadline, start_rel, skipped)
        except TimeoutError:
            raise
        except OSError as e:
            note = DEGRADED + "the walk with ChangeTime failed (%s: %s); the plain walk is used, the signature has no ChangeTime" % (e.__class__.__name__, e)
            if skipped is not None:
                del skipped[:]
            f, d = _walk_scandir(root, skip, excl, deadline, start_rel, skipped)
    else:
        f, d = _walk_scandir(root, skip, excl, deadline, start_rel, skipped)
        note = "no ChangeTime on this system: the signature is (size, mtime)"
    cut_at = None
    if fresh_s is not None or deadline:
        limits = [x for x in ((time.monotonic() + fresh_s) if fresh_s is not None else None, deadline) if x]
        cut_at = min(limits)
    n, cut, unknown = _refresh(root, f, cut_at)
    if cut:
        note = (note + "; " if note else "") + (DEGRADED + "only %d of %d files were looked at fresh in the time given: a file held open by a writer may show its last writes late (limit 4)" % (n, len(f)))
    if unknown:
        note = (note + "; " if note else "") + ("%d file(s) could not be looked at: each is copied (or refuses the step when it cannot be read)" % unknown)
    return f, d, note


# ============================================================================================ the change journal (USN), unprivileged

FSCTL_QUERY_USN_JOURNAL = 0x000900F4
FSCTL_READ_UNPRIVILEGED_USN_JOURNAL = 0x000903AB


class JournalOff(Exception):
    pass


GONE = ""                       # what `Usn.path_of` returns for a file the system proved is gone (falsy: `if p and ...` stays right); None is never returned: unknown raises
GONE_ERRORS = (2, 3, 87)        # ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND, ERROR_INVALID_PARAMETER (an id that does not exist: measured on NTFS, a deleted file)


class Usn(object):
    def __init__(self, root):
        k = _kernel()
        if k is None:
            raise JournalOff("not Windows")
        self.k = k
        self.root = root
        self.h = k.CreateFileW(_lp(root), 0, 7, None, 3, BACKUP, None)
        if self.h == INVALID:
            raise JournalOff("cannot open %s (error %s)" % (root, ctypes.get_last_error()))
        fin = self._final(self.h)
        self.final_known = bool(fin)                         # False: the system gives no final path for the root, and the root is taken as the person wrote it (said loudly, see _tree_signal)
        self.final_root = fin or root                        # the path the system itself gives for the root (a junction, `subst`, a short 8.3 name resolved), once: the paths of the records are in THIS form

    def close(self):
        try:
            self.k.CloseHandle(self.h)
        except Exception:
            pass

    def query(self):
        """(journal id, first usn, next usn) or None when the journal is off."""
        import ctypes.wintypes as wt
        out = ctypes.create_string_buffer(80)
        ret = wt.DWORD(0)
        if not self.k.DeviceIoControl(self.h, FSCTL_QUERY_USN_JOURNAL, None, 0, out, 80, ctypes.byref(ret), None):
            return None
        jid, first, nxt = struct.unpack_from("<QqQ", out.raw, 0)
        return jid, first, nxt

    def read_since(self, jid, start, deadline=None):
        """-> (records [(frn, parent, reason, attrs)], next usn). Reads up to the end of the journal. OSError on a fault (the caller walks instead)."""
        import ctypes.wintypes as wt
        recs, cur = [], start
        out = ctypes.create_string_buffer(1 << 20)
        ret = wt.DWORD(0)
        while True:
            if deadline and time.monotonic() > deadline:
                raise TimeoutError("the read of the journal did not finish in time")
            inb = ctypes.create_string_buffer(struct.pack("<qIIQQQ", cur, 0xFFFFFFFF, 0, 0, 0, jid))
            if not self.k.DeviceIoControl(self.h, FSCTL_READ_UNPRIVILEGED_USN_JOURNAL, inb, 40, out, 1 << 20, ctypes.byref(ret), None):
                raise OSError(ctypes.get_last_error(), "the journal cannot be read")
            buf = out.raw[:ret.value]
            nxt = struct.unpack_from("<q", buf, 0)[0]
            off, got = 8, 0
            while off + 8 <= len(buf):
                rlen, major = struct.unpack_from("<IH", buf, off)
                if rlen == 0:
                    break
                if major == 2:
                    frn, par = struct.unpack_from("<QQ", buf, off + 8)
                    reason, = struct.unpack_from("<I", buf, off + 40)
                    attrs, = struct.unpack_from("<I", buf, off + 52)
                else:                                   # v3: 128-bit ids; the low 64 bits are the NTFS file id
                    frn = struct.unpack_from("<Q", buf, off + 8)[0]
                    par = struct.unpack_from("<Q", buf, off + 24)[0]
                    reason, = struct.unpack_from("<I", buf, off + 56)
                    attrs, = struct.unpack_from("<I", buf, off + 68)
                recs.append((frn, par, reason, attrs))
                got += 1
                off += rlen
            if got == 0 or nxt == cur:
                return recs, nxt
            cur = nxt

    def _final(self, h):
        """The final path of an open handle in the plain form, or '' when it cannot be had."""
        pb = ctypes.create_unicode_buffer(2048)
        n = self.k.GetFinalPathNameByHandleW(h, pb, 2048, 0)
        if n == 0 or n >= 2048:
            return ""
        p = pb.value
        if p.startswith("\\\\?\\UNC\\"):
            return "\\" + p[7:]
        return p[4:] if p.startswith("\\\\?\\") else p

    def path_of(self, fid):
        """The path of a file by its id (the names in the unprivileged records are hidden). GONE ('') only when the system PROVED the file is not there (`ERROR_FILE_NOT_FOUND`,
        `ERROR_PATH_NOT_FOUND`, or `ERROR_INVALID_PARAMETER`, which is what it answers for an id that does not exist: measured, a deleted file); any other fault -- a sharing violation, an
        antivirus lock, a path longer than the buffer -- is `unknown`: it raises OSError and the caller walks the tree (a file that cannot be found is not a file that is gone)."""
        k = self.k
        desc = ctypes.create_string_buffer(struct.pack("<II", 24, 0) + struct.pack("<Q", fid) + b"\0" * 8)
        h = k.OpenFileById(self.h, desc, 0, 7, None, BACKUP)
        if h == INVALID:
            err = ctypes.get_last_error()
            if err in GONE_ERRORS:
                return GONE
            raise OSError(err, "the file with id %d cannot be opened by its id" % fid)
        try:
            p = self._final(h)
            if not p:
                raise OSError(0, "the path of the file with id %d cannot be had (longer than the buffer or refused)" % fid)
            return p
        finally:
            k.CloseHandle(h)


def journal_available(root):
    """(Usn | None, why): the journal of the volume of `root`, when the volume is NTFS and the journal is on (M4.5, M4.5d)."""
    fs = fs_name(root)
    if fs and fs.upper() != "NTFS":
        return None, "the volume is %s, not NTFS: the walk with a signature is used (M4.5d)" % fs
    try:
        u = Usn(root)
    except JournalOff as e:
        return None, "the journal cannot be opened (%s)" % e
    q = u.query()
    if q is None:
        u.close()
        return None, "the change journal of the volume is off: the walk with a signature is used"
    return u, ""


def _under(path, root):
    p, r = os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(root))
    return p == r or p.startswith(r.rstrip("\\/") + os.sep)


def _rel(path, root):
    return os.path.relpath(path, root).replace("\\", "/")


def _is_link(path):
    """Is `path` a LINK (a junction or a symbolic link)? Other reparse points are not. Not known (the look fails) -> False: the folder is walked, which is the safe side."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return getattr(st, "st_reparse_tag", 0) in LINK_TAGS or os.path.islink(path)


def _still_there(root, rel, frn):
    """Is the entry `rel` (as the last copy knew it) still at its old place under `root` with the same file id? The second witness for 'gone': what the system answers for an id is ONE probe
    (one error code, measured on one volume); the old place is another."""
    try:
        return os.lstat(_lp(os.path.join(root, rel.replace("/", os.sep)))).st_ino == frn
    except OSError:
        return False


def _open_files_in(records, dirs):
    """{frn} of the files under the root whose LAST record in `records` has no CLOSE: a file that is still open for writing (open writers)."""
    last = {}
    for frn, par, reason, attrs in records:
        if attrs & ATTR_DIR:
            continue
        if par in dirs:
            last[frn] = reason
    return {frn for frn, reason in last.items() if not reason & REASON_CLOSE and not reason & REASON_DELETE}


# ============================================================================================ kind D

def tree_signal(entry, st, table, settings, skip=(), now=None, left=None):
    """The signal of a tree. `entry`: {root, exclude}; `st`: the base left by the last COPY ({} = none); `table`: the signatures of the last copy
    (`rows()` -> {rel: (size, mtime_ns, ctime_ns, frn)}, `by_frn(frn)`, `under(prefix)`). `left`: a function -> seconds left of the step (None: no limit): every time limit of
    the signal is cut to it, whatever `walk_budget_ms` says. -> Result. Nothing is written here."""
    root = entry["root"]
    walk_budget = Budget(float(settings.get("walk_budget_ms", 3000)) / 1000.0, left)
    resolve_max = int(settings.get("usn_resolve_max", 2000))
    notes = []
    if not os.path.isdir(root):
        return Result(UNKNOWN, "the root %s is not a folder" % root, notes=notes, info={"mode": "none"})
    exclude = list(entry.get("exclude") or ())
    usn, why_off = journal_available(root)
    try:
        return _tree_signal(entry, st, table, settings, skip, exclude, usn, why_off, walk_budget, resolve_max, notes)
    except TimeoutError as e:
        return Result(UNKNOWN, "too slow: %s" % e, notes=notes, info={"mode": "timeout", "ms": int(max(walk_budget.s * 4, 5) * 1000)})
    finally:
        if usn is not None:
            usn.close()


def _tree_signal(entry, st, table, settings, skip, exclude, usn, why_off, walk_budget, resolve_max, notes):
    root = entry["root"]
    if usn is not None and not getattr(usn, "final_known", True):
        notes.append(DEGRADED + "journal: the final path of the root %s cannot be read: the paths of the journal are compared with the root as it is written, so a folder that moves inside a root "
                                "named through a link may be misread" % root)
    try:
        if usn is not None:
            q = usn.query()
            have = bool(st.get("have_base"))
            if not have:
                return _journal_full(entry, usn, q, skip, exclude, walk_budget, notes, "no base: the first copy of the tree")
            if st.get("mode") != "usn" or q is None:
                return _journal_full(entry, usn, q, skip, exclude, walk_budget, notes, "the base is a walk, the journal is on now: new base", compare=table)
            jid, first, nxt = q
            if jid != st.get("jid"):
                notes.append("journal: a NEW journal (id changed): full walk")
                return _journal_full(entry, usn, q, skip, exclude, walk_budget, notes, "new journal", compare=table)
            if first > st.get("usn", 0):
                notes.append("journal: wrapped (lost %d bytes of records): full walk" % (first - st.get("usn", 0)))
                return _journal_full(entry, usn, q, skip, exclude, walk_budget, notes, "the journal wrapped", compare=table)
            try:
                return _journal_incremental(entry, st, usn, q, table, skip, exclude, resolve_max, walk_budget, notes)
            except (OSError, TimeoutError) as e:
                notes.append("journal: read failed (%s: %s): full walk" % (e.__class__.__name__, e))
                return _journal_full(entry, usn, q, skip, exclude, walk_budget, notes, "the journal could not be read", compare=table)
        if why_off:
            notes.append(why_off)
        return _walk_mode(entry, st, table, skip, exclude, walk_budget, notes)
    finally:
        pass


def _walk_mode(entry, st, table, skip, exclude, budget, notes):
    root = entry["root"]
    t0 = time.monotonic()
    links = []
    files, dirs, note = walk_tree(root, skip, exclude, time.monotonic() + budget.walk(), fresh_s=budget.cap(max(budget.s, 1.0)), skipped=links)
    ms = int((time.monotonic() - t0) * 1000)
    if note:
        notes.append(note)
    new_state = {"mode": "walk", "have_base": True, "links": len(links)}
    if not st.get("have_base"):
        return Result(UNKNOWN, "no base: the first copy of the tree", new_state, changed=sorted(files), upserts=files, full=True, notes=notes,
                      info={"mode": "walk", "files": len(files), "ms": ms, "links": len(links)})
    r = _compare(files, table.rows(), new_state, notes, "walk")
    r.info["ms"] = ms
    r.info["links"] = len(links)
    return r


def _compare(files, old, new_state, notes, mode, dir_map=None):
    changed, upserts = [], {}
    for rel, sig in files.items():
        o = old.get(rel)
        if o is None or tuple(o[:3]) != tuple(sig[:3]):
            changed.append(rel)
            upserts[rel] = sig
        elif o[3] != sig[3]:
            upserts[rel] = sig                       # only the file id moved (a copy over the same name): content signature is the same
    deleted = sorted(rel for rel in old if rel not in files)
    verdict = CHANGED if (changed or deleted) else SAME
    return Result(verdict, "%s: %d changed, %d deleted" % (mode, len(changed), len(deleted)), new_state, changed=sorted(changed), deleted=deleted,
                  upserts=upserts, dir_map=dir_map, notes=notes, info={"mode": mode, "files": len(files)})


def _journal_full(entry, usn, q, skip, exclude, budget, notes, why, compare=None):
    """A walk in journal mode: the position is read BEFORE the walk (I2: what changes during the walk shows up at the next check)."""
    root = entry["root"]
    t0 = time.monotonic()
    pos = q[2] if q else 0
    links = []
    files, dirs, note = walk_tree(root, skip, exclude, time.monotonic() + budget.walk(), skipped=links)
    if note:
        notes.append(note)
    new_state = {"mode": "usn" if q else "walk", "have_base": True, "jid": q[0] if q else 0, "usn": pos, "open": {}, "dirs": sorted([f, r] for f, r in dirs.items()), "links": len(links)}
    if q:
        # M4.5c: files that are open for writing since before the history of the journal (and not closed) are in the set of open writers
        try:
            froot = getattr(usn, "final_root", None) or root
            recs, _n = usn.read_since(q[0], q[1], time.monotonic() + budget.cap(max(budget.s * 2, 3)))
            for frn in _open_files_in(recs, set(dirs) | {os.stat(root).st_ino}):
                p = usn.path_of(frn)
                if p and _under(p, froot):
                    rel = _rel(p, froot)
                    if rel in files:
                        new_state["open"][rel] = list(files[rel][:3])
        except (OSError, TimeoutError) as e:
            notes.append(DEGRADED + "journal: the history could not be read (%s: %s): files open since before the guard may not be seen until they are closed (limit 17)" % (e.__class__.__name__, e))
    else:
        new_state["open"] = {}
    ms = int((time.monotonic() - t0) * 1000)
    if compare is None:
        return Result(UNKNOWN, why, new_state, changed=sorted(files), upserts=files, full=True, dir_map=dirs, notes=notes,
                      info={"mode": "usn-init" if q else "walk", "files": len(files), "ms": ms, "links": len(links)})
    r = _compare(files, compare.rows(), new_state, notes, "walk-after-journal-fault", dir_map=dirs)
    r.info["ms"] = ms
    r.info["links"] = len(links)
    return r


def _journal_incremental(entry, st, usn, q, table, skip, exclude, resolve_max, budget, notes):
    root = entry["root"]
    jid, first, nxt = q
    t0 = time.monotonic()
    froot = getattr(usn, "final_root", None) or root          # the records give paths in the form the SYSTEM gives; the root as the person wrote it (a junction, `subst`, an 8.3 name) is not compared with them
    recs, new_pos = usn.read_since(jid, st["usn"], time.monotonic() + budget.cap(max(budget.s, 3)))
    dirs = {int(f): r for f, r in (st.get("dirs") or [])}
    root_ino = os.stat(root).st_ino
    dirs_ids = set(dirs) | {root_ino}
    names, patterns, paths = make_exclusion(exclude)
    new_state = {"mode": "usn", "have_base": True, "jid": jid, "usn": new_pos, "open": dict(st.get("open") or {}), "links": int(st.get("links") or 0)}

    file_last, file_in_root, new_dirs, moved_dirs, gone_dirs, reparse_new = {}, set(), set(), set(), set(), set()
    last_par = {}                                          # the parent of the LAST record of a file: where the journal says the file stands now
    for frn, par, reason, attrs in recs:
        if attrs & ATTR_DIR:
            if frn in dirs_ids and frn != root_ino:
                if reason & REASON_DELETE:
                    gone_dirs.add(frn)
                elif reason & (REASON_RENAME_OLD | REASON_RENAME_NEW):
                    moved_dirs.add(frn)
            elif par in dirs_ids and frn not in dirs_ids:
                new_dirs.add(frn)
                if attrs & ATTR_REPARSE:
                    reparse_new.add(frn)                    # a junction made inside the root (or a placeholder): the tag decides below whether it is a link, which is never entered (limit 9)
        else:
            last_par[frn] = par
            if par in dirs_ids or table.by_frn(frn) is not None:
                file_last[frn] = reason
            if par in dirs_ids:
                file_in_root.add(frn)

    changed_files = set(file_last)
    if len(changed_files) + len(new_dirs) + len(moved_dirs) > resolve_max:
        notes.append("journal: %d changed files under the root, more than usn_resolve_max = %d: full walk is cheaper" % (len(changed_files), resolve_max))
        usn_state = {"mode": "usn", "have_base": True, "jid": jid, "usn": nxt, "open": {}}
        links = []
        files, dmap, note = walk_tree(root, skip, exclude, time.monotonic() + budget.walk(), skipped=links)
        usn_state["links"] = len(links)
        usn_state["dirs"] = sorted([f, r] for f, r in dmap.items())
        if note:
            notes.append(note)
        # the open writers are kept: the ones known before (while the file is still there) and the ones this read shows (the last record of a file has no CLOSE) -- found by the file id in the walk
        by_frn = {v[3]: rel for rel, v in files.items()}
        for rel, sg in (st.get("open") or {}).items():
            if rel in files:
                usn_state["open"][rel] = list(sg)
        for frn, reason in file_last.items():
            rel = by_frn.get(frn)
            if rel is None:
                continue
            if reason & (REASON_CLOSE | REASON_DELETE):
                usn_state["open"].pop(rel, None)
            else:
                usn_state["open"][rel] = list(files[rel][:3])
        r = _compare(files, table.rows(), usn_state, notes, "walk-after-many", dir_map=dmap)
        return r

    upserts, deleted = {}, set()
    open_set = new_state["open"]

    def stat_sig(p, frn):
        size, lw, ch = basic_info(p)
        return (size, lw, ch, frn)

    def forget_branch(rel_prefix):
        for rel in table.under(rel_prefix):
            deleted.add(rel)
            open_set.pop(rel, None)

    # directories that left or were removed (their rows are deleted from the tree; their state is in the copies already taken)
    for frn in gone_dirs:
        rel = dirs.pop(frn, None)
        if rel is not None:
            forget_branch(rel)
    # directories that moved: where are they now?
    branches = []
    for frn in moved_dirs:
        p = usn.path_of(frn)
        old_rel = dirs.get(frn)
        if p is None:
            raise OSError(0, "the path of a folder that moved cannot be told")
        if not p and old_rel and _still_there(root, old_rel, frn):
            raise OSError(0, "the system says the folder %s is gone, but it is there with the same id" % old_rel)
        if not p or not _under(p, froot):
            if old_rel is not None:
                notes.append("journal: the folder %s left the root: it is no longer guarded (its earlier state is in the copies)" % old_rel)
                forget_branch(old_rel)
                for f, r in list(dirs.items()):
                    if r == old_rel or r.startswith(old_rel + "/"):
                        dirs.pop(f, None)
        else:
            new_rel = _rel(p, froot)
            if old_rel is not None and old_rel != new_rel:
                forget_branch(old_rel)                  # the old names are gone; the branch under its new name is walked below
                for f, r in list(dirs.items()):
                    if r == old_rel or r.startswith(old_rel + "/"):
                        dirs.pop(f, None)
                branches.append(new_rel)
    for frn in new_dirs:
        p = usn.path_of(frn)
        if p is None:
            raise OSError(0, "the path of a new folder cannot be told")
        if frn in reparse_new and (not p or not _under(p, froot) or _is_link(p)):
            new_state["links"] = int(new_state.get("links") or 0) + 1           # a junction or a symbolic link (the system names its TARGET, outside the root): not entered (limit 9)
            continue
        if p and _under(p, froot):
            rel = _rel(p, froot)
            if not _is_excluded(rel.split("/"), True, names, patterns, paths):
                branches.append(rel)                       # M4.5a: a folder that moved INTO the root, or a new one: its branch is walked
    for rel in dict.fromkeys(branches):
        lk = []
        files, dmap, note = walk_tree(root, skip, exclude, time.monotonic() + budget.walk(), start_rel=rel, skipped=lk)
        new_state["links"] = int(new_state.get("links") or 0) + len(lk)
        if note:
            notes.append(note)
        top_frn = None
        try:
            top_frn = os.stat(os.path.join(root, rel.replace("/", os.sep))).st_ino
        except OSError:
            pass
        if top_frn:
            dirs[top_frn] = rel
        dirs.update(dmap)
        for r2, sig in files.items():
            upserts[r2] = sig

    # files
    skip_forms = list(skip) + [os.path.realpath(x) for x in skip]          # the store may be named through a link: the record gives the final form
    for frn, reason in file_last.items():
        p = usn.path_of(frn)
        old_rel = table.by_frn(frn)
        if p is None:
            raise OSError(0, "the path of a changed file cannot be told")           # not known is not "gone": the whole tree is walked
        if not p and old_rel is not None and _still_there(root, old_rel, frn):
            raise OSError(0, "the system says the file %s is gone, but it is there with the same id" % old_rel)       # one answer of the system is not proof: the old place is the second witness
        if p and last_par.get(frn) in dirs_ids and not _under(p, froot):
            # the LAST record of the file says it stands in a folder of the tree, and its path is not under the root: the paths of the records and the root are not the same form. Told
            # apart by nobody here -> the walk decides (a contradiction is "not known", never "left the root"). A file whose last record has a parent OUTSIDE the tree was carried out
            # (also into the trash of the kit): it left, and that is told by the journal itself.
            raise OSError(0, "a record puts %d inside the tree but its path %s is outside the root %s" % (frn, p, froot))
        if not p or not _under(p, froot):
            if old_rel is not None:
                deleted.add(old_rel)
                open_set.pop(old_rel, None)
            continue
        rel = _rel(p, froot)
        if _is_excluded(rel.split("/"), False, names, patterns, paths):
            continue
        if any(os.path.normcase(os.path.abspath(p)).startswith(os.path.normcase(os.path.abspath(s)).rstrip("\\/") + os.sep) or
               os.path.normcase(os.path.abspath(p)) == os.path.normcase(os.path.abspath(s)) for s in skip_forms):
            continue
        if old_rel is not None and old_rel != rel:
            deleted.add(old_rel)                            # renamed: the old name is gone
            open_set.pop(old_rel, None)
        try:
            if os.path.isdir(p) or os.path.islink(p):
                continue
            upserts[rel] = stat_sig(p, frn)
        except OSError as e:
            notes.append("journal: %s cannot be looked at (%s)" % (rel, e.__class__.__name__))
            upserts[rel] = (0, 0, 0, frn)                   # unreadable now: kept as changed; the copy decides (limit 10)
        if reason & REASON_CLOSE or reason & REASON_DELETE:
            open_set.pop(rel, None)
        else:
            open_set[rel] = list(upserts[rel][:3])           # open writer: no CLOSE yet -> a writer that may still be writing

    # open writers: the journal is silent while a handle stays open, the signature of the file is not (measured: the modification time moves at every write of an open file)
    for rel in list(open_set):
        if rel in upserts or rel in deleted:
            continue
        p = os.path.join(root, rel.replace("/", os.sep))
        try:
            sig = basic_info(p)
        except OSError:
            open_set.pop(rel, None)
            if table.rows_has(rel):
                deleted.add(rel)
            continue
        if list(sig[:3]) != list(open_set[rel]):
            old = table.row(rel)
            upserts[rel] = (sig[0], sig[1], sig[2], (old[3] if old else 0))
            open_set[rel] = list(sig[:3])

    # what really differs from the table (the journal also records reads of attributes and the like)
    changed = []
    for rel, sig in list(upserts.items()):
        old = table.row(rel)
        if old is None or tuple(old[:3]) != tuple(sig[:3]):
            changed.append(rel)
        elif old[3] != sig[3]:
            pass
        else:
            upserts.pop(rel)
    deleted = sorted(d for d in deleted if table.rows_has(d) and d not in upserts)
    new_state["dirs"] = sorted([f, r] for f, r in dirs.items())
    verdict = CHANGED if (changed or deleted) else SAME
    ms = int((time.monotonic() - t0) * 1000)
    return Result(verdict, "journal: %d records, %d changed, %d deleted" % (len(recs), len(changed), len(deleted)), new_state,
                  changed=sorted(changed), deleted=deleted, upserts=upserts, dir_map=dirs, notes=notes,
                  info={"mode": "usn", "records": len(recs), "ms": ms})


# ============================================================================================ kind S (SQLite file)

def is_sqlite_file(path):
    try:
        with open(_lp(path), "rb") as fh:
            return fh.read(16) == SQLITE_MAGIC
    except OSError:
        return False


def sqlite_signature(path):
    """{part: [size, mtime_ns, ctime_ns] | None} of the file and its -wal (M4.11). A part that is not there is None."""
    sig = {}
    for part, p in (("db", path), ("wal", path + "-wal")):
        try:
            sig[part] = list(basic_info(p))
        except FileNotFoundError:
            sig[part] = None
        except OSError as e:
            if getattr(e, "winerror", None) in (2, 3):
                sig[part] = None
            else:
                raise
    return sig


def sqlite_signal(entry, st):
    path = entry["path"]
    try:
        sig = sqlite_signature(path)
    except OSError as e:
        return Result(UNKNOWN, "the SQLite file cannot be looked at (%s: %s)" % (e.__class__.__name__, e))
    new_state = {"sig": sig, "have_base": True}
    if not st.get("have_base"):
        return Result(UNKNOWN, "no base: the first copy", new_state)
    if sig == st.get("sig"):
        return Result(SAME, "signature of the file and the -wal unchanged", new_state)
    return Result(CHANGED, "the file or the -wal changed", new_state)


# ============================================================================================ kinds P and R (PostgreSQL)

def pg_signal(entry, st, settings, base, psql, timeout=15.0):
    """Counters of the database in `pg_stat_database` (any DIFFERENCE is `changed`, not only growth: a reset of the statistics or a restart of the
    server gives an extra copy, never a miss: M4.13), then the sessions of the database (M4.14): a session that is active, in an open transaction, whose
    `state_change` is later than the last check (the clock of the SERVER), or that cannot be seen -> `unknown` -> a copy. There is no waiting for the counters:
    the statistics of an open session reach the counters 7.5-8.7 s late (measured)."""
    from . import db as _db
    if not psql:
        return Result(UNKNOWN, "psql was not found: the counters of the database cannot be read", dead=True)
    lit = base.dbname.replace("'", "''")
    last = st.get("server_time") if st.get("have_base") else None
    last_clause = ("state_change > '%s'::timestamptz" % last.replace("'", "")) if last else "false"
    q = ("select coalesce((select tup_inserted||','||tup_updated||','||tup_deleted from pg_stat_database where datname='%(d)s'),'none'),"
         "to_char(clock_timestamp() at time zone 'UTC','YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"'),"
         "current_setting('track_counts'),"
         "(select count(*) from pg_stat_activity where datname='%(d)s' and backend_type='client backend' and pid<>pg_backend_pid()),"
         "(select count(*) from pg_stat_activity where datname='%(d)s' and backend_type='client backend' and pid<>pg_backend_pid() and "
         "(state is null or state in ('active','idle in transaction','idle in transaction (aborted)','fastpath function call') or %(last)s))"
         ) % {"d": lit, "last": last_clause}
    env = _db._child_env(base, timeout)
    env["PGDATABASE"] = "postgres"
    rc, out, err, timed = _db._run([psql, "-X", "-w", "-At", "-v", "ON_ERROR_STOP=1", "-F", "|", "-c", q], env, timeout)
    if timed:
        return Result(UNKNOWN, "the query to the database postgres did not finish in time")
    if rc != 0:
        return Result(UNKNOWN, "the counters cannot be read (%s)" % _db.mask_secrets(err.strip())[:200])
    parts = out.strip().split("|")
    if len(parts) != 5:
        return Result(UNKNOWN, "unexpected answer of psql: %r" % out[:80])
    tup_s, now_s, track, n_sess, n_unclear = parts
    if track.strip().lower() != "on":
        return Result(UNKNOWN, "track_counts = %s: the counters do not move, the signal is dead (M4.15)" % track,
                      {"server_time": now_s, "have_base": True, "tup": None}, dead=True, info={"track_counts": track})
    if tup_s == "none":
        return Result(UNKNOWN, "the database has no row in pg_stat_database", {"server_time": now_s, "have_base": True, "tup": None})
    tup = [int(x) for x in tup_s.split(",")]
    new_state = {"tup": tup, "server_time": now_s, "have_base": True}
    if not st.get("have_base") or st.get("tup") is None:
        return Result(UNKNOWN, "no base: the first copy", new_state)
    if tup != st.get("tup"):
        return Result(CHANGED, "the counters differ (%s -> %s)" % (st.get("tup"), tup), new_state)
    if int(n_unclear) > 0:
        return Result(UNKNOWN, "no change in the counters, but %s of %s sessions acted since the last check, are open in a transaction or cannot be seen" % (n_unclear, n_sess), new_state,
                      info={"sessions": int(n_sess), "unclear": int(n_unclear)})
    return Result(SAME, "counters and sessions unchanged", new_state, info={"sessions": int(n_sess)})
