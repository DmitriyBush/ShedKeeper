r"""confirm.py -- a weakening of protection is applied only when the PERSON types a code (0.3.0, rebuilt: the person's word comes from a hook).

  python shedkeeper.py set KEY VALUE     for a setting of weight `protection` whose new value weakens the protection:
                                         1st run   NOT applied; a one-time code `SK-nnnnnn` (valid 30 minutes) is issued, and the agent is
                                                   told: ask the user to type exactly `CONFIRM SK-nnnnnn` in their own message;
                                         2nd run   the same command again: a record says the PERSON typed that code -> applied, the code is spent.

Where the person's word comes from. NOT from a file the agent can write: the hook of the event `UserPromptSubmit` (`hooks/prompt.py`) is run by
Claude Code when the person submits a message, and its input carries the text of that message (`prompt`). The agent cannot make that event happen
by running a command. The hook finds `CONFIRM SK-nnnnnn` in the text, checks that such a code was issued, is not spent and is not expired, and
writes a line `human` into the journal of confirmations (`receive`). `set` / `activate` apply a change only when that line is there (`gate`). The
earlier design read the person's message from the record of the conversation, a file the agent can append to -- and guarded it by a list of forms of
commands; a list of forms is never complete against an agent that LOOKS for a way round it, so that part is gone.

What the journal can and cannot do. It is a file in the store, a folder that only a person changes: the hook refuses the Edit / Write of the agent and a
shell write with a literal path into it (`>`, `>>`, `tee`, a python write). A write by a program the agent runs, with a path the program builds itself, is not
stopped -- declared; what is done about it is that a line `human` is never taken on trust alone: the setting is applied only when the journal has the issue and the
person's line for the same code, and the code is not spent.

The ini is the hand of the person: a value of weight `protection` that weakens protection and stands in it ACTS, whether `set` wrote it or the person edited the file. What the journal tells about it is a REMINDER only: a weakening with no confirmed `state` line for exactly this key and value (`approved`) is
listed in `settings.unconfirmed` and shown with the mark "not through `set`" at the start of a session and in `status` (`settings_cli.reminder_lines`); it changes nothing
in what the kit does, and the hook carries no alarm of its own at a decision. The decisions of the hooks do not even work the mark out (`config.load(integrity=False)`).

A code is spent when a `used` line says so. A `used` line that is DAMAGED (cut off by a crash) counts too when it names the code. When it does not
(`"op": "used"` and no whole code) it is not known which codes are spent: `gate` REFUSES, naming the file and the line, and `quarantine` is the way out
(the damaged lines go to `confirm.jsonl.bad`, nothing is deleted, and every code issued so far is closed). A file of codes that is there and cannot be read
refuses the change of the project's own store; the unreadable or damaged file of ANOTHER store (a disk that is not there) is skipped -- a code issued
there cannot be used here, and a fault there does not lock the confirmations of this project.

The same code mechanism confirms three things: the weakening of a setting (`set`, key = the setting), the activation of an engine adapter
(`activate`, key = `adapter:<id>`, value = the sha256 of the adapter file: a code is for THESE bytes only) and the person's word to switch ShedKeeper itself off by an
ordinary way (`selfguard.py`: key `uninstall`, value = the action the code was issued for).

Files: `<store>/confirm.jsonl`, lines  {"op":"issue"|"human"|"used"|"state"|"seen", "t": <epoch seconds>, ...}.
  issue  {code, key, value, session}      a code was handed to the agent
  human  {code, session}                  the person typed that code (written by the hook of the prompt)
  used   {code}                           the code is spent
  state  {key, value, how, code, session} `set` changed `key` to `value`: how = "confirmed" (with `code`) or "free" (no code was needed)
  seen   {session, form}                  the hook of the prompt saw something that LOOKS like a code in the person's message while a code waited (`near_code`):
                                          `gate` then says that the hook ran and the form was wrong, not that the hook is not running"""
import json
import os
import re
import secrets
import time

from . import advice, config, store

FILE = "confirm.jsonl"
TTL_S = 30 * 60                 # a code lives 30 minutes
CODE_RE = re.compile(r"\bCONFIRM\s+(SK-\d{6})\b", re.I)
NEAR_RE = re.compile(r"SK-\d{3,}", re.I)         # something that looks like a code, in whatever form (`SK-482913` alone, `CONFIRM: SK-482913`, `CONFIRM SK-4829131`, `YES SK-482913` (any other word))
EXIT_ASK = 4                   # `set` exit code: not applied, the person's confirmation is needed


def _path(root):
    return os.path.join(root, FILE)


class _Unreadable(Exception):
    """confirm.jsonl is there and cannot be read: which codes are spent is not known."""


class _Blocked(Exception):
    """The journal of the project's own store cannot be trusted: the text says why and what to do."""


def _read(root):
    """[records] of confirm.jsonl in this folder ([] when there is no file). A file that is there and cannot be read raises _Unreadable."""
    try:
        recs, _bad = store._read_jsonl(_path(root))
        return [r for _n, r in recs]
    except FileNotFoundError:
        return []
    except OSError as e:
        raise _Unreadable("%s cannot be read (%s: %s)" % (_path(root), e.__class__.__name__, e))


USED_LINE = re.compile(r'"op"\s*:\s*"used"')
CODE_IN_LINE = re.compile(r"SK-\d{6}")


def _damage(root):
    """({codes a damaged `used` line names}, [(line number, text) of a damaged `used` line that names none]) of the journal of this folder."""
    named, unnamed = set(), []
    try:
        lines = store._damaged_jsonl(_path(root))
    except OSError:
        return named, unnamed
    for n, line in lines:
        if not USED_LINE.search(line):
            continue
        m = CODE_IN_LINE.search(line)
        if m:
            named.add(m.group(0))
        else:
            unnamed.append((n, line))
    return named, unnamed


def _same(a, b):
    return bool(a) and bool(b) and os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _roots(cwd, settings):
    """(the root of the store of this project or None, [every root to look in, the own one first])."""
    own = None
    try:
        own = store.store_root(cwd, settings)
    except Exception:
        pass
    out = [own] if own else []
    try:
        out += list(store.all_roots(settings))
    except Exception:
        pass
    return own, [r for r in dict.fromkeys(out) if r]


def _damaged_refusal(root, n):
    return ("REFUSED: line %d of %s is damaged: it says a confirmation code was used but not which one, so it is not known which codes are spent. Nothing "
            "was written. Run `python %s quarantine`: it moves the damaged lines to %s.bad (nothing is deleted) and closes every code issued so far; then ask "
            "the user for a NEW code. Or make the change yourself, outside the agent."
            % (n, _path(root), advice.kit_py(), FILE))


def _load(own, roots):
    """([(root, records)] of the roots that can be trusted, [notes of the roots that were skipped]). A fault of the own store raises _Unreadable or _Blocked;
    a fault of another store skips it (a code issued there cannot be used here)."""
    loaded, notes = [], []
    for r in roots:
        try:
            recs = _read(r)
            if _same(r, own):
                _named, unnamed = _damage(r)
                if unnamed:
                    raise _Blocked(_damaged_refusal(r, unnamed[0][0]))
            elif _damage(r)[1]:
                notes.append("%s skipped: a damaged `used` line" % _path(r))
                continue
        except _Unreadable as e:
            if _same(r, own):
                raise
            notes.append("%s skipped" % e)
            continue
        loaded.append((r, recs))
    return loaded, notes


def _add(root, rec):
    store._append_jsonl(_path(root), lambda n: rec)


def _spent(loaded):
    out = set()
    for r, recs in loaded:
        out |= {x.get("code") for x in recs if x.get("op") == "used"}
        out |= _damage(r)[0]
    return out


# ============================================================================================ the person's word (the hook of the prompt)

# The runtime hands more than the person's typing through the event `UserPromptSubmit` (measured in the hooks of this project: the text of a task notification, a message of another
# agent, a reminder of the runtime reach `prompt` too, wrapped in tags). What an agent can influence -- the command line of a background task, a message a helper relays -- must
# never count as the person's word, so only the text BEFORE the first wrapper tag is the person's; a message that starts with one, or with the head the runtime puts on a message of
# another session, has none.
FOREIGN_TAGS = ("<task-notification", "<system-reminder", "<teammate-message", "<agent-message")
FOREIGN_HEAD = "Another Claude session sent a message"


def split_prompt(prompt):
    """(the text the person typed, the text that is not theirs) of the `prompt` of a UserPromptSubmit input."""
    if not isinstance(prompt, str):
        return "", ""
    if prompt.lstrip().startswith(FOREIGN_HEAD):
        return "", prompt
    cuts = [i for i in (prompt.find(tag) for tag in FOREIGN_TAGS) if i >= 0]
    if not cuts:
        return prompt, ""
    i = min(cuts)
    return prompt[:i], prompt[i:]


def receive(prompt, session="", cwd="", settings=None, now=None):
    """The person submitted a message: every `CONFIRM SK-nnnnnn` in the text THEY typed (`split_prompt`). -> [(code, status, text)]; status: accepted | already | unknown | expired | spent |
    ignored (the code stands in a message that is not the person's: said, not counted) | error. `accepted` writes a line `human` to the journal of the store that issued the code.
    Nothing here applies a change: `set` / `activate` do, when they run again."""
    human, foreign = split_prompt(prompt)
    codes = list(dict.fromkeys(c.upper() for c in CODE_RE.findall(human)))
    ignored = [c for c in dict.fromkeys(x.upper() for x in CODE_RE.findall(foreign)) if c not in codes]
    notes = [(c, "ignored", "ShedKeeper: the code %s stands in a message that did NOT come from the user (a task notification, another agent, a reminder of the runtime): it was IGNORED. "
                            "Only what the user types counts." % c) for c in ignored]
    if not codes:
        return notes
    now = time.time() if now is None else now
    settings = settings if settings is not None else config.load(integrity=False)[0]
    own, roots = _roots(cwd or os.getcwd(), settings)
    try:
        loaded, _notes = _load(own, roots)
    except (_Unreadable, _Blocked) as e:
        return notes + [(c, "error", "ShedKeeper: the code %s could NOT be received -- %s" % (c, str(e)[:300])) for c in codes]
    spent = _spent(loaded)
    out = list(notes)
    for code in codes:
        issued = [(r, x) for r, recs in loaded for x in recs if x.get("op") == "issue" and x.get("code") == code]
        if not issued:
            out.append((code, "unknown", "ShedKeeper: the code %s is unknown here (no such code was issued, or its store cannot be read)." % code))
            continue
        root, issue = max(issued, key=lambda p: p[1].get("t", 0))
        if code in spent:
            out.append((code, "spent", "ShedKeeper: the code %s was already used." % code))
        elif now - issue.get("t", 0) >= TTL_S:
            out.append((code, "expired", "ShedKeeper: the code %s has expired (a code lives 30 minutes); the agent has to ask for a new one." % code))
        elif any(x.get("op") == "human" and x.get("code") == code for _r, recs in loaded for x in recs):
            out.append((code, "already", "ShedKeeper: the code %s was received from the user before." % code))
        else:
            try:
                if not store._is_mine(root):
                    raise OSError("the store %s is not this user's" % root)
                _add(root, {"op": "human", "t": now, "code": code, "session": session or ""})
            except Exception as e:
                out.append((code, "error", "ShedKeeper: the code %s could NOT be recorded (%s: %s) -- the confirmation did not reach the agent." % (
                    code, e.__class__.__name__, str(e)[:200])))
                continue
            out.append((code, "accepted", "ShedKeeper: the code %s typed by the user was received; the agent may run the same command again." % code))
    return out


def near_code(prompt, session="", cwd="", settings=None, now=None):
    """The person's message holds something that LOOKS like a code (NEAR_RE) but no code in the form ShedKeeper reads (CODE_RE), while a code is waiting for them (issued,
    not spent, not expired). -> the fact for both readers, '' when there is nothing to say. A line `seen` goes into the journal of the store that issued the code, so that
    `gate` can say that the hook DID run and the form was wrong -- not that the hook is not running. Never raises."""
    try:
        human, _foreign = split_prompt(prompt)
        if not NEAR_RE.search(human) or CODE_RE.search(human):
            return ""
        now = time.time() if now is None else now
        settings = settings if settings is not None else config.load(integrity=False)[0]
        own, roots = _roots(cwd or os.getcwd(), settings)
        loaded, _notes = _load(own, roots)
        spent = _spent(loaded)
        waiting = [(r, x) for r, recs in loaded for x in recs if x.get("op") == "issue" and x.get("code") not in spent and now - x.get("t", 0) < TTL_S]
        if not waiting:
            return ""
        root = max(waiting, key=lambda p: p[1].get("t", 0))[0]
        try:
            _add(root, {"op": "seen", "t": now, "session": session or "", "form": "near"})
        except Exception:
            pass
        return ("ShedKeeper: the message has something that looks like a confirmation code, but not in the form ShedKeeper reads, so it was NOT counted. The form is exactly "
                "CONFIRM SK-nnnnnn (the word CONFIRM, a space, SK-, six digits), typed in the user's own message.")
    except Exception:
        return ""


# ============================================================================================ the setting that the person confirmed

def record_state(key, value, how, code, cwd, settings, session="", now=None):
    """`set` changed `key` to `value` (how: confirmed | free). -> (written, why). A line `state` is what makes a weakening ACT (`approved`); the line is made after the ini."""
    now = time.time() if now is None else now
    try:
        own, _roots_ = _roots(cwd, settings)
        if not own:
            return False, "no store folder can be named for this folder"
        ok, note, _rep = store.ensure_store_report(own, settings)
        if not ok:
            return False, note
        _add(own, {"op": "state", "t": now, "key": key, "value": value, "how": how, "code": code or "", "session": session or ""})
        return True, ""
    except Exception as e:
        return False, "%s: %s" % (e.__class__.__name__, e)


def _journal_roots(settings):
    """Every store of this user the journal may be in: the roots under the name in force and under the shipped default name (a changed `store_dir_name`
    is itself a setting that has to be confirmed)."""
    out = []
    for s in (settings, dict(settings, store_dir_name=config.DEFAULT_STORE_DIR)):
        try:
            out += list(store.all_roots(s))
        except Exception:
            pass
    return [r for r in dict.fromkeys(os.path.normcase(os.path.abspath(r)) for r in out) if r]


def approved(cands, settings):
    """{key: True | tuple of names} -- which of the weakening values `cands` ([(key, value text)]) the person confirmed. `mcp_no_db`: the names allowed.
    Reads the journal of every store of the user (an unreadable one is skipped: nothing is approved by a file that cannot be read; a damaged line is skipped
    too -- a lost `state` or `human` line can only leave a value unconfirmed, never confirm one)."""
    loaded = []
    for r in _journal_roots(settings):
        try:
            loaded.append((r, _read(r)))
        except _Unreadable:
            continue
    states, issues, humans = {}, {}, {}
    for i, (_r, recs) in enumerate(loaded):
        for j, x in enumerate(recs):
            op = x.get("op")
            if op == "state" and isinstance(x.get("key"), str):
                k = (x.get("t", 0), i, j)
                if x["key"] not in states or k >= states[x["key"]][0]:
                    states[x["key"]] = (k, x)
            elif op == "issue":
                issues[x.get("code")] = x
            elif op == "human":
                humans.setdefault(x.get("code"), x)

    def confirmed(st, key):
        if st.get("how") != "confirmed":
            return False
        iss, hum = issues.get(st.get("code")), humans.get(st.get("code"))
        return bool(iss and hum and iss.get("key") == key and iss.get("value") == st.get("value") and hum.get("t", 0) >= iss.get("t", 0))

    out = {}
    for key, canon in cands:
        got = states.get(key)
        if not got:
            continue
        st = got[1]
        if key == "mcp_no_db":
            if st.get("how") == "free" or confirmed(st, key):       # a free record holds a list that was not longer than the one in force
                out[key] = tuple(x for x in re.split(r"[\s,]+", str(st.get("value") or "")) if x)
        elif st.get("value") == canon and confirmed(st, key):
            out[key] = True
    return out


# ============================================================================================ the way out of a damaged journal

def quarantine(settings, cwd=None):
    """Move the damaged lines of the journals of this user's stores to `<file>.bad` (nothing is deleted), and close every code issued so far (a line `used`
    for each) -- an old word of the person must not confirm anything again. -> (exit code, text). Also the proofs of adapters (engines.quarantine_proofs)."""
    lines, bad_any = [], False
    _own, roots = _roots(cwd or os.getcwd(), settings)
    for root in roots:
        path = _path(root)
        try:
            damaged = store._damaged_jsonl(path)
        except OSError as e:
            lines.append("%s cannot be read (%s: %s) -- left as it is" % (path, e.__class__.__name__, e))
            bad_any = True
            continue
        if not damaged:
            continue
        try:
            n_closed = store.quarantine_jsonl(path, damaged, _close_open_codes)
        except Exception as e:
            lines.append("%s could not be repaired (%s: %s) -- left as it is" % (path, e.__class__.__name__, e))
            bad_any = True
            continue
        lines.append("%s: %d damaged line(s) moved to %s.bad; %d code(s) closed (ask the user for a NEW code)" % (path, len(damaged), FILE, n_closed))
    try:
        from . import engines
        lines += [] if not config.DATABASES_IN_THIS_RELEASE else engines.quarantine_proofs(roots)        # a release without databases has no proofs of adapters to look at
    except Exception as e:
        lines.append("the proofs of the adapters could not be looked at (%s: %s)" % (e.__class__.__name__, e))
        bad_any = True
    if not lines:
        lines.append("nothing to quarantine: no damaged line in the journals of the stores")
    return (1 if bad_any else 0), "\n".join(lines)


def _close_open_codes(recs):
    """A `used` line for every code issued in the journal that is not spent yet: an old word of the person must not confirm anything after a repair."""
    used = {r.get("code") for r in recs if r.get("op") == "used"}
    now = time.time()
    return [{"op": "used", "t": now, "code": c, "how": "quarantine"} for c in dict.fromkeys(r.get("code") for r in recs if r.get("op") == "issue" and r.get("code"))
            if c not in used]


# ============================================================================================ the gate

def weakens(key, canon, settings):
    """The effect text when setting `key` to `canon` WEAKENS protection (the definition of the settings menu: `config.effect_for`), '' when not.
    Two things that cannot weaken are let go: the value the kit already reads, and a shorter list of `mcp_no_db`."""
    eff = config.effect_for(key, canon)
    if not eff:
        return ""
    cur = settings.get(key)
    if key in config.LIST_KEYS:
        if key == "mcp_no_db" and set(config.apply_value_to_tuple(key, canon)) <= set(cur or ()):
            return ""
        return eff
    if config.format_value(cur) == canon:
        return ""
    return eff


def _the_same(verb):
    """`the same set command` for a `set`; `the same command` when no verb is given (a command or an edit the hook stopped)."""
    return "the same %s command" % verb if verb else "the same command"


def _ask_text(key, canon, effect, code, expired=False, shell="bash", cmd=None, what="would weaken protection", verb="set"):
    cmd = cmd or advice.set_command(key, canon, shell)
    return ("NOT APPLIED: `%s` %s: %s. %s"
            "The user decides; what would be switched off: %s. The way on: the user types exactly CONFIRM %s in their own message, then %s "
            "is run again. The code is valid 30 minutes and works once; only a message the user typed counts -- not a text of the agent, a tool result or a note of a hook." % (
                cmd, what, effect, "The code of before had expired; a new one was issued. " if expired else "", effect, code, _the_same(verb)))


def gate(key, canon, settings, cwd, session="", now=None, shell="bash", effect=None, cmd=None, what="would weaken protection", issue_only=False, verb="set"):
    """-> (status, text, code). status: 'free' (no confirmation needed), 'apply' (confirmed; `spend(code)` BEFORE the change is made), 'ask' (a code
    was issued and is to be typed by the person), 'refuse' (cannot be confirmed here; the text says why). `effect` given: the change is
    one that always needs the person (the activation of an adapter), `cmd` is the command to run again. `issue_only`: only make sure a code is
    open (the open one is returned, or a new one is issued) -- 'ask' either way; `prove` does this for the adapter it has just proved."""
    if effect is None:
        effect = weakens(key, canon, settings)
    if not effect:
        return "free", "", None
    now = time.time() if now is None else now
    own, roots = _roots(cwd, settings)
    try:
        loaded, notes = _load(own, roots)
    except _Unreadable as e:           # which codes are spent is not known: nothing is applied (and no new code, which could not be recorded as spent either)
        return "refuse", ("REFUSED: %s, so it is not known which confirmation codes are already spent. Nothing was written. Repair the file (or run `python %s "
                          "quarantine`, which moves a damaged file's bad lines away), or make the change yourself, outside the agent." % (e, advice.kit_py())), None
    except _Blocked as e:
        return "refuse", str(e), None
    spent = _spent(loaded)
    skipped = (" Note: %s -- a journal of another store that cannot be used here; it does not matter for this code." % "; ".join(notes)) if notes else ""
    open_issue, expired = None, False
    for _r, recs in loaded:
        for rec in recs:
            if rec.get("op") != "issue" or rec.get("key") != key or rec.get("value") != canon or rec.get("code") in spent:
                continue
            if now - rec.get("t", 0) < TTL_S:
                if open_issue is None or rec["t"] >= open_issue["t"]:
                    open_issue = rec
            else:
                expired = True
    if open_issue is not None and issue_only:
        return "ask", _ask_text(key, canon, effect, open_issue["code"], False, shell, cmd, what, verb) + skipped, open_issue["code"]
    if open_issue is None:
        root = None
        try:
            root = store.store_root(cwd, settings)
            ok, note, _rep = store.ensure_store_report(root, settings)
        except Exception as e:
            ok, note = False, "%s: %s" % (e.__class__.__name__, e)
        if not ok or not root:
            return "refuse", ("REFUSED: this %s (%s), and it needs the person's confirmation code, which must be "
                              "kept in the store -- the store cannot be used (%s). Nothing was written. A person who wants it can do it "
                              "themselves, outside the agent." % (what, effect, note)), None
        code = "SK-%06d" % secrets.randbelow(10 ** 6)
        try:
            _add(root, {"op": "issue", "t": now, "code": code, "key": key, "value": canon, "session": session or ""})
        except Exception as e:
            return "refuse", "REFUSED: the code could not be kept in the store (%s: %s). Nothing was written." % (e.__class__.__name__, e), None
        return "ask", _ask_text(key, canon, effect, code, expired, shell, cmd, what, verb) + skipped, code
    # a code is open: has the person typed it? (the hook of the prompt wrote a line `human` for it, after the code was issued)
    code = open_issue["code"]
    if any(x.get("op") == "human" and x.get("code") == code and x.get("t", 0) >= open_issue["t"] for _r, recs in loaded for x in recs):
        return "apply", "", code
    left = int(TTL_S - (now - open_issue["t"]))
    if any(x.get("op") == "seen" and x.get("t", 0) >= open_issue["t"] for _r, recs in loaded for x in recs):
        return "refuse", ("NOT APPLIED: the code %s has not been received from the user. ShedKeeper's hook of the prompt DID run for a message of the user that looked like a "
                          "code, but it was not in the form the hook reads. The way on: the user types exactly CONFIRM %s (the word CONFIRM, a space, the code), in their own "
                          "message; then %s is run again. It is valid for %d more minute(s).%s" % (code, code, _the_same(verb), max(1, left // 60), skipped)), None
    return "refuse", ("NOT APPLIED: the code %s has not been received from the user. The way on: the user types exactly CONFIRM %s (in their own message), then "
                      "%s is run again. It is valid for %d more minute(s). Only a message the user typed counts -- not a text of the agent, a "
                      "tool result or a note of a hook. (ShedKeeper learns of it from its hook of the user's prompt: if the user did type it and this stays, the "
                      "hook is not running -- `python %s status` shows the hooks.)%s" % (code, code, _the_same(verb), max(1, left // 60), advice.kit_py(), skipped)), None


def spend(code, cwd, settings, now=None):
    """The code is used up (written after the change). -> True when the line was written."""
    now = time.time() if now is None else now
    _own, roots = _roots(cwd, settings)
    for root in roots:
        try:
            if any(r.get("op") == "issue" and r.get("code") == code for r in _read(root)):
                _add(root, {"op": "used", "t": now, "code": code})
                return True
        except Exception:
            continue
    return False
