"""Settings: reads shedkeeper.ini. A value that is not understood becomes the SAFE default and the
reason is returned, so the caller can name it in its output (nothing is ignored silently).

The product name lives here and nowhere else in the code: PRODUCT and DEFAULT_STORE_DIR.
"""
import os
import re

PRODUCT = "shedkeeper"
DEFAULT_STORE_DIR = "_shedkeeper"
KIT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_INI = os.path.join(KIT_DIR, "shedkeeper.ini")

TRUE_WORDS = ("true", "1", "yes", "on")
FALSE_WORDS = ("false", "0", "no", "off")

DEFAULTS = {
    "store_dir_name": DEFAULT_STORE_DIR,
    "auto_create_store": True,
    "max_copy_mb": 512,
    "on_too_large": "deny",
    "cmd_max_files": 200,
    "cmd_deadline_s": 20,
}
_BAD_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _parse_bool(value):
    v = value.lower()
    if v in TRUE_WORDS:
        return True
    if v in FALSE_WORDS:
        return False
    return None


def _parse_int(value, low, high):
    try:
        n = int(value)
    except ValueError:
        return None
    return n if low <= n <= high else None


def load(path=None):
    """Return (settings dict, problems list). Missing file -> the defaults, no problem."""
    path = path or os.environ.get("SHEDKEEPER_INI") or DEFAULT_INI
    settings = dict(DEFAULTS)
    if not os.path.lexists(path):
        return settings, []
    try:
        with open(path, encoding="utf-8-sig") as fh:
            text = fh.read()
    except (OSError, ValueError) as e:
        settings["auto_create_store"] = False
        return settings, ["settings file unreadable (%s: %s): %s -- defaults used, "
                          "store creation switched OFF" % (e.__class__.__name__, e, path)]
    problems = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        key, sep, value = (x.strip() for x in line.partition("="))
        where = "%s:%d" % (path, n)
        if not sep or key not in DEFAULTS:
            problems.append("%s: not understood, ignored: %s" % (where, raw.strip()[:80]))
            continue
        if key == "auto_create_store":
            b = _parse_bool(value)
            if b is None:
                settings[key] = False
                problems.append("%s: auto_create_store = %r not understood -> false (safe)"
                                % (where, value))
            else:
                settings[key] = b
        elif key == "on_too_large":
            if value.lower() in ("deny", "allow"):
                settings[key] = value.lower()
            else:
                settings[key] = "deny"
                problems.append("%s: on_too_large = %r not understood -> deny (safe)" % (where, value))
        elif key == "store_dir_name":
            if value and not _BAD_NAME.search(value) and value not in (".", "..") \
                    and value == value.rstrip(". "):
                settings[key] = value
            else:
                settings[key] = DEFAULT_STORE_DIR
                problems.append("%s: store_dir_name = %r is not a plain folder name -> %s"
                                % (where, value, DEFAULT_STORE_DIR))
        else:
            hi = {"max_copy_mb": 10 ** 7, "cmd_max_files": 10 ** 6, "cmd_deadline_s": 3600}[key]
            n_val = _parse_int(value, 1, hi)
            if n_val is None:
                problems.append("%s: %s = %r not understood -> default %s"
                                % (where, key, value, DEFAULTS[key]))
            else:
                settings[key] = n_val
    return settings, problems
