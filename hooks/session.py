"""SessionStart / SessionEnd hook: keeps copies of the session transcripts.

SessionEnd:   copies `transcript_path` to <store>\\transcripts\\<project folder>\\<session_id>.jsonl unless
              a copy at least as long is already there (the copy goes to a temporary name, then one
              os.replace puts it in place).
SessionStart: goes through every *.jsonl of the same project folder and saves what is missing by the
              same rule -- this catches sessions that ended without a SessionEnd (a crash). A time
              ceiling of 20 seconds; what is left waits for the next start.
              Prints ONE line (additionalContext) only when the store is not usable or the hooks of the
              kit are not found in the settings; otherwise it is silent.

Never blocks: exit 0 always. Errors go to <store>\\log.jsonl.
"""
import json
import os
import sys
import time

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if KIT not in sys.path:
    sys.path.insert(0, KIT)

SWEEP_DEADLINE_S = 20


def _sweep(store, root, tdir, deadline_s):
    """Save every transcript of the folder `tdir`. (copied, same, failed list, left)."""
    name = os.path.basename(os.path.normpath(tdir))
    files = []
    try:
        for fn in os.listdir(tdir):
            if fn.endswith(".jsonl"):
                p = os.path.join(tdir, fn)
                files.append((os.path.getmtime(p), p, fn[:-6]))
    except OSError as e:
        return 0, 0, ["cannot list %s: %s" % (tdir, e)], 0
    files.sort(reverse=True)        # the newest first: they are the ones a crash just lost
    copied = same = 0
    failed = []
    t0 = time.monotonic()
    for i, (_m, p, sid) in enumerate(files):
        if time.monotonic() - t0 > deadline_s:
            return copied, same, failed, len(files) - i
        r = store.save_transcript(p, root, name, sid)
        if r == "copied":
            copied += 1
        elif r == "same":
            same += 1
        else:
            failed.append("%s: %s" % (sid, r))
    return copied, same, failed, 0


def _warnings(cwd, root, settings, store):
    out = []
    ok, why = store.check_available(root, settings)
    if not ok:
        out.append("the store is not usable (%s)" % why)
    try:
        import install
        found = install.installed_hooks(cwd)
        if not found["pre_tool"]:
            out.append("the pre_tool hook of the kit is not found in the settings (python install.py check)")
        if not found["session"]:
            out.append("the session hook of the kit is not found in the settings")
    except Exception as e:
        out.append("could not read the settings (%s)" % e)
    return out


def _read_input():
    """stdin as BYTES decoded as UTF-8 (Claude Code sends UTF-8, not the console code page).
    -> (text, decoded_ok); an undecodable input is read with replacement characters and reported."""
    buf = getattr(sys.stdin, "buffer", None)
    raw = buf.read() if buf is not None else sys.stdin.read().encode("utf-8")
    try:
        return raw.decode("utf-8-sig"), True
    except UnicodeDecodeError:
        return raw.decode("utf-8", "replace"), False


def main():
    text, decoded_ok = _read_input()
    try:
        data = json.loads(text)
    except ValueError:
        return 0
    if not isinstance(data, dict):
        return 0
    root = None
    try:
        from shedkeeper import config, store
        settings, _problems = config.load()
        event = data.get("hook_event_name")
        tp = data.get("transcript_path")
        cwd = data.get("cwd") if isinstance(data.get("cwd"), str) and data.get("cwd") else (
            os.path.dirname(tp) if isinstance(tp, str) and tp else os.getcwd())
        root = store.store_root(cwd, settings)
        ok, _why = store.check_available(root, settings)
        if ok:
            ok, _note = store.ensure_store(root, settings)
        problems = []
        if not decoded_ok:
            problems.append("the hook input was not valid UTF-8 (read with replacement characters)")
        if isinstance(tp, str) and tp and ok:
            tdir = os.path.dirname(os.path.abspath(tp))
            name = os.path.basename(tdir)
            if os.path.isfile(tp):
                sid = data.get("session_id") if isinstance(data.get("session_id"), str) and data.get("session_id") \
                    else os.path.splitext(os.path.basename(tp))[0]
                r = store.save_transcript(tp, root, name, sid)
                if r.startswith("failed"):
                    problems.append(r)
            if event != "SessionEnd":
                # the sweep goes by the project folder of the transcript path even when the file of the
                # session that is starting does not exist yet: it is the crashed OLD sessions that matter
                c, s, f, left = _sweep(store, root, tdir, SWEEP_DEADLINE_S)
                problems += f
                store.log(root, {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": "transcripts_sweep",
                                 "copied": c, "same": s, "failed": len(f), "left": left})
        for p in problems:
            store.log(root, {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": "transcript_error",
                             "error": p[:300]})
        if event != "SessionEnd":
            warn = _warnings(cwd, root, settings, store)
            if warn:
                sys.stdout.write(json.dumps({"hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": "shedkeeper: " + "; ".join(warn)}}, ensure_ascii=True))
    except Exception as e:
        try:
            from shedkeeper import store as _s
            _s.log(root, {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": "session_hook_error",
                          "error": "%s: %s" % (e.__class__.__name__, e)})
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
