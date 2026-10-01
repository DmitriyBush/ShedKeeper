r"""store.py -- the store on a volume: labels, a PERSONAL folder per user, copies of versions, trash, index,
search, restore.

Layout (one shared folder per VOLUME, outside any project, so `git clean -x` of a project cannot reach it;
inside it one personal folder per user, the way the Windows recycle bin keeps a folder per user on every disk):

  <X>:\<store_dir_name>\                      the volume folder: default permissions, label (JSON: product, created)
      .shedkeeper-store
      <login name>\                          the PERSONAL folder: only that user and SYSTEM (private_store = true)
          .shedkeeper-user                   label (JSON: product, sid, login, full_name, created)
          trash\<YYYY-MM-DD>\<DRIVE>\<original path without the letter>
          trash\_index\<YYYY-MM-DD>.jsonl    one line per move (id t:<day>:<line>)
          trash\_index\_orphans.jsonl        records that could not go to the day index (id t:orphans:<line>)
          versions\<YYYY-MM-DD>\<DRIVE>\<folders>\<name>.v_<HHMMSS>_<n><ext>
          versions\_index\<YYYY-MM-DD>.jsonl one line per copy (id v:<day>:<line>)
          transcripts\<project folder>\<session>.jsonl
          log.jsonl                           one line per hook firing

store_root() is the PERSONAL folder: everything the tool writes goes there. Moving into the trash is a
rename: same volume, atomic, no copy, and the moved item keeps its own permissions. Nothing here ever
deletes: there is NO command that empties the store -- only a person cleans it (for example by removing
old day folders).

Permissions. Both folders are CREATED closed (CreateDirectoryW with a security descriptor -- there is no moment
at which they exist with other rights): the personal folder to the user and SYSTEM only (private_store), the
volume folder the way the Windows recycle bin is -- others may list it, make their OWN folder and read the label,
nothing more. The personal folder is judged MINE by its owner and label (and not being a link); its permissions
are READ and reported (private_state) but do not decide whose it is. Who I am: the SID of the process token --
no program is started anywhere on the hook path.

Environment: SHEDKEEPER_STORE_ROOT names ONE VOLUME folder for every volume (used by the tests; the volume
type check is skipped for it); the personal folder is created under it with the permissions of real life.
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

LABEL = ".shedkeeper-store"             # in the volume folder
USER_LABEL = ".shedkeeper-user"         # in the personal folder
SYSTEM_SID = "S-1-5-18"
AUTH_USERS_SID = "S-1-5-11"       # Authenticated Users
CREATOR_OWNER_SID = "S-1-3-0"     # replaced by the owner of what is made inside a folder that carries it
ADMINS_SID = "S-1-5-32-544"      # the owner of what an elevated administrator creates
README_NAME = "README.txt"
_SID_RE = re.compile(r"S-1-\d+(?:-\d+)+")
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


def _is_mine(root):
    """Is `root` a personal folder of ours that belongs to THIS account (label ours with my SID, owner is me)?"""
    return judge_personal(root)[0]


def log(root, rec):
    """One line in <root>/log.jsonl -- only in a personal folder that is ours and this account's: a folder
    that is refused (another account's, unlabelled, damaged) is not written into, not even by the log. A
    failure of the log never changes anything else."""
    try:
        if root and os.path.isdir(root) and _is_mine(root):
            with open(os.path.join(root, "log.jsonl"), "ab") as fh:
                fh.write(json.dumps(rec).encode("utf-8") + b"\n")
    except Exception:
        pass


# ---------------------------------------------------------------- the store itself

def env_root():
    r = os.environ.get("SHEDKEEPER_STORE_ROOT")
    return os.path.abspath(r) if r else None


_IDENT = {}


def _login():
    """The login name of the user of this process: GetUserNameW, NOT an environment variable (a variable
    can be set to anything by whoever starts the process). '' when it cannot be read."""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(257)
        size = ctypes.c_ulong(len(buf))
        if ctypes.windll.advapi32.GetUserNameW(buf, ctypes.byref(size)):
            return buf.value
    except Exception:
        pass
    return ""


def _token_user_sid():
    """The SID (string) of the user of THIS process, from the process token: OpenProcessToken +
    GetTokenInformation(TokenUser) + ConvertSidToStringSidW. No program is started, no environment variable
    and no PATH is consulted. Raises OSError."""
    import ctypes
    import ctypes.wintypes as wt
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    vp = ctypes.c_void_p
    k32.GetCurrentProcess.restype = wt.HANDLE
    k32.CloseHandle.argtypes = [wt.HANDLE]
    k32.LocalFree.argtypes = [vp]
    k32.LocalFree.restype = vp
    adv.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
    adv.OpenProcessToken.restype = wt.BOOL
    adv.GetTokenInformation.argtypes = [wt.HANDLE, ctypes.c_int, vp, wt.DWORD, ctypes.POINTER(wt.DWORD)]
    adv.GetTokenInformation.restype = wt.BOOL
    adv.ConvertSidToStringSidW.argtypes = [vp, ctypes.POINTER(wt.LPWSTR)]
    adv.ConvertSidToStringSidW.restype = wt.BOOL
    tok = wt.HANDLE()
    if not adv.OpenProcessToken(k32.GetCurrentProcess(), 0x0008, ctypes.byref(tok)):       # TOKEN_QUERY
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        need = wt.DWORD()
        adv.GetTokenInformation(tok, 1, None, 0, ctypes.byref(need))                       # TokenUser: the size
        if not need.value:
            raise ctypes.WinError(ctypes.get_last_error())
        buf = ctypes.create_string_buffer(need.value)
        if not adv.GetTokenInformation(tok, 1, buf, need, ctypes.byref(need)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid_ptr = ctypes.c_void_p.from_buffer(buf).value          # TOKEN_USER starts with the SID pointer
        s = wt.LPWSTR()
        if not adv.ConvertSidToStringSidW(sid_ptr, ctypes.byref(s)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return s.value
        finally:
            k32.LocalFree(ctypes.cast(s, vp))
    finally:
        k32.CloseHandle(tok)


def _lookup_sid():
    """(sid, why): the SID of this user from the process token (see _token_user_sid). (None, reason) when it
    cannot be had -- never an exception."""
    try:
        sid = _token_user_sid()
    except Exception as e:
        return None, "the process token could not be read (%s: %s)" % (e.__class__.__name__, e)
    if not sid or not _SID_RE.match(sid):
        return None, "the process token gave no usable SID (%r)" % (sid,)
    return sid, ""


def _full_name(login):
    """The full name of the account (NetUserGetInfo, level 10); '' when there is none or it cannot be had
    -- that is not a refusal."""
    try:
        import ctypes
        import ctypes.wintypes as wt

        class UI10(ctypes.Structure):
            _fields_ = [("name", wt.LPWSTR), ("comment", wt.LPWSTR), ("usr_comment", wt.LPWSTR),
                        ("full_name", wt.LPWSTR)]

        buf = ctypes.c_void_p()
        rc = ctypes.windll.netapi32.NetUserGetInfo(None, login, 10, ctypes.byref(buf))
        if rc != 0 or not buf.value:
            return ""
        try:
            return (ctypes.cast(buf, ctypes.POINTER(UI10)).contents.full_name or "").strip()
        finally:
            ctypes.windll.netapi32.NetApiBufferFree(buf)
    except Exception:
        return ""


def login_name():
    """The login name (cached for the life of the process)."""
    if "login" not in _IDENT:
        _IDENT["login"] = _login()
    return _IDENT["login"]


def identity(full_name=False):
    """{'login', 'sid', 'why'} of the user of this process, cached for the life of the process (a hook is a
    new process every time). 'sid' is None when it cannot be found, and 'why' says why: without the number a
    personal folder can be neither closed nor recognised. The full name of the account is looked up ONLY
    when asked for (`full_name=True`; it is written to a label, shown by `status`): the hook path never
    needs it."""
    if "ident" not in _IDENT:
        login = login_name()
        sid, why = _lookup_sid()
        if not login:
            why = (why + "; " if why else "") + "the login name cannot be read"
        _IDENT["ident"] = {"login": login, "sid": sid, "why": why}
    ident = _IDENT["ident"]
    if full_name and "full_name" not in ident:
        ident["full_name"] = _full_name(ident["login"]) if ident["login"] else ""
    return ident


def reset_identity():
    """Forget the cached identity (the tests, which change who is asking)."""
    _IDENT.clear()


def volume_root(path, settings):
    """The shared volume folder for the volume of `path`; None when the path has no drive letter."""
    er = env_root()
    if er:
        return er
    drive = os.path.splitdrive(os.path.abspath(path or ""))[0]
    if not DRIVE_RE.match(drive):
        return None
    return drive.upper() + os.sep + settings["store_dir_name"]


def personal_names(login):
    """The names a personal folder of this user may have, in the order they are tried: <login>, <login>.2 ..."""
    return [login] + ["%s.%d" % (login, n) for n in range(2, MAX_SUFFIX + 1)]


def resolve_personal(vol, login, settings):
    """(mine, free, skipped) for the volume folder `vol`: `mine` -- every folder of this user in the order of
    the names; `free` -- the first name nobody uses (or None); `skipped` -- [(path, why)] for the names taken by
    something that is NOT this user's store (another account's folder, a forged label, an unlabelled folder).
    Nothing is touched. A folder is the user's by judge_personal(): the label alone is a file anybody can write."""
    mine, free, skipped = [], None, []
    for name in personal_names(login):
        p = os.path.join(vol, name)
        if not os.path.lexists(p):
            free = free or p
            continue
        if not os.path.isdir(p):
            skipped.append((p, "it is not a folder"))
            continue
        ok, why = judge_personal(p, settings)
        if ok:
            mine.append(p)
        else:
            skipped.append((p, why))
    return mine, free, skipped


def store_root(path, settings):
    """THE root of everything the tool writes for `path`: the personal folder of this user in the volume folder
    -- <login name>, or <login name>.2, .3 ... when the plain name is taken by something that is not ours. The
    first folder that is ours; else the first free name (it will be created there); else, when all names up to
    the ceiling are taken, the last one (check_available refuses it). The one function that knows the layout.
    None when the path has no drive letter or the login name of this user cannot be read."""
    vol = volume_root(path, settings)
    login = login_name()
    if not vol or not login:
        return None
    free = None
    try:
        for name in personal_names(login):          # the first folder that is ours wins: nothing after it matters
            p = os.path.join(vol, name)
            if not os.path.lexists(p):
                free = free or p
            elif os.path.isdir(p) and judge_personal(p, settings)[0]:
                return p
    except OSError:
        return os.path.join(vol, login)
    return free or os.path.join(vol, personal_names(login)[-1])


def _volume_dirs(settings):
    """The existing volume folders of ours: the environment root, or <X>:\\<name> for every letter."""
    er = env_root()
    if er:
        try:
            return [er] if os.path.isdir(er) and label_state(er)[0] == "ours" else []
        except OSError:
            return []
    out = []
    for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        r = "%s:%s%s" % (c, os.sep, settings["store_dir_name"])
        try:
            if os.path.isdir(r) and label_state(r)[0] == "ours":
                out.append(r)
        except OSError:
            pass
    return out


def all_roots(settings):
    """Existing personal stores of THIS user: <volume folder>\\<login> (or <login>.2 ...) that judge_personal()
    accepts. A folder that belongs to another account, or only carries a label, is not listed."""
    login = login_name()
    sid = identity()["sid"]
    if not login or not sid:
        return []
    out = []
    for v in _volume_dirs(settings):
        try:
            out += resolve_personal(v, login, settings)[0]
        except OSError:
            pass
    return out


def _volume_candidates(settings):
    """Every EXISTING shared folder that might hold a store of ours, whatever its label says: the environment
    root, or <X>:\\<name> for every letter. Raises nothing: a folder that cannot be asked is returned too."""
    er = env_root()
    cands = [er] if er else ["%s:%s%s" % (c, os.sep, settings["store_dir_name"]) for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"]
    out = []
    for r in cands:
        try:
            if os.path.isdir(r):
                out.append(r)
        except OSError:
            out.append(r)
    return out


def unsearched(settings):
    """[(path or '', why)]: what `find` / `restore` / `status` did NOT look at although it may hold copies of
    THIS user -- so that 'nothing found' is never said about a place that was not looked at. (1) the SID or
    the login of this user is unknown: NOTHING was searched; (2) a shared folder whose label is damaged;
    (3) any folder of a shared folder that carries a label with MY SID but is not listed in all_roots(): its
    name is not <login>[.N], or its owner is not me, or it is a link ... (the reason from judge_personal).
    Folders that carry another SID are not mine and are not named. Names that begin with a dot are the
    temporary and rejected folders of a creation (leftovers() names them)."""
    login = login_name()
    ident = identity()
    if not login or not ident["sid"]:
        return [("", "nothing was searched: the %s of this user cannot be found (%s)" % (
            "SID" if not ident["sid"] else "login name", ident["why"] or "the login name cannot be read"))]
    out = []
    mine = {_norm(p) for p in all_roots(settings)}
    for v in _volume_candidates(settings):
        try:
            state, why = label_state(v)
            if state == "bad":
                out.append((v, "the label of this shared folder is unreadable (%s)" % why))
            entries = sorted(os.listdir(v))
        except OSError as e:
            out.append((v, "cannot be read (%s: %s)" % (e.__class__.__name__, e)))
            continue
        for n in entries:
            p = os.path.join(v, n)
            if n.startswith(".") or _norm(p) in mine:
                continue
            try:
                if not os.path.isdir(p):
                    continue
                st, rec = user_label_state(p)
            except OSError:
                continue
            if st != "ours" or rec["sid"].upper() != ident["sid"].upper():
                continue
            ok, why = judge_personal(p)
            out.append((p, why if not ok else "its name is not %s or %s.N (N = 2..%d)" % (login, login, MAX_SUFFIX)))
    return out


def unsearched_lines(settings):
    """`unsearched()` as text lines: 'NOT SEARCHED: <path> -- <why>'."""
    return ["NOT SEARCHED: %s%s" % ((p + " -- ") if p else "", why) for p, why in unsearched(settings)]


def leftovers(settings):
    """[path]: the temporary / rejected folders of a creation that are still lying in a shared folder of ours
    (`.<login>.part-*` after a lost race, `.<login>.rejected-*` after a failed acceptance). The tool never
    deletes them; `status` names them so that nobody reads them as garbage by mistake."""
    login = login_name()
    out = []
    if not login:
        return out
    for v in _volume_candidates(settings):
        try:
            for n in sorted(os.listdir(v)):
                if n.startswith("."):
                    base = n[1:]
                    if any(base.startswith(nm + ".part-") or base.startswith(nm + ".rejected-")
                           for nm in personal_names(login)) and os.path.isdir(os.path.join(v, n)):
                        out.append(os.path.join(v, n))
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


def user_label_state(root):
    """('ours', record) | ('none', None) | ('bad', why): the label of a personal folder."""
    m = os.path.join(root, USER_LABEL)
    if not os.path.lexists(m):
        return "none", None
    try:
        with open(m, encoding="utf-8") as fh:
            rec = json.load(fh)
    except (OSError, ValueError) as e:
        return "bad", "%s: %s" % (e.__class__.__name__, e)
    if not isinstance(rec, dict) or rec.get("product") != config.PRODUCT or not isinstance(rec.get("sid"), str) \
            or not rec["sid"]:
        return "bad", "not a label of this tool (no product or no SID)"
    return "ours", rec


# Putting a label in place: on Windows, replacing a file that another process replaces at that very moment
# answers "access denied" (PermissionError, WinError 5) -- measured: 12 hooks at once on an empty volume, 7
# refusals in 300 starts. That is a collision, not an unusable store, so the rename is tried again a few
# times. Budget: at most PLACE_TRIES tries, PLACE_PAUSE_S * try number between them (0.05 + 0.10 + ... + 0.35
# = 1.4 s at most over 7 pauses) -- far below the 60 s hook timeout (see cmd_deadline_s in shedkeeper.ini, which
# leaves ~30 s of margin before the creation-lock wait of CREATE_LOCK_WAIT_S, ~27 s after it). Only PermissionError
# is retried (a permanent one costs the whole 1.4 s before it is refused -- a declared limit); any other error is
# refused at once. After the last try the old refusal stands, with the reason.
PLACE_TRIES = 8
PLACE_PAUSE_S = 0.05


def _place(tmp, dst, op="replace"):
    """Put the temporary file `tmp` at `dst` with os.<op> ('replace' or 'rename'), trying again on PermissionError
    (see PLACE_TRIES). On failure the error is raised and OUR temporary file is removed (it is this tool's own
    file made a moment ago, never the person's); on success nothing of it is left."""
    for attempt in range(1, PLACE_TRIES + 1):
        try:
            getattr(os, op)(tmp, dst)
            return
        except PermissionError:
            if attempt == PLACE_TRIES:
                _drop_own_temp(tmp)
                raise
            time.sleep(PLACE_PAUSE_S * attempt)
        except OSError:
            _drop_own_temp(tmp)
            raise


def _drop_own_temp(tmp):
    try:
        os.unlink(tmp)
    except OSError:
        pass                # already gone or cannot be removed: the refusal carries the last error (the one raised)


def _write_json_label(root, name, rec):
    """A label file: written to a temporary name, then one rename puts it in place."""
    data = json.dumps(rec, indent=1).encode("utf-8")
    tmp = os.path.join(root, "%s.part-%d-%d" % (name, os.getpid(), time.time_ns()))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0))
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    _place(tmp, os.path.join(root, name))         # two processes writing the same label at once: the second replaces the first, no leftover


def _write_label(root):
    _write_json_label(root, LABEL, {"product": config.PRODUCT,
                                    "created": datetime.now().isoformat(timespec="seconds")})


def _write_user_label(root, ident):
    _write_json_label(root, USER_LABEL, {"product": config.PRODUCT, "sid": ident["sid"], "login": ident["login"],
                                         "full_name": ident["full_name"],
                                         "created": datetime.now().isoformat(timespec="seconds")})


# ---------------------------------------------------------------- permissions of the personal folder

def read_dacl(path):
    """(protected, aces): the permissions of `path` READ from the file system (advapi32 through ctypes) --
    never taken from what a tool reported. `protected` -- the DACL does not inherit from the parent folder
    (SE_DACL_PROTECTED); `aces` -- a list of (ace_type, ace_flags, mask, sid string), or None when the
    object has no DACL at all (then everybody has access). Raises OSError."""
    import ctypes
    import ctypes.wintypes as wt
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    vp = ctypes.c_void_p
    adv.GetNamedSecurityInfoW.argtypes = [wt.LPCWSTR, ctypes.c_int, wt.DWORD, vp, vp, ctypes.POINTER(vp), vp,
                                          ctypes.POINTER(vp)]
    adv.GetNamedSecurityInfoW.restype = wt.DWORD
    adv.GetSecurityDescriptorControl.argtypes = [vp, ctypes.POINTER(wt.WORD), ctypes.POINTER(wt.DWORD)]
    adv.GetSecurityDescriptorControl.restype = wt.BOOL
    adv.GetAclInformation.argtypes = [vp, vp, wt.DWORD, ctypes.c_int]
    adv.GetAclInformation.restype = wt.BOOL
    adv.GetAce.argtypes = [vp, wt.DWORD, ctypes.POINTER(vp)]
    adv.GetAce.restype = wt.BOOL
    adv.ConvertSidToStringSidW.argtypes = [vp, ctypes.POINTER(wt.LPWSTR)]
    adv.ConvertSidToStringSidW.restype = wt.BOOL
    k32.LocalFree.argtypes = [vp]
    k32.LocalFree.restype = vp

    class AclSizeInfo(ctypes.Structure):
        _fields_ = [("AceCount", wt.DWORD), ("AclBytesInUse", wt.DWORD), ("AclBytesFree", wt.DWORD)]

    class AceHead(ctypes.Structure):          # ACCESS_ALLOWED_ACE up to the SID
        _fields_ = [("AceType", ctypes.c_ubyte), ("AceFlags", ctypes.c_ubyte), ("AceSize", wt.WORD),
                    ("Mask", wt.DWORD)]

    dacl, sd = vp(), vp()
    rc = adv.GetNamedSecurityInfoW(path, 1, 4, None, None, ctypes.byref(dacl), None, ctypes.byref(sd))   # FILE, DACL
    if rc != 0:
        raise OSError(rc, "GetNamedSecurityInfoW failed (error %d)" % rc, path)
    try:
        control, rev = wt.WORD(), wt.DWORD()
        if not adv.GetSecurityDescriptorControl(sd, ctypes.byref(control), ctypes.byref(rev)):
            raise OSError(ctypes.get_last_error(), "GetSecurityDescriptorControl failed", path)
        protected = bool(control.value & 0x1000)                  # SE_DACL_PROTECTED
        if not dacl.value:
            return protected, None
        info = AclSizeInfo()
        if not adv.GetAclInformation(dacl, ctypes.byref(info), ctypes.sizeof(info), 2):   # AclSizeInformation
            raise OSError(ctypes.get_last_error(), "GetAclInformation failed", path)
        aces = []
        for i in range(info.AceCount):
            ace = vp()
            if not adv.GetAce(dacl, i, ctypes.byref(ace)):
                raise OSError(ctypes.get_last_error(), "GetAce failed", path)
            head = AceHead.from_address(ace.value)
            sid = None
            if head.AceType == 0:                                  # ACCESS_ALLOWED: the SID follows the mask
                s = wt.LPWSTR()
                if not adv.ConvertSidToStringSidW(ace.value + ctypes.sizeof(AceHead), ctypes.byref(s)):
                    raise OSError(ctypes.get_last_error(), "ConvertSidToStringSidW failed", path)
                try:
                    sid = s.value
                finally:
                    k32.LocalFree(ctypes.cast(s, vp))
            aces.append((head.AceType, head.AceFlags, head.Mask, sid))
        return protected, aces
    finally:
        k32.LocalFree(sd)


def read_owner(path):
    """The OWNER of `path` as a SID string, READ from the file system (a label file can be written by anybody;
    the owner of a folder cannot be given away without administrator rights). Raises OSError."""
    import ctypes
    import ctypes.wintypes as wt
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    vp = ctypes.c_void_p
    adv.GetNamedSecurityInfoW.argtypes = [wt.LPCWSTR, ctypes.c_int, wt.DWORD, ctypes.POINTER(vp), vp, vp, vp,
                                          ctypes.POINTER(vp)]
    adv.GetNamedSecurityInfoW.restype = wt.DWORD
    adv.ConvertSidToStringSidW.argtypes = [vp, ctypes.POINTER(wt.LPWSTR)]
    adv.ConvertSidToStringSidW.restype = wt.BOOL
    k32.LocalFree.argtypes = [vp]
    k32.LocalFree.restype = vp
    owner, sd = vp(), vp()
    rc = adv.GetNamedSecurityInfoW(path, 1, 1, ctypes.byref(owner), None, None, None, ctypes.byref(sd))  # FILE, OWNER
    if rc != 0:
        raise OSError(rc, "GetNamedSecurityInfoW (owner) failed (error %d)" % rc, path)
    try:
        if not owner.value:
            raise OSError(0, "the object has no owner", path)
        s = wt.LPWSTR()
        if not adv.ConvertSidToStringSidW(owner, ctypes.byref(s)):
            raise OSError(ctypes.get_last_error(), "ConvertSidToStringSidW failed", path)
        try:
            return s.value
        finally:
            k32.LocalFree(ctypes.cast(s, vp))
    finally:
        k32.LocalFree(sd)


def _volume_has_acls(path):
    """Does the volume that holds `path` keep permissions and owners (FILE_PERSISTENT_ACLS)? NTFS does; FAT and
    exFAT do not. Raises OSError when the volume cannot be asked."""
    import ctypes
    import ctypes.wintypes as wt
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetVolumePathNameW.argtypes = [wt.LPCWSTR, wt.LPWSTR, wt.DWORD]
    k32.GetVolumePathNameW.restype = wt.BOOL
    k32.GetVolumeInformationW.argtypes = [wt.LPCWSTR, wt.LPWSTR, wt.DWORD, ctypes.POINTER(wt.DWORD),
                                          ctypes.POINTER(wt.DWORD), ctypes.POINTER(wt.DWORD), wt.LPWSTR, wt.DWORD]
    k32.GetVolumeInformationW.restype = wt.BOOL
    p = os.path.abspath(path)
    while not os.path.lexists(p) and os.path.dirname(p) != p:
        p = os.path.dirname(p)
    vol = ctypes.create_unicode_buffer(1024)
    if not k32.GetVolumePathNameW(p, vol, len(vol)):
        raise ctypes.WinError(ctypes.get_last_error())
    flags = wt.DWORD()
    if not k32.GetVolumeInformationW(vol.value, None, 0, None, None, ctypes.byref(flags), None, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    return bool(flags.value & 0x00000008)               # FILE_PERSISTENT_ACLS


def judge_personal(path, settings=None):
    """(mine, why): is the existing folder `path` the personal store of THIS user? Recognised by the OWNER read
    from the file system, with the label as the second witness (it names the account; it proves nothing alone,
    since anybody can write a file): the folder is not a link or junction; the label is ours and holds my SID;
    the owner is my SID (or the Administrators group, which owns what an elevated administrator creates).
    The PERMISSIONS do not decide whether the folder is mine: a legitimate change of them (an administrator, a
    backup program, a group added by the person) leaves the folder the person's own -- the copies in it must
    stay findable -- and `private_state()` reports the deviation aloud (status, session start, install check).
    An owner that cannot be read: the label is believed ONLY on a volume that keeps no owners (exFAT); on a
    volume that does, an unreadable owner (access denied, a sharing error) is 'not mine'. `settings` is
    accepted for the callers' sake; nothing in it changes who a folder belongs to. Another account's folder, a
    forged label, an unlabelled or damaged folder -> (False, why); it is not touched."""
    if _is_link(path):
        return False, "it is a link or a junction, not a folder"
    state, rec = user_label_state(path)
    if state == "none":
        return False, "no %s label" % USER_LABEL
    if state == "bad":
        return False, "the label %s is unreadable (%s)" % (USER_LABEL, rec)
    sid = identity()["sid"]
    if not sid:
        return False, "the SID of this user cannot be found (%s)" % identity()["why"]
    if rec["sid"].upper() != sid.upper():
        return False, "its label holds another SID"
    try:
        owner = read_owner(path)
    except OSError as e:
        try:
            acls = _volume_has_acls(path)
        except OSError as e2:
            return False, "its owner cannot be read (%s) and the volume cannot be asked whether it keeps owners (%s)" % (e, e2)
        if acls:
            return False, "its owner cannot be read (%s) on a volume that keeps owners: the label alone is not believed" % e
        return True, ""                 # a volume without owners: nothing there to forge against
    if owner.upper() not in (sid.upper(), ADMINS_SID):
        return False, "its owner is %s, not this user" % owner
    return True, ""


def check_private(path, sid):
    """(ok, why): the DACL of `path`, read now, is protected and holds EXACTLY the ACCESS_ALLOWED entries for
    the SID given and SYSTEM (S-1-5-18) -- two entries, or one when the user IS SYSTEM. The proof of 'closed'."""
    try:
        protected, aces = read_dacl(path)
    except Exception as e:
        return False, "the permissions of %s could not be read (%s: %s)" % (path, e.__class__.__name__, e)
    if aces is None:
        return False, "the folder has no permission list at all (everybody has access)"
    if not protected:
        return False, "the folder inherits its permissions from the parent folder (they are not protected)"
    if any(a[0] != 0 for a in aces):
        return False, "the permission list holds an entry that is not 'allow' (types %s)" % sorted({a[0] for a in aces})
    got = sorted(a[3].upper() for a in aces)
    want = sorted({sid.upper(), SYSTEM_SID})
    if got != want:
        return False, "the permission list is %s, expected exactly %s" % (", ".join(got) or "empty", ", ".join(want))
    return True, ""


def private_sddl(sid):
    """The security descriptor of a CLOSED personal folder: protected (no inheritance), full control for the
    user and for SYSTEM -- one entry when they are the same."""
    return "D:P" + "".join("(A;OICI;FA;;;%s)" % x for x in dict.fromkeys([sid, SYSTEM_SID]))


def volume_sddl(sid):
    """The descriptor of the SHARED volume folder: protected; full control for its creator (on the folder itself),
    for CREATOR OWNER on whatever is made inside (so that the owner of a folder made there, a second user, can
    write in it when it inherits -- private_store = false), for SYSTEM and for the Administrators; for every
    other authenticated user only what the Windows recycle bin gives -- list, walk through, read attributes,
    make a SUBFOLDER (their own personal folder) -- on the folder itself, and read on the files inside (the
    label). Nobody else can write, rename or delete the label or another's folder."""
    full = "".join("(A;OICI;FA;;;%s)" % x for x in (CREATOR_OWNER_SID, SYSTEM_SID, ADMINS_SID))
    return "D:P(A;;FA;;;%s)" % sid + full + "(A;;0x1200ad;;;%s)(A;OIIO;GR;;;%s)" % (AUTH_USERS_SID, AUTH_USERS_SID)


def make_dir_with_sddl(path, sddl):
    """Create the folder `path` ALREADY carrying the security descriptor given as SDDL text (CreateDirectoryW
    with SECURITY_ATTRIBUTES): there is no moment at which it exists with other rights. FileExistsError when
    it exists; OSError otherwise. The descriptor is freed on every path."""
    import ctypes
    import ctypes.wintypes as wt
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    vp = ctypes.c_void_p

    class SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("nLength", wt.DWORD), ("lpSecurityDescriptor", vp), ("bInheritHandle", wt.BOOL)]

    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wt.LPCWSTR, wt.DWORD, ctypes.POINTER(vp),
                                                                         ctypes.POINTER(wt.DWORD)]
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wt.BOOL
    k32.CreateDirectoryW.argtypes = [wt.LPCWSTR, ctypes.POINTER(SECURITY_ATTRIBUTES)]
    k32.CreateDirectoryW.restype = wt.BOOL
    k32.LocalFree.argtypes = [vp]
    k32.LocalFree.restype = vp
    sd = vp()
    if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(sd), None):   # SDDL_REVISION_1
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        sa = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), sd, False)
        if not k32.CreateDirectoryW(_lp(path), ctypes.byref(sa)):
            err = ctypes.get_last_error()
            if err == 183:                                  # ERROR_ALREADY_EXISTS
                raise FileExistsError(errno.EEXIST, "the folder exists", path)
            raise ctypes.WinError(err)
    finally:
        k32.LocalFree(sd)


# add file, write EA, delete child, write attributes, DELETE, WRITE_DAC, WRITE_OWNER
_WRITE_BITS = 0x2 | 0x10 | 0x40 | 0x100 | 0x10000 | 0x40000 | 0x80000
_GENERIC_WRITE_BITS = 0x40000000 | 0x10000000                              # GENERIC_WRITE, GENERIC_ALL
_ADD_SUBDIR, _LIST_DIR, _TRAVERSE = 0x4, 0x1, 0x20


def check_volume_rights(path):
    """(ok, why): the shared volume folder `path`, its permissions READ now, is closed the way volume_sddl() makes
    it: protected; every authenticated user may list it, walk through it and make a SUBFOLDER in it, and may only
    READ what is inside (the label) -- no adding files, no deleting or renaming, no changing rights; and nothing
    is given to Everyone or to the group Users. (The creator's own entry cannot be told from any other single
    foreign SID; the Windows way of asking 'what may user X do' is not used -- there is no second user here.)"""
    try:
        protected, aces = read_dacl(path)
    except Exception as e:
        return False, "the permissions of %s could not be read (%s: %s)" % (path, e.__class__.__name__, e)
    if aces is None:
        return False, "the folder has no permission list at all (everybody has access)"
    if not protected:
        return False, "the folder inherits its permissions from the parent folder (they are not protected)"
    if any(a[0] != 0 for a in aces):
        return False, "the permission list holds an entry that is not 'allow'"
    au_dir = au_files = 0
    others = []
    for _typ, flags, mask, sid in aces:
        sid = (sid or "").upper()
        if sid == AUTH_USERS_SID:
            if flags & 0x08:                                    # INHERIT_ONLY: it speaks of the files inside
                au_files |= mask
            else:
                au_dir |= mask
            continue
        if sid in ("S-1-1-0", "S-1-5-32-545"):
            return False, "the folder gives access to %s (Everyone / Users)" % sid
        if sid not in (SYSTEM_SID, ADMINS_SID, CREATOR_OWNER_SID):
            others.append(sid)
    if len(others) > 1:
        return False, "more than one unknown entry with access: %s" % ", ".join(others)
    need = _ADD_SUBDIR | _LIST_DIR | _TRAVERSE
    if (au_dir & need) != need:
        return False, "authenticated users cannot make their own folder here (list/traverse/add-subfolder missing)"
    if au_dir & (_WRITE_BITS | _GENERIC_WRITE_BITS):
        return False, "authenticated users may write in the folder itself (mask 0x%x)" % au_dir
    if au_files & (_WRITE_BITS | _GENERIC_WRITE_BITS | _ADD_SUBDIR):
        return False, "authenticated users may write the files inside (mask 0x%x)" % au_files
    return True, ""


def volume_state(vol):
    """(closed, text): for `status`. A volume without permissions (exFAT) has nothing to close."""
    try:
        if not _volume_has_acls(vol):
            return True, "this volume keeps no permissions: nothing to close"
    except OSError as e:
        return False, "the volume cannot be asked about permissions (%s)" % e
    ok, why = check_volume_rights(vol)
    return ok, ("others can list it, make their own folder and read the label, nothing more" if ok else
                "NOT closed as a shared folder (%s) -- made by an earlier version or by hand: other users may be "
                "able to change the label or the folders in it" % why)


def private_state(root, settings):
    """(private, text): whether the personal folder `root` is closed, for `status`, the session start and
    `install check`. private_store = false -> (False, 'no (private_store = false)'); true -> the DACL is read
    and judged. A folder that is NOT closed stays the store: the tool never re-closes it on its own."""
    if not settings.get("private_store", True):
        return False, "no (private_store = false)"
    sid = identity()["sid"]
    if not sid:
        return False, "NO (the SID of this user cannot be found: %s)" % identity()["why"]
    ok, why = check_private(root, sid)
    if ok:
        return True, "yes"
    return False, ("NO (%s) -- it is still your store and copies go on being kept in it; the tool does not "
                   "change its permissions on its own" % why)


def describe(root, settings):
    """One line for `status` / `install check`: the owner, the folder, whether it is private."""
    ident = identity(full_name=True)
    who = ident["login"] + (" (%s)" % ident["full_name"] if ident["full_name"] else "")
    return "store of %s: %s -- private: %s" % (who, root, private_state(root, settings)[1])


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


def check_volume(root, settings):
    """(ok, why) without creating anything: can this VOLUME folder be used (now or after auto-creation)?"""
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


def ensure_volume(root, settings):
    """(ok, note). Make sure `root` is a usable VOLUME folder of ours, creating it when allowed. On success
    the note is a line to print when it was created or taken just now (else ''); on failure it is the
    refusal text. Nothing is created on a removable or network volume."""
    ok, why = check_volume(root, settings)
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
        _make_volume_dir(root)
        _write_label(root)
    except FileExistsError:
        return ensure_volume(root, settings)
    except OSError as e:
        return False, "could not create the store %s (%s: %s)" % (root, e.__class__.__name__, e)
    return True, "created the store %s (automatic; to turn off: auto_create_store = false in shedkeeper.ini)" % root


def _make_volume_dir(root):
    """Make the shared volume folder with its closed rights from the first moment (volume_sddl), then READ the
    rights back: a folder that did not come out closed is taken away (it is empty -- os.rmdir) and the creation
    fails with the reason. On a volume that keeps no permissions (exFAT) nothing can be closed and nothing is
    checked. Raises FileExistsError / OSError."""
    ident = identity()
    if not ident["sid"]:
        raise OSError(0, "the SID of this user cannot be found (%s): the shared folder cannot be made closed"
                      % ident["why"], root)
    parent = os.path.dirname(os.path.abspath(root))
    try:
        os.makedirs(_lp(parent), exist_ok=True)
    except FileExistsError:             # a parent is a file: NOT the lost race of ensure_volume (it would loop)
        raise OSError(errno.ENOTDIR, "a parent of the store folder is a file, not a folder", parent)
    make_dir_with_sddl(root, volume_sddl(ident["sid"]))
    if _volume_has_acls(root):
        ok, why = check_volume_rights(root)
        if not ok:
            try:
                os.rmdir(root)
                left = ""
            except OSError:
                left = " (and it could not be removed: look at it by hand)"
            raise OSError(0, "the shared folder did not come out closed: %s%s" % (why, left), root)


def _taken_text(root, why):
    """The refusal for an existing folder `root` that is not this user's store: nothing was touched."""
    login = identity()["login"]
    if os.path.basename(root) == personal_names(login)[-1]:
        return ("every name %s ... %s in %s is taken by something that is not this user's store (the last: %s); "
                "nothing was touched. Look at those folders by hand, or choose another folder name: "
                "store_dir_name in shedkeeper.ini" % (login, personal_names(login)[-1], os.path.dirname(root), why))
    return "%s is not this user's store (%s); nothing was touched" % (root, why)


def check_available(root, settings):
    """(ok, why) without creating anything: can this PERSONAL store (`root` from store_root) be used, now
    or after auto-creation? An existing folder is judged by judge_personal() (label, owner, permissions);
    one that is not ours is refused and left as it is."""
    if not root:
        return False, "the path has no drive letter, or the login name of this user cannot be read"
    ok, why = check_volume(os.path.dirname(root), settings)
    if not ok:
        return False, why
    if os.path.lexists(root):
        if not os.path.isdir(root):
            return False, "%s exists and is not a folder" % root
        ok, why = judge_personal(root, settings)
        return (True, "") if ok else (False, _taken_text(root, why))
    ident = identity()
    if not ident["sid"]:
        return False, ("no personal folder %s can be made: the SID of this user cannot be found (%s) -- without "
                       "it the folder can neither be closed nor recognised" % (root, ident["why"]))
    return True, ""


def _readme_text(root, ident, settings, number, skipped, created):
    """The text of README.txt in the personal folder: for the person who opens the folder."""
    private = settings.get("private_store", True)
    who = ident["login"] + (" (%s)" % ident["full_name"] if ident["full_name"] else "")
    kit_py = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shedkeeper.py")
    lines = [
        "ShedKeeper -- the personal store of %s" % who,
        "",
        "What this folder is: before an AI agent changes one of your files, ShedKeeper keeps a copy of it here;",
        "what an agent \"deletes\" is moved to the trash folder here instead of being destroyed. Nothing is",
        "ever removed from this folder by the tool.",
        "",
        "Whose it is: %s. Created %s." % (who, created),
        "Closed to other users: %s" % (
            "yes -- only this user and the system can open it (the permissions were read back and checked)."
            if private else "NO -- private_store = false in shedkeeper.ini: the permissions are those of the "
                            "parent folder."),
    ]
    if number > 1:
        lines += ["Why the name has a number: the name %s was already taken by a folder that is not this user's "
                  "store (%s)." % (ident["login"], "; ".join("%s: %s" % (os.path.basename(p), w)
                                                              for p, w in skipped[:3]) or "see the folder")]
    lines += [
        "",
        "To find something or bring it back: ask your agent -- it finds the item, tells you what it was and",
        "why it is here, and brings it back after your word. By hand: python \"%s\" find <part of the path>," % kit_py,
        "then python \"%s\" restore <id>." % kit_py,
        "",
        "Please do not delete anything here by hand unless you are sure. Cleaning this folder is your decision",
        "and nobody else's: the tool never does it.",
        "",
    ]
    return "\r\n".join(lines)


def _write_readme(root, text):
    """README.txt, UTF-8 with a BOM (Notepad shows a Cyrillic login name right). (ok, error text)."""
    try:
        data = b"\xef\xbb\xbf" + text.encode("utf-8")
        tmp = os.path.join(root, "%s.part-%d-%d" % (README_NAME, os.getpid(), time.time_ns()))
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0))
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        _place(tmp, os.path.join(root, README_NAME), "rename")
        return True, ""
    except OSError as e:
        return False, "%s: %s" % (e.__class__.__name__, e)


def _is_reparse(p):
    """Is `p` a reparse point of ANY kind (junction, symbolic link, mount point, cloud placeholder ...)?"""
    try:
        return bool(getattr(os.lstat(_lp(p)), "st_file_attributes", 0) & 0x400)     # FILE_ATTRIBUTE_REPARSE_POINT
    except OSError:
        return False


def _verify_fresh(path, sid, private):
    """(ok, why): the personal folder just made, now under its own name, is what we made: not a link, judged as
    ours, closed when it must be, and holding ONLY what this tool makes -- the label, README.txt, the temporary
    name of one of them, and what the other hooks of this user may have put in a moment after the rename (found
    by a race of six parallel hooks: log.jsonl, trash, versions, transcripts) -- with no reparse point among
    them: a junction under one of those names is exactly what is looked for."""
    if _is_link(path) or _is_reparse(path):
        return False, "it is a link or a reparse point, not a folder"
    ok, why = judge_personal(path)
    if not ok:
        return False, why
    if private:
        ok, why = check_private(path, sid)
        if not ok:
            return False, why
    try:
        names = os.listdir(_lp(path))
    except OSError as e:
        return False, "its content cannot be listed (%s)" % e
    own = (USER_LABEL, README_NAME, "log.jsonl", "trash", "versions", "transcripts")
    for n in names:
        if n not in own and not n.startswith((USER_LABEL + ".part-", README_NAME + ".part-")):
            return False, "it holds an entry that is not ours: %r" % n
        if _is_reparse(os.path.join(path, n)):
            return False, "it holds a reparse point: %r" % n
    return True, ""


def _create_personal(root, settings):
    """(ok, note, report): make the personal folder `root`. Atomic and closed from the first moment: a temporary
    folder in the volume folder CREATED already carrying its security descriptor (CreateDirectoryW; with
    private_store -- protected, the user and SYSTEM only), so there is no window in which another user can reach
    it -> the permissions READ BACK and judged -> the folder is required to be EMPTY (nothing planted by
    anybody) -> label -> README.txt -> one rename -> the folder under its own name is judged AGAIN and its
    content checked (_verify_fresh); one that fails is renamed out of the way (never deleted) and named.
    Any step that fails: no store, the reason in the text; the temporary folder is removed with os.rmdir ONLY
    when it is empty, otherwise it is left and named. `report` is a dict for the person (see creation_report)
    or None."""
    ident = identity(full_name=True)
    vol, name = os.path.split(root)
    login = ident["login"]
    number = personal_names(login).index(name) + 1 if name in personal_names(login) else 1
    _mine, _free, skipped = resolve_personal(vol, login, settings)
    private = settings.get("private_store", True)
    tmp = os.path.join(vol, ".%s.part-%d-%d" % (name, os.getpid(), time.time_ns()))
    try:
        if private:
            make_dir_with_sddl(tmp, private_sddl(ident["sid"]))
        else:
            os.mkdir(tmp)                   # the permissions of the volume folder, inherited (the person's choice)
    except OSError as e:
        return False, ("the personal folder %s was NOT made: it could not be created%s (%s: %s)"
                       % (root, " closed" if private else "", e.__class__.__name__, e)), None

    def fail(text):
        try:
            os.rmdir(tmp)
            left = ""
        except OSError:
            left = "; the temporary folder %s was left (it is not empty or cannot be removed) -- look at it by hand" % tmp
        return False, text + left, None

    if private:
        ok, why = check_private(tmp, ident["sid"])
        if not ok:
            return fail("the personal folder %s was NOT made: it did not come out closed to other users; %s. To keep "
                        "the store on a volume without permissions (for example exFAT) set private_store = false in "
                        "shedkeeper.ini" % (root, why))
    try:
        planted = os.listdir(_lp(tmp))
    except OSError as e:
        return fail("the personal folder %s was NOT made: the new folder cannot be listed (%s: %s)"
                    % (root, e.__class__.__name__, e))
    if planted:
        return fail("the personal folder %s was NOT made: the new folder was not empty (%s) -- somebody put "
                    "something into it" % (root, ", ".join(repr(n) for n in planted[:5])))
    try:
        _write_user_label(tmp, ident)
    except OSError as e:
        return fail("the personal folder %s was NOT made: its label could not be written (%s: %s)"
                    % (root, e.__class__.__name__, e))
    created = datetime.now().isoformat(timespec="seconds")
    readme_ok, readme_err = _write_readme(tmp, _readme_text(root, ident, settings, number, skipped, created))
    try:
        os.rename(tmp, root)
    except OSError as e:
        ok, _why = judge_personal(root) if os.path.isdir(root) else (False, "")
        if ok:              # another process of this user made it a moment ago: it is the same folder
            return True, ("the personal folder %s was made by another process at the same moment; %s is a "
                          "leftover (an empty store with a label: yours to remove)" % (root, tmp)), None
        return fail("the personal folder %s was NOT made: the rename failed (%s: %s)" % (root, e.__class__.__name__, e))
    ok, why = _verify_fresh(root, ident["sid"], private)
    if not ok:
        away = os.path.join(vol, ".%s.rejected-%d-%d" % (name, os.getpid(), time.time_ns()))
        try:
            os.rename(root, away)
            where = "it was renamed to %s (nothing was deleted)" % away
        except OSError as e:
            where = "it could NOT be renamed out of the way (%s: %s) and stays at %s" % (e.__class__.__name__, e, root)
        return False, "the personal folder %s was NOT accepted after the rename: %s; %s" % (root, why, where), None
    report = {"path": root, "name": name, "login": login, "full_name": ident["full_name"], "number": number,
              "private": bool(private), "created": created, "skipped": [(os.path.basename(p), w) for p, w in skipped],
              "readme": readme_ok, "readme_error": readme_err}
    log(root, {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": "store_created", "path": root, "name": name,
               "number": number, "private": bool(private), "readme": readme_ok})
    return True, "created the personal folder %s (%s)" % (
        root, "private: only this user and SYSTEM" if private
        else "private_store = false: the permissions are those of the volume folder"), report


def creation_report(rep):
    """ONE paragraph for the person (systemMessage of a hook): where the store was made, who it is closed
    to, why the name has a number, how to switch the closing off. Plain text."""
    if not rep:
        return ""
    who = rep["login"] + (" (%s)" % rep["full_name"] if rep["full_name"] else "")
    text = "ShedKeeper made a personal store for %s: %s. " % (who, rep["path"])
    if rep["private"]:
        text += "It is closed to other users of this computer: only you and the system can open it (the permissions were read back and checked). "
    else:
        text += "It is NOT closed to other users: private_store = false in shedkeeper.ini, so the permissions are those of the parent folder. "
    if rep["number"] > 1:
        why = "; ".join("%s: %s" % (n, w) for n, w in rep["skipped"][:2])
        text += "The name has the number %d because the name %s is taken by a folder that is not this user's store (%s). " % (
            rep["number"], rep["login"], why or "another account's folder")
    if rep["private"]:
        text += "To switch the closing off: private_store = false in shedkeeper.ini. "
    text += "Inside: README.txt tells what this folder is" + ("" if rep["readme"] else " (it could NOT be written: %s)" % rep["readme_error"]) + ", copies are kept before changes, deleted files go to the trash here, and nothing is ever cleaned by the tool."
    return text


# The FIRST creation of a store is several steps (the volume folder, its label, the personal folder, its label, the
# README, one rename) and a second hook that starts in the middle of them sees half a store: a label that is being
# replaced right now (unreadable for a moment), or a volume folder that is made but not yet labelled (taken for someone
# else's). Retrying each read cannot close that; taking turns can. So the slow path -- anything that is not a ready
# store -- runs under ONE named mutex per volume folder (in the logon session of the user: `Local\`). Measured on the authors' machine
# (the staging findings, 12 hooks x 25 rounds): 12 refusals in 300 starts even with the retry of _place.
# The wait is bounded: CREATE_LOCK_WAIT_S = 3 s, far below the 60 s hook timeout (cmd_deadline_s in shedkeeper.ini
# already counts 20 s of copies + 10 s of git against it, which leaves about 30 s; the wait comes out of that margin,
# so about 27 s are left, less 1.4 s of the _place retries -- and a real creation takes milliseconds, so 3 s is
# many times what it needs). The wait is OUTSIDE the cmd_deadline_s and git budgets, which is why it is kept short:
# a hook that times out lets the action through WITHOUT a copy (the forbidden side). A mutex that cannot be made or
# taken in time is NOT a refusal by itself: the old way (without turns) is gone through, and the log says so; at
# worst that way refuses (the safe side).
# Declared limits (each degrades to the old way with a note, none is silent):
#  - the name comes from the TEXT of the path (normcase + abspath): `subst`, a junction or an 8.3 short name of the
#    same folder give DIFFERENT mutexes, so hooks that reach the volume folder by different spellings are not
#    ordered against each other (the label retry of _place still covers part of it);
#  - an elevated process and then an ordinary one: the mutex made by the elevated one may refuse access to the
#    ordinary one -> OpenMutex/CreateMutex fails -> the way without turns, with the note;
#  - `Local\` is per logon session: two users in different sessions are not ordered against each other (their
#    personal folders differ; the shared volume folder keeps its earlier protection, the label retry);
#  - the window between the rename of the label and _verify_fresh is outside the mutex (it was before too); the
#    measure "0 of 600" does not cover it.
CREATE_LOCK_WAIT_S = 3.0
_WAIT_OBJECT_0, _WAIT_ABANDONED, _WAIT_TIMEOUT = 0x0, 0x80, 0x102


def _lock_name(vol):
    """The name of the mutex for the volume folder `vol`: product + a hash of its normalised path (a path cannot be a
    mutex name; two folders must not share one -- the tests make many)."""
    key = os.path.normcase(os.path.abspath(vol)).encode("utf-8", "surrogatepass")
    return "Local\\%s-store-%s" % (config.PRODUCT, hashlib.sha1(key).hexdigest()[:16])


def _open_mutex(name):
    """The handle of the named mutex `name` (made if it does not exist; not owned yet). Raises OSError."""
    import ctypes
    import ctypes.wintypes as wt
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
    k32.CreateMutexW.restype = wt.HANDLE
    h = k32.CreateMutexW(None, False, name)
    if not h:
        raise ctypes.WinError(ctypes.get_last_error())
    return h


def _wait_mutex(h, ms):
    """WaitForSingleObject on the mutex handle: 0 owned, 0x80 owned but the old owner died, 0x102 time is out,
    0xFFFFFFFF failed."""
    import ctypes
    import ctypes.wintypes as wt
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
    k32.WaitForSingleObject.restype = wt.DWORD
    return k32.WaitForSingleObject(h, int(ms))


def _release_mutex(h, owned):
    import ctypes
    import ctypes.wintypes as wt
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.ReleaseMutex.argtypes = [wt.HANDLE]
    k32.CloseHandle.argtypes = [wt.HANDLE]
    if owned:
        k32.ReleaseMutex(h)
    k32.CloseHandle(h)


class _CreationLock(object):
    """`with _creation_lock(vol) as lk:` -- `lk.held` is True when this thread owns the mutex; `lk.note` is '' or the
    reason the turns were not taken (the work goes on without them). The mutex belongs to the THREAD that took it."""

    def __init__(self, vol, wait_s):
        self.vol, self.wait_s, self.held, self.note, self._h = vol, wait_s, False, "", None

    def __enter__(self):
        try:
            self._h = _open_mutex(_lock_name(self.vol))
        except Exception as e:
            self.note = "the creation lock could not be made (%s: %s)" % (e.__class__.__name__, e)
            return self
        try:
            rc = _wait_mutex(self._h, self.wait_s * 1000)
        except Exception as e:
            rc = e
        if rc in (_WAIT_OBJECT_0, _WAIT_ABANDONED):
            self.held = True
            if rc == _WAIT_ABANDONED:
                self.note = "the creation lock was left by a process that died in the middle (the state is checked again)"
        else:
            self.note = "the creation lock was not got in %.0f s (%s)" % (
                self.wait_s, "time is out" if rc == _WAIT_TIMEOUT else "wait failed: %r" % (rc,))
            _release_mutex(self._h, False)
            self._h = None
        return self

    def __exit__(self, *exc):
        if self._h is not None:
            try:
                _release_mutex(self._h, self.held)
            except Exception:
                pass
            self._h = None
        return False


def _creation_lock(vol, wait_s=None):
    return _CreationLock(vol, CREATE_LOCK_WAIT_S if wait_s is None else wait_s)


def _store_ready(root, settings):
    """Is everything there already -- the volume folder is ours and the personal folder is this user's? The fast
    path: no mutex. Any doubt (an error, a label unreadable for a moment) is 'not ready' -> the slow path, which
    judges it again under the mutex."""
    try:
        return bool(root) and os.path.isdir(root) and label_state(os.path.dirname(root))[0] == "ours" \
            and judge_personal(root, settings)[0]
    except Exception:
        return False


def ensure_store_report(root, settings):
    """(ok, note, report). As ensure_store, and `report` is the creation report dict when the personal folder
    was made in THIS call (else None): the hooks turn it into the message for the person. A store that is already
    ready costs no mutex; anything else is checked and made under the creation lock of the volume folder."""
    if _store_ready(root, settings):
        return True, "", None
    if not root:
        return _ensure_store_locked(root, settings)
    with _creation_lock(os.path.dirname(root)) as lk:
        result = _ensure_store_locked(root, settings)
    if lk.note:                         # the folder exists by now when it could be made; log() judges it itself
        log(root, {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": "creation_lock", "note": lk.note})
        if not result[0]:               # at the first creation there is no journal yet: the refusal itself carries the reason
            result = (False, "%s (%s)" % (result[1], lk.note), result[2])
    return result


def _ensure_store_locked(root, settings):
    ok, why = check_available(root, settings)
    if not ok:
        return False, why, None
    vol = os.path.dirname(root)
    ok, note = ensure_volume(vol, settings)
    if not ok:
        return False, note, None
    notes = [note] if note else []
    report = None
    if not os.path.isdir(root):
        ok, note, report = _create_personal(root, settings)
        if not ok:
            return False, note, None
        if note:
            notes.append(note)
    return True, "\n".join(notes), report


def ensure_store(root, settings):
    """(ok, note). Make sure the PERSONAL store `root` (from store_root) is usable, creating the volume
    folder and the personal folder when allowed. On success the note is a line to print when something was
    created or taken just now (else ''); on failure it is the refusal text."""
    ok, note, _report = ensure_store_report(root, settings)
    return ok, note


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
    if root is None:
        return "refused", "REFUSED no store folder can be named for this path (the login name of this user cannot be read): %s" % src, {}
    if is_store_path(src, settings) or _within(src, os.path.dirname(root)):
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
            # the record says "same"; the COPY is read now (size + sha256 against the source): a copy that was
            # damaged, replaced or emptied since is not a copy of this content -- a new one is taken
            good, why = _verify_copy(src, lc)
            if good:
                res.update(status="same", copy=lc, id=last.get("id"),
                           msg="the last copy of this file already has this content")
                return res
            log(root, {"time": now.isoformat(timespec="seconds"), "event": "last_copy_mismatch", "src": src,
                       "copy": lc, "why": why[:200]})
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
    kind ('trash'|'version'), id, time, src, where, present, restored, reason, session, via, root."""
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
                                     "session": rec.get("session", ""), "via": rec.get("via", ""),
                                     "root": root})
    hits.sort(key=lambda h: h["time"])
    problems += unsearched_lines(settings)         # 'nothing found' must not be said about a place not looked at
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
    """Bring one item back by its id. Returns (status, message): 'ok' | 'refused' | 'unindexed' | 'blind'
    ('blind': the id was not found, and some places that may hold copies of this user were NOT searched --
    the message names them). When the item is brought back and some places were not searched, the message
    says so after the result.
    A trash item comes back by a rename and is refused when the place is occupied (a restore never
    overwrites; --to names another place). A version comes back as a copy verified by sha256; the
    file that is there now goes to the trash FIRST (reason 'replaced by restore'), and only after
    the verification of the copy."""
    status, msg = _restore(item_id, settings, to, session, volume, now)
    if status == "refused" and msg.startswith("REFUSED no readable record"):
        lost = unsearched_lines(settings)
        if lost:
            return "blind", msg + "; " + "; ".join(lost)
    elif status in ("ok", "unindexed"):
        lost = unsearched_lines(settings)
        if lost:
            msg += " | " + "; ".join(lost)
    return status, msg


def _restore(item_id, settings, to, session, volume, now):
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
    if _within(target, os.path.dirname(root)):
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
            os.rename(_lp(dst), _lp(target))        # not os.replace: a place taken since the check above is refused, not overwritten
        except FileExistsError:
            return "refused", ("REFUSED the place was taken while restoring: %s -- nothing changed; the item is "
                               "still at %s; give another place with --to PATH" % (target, dst))
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
        os.rename(_lp(part), _lp(target))           # not os.replace: it would overwrite a file that appeared since the check
    except OSError as e:
        taken = isinstance(e, FileExistsError)
        back = ""
        if displaced:
            try:
                os.rename(_lp(displaced), _lp(target))
                back = "; the displaced file was put back"
            except OSError as e2:
                back = "; the displaced file could NOT be put back (%s) -- it is at %s" % (e2, displaced)
        if taken:
            return "refused", ("REFUSED the place was taken while restoring: %s -- the file that appeared there was not "
                               "touched%s; the verified copy is at %s" % (target, back, part))
        return "refused", "REFUSED the final rename failed (%s: %s)%s; the verified copy is at %s" % (e.__class__.__name__, e, back, part)
    okrec, where = _record_restore(idx, orphans, rec_make({"from": copy, "displaced": displaced}))
    note = "; the file that was there is in the trash: %s" % displaced if displaced else ""
    if not okrec:
        return "unindexed", "RESTORED BUT NOT RECORDED (%s); the file is at %s%s" % (where, target, note)
    return "ok", "OK restored the version %s -> %s%s" % (copy, target, note)


# ---------------------------------------------------------------- transcripts

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}\Z")


def save_transcript(src, root, project_name, session_id):
    """Copy a transcript into the store unless a copy at least as long is there. The copy goes to a
    temporary name and one os.replace puts it in place. Returns 'copied' | 'same' | 'failed:<why>'."""
    try:
        # the id comes from the hook input (or a file name) and becomes a file name: only a plain one is taken, so a
        # `..\..` or a drive in it cannot put the copy outside <root>/transcripts
        if not isinstance(session_id, str) or not SESSION_ID_RE.match(session_id):
            return "failed:the session id is not a plain name ([A-Za-z0-9_-], 1-128 characters), the transcript is not copied: %r" % (str(session_id)[:60],)
        dst = os.path.join(root, "transcripts", project_name, session_id + ".jsonl")
        if not _within(dst, os.path.join(root, "transcripts")):
            return "failed:the copy would land outside the transcripts folder of the store: %s" % dst
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
    out = {"root": root, "label": user_label_state(root)[0]}
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
