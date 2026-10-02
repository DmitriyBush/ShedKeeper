"""UserPromptSubmit hook: receives the code the PERSON types to confirm a change (0.3.0).

Claude Code runs this hook when the person submits a message; the input carries the text of the message (`prompt`). The agent cannot make that
event happen by running a command, so a code found here was typed by the person -- this is where ShedKeeper takes the person's word from
(shedkeeper/confirm.py). For every `CONFIRM SK-nnnnnn` in the message the hook checks that such a code was issued, is not spent and is not expired,
and writes a line `human` into the journal of confirmations of the store; `shedkeeper.py set` / `activate` apply the change when they run again.

Output: nothing at all for a message with no code and nothing like one (it costs two regular expressions). For a message with a code: one line for the agent
(additionalContext) and the same line for the person (systemMessage): the code was received / was already received before / is unknown / has expired / was already
used / could NOT be recorded / could NOT be received / could NOT be processed; and a code in a text that is not the person's was IGNORED. Something that only LOOKS like a code
(`SK-482913` alone, `CONFIRM: SK-482913`, `CONFIRM SK-4829131`), while a code waits, gets one line saying so and what the form is. The hook never blocks and adds nothing
else to the context. It lives for a fraction of a second; the installed timeout is 30 s (install.py TIMEOUTS).
A fault is said, not hidden: "the code could NOT be recorded" goes to the person and the agent, and a line to the log of the store.

Two doors: started with `--via-plugin` (the plugin's hooks.json) it does nothing while a live entry of install.py covers UserPromptSubmit
(install.plugin_yields), so one handling per event.

Exit 0 always.
"""
import json
import os
import re
import sys
import time

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if KIT not in sys.path:
    sys.path.insert(0, KIT)

# the same words as confirm.CODE_RE / NEAR_RE, repeated here so that a message with no code and nothing like one never loads the kit
QUICK = re.compile(r"CONFIRM\s+SK-\d{6}", re.I)
NEAR = re.compile(r"SK-\d{3,}", re.I)


def _read_input():
    """stdin as BYTES decoded as UTF-8 (Claude Code sends UTF-8, not the console code page). An undecodable input is read with replacement characters."""
    buf = getattr(sys.stdin, "buffer", None)
    raw = buf.read() if buf is not None else sys.stdin.read().encode("utf-8")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("utf-8", "replace")


def _yields_to_install(data):
    """Two doors, one handling: started by the PLUGIN while a live entry of install.py covers this event, this copy stays silent. Any doubt -> False
    (a doubled hook is untidy -- the second one finds the code received already -- a silent pair is no confirmation at all)."""
    if "--via-plugin" not in sys.argv[1:]:
        return False
    try:
        import install
        cwd = data.get("cwd") if isinstance(data.get("cwd"), str) else None
        return bool(install.plugin_yields("UserPromptSubmit", cwd))
    except Exception:
        return False


MAX_LINES = 5           # the runtime cuts a hook answer at 10000 characters and then reads no JSON: a message with hundreds of codes is told in a few lines


def _say(texts):
    if len(texts) > MAX_LINES:
        texts = list(texts[:MAX_LINES]) + ["ShedKeeper: %d more codes in the message were looked at in the same way (not listed here)." % (len(texts) - MAX_LINES)]
    out = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": " ".join(texts)}, "systemMessage": " ".join(texts)}
    sys.stdout.write(json.dumps(out, ensure_ascii=True))
    sys.stdout.flush()


def main():
    try:
        data = json.loads(_read_input())
    except ValueError:
        return 0
    if not isinstance(data, dict):
        return 0
    prompt = data.get("prompt")
    if not isinstance(prompt, str) or not (QUICK.search(prompt) or NEAR.search(prompt)):
        return 0
    if _yields_to_install(data):
        return 0
    t0 = time.monotonic()
    session = data.get("session_id") if isinstance(data.get("session_id"), str) else ""
    cwd = data.get("cwd") if isinstance(data.get("cwd"), str) and data.get("cwd") else os.getcwd()
    try:
        from shedkeeper import config, confirm
        settings, _problems = config.load(integrity=False)
        results = confirm.receive(prompt, session, cwd, settings)
        if not results:                 # nothing was a code in the form the hook reads: something that only LOOKS like one is said (while a code waits)
            near = confirm.near_code(prompt, session, cwd, settings)
            if near:
                results = [("", "near", near)]
    except Exception as e:
        _say(["ShedKeeper: the code you typed could NOT be processed (%s: %s) -- the confirmation was NOT recorded, so the command that waits for it is still refused; try again." % (
            e.__class__.__name__, str(e)[:200])])
        _log(cwd, {"event": "prompt_hook_error", "error": "%s: %s" % (e.__class__.__name__, e)})
        return 0
    if results:
        _say([text for _code, _status, text in results])
        _log(cwd, {"event": "prompt_codes", "codes": [[c, s] for c, s, _t in results], "ms": int((time.monotonic() - t0) * 1000)})
    return 0


def _log(cwd, rec):
    """A line in the log of the store, when a store can be named; never raises."""
    try:
        from shedkeeper import config, store
        settings, _p = config.load(integrity=False)
        rec = dict(rec, time=time.strftime("%Y-%m-%dT%H:%M:%S"))
        store.log(store.store_root(cwd, settings), rec)
    except Exception:
        pass


if __name__ == "__main__":
    try:
        _rc = main()
    except Exception:           # nothing the hook meets may become an exit code the runtime reads as a block of the person's message
        _rc = 0
    for _stream in (sys.stderr, sys.stdout):
        try:
            _stream.flush()
        except Exception:
            pass
    os._exit(_rc)
